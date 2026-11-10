"""TV shortcuts (beta): recording presses made on the web remote and playing them back."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import tempfile
import unittest

from sentinel.engine.tv_shortcuts import EXTRA, LONGEST_WAIT, QUIT_WAIT, RECORD_SECONDS, SLOWER, TVShortcuts


class FakeSwitches:
    def __init__(self):
        self.on_tv_step = None
        self.done = []
        self.fail_on = None

    def _do(self, what):
        self.done.append(what)
        return (False, "no") if what == self.fail_on else (True, "ok")

    def tv_key(self, tv, key):
        return self._do(("key", tv, key))

    def tv_open(self, tv, app):
        return self._do(("open", tv, app))

    def tv_close(self, tv, app):
        return self._do(("close", tv, app))

    def tv_type(self, tv, text):
        return self._do(("type", tv, text))


class TVShortcutsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = 1000.0
        self.slept = []
        self.sw = FakeSwitches()
        self.sc = TVShortcuts(self.dir, self.sw, clock=lambda: self.now, sleep=self.slept.append,
                              spawn=lambda fn, *a: fn(*a))

    def press(self, after, do, value, tv="samsung-1"):
        self.now += after
        self.sw.on_tv_step(tv, do, value)

    def record_judge_judy(self):
        self.assertTrue(self.sc.start("samsung-1")[0])
        self.press(5, "open", "3201707014489")
        self.press(6, "key", "ok")
        self.press(2, "key", "up")
        self.press(60, "type", "judge judy")
        return self.sc.stop("samsung-1", "Judge Judy")

    def test_records_steps_with_pauses_and_saves(self):
        ok, msg = self.record_judge_judy()
        self.assertTrue(ok)
        self.assertIn("4 steps", msg)
        saved = json.load(open(os.path.join(self.dir, "tv_shortcuts.json")))
        steps = saved[0]["steps"]
        self.assertEqual([s["do"] for s in steps], ["open", "key", "key", "type"])
        self.assertEqual([s["wait"] for s in steps], [0.0, 6, 2, LONGEST_WAIT])  # first: no wait; long: capped
        view = self.sc.view("samsung-1")
        self.assertFalse(view["recording"])
        self.assertEqual(view["shortcuts"][0]["name"], "Judge Judy")
        self.assertEqual(self.sc.view("firetv-2")["shortcuts"], [])  # each TV has its own

    def test_presses_only_count_while_recording_that_tv(self):
        self.press(1, "key", "ok")  # not recording: ignored
        self.sc.start("samsung-1")
        self.press(1, "key", "down", tv="firetv-2")  # another TV
        self.assertEqual(self.sc.view("samsung-1")["steps"], 0)
        self.assertEqual(self.sc.stop("samsung-1", "Nothing"), (False, "No buttons were pressed yet."))

    def test_play_quits_the_first_app_then_replays_slower(self):
        self.record_judge_judy()
        sid = self.sc.view("samsung-1")["shortcuts"][0]["id"]
        self.assertTrue(self.sc.play("samsung-1", sid)[0])
        self.assertEqual(self.sw.done, [("close", "samsung-1", "3201707014489"), ("open", "samsung-1", "3201707014489"),
                                        ("key", "samsung-1", "ok"), ("key", "samsung-1", "up"),
                                        ("type", "samsung-1", "judge judy")])
        self.assertEqual(self.slept, [QUIT_WAIT, 6 * SLOWER + EXTRA, 2 * SLOWER + EXTRA, LONGEST_WAIT * SLOWER + EXTRA])
        self.assertIsNone(self.sc.view("samsung-1")["playing"])

    def test_playback_isnt_recorded_and_stops_at_a_failed_step(self):
        self.record_judge_judy()
        sid = self.sc.view("samsung-1")["shortcuts"][0]["id"]
        self.sw.fail_on = ("key", "samsung-1", "ok")
        self.sc.start("samsung-1")
        self.assertEqual(self.sc.play("samsung-1", sid), (False, "Stop recording first."))
        self.sc.cancel("samsung-1")
        self.sc.play("samsung-1", sid)
        self.assertNotIn(("key", "samsung-1", "up"), self.sw.done)  # stopped after the failed OK

    def test_names_and_limits(self):
        self.sc.start("samsung-1")
        self.press(1, "key", "ok")
        self.assertFalse(self.sc.stop("samsung-1", "<script>")[0])
        self.assertFalse(self.sc.stop("samsung-1", "")[0])
        self.assertTrue(self.sc.view("samsung-1")["recording"])  # a bad name keeps the recording
        self.now += RECORD_SECONDS + 1
        self.assertFalse(self.sc.view("samsung-1")["recording"])  # left running too long: dropped

    def test_same_name_replaces_and_delete(self):
        self.record_judge_judy()
        self.record_judge_judy()
        shortcuts = self.sc.view("samsung-1")["shortcuts"]
        self.assertEqual(len(shortcuts), 1)
        self.assertTrue(self.sc.delete("samsung-1", shortcuts[0]["id"])[0])
        self.assertFalse(self.sc.delete("samsung-1", shortcuts[0]["id"])[0])
        reloaded = TVShortcuts(self.dir, FakeSwitches())
        self.assertEqual(reloaded.view("samsung-1")["shortcuts"], [])


if __name__ == "__main__":
    unittest.main()
