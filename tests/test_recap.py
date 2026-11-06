"""Morning recap: the night in one message, the moments worth a video, and the video itself."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import unittest

import cv2
import numpy as np

from sentinel.engine import recap as R


def at(hhmm: str, day: str = "2026-11-06") -> float:
    return time.mktime(time.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"))


def ev(hhmm, grp, text, cam="Garage", day="2026-11-06"):
    return {"ts": at(hhmm, day), "grp": grp, "description": text, "camera_key": cam, "camera_name": cam, "label": None}


NIGHT = [
    ev("22:15", "person", "🚶 Someone is walking past the Garage.", day="2026-11-05"),
    ev("23:10", "vehicle", "🚗 A car is driving past the Driveway.", day="2026-11-05"),
    ev("23:40", "vehicle", "🚗 A car is driving past the Driveway.", day="2026-11-05"),
    ev("01:45", "household", "🏠 Stephanie's Edge got home (Garage)."),
    ev("02:14", "animal", "🦌 A deer is in the Back Yard.", cam="Back Yard"),
    ev("02:15", "animal", "🦌 A deer is in the Back Yard.", cam="Back Yard"),  # the same deer, a minute later
    ev("06:50", "vehicle", "🚗 A red car pulled into the Driveway."),
]


class TestRecap(unittest.TestCase):
    def test_window(self):
        self.assertEqual(R.window(at("07:00"), "21:00", "07:00"), (at("21:00", "2026-11-05"), at("07:00")))
        self.assertEqual(R.window(at("06:30"), "21:00", "07:00")[1], at("07:00", "2026-11-05"))  # before 7: last night's
        self.assertEqual(R.window(at("23:05"), "21:00", "23:05")[0], at("21:00"))  # asked late: since this evening

    def test_summary(self):
        text = R.summary(NIGHT, at("21:00", "2026-11-05"), at("07:00"), ["📦 1 package still at the Front Door"])
        lines = text.split("\n")
        self.assertEqual(lines[0], "🌅 Overnight, 9 pm – 7 am")
        self.assertIn("🚶 Someone is walking past the Garage at 10:15 PM", lines)
        self.assertEqual(sum("deer" in l for l in lines), 1)  # once, not twice
        self.assertIn("🚗 2 cars drove past", lines)
        self.assertIn("🚗 A red car pulled into the Driveway at 6:50 AM", lines)
        self.assertIn("🏠 Stephanie's Edge got home (Garage) at 1:45 AM", lines)
        self.assertEqual(lines[-1], "📦 1 package still at the Front Door")
        self.assertIn("A quiet night", R.summary([], 0, 3600, []))

    def test_highlights_skip_cars_driving_past(self):
        picked = R.highlights(NIGHT)
        self.assertEqual([p["description"][:6] for p in picked], ["🚶 Some", "🦌 A de", "🚗 A re"])

    def test_on_request_by_name_only(self):
        self.assertTrue(R.RECAP.match("Recap"))
        self.assertTrue(R.RECAP.match("morning recap?"))
        self.assertFalse(R.RECAP.match("what happened last night"))  # the visits answer that one

    def test_due_once_a_day(self):
        d = tempfile.mkdtemp(prefix="sentinel-recap-")
        self.addCleanup(shutil.rmtree, d, True)
        sent = []
        store = type("S", (), {"events": lambda self, a, b: [], "query": lambda self, a, b: []})()
        r = R.Recap({}, store, lambda: [], lambda chat, text: sent.append(text), None, os.path.join(d, "sent"))
        self.assertFalse(r.due(at("06:59")))
        self.assertTrue(r.due(at("07:00")))
        r.run(now=at("07:00"))
        self.assertFalse(r.due(at("07:01")))  # done today
        self.assertFalse(r.due(at("10:30", "2026-11-07")))  # Sentinel was down all morning: skip that day
        self.assertEqual(len(sent), 1)

    def test_video(self):
        from sentinel.recordings.store import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("no ffmpeg")
        d = tempfile.mkdtemp(prefix="sentinel-recap-")
        self.addCleanup(shutil.rmtree, d, True)
        clip = os.path.join(d, "clip.avi")
        w = cv2.VideoWriter(clip, cv2.VideoWriter_fourcc(*"MJPG"), 12, (640, 360))
        for i in range(60):
            w.write(np.full((360, 640, 3), i * 4, np.uint8))
        w.release()
        out = os.path.join(d, "out.mp4")
        self.assertTrue(R.make_video([(clip, 2.0, "2:14 AM  Back Yard: A deer"), (clip, 4.5, "6:50 AM  Garage")], out, ffmpeg))
        cap = cv2.VideoCapture(out)
        self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), R.VIDEO_WIDTH)
        self.assertGreater(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 60)


if __name__ == "__main__":
    unittest.main()
