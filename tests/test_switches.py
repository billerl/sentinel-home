"""Smart plugs: the Kasa wire format, outlets as switches, words -> switches, and the voice/Telegram/web paths."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sentinel.engine import Engine
from sentinel.engine.switches import (SmartSwitches, _kasa_scramble, _kasa_unscramble, guess_icon)
from sentinel.notifications.telegram_notifier import TelegramNotifier
from sentinel.settings.manager import SettingsManager

NOW = 1_800_000_000.0


def ep40(lights=0, outlet=1):
    """An EP40 outdoor plug's answer (two outlets)."""
    return {"model": "EP40(US)", "alias": "TP-LINK_Smart Plug_00AA", "mac": "9C:53:22:00:00:AA", "children": [
        {"id": "80060000", "state": lights, "alias": "patio lights", "on_time": 0},
        {"id": "80060001", "state": outlet, "alias": "patio outlet", "on_time": 3600 if outlet else 0}]}


def hs103(on=0):
    return {"model": "HS103(US)", "alias": "Porch lamp", "mac": "50:C7:BF:00:00:01", "relay_state": on,
            "on_time": 60 if on else 0}


class FakeKasa:
    kind = "kasa"

    def __init__(self):
        self.devices = {"192.168.1.40": ep40(), "192.168.1.50": hs103()}
        self.calls = []
        self.dead = set()

    def discover(self, wait=2.0):
        return [(h, json.loads(json.dumps(i))) for h, i in self.devices.items() if h not in self.dead]

    def status(self, host):
        if host in self.dead:
            raise OSError("timed out")
        return json.loads(json.dumps(self.devices[host]))

    def set(self, host, child_id, on):
        if host in self.dead:
            raise OSError("timed out")
        self.calls.append((host, child_id, on))
        info = self.devices[host]
        if child_id:
            next(c for c in info["children"] if c["id"] == child_id)["state"] = int(on)
        else:
            info["relay_state"] = int(on)

    def rename(self, host, child_id, name):
        info = self.devices[host]
        (next(c for c in info["children"] if c["id"] == child_id) if child_id else info)["alias"] = name


class TestKasa(unittest.TestCase):
    def test_scramble_round_trip(self):
        text = '{"system":{"get_sysinfo":{}}}'
        self.assertNotEqual(_kasa_scramble(text), text.encode())
        self.assertEqual(_kasa_unscramble(_kasa_scramble(text)), text)
        self.assertEqual(_kasa_scramble("{")[0], 171 ^ ord("{"))  # the protocol's starting key

    def test_icons(self):
        self.assertEqual(guess_icon("patio lights"), "light")
        self.assertEqual(guess_icon("Christmas tree"), "tree")
        self.assertEqual(guess_icon("patio outlet"), "plug")
        self.assertEqual(guess_icon("Box fan"), "fan")


class TestSwitches(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.kasa = FakeKasa()
        self.sw = SmartSwitches(self.dir, drivers=[self.kasa], clock=lambda: NOW)
        self.sw.discover()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_outlets_are_switches(self):
        items = {s.name: s for s in self.sw.switches()}
        self.assertEqual(set(items), {"patio lights", "patio outlet", "Porch lamp"})
        self.assertEqual(items["patio lights"].device, "Kasa EP40")  # the plug's own default name is hidden
        self.assertEqual(items["patio lights"].id, "kasa-9c53220000aa-1")
        self.assertEqual(items["Porch lamp"].id, "kasa-50c7bf000001")
        self.assertEqual(items["patio outlet"].since, NOW - 3600)
        self.assertIsNone(items["patio lights"].since)

    def test_set_one_outlet(self):
        ok, said = self.sw.set("kasa-9c53220000aa-1", True)
        self.assertTrue(ok, said)
        self.assertEqual(self.kasa.calls, [("192.168.1.40", "80060000", True)])
        self.assertTrue(next(s for s in self.sw.switches() if s.name == "patio lights").on)
        self.assertFalse(self.sw.set("kasa-9c53220000aa-9", True)[0])
        self.assertFalse(self.sw.set("kasa-9c53220000aa", True)[0])  # a two-outlet plug needs the outlet
        self.assertFalse(self.sw.set("../etc", True)[0])

    def test_known_devices_survive_restart_and_moves(self):
        self.assertEqual(os.stat(os.path.join(self.dir, "switches.json")).st_mode & 0o777, 0o600)
        again = SmartSwitches(self.dir, drivers=[self.kasa], clock=lambda: NOW)
        again.poll()  # no broadcast needed: it remembers where they were
        self.assertEqual(len(again.switches()), 3)
        self.kasa.devices["192.168.1.99"] = self.kasa.devices.pop("192.168.1.40")  # router gave it a new address
        again.discover()
        self.assertTrue(again.set("kasa-9c53220000aa-2", False)[0])
        self.assertEqual(self.kasa.calls[-1][0], "192.168.1.99")

    def test_offline_after_three_misses(self):
        self.kasa.dead.add("192.168.1.50")
        self.sw.poll()
        self.sw.poll()
        self.assertTrue(next(s for s in self.sw.switches() if s.name == "Porch lamp").online)
        self.sw.poll()
        self.assertFalse(next(s for s in self.sw.switches() if s.name == "Porch lamp").online)
        ok, said = self.sw.set("kasa-50c7bf000001", True)
        self.assertFalse(ok)
        self.assertIn("isn't answering", said)

    def test_words(self):
        def names(text):
            parsed = self.sw.parse(text)
            return parsed and (parsed[0], sorted(s.name for s in parsed[1]))

        self.assertEqual(names("turn on the patio lights"), (True, ["patio lights"]))
        self.assertEqual(names("Patio light off"), (False, ["patio lights"]))
        self.assertEqual(names("switch the patio outlet off please"), (False, ["patio outlet"]))
        self.assertEqual(names("are the patio lights on"), (None, ["patio lights"]))
        self.assertEqual(names("is the porch lamp on or off"), (None, ["Porch lamp"]))
        self.assertEqual(names("lights on"), (True, ["Porch lamp", "patio lights"]))
        self.assertEqual(names("turn off patio"), (False, ["patio lights", "patio outlet"]))
        for other in ("turn on alerts", "turn the music off", "enable recording", "come on", "is anyone home"):
            self.assertIsNone(self.sw.parse(other), other)

    def test_command_reply(self):
        self.assertEqual(self.sw.command("turn on the patio lights", "voice"), "Okay, patio lights on.")
        self.assertEqual(self.sw.command("are the patio lights on", "voice"), "patio lights are on.")
        self.assertEqual(self.sw.command("is the porch lamp on", "voice"), "Porch lamp is off.")
        self.assertIsNone(self.sw.command("what time is it", "voice"))

    def test_rename_writes_to_the_plug(self):
        ok, name = self.sw.rename("kasa-9c53220000aa-1", "  String  lights ")
        self.assertTrue(ok)
        self.assertEqual(name, "String lights")
        self.assertEqual(self.kasa.devices["192.168.1.40"]["children"][0]["alias"], "String lights")
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "porch lamp")[0])  # taken
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "<script>")[0])
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "x" * 31)[0])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings.update({"volume": 0, "assistant_enabled": True})
        self.engine = Engine(manager)
        self.kasa = FakeKasa()
        self.engine.switches = SmartSwitches(self.dir, drivers=[self.kasa])
        self.engine.switches.discover()
        self.engine.telegram.switch_filter = lambda text: self.engine.switches.parse(text) is not None
        self.sent = []
        self.engine.telegram.reply_to_chat = lambda chat, text: self.sent.append((chat, text)) or True

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_voice_switches_straight_away(self):
        reply = self.engine.answer_voice("Turn on the patio lights.", lambda _s: None)
        self.assertEqual(reply.text, "Okay, patio lights on.")
        self.assertIsNone(reply.confirm)
        self.assertEqual(self.kasa.calls, [("192.168.1.40", "80060000", True)])

    def test_claude_suggestion_asks_first(self):
        with mock.patch.object(self.engine.assistant, "ask",
                               return_value="I can turn the porch lamp on for you.\nCOMMAND: Turn on porch lamp"), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            reply = self.engine.answer_voice("it's dark at the front door", lambda _s: None)
        self.assertIn("Should I do that?", reply.text)
        self.assertEqual(self.kasa.calls, [])
        self.assertEqual(reply.confirm(), "Okay, Porch lamp on.")

    def test_telegram(self):
        t = self.engine.telegram
        t.route_text("111", "Are the patio lights on?")  # not taken for a camera question
        t.route_text("111", "patio outlet off")
        self.assertEqual(t.consume_questions(), [])
        for chat, text in t.consume_switch_commands():
            self.engine._switch_reply(chat, text)
        self.assertEqual(self.sent, [("111", "💡 patio lights are off."), ("111", "💡 Okay, patio outlet off.")])
        self.assertTrue(TelegramNotifier.is_command("Turn on patio lights"))  # Claude may offer it as a button
        self.assertFalse(TelegramNotifier.is_command("turn on the oven and burn the house down; rm -rf"))

    def test_claude_sees_them(self):
        self.assertIn("SMART PLUGS AND SWITCHES", self.engine.assistant_context())
        self.assertIn("patio outlet: on", self.engine.assistant_context())


class TestWeb(unittest.TestCase):
    def setUp(self):
        import socket

        import bcrypt

        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.kasa = FakeKasa()
        self.sw = SmartSwitches(self.dir, drivers=[self.kasa])
        self.sw.discover()
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        manager.settings["web"].update({"enabled": True, "port": port})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.server = WebServer(manager, FrameBroker(), engine=mock.Mock(switches=self.sw))
        self.assertTrue(self.server.start())
        self.server._auth.validate_session = lambda token, ip: mock.Mock(username="alex")
        self.client = self.server._app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body, header=True):
        return self.client.post(path, json=body, headers={"X-Sentinel-Request": "1"} if header else {})

    def test_list_and_set(self):
        data = self.client.get("/api/switches").get_json()
        self.assertEqual(len(data["switches"]), 3)
        r = self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(next(s for s in r.get_json()["switches"] if s["name"] == "Porch lamp")["on"])
        self.assertEqual(self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": "yes"}).status_code, 400)
        self.assertEqual(self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": False}, header=False)
                         .status_code, 400)  # no cross-site requests
        self.assertEqual(len(self.kasa.calls), 1)


if __name__ == "__main__":
    unittest.main()
