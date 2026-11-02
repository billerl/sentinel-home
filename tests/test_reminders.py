"""Reminders: the exact command, saving only after a yes / tap, and sending at the right time."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from sentinel.engine import Engine
from sentinel.engine.reminders import ReminderStore, due_message, is_reminder_command, parse_remind, when_words
from sentinel.notifications.telegram_notifier import TelegramNotifier
from sentinel.settings.manager import SettingsManager

# A Monday morning (local time), so "Wednesday" and "tomorrow" are fixed.
NOW = time.mktime(time.strptime("2026-11-02 12:55", "%Y-%m-%d %H:%M"))
WED_9 = time.mktime(time.strptime("2026-11-04 09:00", "%Y-%m-%d %H:%M"))
SOON = time.strftime("%Y-%m-%d", time.localtime(time.time() + 10 * 86400))  # for the engine tests (real clock)


class TestParse(unittest.TestCase):
    def test_exact_command(self):
        self.assertEqual(parse_remind("Remind 2026-11-04 09:00 PT appointment at 11am", NOW),
                         ((WED_9, "PT appointment at 11am"), ""))
        self.assertEqual(parse_remind("remind 2026-11-04 9:00   PT", NOW)[0], (WED_9, "PT"))

    def test_bad_ones_say_why(self):
        self.assertIn("passed", parse_remind("Remind 2026-11-01 09:00 PT", NOW)[1])
        self.assertIn("doesn't exist", parse_remind("Remind 2026-02-30 09:00 PT", NOW)[1])
        self.assertIn("doesn't exist", parse_remind("Remind 2026-11-04 25:00 PT", NOW)[1])
        self.assertIn("year", parse_remind("Remind 2028-11-04 09:00 PT", NOW)[1])
        self.assertIsNone(parse_remind("remind me on Wednesday", NOW)[0])

    def test_recognised_commands(self):
        for text in ("Remind 2026-11-04 09:00 PT", "Reminders", "my reminders", "What are my reminders?",
                     "Cancel reminder 3"):
            self.assertTrue(is_reminder_command(text), text)
        for text in ("remind me Wednesday at 9 about PT", "cancel", "reminder"):
            self.assertFalse(is_reminder_command(text), text)

    def test_when_words(self):
        self.assertEqual(when_words(WED_9, NOW), "Wednesday November 4 at 9:00 am")
        self.assertEqual(when_words(NOW + 3600, NOW), "today at 1:55 pm")
        self.assertEqual(when_words(WED_9 - 86400, NOW), "tomorrow at 9:00 am")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-reminders-")
        self.store = ReminderStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_due_sent_cancel_and_survives_restart(self):
        a = self.store.add(WED_9, "PT", "111")
        b = self.store.add(WED_9 + 60, "voice one")
        self.assertEqual([r.id for r in ReminderStore(self.dir).pending()], [a.id, b.id])  # kept on disk
        self.assertEqual([r.id for r in self.store.pending("222")], [b.id])  # voice ones show in every chat
        self.assertEqual(self.store.due(WED_9 - 1), [])
        self.assertEqual([r.id for r in self.store.due(WED_9)], [a.id])
        self.store.mark_sent(a.id)
        self.assertEqual([r.id for r in self.store.pending()], [b.id])
        self.assertFalse(self.store.cancel(b.id + 99))
        self.assertFalse(self.store.cancel(a.id))  # already sent
        self.assertTrue(self.store.cancel(b.id))
        self.assertEqual(self.store.pending(), [])

    def test_late_message_says_so(self):
        r = self.store.add(WED_9, "PT", "1")
        self.assertEqual(due_message(r, WED_9 + 30), "⏰ Reminder: PT")
        self.assertIn("Sentinel wasn't running", due_message(r, WED_9 + 3600))


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-reminders-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings.update({"volume": 0, "assistant_enabled": True})
        self.engine = Engine(manager)
        self.sent = []
        self.engine.telegram.chat_ids = ["111"]
        self.engine.telegram.reply_to_chat = lambda chat, text: self.sent.append((chat, text)) or True
        self.engine.telegram.is_configured = lambda: True

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_typed_command_is_routed_and_saved(self):
        t = self.engine.telegram
        t.route_text("111", f"Remind {SOON} 09:00 PT appointment")
        t.route_text("111", "What are my reminders?")  # not mistaken for a camera question
        self.assertEqual(t.consume_questions(), [])
        commands = t.consume_reminder_commands()
        self.assertEqual(len(commands), 2)
        self.assertTrue(TelegramNotifier.is_command("Remind 2030-01-02 09:00 PT"))  # Claude may offer it as a button
        replies = [self.engine.reminder_command(chat, text) for chat, text in commands]
        self.assertIn("I'll remind you", replies[0])
        self.assertIn("PT appointment", replies[1])
        self.assertIn("Cancel reminder", replies[1])

    def test_sent_once_at_its_time(self):
        self.engine.reminders.add(time.time() - 1, "PT", "111")
        self.engine.reminders.add(time.time() + 3600, "later", "")
        self.engine.send_due_reminders()
        self.engine.send_due_reminders()
        self.assertEqual(self.sent, [("111", "⏰ Reminder: PT")])

    def test_voice_reminder_goes_to_every_chat_and_failed_send_retries(self):
        self.engine.reminders.add(time.time() - 1, "PT", "")
        self.engine.telegram.chat_ids = ["111", "222"]
        self.engine.telegram.reply_to_chat = lambda chat, text: False
        self.engine.send_due_reminders()
        self.assertEqual(len(self.engine.reminders.pending()), 1)  # kept for the next try
        self.engine.telegram.reply_to_chat = lambda chat, text: self.sent.append(chat) or True
        self.engine.send_due_reminders()
        self.assertEqual(self.sent, ["111", "222"])
        self.assertEqual(self.engine.reminders.pending(), [])

    def test_voice_asks_yes_before_saving(self):
        reply_text = ("I'll remind you Wednesday November 4 at 9 am about your PT appointment at 11.\n"
                      f"COMMAND: Remind {SOON} 09:00 PT appointment at 11am")
        with mock.patch.object(self.engine.assistant, "ask", return_value=reply_text), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            reply = self.engine.answer_voice("At 9am on Wednesday, send a message to Telegram to remind me of my PT "
                                             "appointment at 11am.", lambda _s: None)
        self.assertIn("Should I do that?", reply.text)
        self.assertNotIn("COMMAND", reply.text)
        self.assertEqual(self.engine.reminders.pending(), [])  # nothing saved before "yes"
        self.assertIn("I'll remind you", reply.confirm())
        pending = self.engine.reminders.pending()
        self.assertEqual([(r.text, r.chat_id) for r in pending], [("PT appointment at 11am", "")])
        self.assertIn("PT appointment", self.engine.answer_voice("What are my reminders?", lambda _s: None).text)

    def test_claude_sees_the_reminders(self):
        self.engine.reminders.add(time.time() + 3600, "PT", "111")
        self.assertIn("REMINDERS ALREADY SET:\n", self.engine.assistant_context())


if __name__ == "__main__":
    unittest.main()
