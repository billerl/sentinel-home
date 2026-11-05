"""Wi-Fi motion sensors: judging motion from scores, board hellos, polling, names."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest

from sentinel.engine import wifi_motion
from sentinel.engine.wifi_motion import MotionJudge, WifiMotion

NOW = 1_800_000_000.0
HELLO = json.dumps({"sentinel_sensor": "wifi_motion", "id": "8d24", "host": "sentinel-motion-8d24",
                    "ip": "10.9.9.9", "fw": "1.0.0"}).encode()


def quiet(judge, seconds, start=NOW, level=2.0):
    """Feed a still room: small wobble around `level`."""
    out = []
    for i in range(int(seconds * 4)):
        out.append(judge.feed(start + i * 0.25, level + (i % 5) * 0.1))
    return out


class JudgeTest(unittest.TestCase):
    def test_learns_first_then_sees_walking_and_lets_go(self):
        j = MotionJudge()
        quiet(j, 20)
        self.assertIsNone(j.baseline)  # needs 30 steady seconds before judging anything
        quiet(j, 110, start=NOW + 20)
        self.assertAlmostEqual(j.baseline, 2.2, delta=0.2)
        t = NOW + 130
        changes = [j.feed(t + i * 0.25, 12.0) for i in range(8)]
        self.assertIn("started", changes)
        self.assertTrue(j.motion)
        later = [c for c in quiet(j, 20, start=t + 2) if c]
        self.assertEqual(later, ["ended"])

    def test_one_spike_is_not_motion(self):
        j = MotionJudge()
        quiet(j, 130)
        t = NOW + 130
        self.assertIsNone(j.feed(t, 40.0))  # the median of the last second ignores a single spike
        self.assertIsNone(j.feed(t + 0.25, 2.0))
        self.assertFalse(j.motion)

    def test_a_steady_new_level_is_learned_not_reported_as_motion(self):
        """A door opened or someone sat down by a board: the score jumps and stays, steady. Not motion for long."""
        j = MotionJudge()
        quiet(j, 130)
        changes = [c for c in quiet(j, 120, start=NOW + 130, level=4.0) if c]
        self.assertFalse(j.motion)
        self.assertLessEqual(len(changes), 2)  # at most one short stretch while it re-learns
        self.assertAlmostEqual(j.baseline, 4.2, delta=0.2)

    def test_swinging_scores_never_become_the_quiet_level(self):
        j = MotionJudge()
        quiet(j, 130)
        for i in range(4 * 120):  # two minutes of walking: scores swing between 2 and 9
            j.feed(NOW + 130 + i * 0.25, 9.0 if i % 3 else 2.5)
        self.assertTrue(j.motion)
        self.assertAlmostEqual(j.baseline, 2.2, delta=0.2)

    def test_never_steady_falls_back_after_two_minutes(self):
        j = MotionJudge()
        for i in range(4 * 125):
            j.feed(NOW + i * 0.25, 9.0 if i % 3 else 2.5)
        self.assertEqual(j.baseline, 2.5)

    def test_no_reading_is_skipped(self):
        j = MotionJudge()
        self.assertIsNone(j.feed(NOW, -1))
        self.assertIsNone(j.smoothed)


class SensorsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.replies = {}
        self.wm = WifiMotion(self.dir, fetch=self._fetch)

    def tearDown(self):
        if self.wm._csv_file:
            self.wm._csv_file.close()
        shutil.rmtree(self.dir)

    def _fetch(self, ip):
        if ip not in self.replies:
            raise OSError("no answer")
        return self.replies[ip]

    def test_hello_uses_the_senders_address_and_is_remembered(self):
        self.assertTrue(self.wm.hello(HELLO, "192.168.1.77"))
        self.assertEqual(self.wm._sensors["8d24"].ip, "192.168.1.77")
        again = WifiMotion(self.dir)
        self.assertEqual(again._sensors["8d24"].ip, "192.168.1.77")
        self.assertEqual(again._sensors["8d24"].name, "Wi-Fi sensor 8d24")

    def test_bad_hellos_are_ignored(self):
        self.assertFalse(self.wm.hello(b"not json", "192.168.1.77"))
        self.assertFalse(self.wm.hello(json.dumps({"sentinel_sensor": "other", "id": "8d24"}).encode(), "192.168.1.77"))
        self.assertFalse(self.wm.hello(json.dumps({"sentinel_sensor": "wifi_motion", "id": "../x"}).encode(), "192.168.1.77"))
        self.assertFalse(self.wm.hello(HELLO, "8.8.8.8"))  # only boards on the home network
        self.assertEqual(self.wm._sensors, {})

    def test_poll_takes_each_score_once_and_survives_a_restart(self):
        self.wm.hello(HELLO, "192.168.1.77")
        s = self.wm._sensors["8d24"]
        self.replies["192.168.1.77"] = {"wifi_rssi": -60, "battery": {"v": 4.1, "pct": 88.0},
                                        "samples": [[10, 2.5, 49.0], [11, 2.6, 50.0]]}
        self.wm.poll(s, now=NOW)
        self.wm.poll(s, now=NOW + 1)  # same samples again: nothing new
        self.assertEqual(len(s.series), 2)
        self.replies["192.168.1.77"]["samples"] = [[0, 3.0, 48.0]]  # the board rebooted
        s.judge._baseline = 57.0  # pretend it had learned the old spot
        self.wm.poll(s, now=NOW + 2)
        self.assertEqual(len(s.series), 1)  # a restart (usually a move) starts the chart and the learning over
        self.assertIsNone(s.judge.baseline)
        view = self.wm.view(now=NOW + 2)["sensors"][0]
        self.assertTrue(view["online"])
        self.assertEqual(view["battery"]["pct"], 88.0)
        self.assertTrue(view["learning"])
        self.assertFalse(self.wm.view(now=NOW + 60)["sensors"][0]["online"])
        with open(os.path.join(self.dir, wifi_motion.LOG_DIR, os.listdir(os.path.join(self.dir, wifi_motion.LOG_DIR))[0])) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 4)  # header + three scores

    def test_switching_to_the_beacon_relearns_the_room(self):
        self.wm.hello(HELLO, "192.168.1.77")
        s = self.wm._sensors["8d24"]
        self.replies["192.168.1.77"] = {"source": "router", "samples": [[10, 2.5, 49.0]]}
        self.wm.poll(s, now=NOW)
        s.judge._baseline = 57.0
        self.replies["192.168.1.77"] = {"source": "beacon", "samples": [[11, 9.0, 49.0]]}
        self.wm.poll(s, now=NOW + 1)
        self.assertIsNone(s.judge.baseline)
        self.assertEqual(self.wm.view(now=NOW + 1)["sensors"][0]["source"], "beacon")

    def test_unreachable_or_junk_replies_change_nothing(self):
        self.wm.hello(HELLO, "192.168.1.77")
        s = self.wm._sensors["8d24"]
        self.wm.poll(s, now=NOW)
        self.replies["192.168.1.77"] = {"samples": [["x", None], "junk"]}
        self.wm.poll(s, now=NOW)
        self.assertEqual(len(s.series), 0)

    def test_rename(self):
        self.wm.hello(HELLO, "192.168.1.77")
        self.assertTrue(self.wm.rename("8d24", "  Family   room ")[0])
        self.assertEqual(WifiMotion(self.dir)._sensors["8d24"].name, "Family room")
        self.assertFalse(self.wm.rename("8d24", "<script>")[0])
        self.assertFalse(self.wm.rename("ffff", "Kitchen")[0])


if __name__ == "__main__":
    unittest.main()
