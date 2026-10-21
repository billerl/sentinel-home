"""Tests for object tracking, event rules, and plain-English descriptions."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import threading
import time
import unittest

import numpy as np

from sentinel.detection.objects import DEFAULT_MODEL, Detection, ObjectDetector
from sentinel.engine.objects import CameraObjectTracker, DetectionWorker, Track, describe, vehicle_color

W, H = 640, 360
GREY = np.full((H, W, 3), 120, dtype=np.uint8)


def _always(_box):
    return True


def _feed(tracker, boxes, label="car", frame=GREY, in_zone=_always, start=1000.0):
    ready = []
    for i, box in enumerate(boxes):
        ready += tracker.update([Detection(label, 0.8, box)], frame, start + i, in_zone)
    return ready


class TestTracker(unittest.TestCase):
    def test_moving_vehicle_reports_once(self):
        t = CameraObjectTracker("cam")
        ready = _feed(t, [(100 + 30 * i, 150, 120, 80) for i in range(5)])
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].label, "car")

    def test_parked_vehicle_never_reports(self):
        t = CameraObjectTracker("cam")
        self.assertEqual(_feed(t, [(100, 150, 120, 80)] * 6), [])

    def test_person_reports_even_when_standing(self):
        t = CameraObjectTracker("cam")
        ready = _feed(t, [(300, 100, 40, 120)] * 3, label="person")
        self.assertEqual([r.label for r in ready], ["person"])

    def test_outside_zone_is_ignored(self):
        t = CameraObjectTracker("cam")
        self.assertEqual(_feed(t, [(300, 100, 40, 120)] * 3, label="person", in_zone=lambda b: False), [])

    def test_single_sighting_is_not_enough(self):
        t = CameraObjectTracker("cam")
        self.assertEqual(_feed(t, [(300, 100, 40, 120)], label="person"), [])


class TestTrackerLearning(unittest.TestCase):
    def test_label_is_majority_vote(self):
        t = CameraObjectTracker("cam")
        boxes = [(100 + 30 * i, 150, 120, 80) for i in range(4)]
        labels = ["car", "bus", "car", "car"]
        ready = []
        for i, (box, label) in enumerate(zip(boxes, labels)):
            ready += t.update([Detection(label, 0.8, box)], GREY, 1000.0 + i, _always)
        self.assertEqual(t.tracks[0].label, "car")

    def test_static_false_person_is_learned(self):
        t = CameraObjectTracker("cam")
        spot = (300, 100, 40, 120)
        first = _feed(t, [spot] * 3, label="person")
        self.assertEqual(len(first), 1)  # first time: could be a real person standing still
        t.update([], GREY, 2000.0, _always)  # track expires -> remembered as a static spot
        again = []
        for i in range(3):
            again += t.update([Detection("person", 0.8, spot)], GREY, 3000.0 + i, _always)
        self.assertEqual(again, [])
        # Someone walking through the same spot still counts.
        walk = _feed(t, [(200 + 40 * i, 100, 40, 120) for i in range(3)], label="person", start=4000.0)
        self.assertEqual(len(walk), 1)

    def test_jittering_box_is_not_walking(self):
        t = CameraObjectTracker("cam")
        jitter = [(300, 100, 40, 120), (322, 96, 30, 110), (305, 104, 45, 125)]  # ~20 px wobble
        ready = []
        for i, box in enumerate(jitter):
            ready += t.update([Detection("person", 0.7, box)], GREY, 1000.0 + i, _always)
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0].is_still(W))
        self.assertEqual(describe(ready[0], "Front Door", None, GREY, W), "🚶 Someone is at the Front Door.")

    def test_person_called_dog_is_not_a_second_event(self):
        t = CameraObjectTracker("cam")
        ready = []
        for i in range(3):
            box = (100 + 60 * i, 100, 40, 120)
            ready += t.update([Detection("person", 0.8, box), Detection("dog", 0.6, box)], GREY, 1000.0 + i, _always)
        self.assertEqual([r.label for r in ready], ["person"])

    def test_uncertain_person_needs_three_sightings(self):
        # e.g. someone sitting on a dark porch, scored ~0.5 by the small model
        t = CameraObjectTracker("cam")
        after_two = []
        for i in range(2):
            after_two += t.update([Detection("person", 0.5, (300, 100, 40, 120))], GREY, 1000.0 + i, _always)
        self.assertEqual(after_two, [])
        third = t.update([Detection("person", 0.5, (300, 100, 40, 120))], GREY, 1002.0, _always)
        self.assertEqual([r.label for r in third], ["person"])

    def test_below_minimum_never_reports(self):
        t = CameraObjectTracker("cam")
        ready = []
        for i in range(5):
            ready += t.update([Detection("person", 0.4, (300, 100, 40, 120))], GREY, 1000.0 + i, _always)
        self.assertEqual(ready, [])

    def test_static_spots_persist(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "mem.json")
        t = CameraObjectTracker("cam", path)
        _feed(t, [(300, 100, 40, 120)] * 3, label="person")
        t.update([], GREY, 2000.0, _always)  # expire -> remembered + saved
        again = CameraObjectTracker("cam", path)
        self.assertEqual(len(again.static_spots), 1)
        ready = []
        for i in range(3):
            ready += again.update([Detection("person", 0.8, (302, 101, 40, 120))], GREY, 3000.0 + i, _always)
        self.assertEqual(ready, [])


class TestDescribe(unittest.TestCase):
    def _track(self, label, group, boxes):
        return Track(1, label, group, 0, 0, [(i, b) for i, b in enumerate(boxes)], 0.9, len(boxes))

    def test_person_phrases(self):
        still = self._track("person", "person", [(300, 100, 40, 120)] * 2)
        self.assertEqual(describe(still, "Front Door", None, GREY, W), "🚶 Someone is at the Front Door.")
        self.assertEqual(describe(still, "Rear Door", "Patio", GREY, W), "🚶 Someone is in the Patio (Rear Door).")
        self.assertEqual(describe(still, "Front Door", None, GREY, W, companions=1), "🚶 2 people are at the Front Door.")
        walking = self._track("person", "person", [(100, 100, 40, 120), (300, 100, 40, 120)])
        self.assertEqual(describe(walking, "Front Door", None, GREY, W), "🚶 Someone is walking past the Front Door.")

    def test_vehicle_arriving_with_colour(self):
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        frame[:] = (40, 40, 40)
        frame[200:300, 200:400] = (30, 30, 200)  # BGR red car
        car = self._track("car", "vehicle", [(200, 60, 200, 100), (200, 200, 200, 100)])
        self.assertEqual(describe(car, "Garage", "Driveway", frame, W), "🚗 A red car pulled into the Driveway (Garage).")

    def test_uncertain_vehicle_type_is_generic(self):
        bus = self._track("bus", "vehicle", [(0, 150, 150, 90), (300, 150, 150, 90)])
        bus.best_score = 0.6
        self.assertEqual(describe(bus, "Garage", None, GREY, W), "🚗 A vehicle is driving past the Garage.")

    def test_ambiguous_colour_is_omitted(self):
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        frame[:] = (40, 90, 40)
        frame[150:250, 200:400] = (120, 100, 90)  # bluish-grey car in shade
        car = self._track("car", "vehicle", [(200, 150, 200, 100)] * 2)
        self.assertEqual(describe(car, "Garage", None, frame, W), "🚗 A car is at the Garage.")

    def test_vehicle_at_night_has_no_colour(self):
        truck = self._track("truck", "vehicle", [(0, 150, 150, 90), (300, 150, 150, 90)])
        self.assertEqual(describe(truck, "Garage", None, GREY, W), "🚚 A truck is driving past the Garage.")

    def test_colour_names(self):
        def solid(bgr):
            f = np.zeros((100, 100, 3), dtype=np.uint8)
            f[:] = (10, 60, 10)  # make the scene colourful (not IR grey)
            f[20:80, 20:80] = bgr
            return vehicle_color(f, (20, 20, 60, 60))
        self.assertEqual(solid((240, 240, 240)), "white")
        self.assertEqual(solid((200, 80, 20)), "blue")
        self.assertEqual(solid((20, 20, 20)), "black")
        self.assertIsNone(vehicle_color(GREY, (10, 10, 50, 50)))


class _FakeDetector:
    def __init__(self):
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return [Detection("person", 0.9, (1, 1, 10, 10))]


class TestDetectionWorker(unittest.TestCase):
    def test_rate_limited_per_camera_and_calls_back(self):
        det = _FakeDetector()
        worker = DetectionWorker(det, min_interval=10.0)
        worker.start()
        got = threading.Event()
        self.assertTrue(worker.submit("a", GREY, lambda d, f, ts: got.set()))
        self.assertFalse(worker.submit("a", GREY, lambda d, f, ts: None))  # too soon
        self.assertTrue(worker.submit("b", GREY, lambda d, f, ts: None))   # other camera ok
        self.assertTrue(got.wait(5))
        worker.stop()


@unittest.skipUnless(os.path.isfile(os.path.join(os.path.dirname(__file__), "..", DEFAULT_MODEL)) or os.path.isfile(DEFAULT_MODEL),
                     "detector model not downloaded")
class TestDetectorRuns(unittest.TestCase):
    def test_runs_on_a_frame(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        det = ObjectDetector(os.path.join(root, "models", "detector", "yolox_tiny.onnx"))
        self.assertTrue(det.available, det.error)
        start = time.time()
        result = det.detect(GREY)
        self.assertIsInstance(result, list)
        self.assertLess(time.time() - start, 10)


if __name__ == "__main__":
    unittest.main()
