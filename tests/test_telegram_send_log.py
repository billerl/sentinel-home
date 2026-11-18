"""Telegram sends that fail or crawl leave a trace (sentinel/notifications/telegram_notifier.py _open)."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import unittest
import urllib.error
from unittest import mock

from sentinel.notifications import telegram_notifier
from sentinel.notifications.telegram_notifier import TelegramNotifier


class TestSendLog(unittest.TestCase):
    def setUp(self):
        self.logged = []
        patch = mock.patch.object(telegram_notifier.app_logger, "log",
                                  side_effect=lambda level, event, **k: self.logged.append((level, event, k)))
        patch.start()
        self.addCleanup(patch.stop)
        self.bot = TelegramNotifier({"telegram_bot_token": "123:SECRET", "telegram_chat_ids": "42"}, listen=False)

    def test_a_failed_send_is_logged_without_the_token(self):
        # 2026-11-18: alerts reached the phones half an hour late in a burst; nothing said what Telegram had done
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            self.assertFalse(self.bot._send_message_to_chat("42", "Someone is at the door."))
        self.assertEqual([(lvl, ev) for lvl, ev, _ in self.logged], [("warning", "telegram_send_failed")])
        fields = self.logged[0][2]
        self.assertEqual((fields["method"], fields["error_message"]), ("sendMessage", "timed out"))
        self.assertNotIn("SECRET", json.dumps(self.logged))

    def test_a_quick_send_says_nothing(self):
        answer = mock.MagicMock(status=200)
        answer.__enter__.return_value = answer
        with mock.patch("urllib.request.urlopen", return_value=answer):
            self.assertTrue(self.bot._send_message_to_chat("42", "hi"))
        self.assertEqual(self.logged, [])


if __name__ == "__main__":
    unittest.main()
