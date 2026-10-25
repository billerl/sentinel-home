"""Messages Sentinel doesn't understand go to Claude (opt-in), locked down and capped per day."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import types
import unittest
from unittest import mock

from sentinel.engine import assistant as am
from sentinel.engine.assistant import Assistant, build_command, build_context, split_command
from sentinel.notifications.telegram_notifier import TelegramNotifier


def _ok(result="It was quiet out back."):
    return types.SimpleNamespace(returncode=0, stdout=json.dumps({"result": result, "is_error": False,
                                                                  "usage": {"input_tokens": 900, "output_tokens": 40}}),
                                 stderr="")


class TestAssistant(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.patch = mock.patch.object(am, "find_claude", return_value="/usr/bin/claude")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def make(self, response=None, **settings):
        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            return response or _ok()
        return Assistant({"assistant_enabled": True, **settings}, run=run)

    def test_off_by_default(self):
        a = Assistant({}, run=lambda *a, **k: self.fail("must not run"))
        self.assertIsNone(a.ask("hi", "DATA"))

    def test_locked_down_call(self):
        a = self.make()
        self.assertEqual(a.ask("was it busy out back?", "DATA\nNOW: ..."), "It was quiet out back.")
        cmd, kw = self.calls[0]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")  # no tools at all
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "1")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertIn("was it busy out back?", cmd[cmd.index("-p") + 1])
        self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])  # bill the subscription, never the API
        self.assertTrue(kw["cwd"].endswith("sentinel-assistant"))
        self.assertEqual(kw["timeout"], am.TIMEOUT_SECONDS)

    def test_daily_limit(self):
        a = self.make(assistant_daily_limit=2)
        self.assertIsNotNone(a.ask("one", "D"))
        self.assertIsNotNone(a.ask("two", "D"))
        self.assertIsNone(a.ask("three", "D"))
        self.assertIn("limit", a.last_error)
        self.assertEqual(len(self.calls), 2)

    def test_signed_out(self):
        a = self.make(types.SimpleNamespace(returncode=1, stdout=json.dumps(
            {"result": "Login expired · Please run /login", "is_error": True}), stderr=""))
        self.assertIsNone(a.ask("hi", "D"))
        self.assertIn("signing in again", a.last_error)

    def test_timeout(self):
        def run(cmd, **kw):
            raise am.subprocess.TimeoutExpired(cmd, 90)
        a = Assistant({"assistant_enabled": True}, run=run)
        self.assertIsNone(a.ask("hi", "D"))
        self.assertIn("too long", a.last_error)

    def test_command_has_fixed_system_prompt(self):
        cmd = build_command("/c", "p", "haiku")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], am.SYSTEM_PROMPT)

    def test_context(self):
        v = types.SimpleNamespace(name="Stephanie's Edge", owner="Stephanie", aliases=["Steph", "Mom"])
        events = [{"ts": 1792957107.0 + i * 60, "description": f"event {i}", "grp": "person"} for i in range(200)]
        text = build_context(1792960000.0, events, [("Garage", True), ("Back Yard", False)],
                             "Home: Stephanie's Edge (Garage)", [v],
                             {"temp": 69, "unit": "°F", "words": "Clear", "place": "Tiffin, Ohio", "high": 70,
                              "low": 40, "wind": 2, "rain_today": 0}, "Help: ...")
        self.assertIn("Back Yard (OFFLINE)", text)
        self.assertIn("Stephanie's Edge: Stephanie / Steph, Mom", text)
        self.assertIn("only the latest 150 of 200 shown", text)
        self.assertIn("event 199", text)
        self.assertNotIn("event 49\n", text)
        self.assertIn("WEATHER in Tiffin, Ohio: 69°F", text)


class TestTelegramRouting(unittest.TestCase):
    def notifier(self, available):
        n = TelegramNotifier({"telegram_enabled": False, "telegram_chat_ids": "42"})
        n.chat_ids = ["42"]
        n.assistant_available = (lambda: available) if available is not None else None
        n._sent = []
        n._send_message_to_chat = lambda chat, text, *a, **k: n._sent.append(text)
        return n

    def feed(self, n, text):
        body = json.dumps({"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 42}, "text": text}}]})
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body.encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            n._process_pause_commands()

    def test_unknown_message_goes_to_claude_when_on(self):
        n = self.notifier(True)
        self.feed(n, "was it busy out back this afternoon")
        self.assertEqual(n.consume_free_text(), [("42", "was it busy out back this afternoon")])
        self.assertEqual(n._sent, [])
        self.feed(n, "status")  # built-in commands still handled by Sentinel
        self.assertEqual(n.consume_free_text(), [])

    def test_enable_alerts_means_messages(self):
        for phrase in ("enable alerts", "Enable notifications", "disable motion alerts"):
            changes, err = TelegramNotifier._parse_feature_toggle_command(phrase)
            self.assertIsNone(err, phrase)
            self.assertEqual(list(changes), ["messages"], phrase)
        _, err = TelegramNotifier._parse_feature_toggle_command("enable lasers")
        self.assertIn("alerts (messages)", err)

    def test_photos_and_strangers_are_logged_not_silent(self):
        n = self.notifier(True)
        body = json.dumps({"ok": True, "result": [{"update_id": 2, "message": {"chat": {"id": 42}, "voice": {}}}]})
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body.encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            n._process_pause_commands()
        self.assertIn("typed messages", n._sent[0])

    def test_half_known_command_goes_to_claude(self):
        n = self.notifier(True)
        self.feed(n, "enable lasers")
        self.assertEqual(n.consume_free_text(), [("42", "enable lasers")])
        self.assertEqual(n._sent, [])

    def test_button_runs_only_real_commands_and_never_loops_to_claude(self):
        self.assertTrue(TelegramNotifier.is_command("Enable messages"))
        self.assertTrue(TelegramNotifier.is_command("pause 30"))
        self.assertFalse(TelegramNotifier.is_command("rm -rf /"))
        self.assertFalse(TelegramNotifier.is_command("enable lasers"))
        n = self.notifier(True)
        n.route_text("42", "enable lasers", from_button=True)
        self.assertEqual(n.consume_free_text(), [])
        self.assertIn("Unknown", n._sent[0])

    def test_split_command(self):
        self.assertEqual(split_command("Alerts are off.\nCOMMAND: Enable messages"),
                         ("Alerts are off.", "Enable messages"))
        self.assertEqual(split_command("Just chatting."), ("Just chatting.", None))

    def test_help_text_when_off(self):
        n = self.notifier(False)
        self.feed(n, "was it busy out back this afternoon?")
        self.assertEqual(n.consume_free_text(), [])
        self.assertTrue(n._sent and "didn't catch that" in n._sent[0])


if __name__ == "__main__":
    unittest.main()
