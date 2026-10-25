"""Tests for never-ending motion (a web swinging in front of the lens) and parked cars swapping tracks at night."""

import _test_env  # noqa: F401  (isolates the event log)

import types
import unittest

import numpy as np

from sentinel.detection import motion_detector as md, nuisance as nz
from sentinel.detection.nuisance import NuisanceMap
from sentinel.detection.objects import Detection

W, H = 1280, 720
SPOT = (600, 160, 100, 80)


class TestNuisanceMap(unittest.TestCase):
    def test_never_ending_motion_is_muted_and_unlearned(self):
        m = NuisanceMap()
        for t in range(0, 600, 5):  # 10 minutes of motion in one spot
            m.observe([SPOT], W, H, now=1000.0 + t)
        self.assertTrue(m.is_muted(SPOT, W, H, now=1600.0))
        self.assertFalse(m.is_muted((100, 400, 150, 250), W, H, now=1600.0))  # elsewhere: normal
        self.assertFalse(m.is_muted((500, 100, 400, 400), W, H, now=1600.0))  # bigger than the spot: normal
        self.assertFalse(m.is_muted(SPOT, W, H, now=1600.0 + nz.UNMUTE_QUIET_SECONDS + 1))  # went quiet

    def test_occasional_motion_is_not_muted(self):
        m = NuisanceMap()
        for t in range(0, 3600, 120):  # something every 2 minutes for an hour
            m.observe([SPOT], W, H, now=1000.0 + t)
        self.assertFalse(m.is_muted(SPOT, W, H, now=4600.0))

    def test_real_subject_unmutes(self):
        m = NuisanceMap()
        for t in range(0, 600, 5):
            m.observe([SPOT], W, H, now=1000.0 + t)
        m.clear(SPOT, W, H)
        self.assertFalse(m.is_muted(SPOT, W, H, now=1600.0))


class _Frames:
    def __init__(self):
        self.next = None

    def isOpened(self):
        return True

    def read(self):
        return True, self.next

    def release(self):
        pass


class TestMotionDetector(unittest.TestCase):
    def setUp(self):
        self.clock = [5000.0]
        self._saved = (md.time, nz.time)
        fake = types.SimpleNamespace(time=lambda: self.clock[0], localtime=lambda *a: __import__("time").localtime(12 * 3600))
        md.time = nz.time = fake
        self.d = md.MotionDetector()
        self.d.cap = _Frames()
        self.d.feed_active = True
        self.d.recording_enabled = False
        self.d.set_night_bug_filter(False, 20, 6)

    def tearDown(self):
        md.time, nz.time = self._saved

    def frame(self, blobs):
        f = np.full((H, W, 3), 40, np.uint8)
        for x, y, w, h in blobs:
            f[y:y + h, x:x + w] = 230
        return f

    def run_frames(self, seconds, blobs_at):
        gated = 0
        self.active = 0
        for i in range(int(seconds * 2)):
            self.clock[0] += 0.5
            self.d.cap.next = self.frame(blobs_at(i))
            _, _, _, g, _ = self.d.detect_motion()
            gated += g
            self.active += self.d.activity_streak >= self.d.min_motion_frames
        return gated

    def test_swinging_web_stops_triggering_but_a_person_still_does(self):
        self.run_frames(30, lambda i: [])  # learn the empty background
        wobble = lambda i: [(600 + (i * 37) % 70, 165 + (i * 13) % 20, 40, 60)]  # noqa: E731
        self.assertGreater(self.run_frames(60, wobble), 0)  # at first it triggers, as now
        self.run_frames(600, wobble)
        self.assertEqual(self.run_frames(60, wobble), 0)  # learned: muted
        self.assertGreater(self.active, 60)  # ... but it's still moving and the object detector still gets the frames
        walker = lambda i: wobble(i) + [(100 + 25 * i, 400, 80, 200)]  # noqa: E731
        self.assertGreater(self.run_frames(10, walker), 0)  # someone walking in elsewhere still triggers


class TestParkedNeighbours(unittest.TestCase):
    def test_a_missed_parked_car_does_not_jump_to_its_neighbour(self):
        from sentinel.engine.objects import CameraObjectTracker

        tracker = CameraObjectTracker("cam")
        frame = np.zeros((H, W, 3), np.uint8)
        left, middle = (60, 275, 415, 440), (589, 271, 373, 449)
        frames = [[middle], [middle], [left], [left], [middle, left], [left], [middle]]  # IR: each missed now and then
        for i, boxes in enumerate(frames):
            tracker.update([Detection("car", 0.8, b) for b in boxes], frame, 1000.0 + 0.5 * i, lambda b: True)
        for track in tracker.tracks:
            self.assertFalse(track.vehicle_moved(W), track.boxes)


if __name__ == "__main__":
    unittest.main()
