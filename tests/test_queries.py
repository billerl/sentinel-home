"""Tests for Telegram 'videos ...' requests."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import unittest

from sentinel.engine import Engine
from sentinel.engine.queries import is_video_query, parse_video_query
from sentinel.settings.manager import SettingsManager

NOW = time.mktime((2026, 10, 20, 17, 30, 0, 0, 0, -1))  # Tue 17:30 local
TODAY = time.mktime((2026, 10, 20, 0, 0, 0, 0, 0, -1))
CAMS = ["Front Door", "Garage", "Rear Door"]


def at(h, m=0, day=TODAY):
    return day + h * 3600 + m * 60


class TestParse(unittest.TestCase):
    def parse(self, text):
        q, err = parse_video_query(text, CAMS, now=NOW)
        self.assertIsNotNone(q, err)
        return q

    def test_recognises_requests(self):
        for t in ["Videos last hour", "video today", "recordings yesterday", "footage 2pm-4pm", "clips from 2pm"]:
            self.assertTrue(is_video_query(t), t)
        for t in ["clip", "clips", "enable clips", "disable clips", "status"]:
            self.assertFalse(is_video_query(t), t)

    def test_relative(self):
        q = self.parse("videos last 2 hours")
        self.assertEqual((q.start, q.end), (NOW - 7200, NOW))
        self.assertEqual(self.parse("videos last 30 minutes").start, NOW - 1800)
        self.assertEqual(self.parse("videos").start, NOW - 3600)

    def test_days(self):
        self.assertEqual((self.parse("videos today").start, self.parse("videos today").end), (TODAY, NOW))
        y = self.parse("videos yesterday")
        self.assertEqual((y.start, y.end), (TODAY - 86400, TODAY))

    def test_ranges(self):
        q = self.parse("videos 2pm-4pm")
        self.assertEqual((q.start, q.end), (at(14), at(16)))
        q = self.parse("videos from 14:00 to 15:30")
        self.assertEqual((q.start, q.end), (at(14), at(15, 30)))
        q = self.parse("videos 2-4")  # small numbers default to the afternoon
        self.assertEqual((q.start, q.end), (at(14), at(16)))
        q = self.parse("videos 11am-1pm")
        self.assertEqual((q.start, q.end), (at(11), at(13)))
        q = self.parse("videos yesterday 9am-10am")
        self.assertEqual((q.start, q.end), (at(9, day=TODAY - 86400), at(10, day=TODAY - 86400)))
        self.assertEqual(self.parse("videos since 8am").start, at(8))

    def test_filters(self):
        q = self.parse("videos today people front door")
        self.assertEqual(q.groups, {"person"})
        self.assertEqual(q.cameras, ["Front Door"])
        self.assertEqual(self.parse("videos last hour cars").groups, {"vehicle"})

    def test_nonsense_gets_help(self):
        q, err = parse_video_query("videos whenever", CAMS, now=NOW)
        self.assertIsNone(q)
        self.assertIn("Videos last 2 hours", err)


class TestAnswer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-q-")
        self.old = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        url = "rtsp://u:p@192.168.0.10:554/x"
        sm.settings["camera_profiles"] = {url: {"source": url, "display": "Front Door"}}
        sm.settings["volume"] = 0
        self.engine = Engine(sm)
        self.engine._build_cameras()
        store = self.engine.recording_store
        now = time.time()
        for i, labels in enumerate(["person", "", "car", ""]):
            p = store.new_clip_path("192.168.0.10", now - 600 + i * 60)
            open(p, "wb").write(b"x")
            rid = store.add_recording(p, "192.168.0.10", "Front Door", now - 600 + i * 60, now - 590 + i * 60)
            if labels:
                store.set_labels(rid, [labels])
        self.sent, self.replies = [], []
        self.engine.telegram.send_video = lambda chat, path, caption: self.sent.append(caption) or True
        self.engine.telegram.reply_to_chat = lambda chat, msg: self.replies.append(msg)
        store.web_playable_path = lambda rid: os.path.join(self.dir, "fake.mp4")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_sends_matching_clips(self):
        self.engine.answer_video_query("1", "videos last hour")
        self.assertEqual(len(self.sent), 4)
        self.assertIn("Sending 4 clips", self.replies[0])

    def test_filters_by_group_and_caps(self):
        self.engine.answer_video_query("1", "videos last hour people")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("person", self.sent[0])
        self.sent.clear()
        self.engine.answer_video_query("1", "videos last hour", max_clips=2)
        self.assertEqual(len(self.sent), 2)  # the two labelled clips are preferred
        self.assertIn("Found 4 clips", self.replies[-1])

    def test_nothing_found(self):
        self.engine.answer_video_query("1", "videos yesterday")
        self.assertEqual(self.sent, [])
        self.assertIn("No recorded clips", self.replies[0])


if __name__ == "__main__":
    unittest.main()
