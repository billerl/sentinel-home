"""Alerts read out in a browser (sentinel/engine/heard.py): what a page is handed, and the sound made once."""

import _test_env  # noqa: F401  (isolates the event log)

import unittest

from sentinel.engine.heard import FRESH_SECONDS, HELLO, HELLO_ID, HeardAlerts, spoken


class TestHeardAlerts(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.heard = HeardAlerts(clock=lambda: self.now)

    def test_said_without_emoji(self):
        self.assertEqual(spoken("🚗 A car pulled into the driveway."), "A car pulled into the driveway.")
        self.assertEqual(spoken("📦 Reminder:\nthe package is still at the front door."),
                         "Reminder: the package is still at the front door.")
        self.assertEqual(spoken("⚠️"), "")

    def test_a_page_starts_from_now_and_then_gets_only_what_is_new(self):
        self.heard.add("🧍 A person is at the front door.")
        first = self.heard.since(None)
        self.assertEqual(first["items"], [])  # (what happened before the switch went on isn't recited)
        self.heard.add("🚗 A car pulled in.")
        self.heard.add("")
        got = self.heard.since(first["latest"])
        self.assertEqual([i["text"] for i in got["items"]], ["A car pulled in."])
        self.assertEqual(self.heard.since(got["latest"])["items"], [])

    def test_a_page_that_was_asleep_skips_the_stale_ones(self):
        start = self.heard.since(None)["latest"]
        self.heard.add("A person is at the front door.")
        self.now += FRESH_SECONDS + 1
        self.heard.add("A car pulled in.")
        self.assertEqual([i["text"] for i in self.heard.since(start)["items"]], ["A car pulled in."])

    def test_numbers_keep_rising_after_a_restart(self):
        self.heard.add("A car pulled in.")
        before = self.heard.since(None)["latest"]
        self.now += 20
        again = HeardAlerts(clock=lambda: self.now)
        again.add("A person is at the front door.")
        self.assertEqual(len(again.since(before)["items"]), 1)

    def test_the_voice_is_made_once_and_only_for_a_real_alert(self):
        made = []

        def synth(text):
            made.append(text)
            return b"RIFF" + text.encode()

        self.heard.add("A car pulled in.")
        alert = self.heard.since(self.heard.since(None)["latest"] - 1)["items"][0]
        self.assertEqual(self.heard.voice(alert["id"], synth), b"RIFFA car pulled in.")
        self.heard.voice(alert["id"], synth)
        self.assertEqual(made, ["A car pulled in."])
        self.assertIsNone(self.heard.voice(alert["id"] + 50, synth))
        self.assertEqual(self.heard.voice(HELLO_ID, synth), b"RIFF" + HELLO.encode())

    def test_no_voice_set_up_is_none(self):
        self.heard.add("A car pulled in.")
        alert = self.heard.since(self.heard.since(None)["latest"] - 1)["items"][0]
        self.assertIsNone(self.heard.voice(alert["id"], lambda text: None))


if __name__ == "__main__":
    unittest.main()
