"""Tests for the CLIP second opinion on person/animal detections and how the pipeline uses it."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest

import numpy as np

from sentinel.detection.objects import Detection
from sentinel.detection.subject_check import SubjectChecker

GREY = np.full((360, 640, 3), 120, dtype=np.uint8)
CAM_URL = "rtsp://hass:secret@192.168.0.9:554/h264/ch1/sub/av_stream"


class _Fixed(SubjectChecker):
    """Real rules, scripted CLIP probabilities."""

    def __init__(self, **probs):
        self.available = True
        self.probs = dict({"person": 0, "cat": 0, "dog": 0, "deer": 0, "bird": 0, "object": 0}, **probs)

    def probabilities(self, frame, box):
        return dict(self.probs)


class TestRules(unittest.TestCase):
    def kind(self, group, score=0.6, **probs):
        return _Fixed(**probs).check(GREY, (0, 0, 10, 10), group, score).kind

    def test_person_that_is_a_cat(self):
        self.assertEqual(self.kind("person", 0.69, person=0.26, cat=0.55, object=0.09), "cat")

    def test_person_is_never_a_bird(self):
        self.assertEqual(self.kind("person", 0.76, person=0.3, bird=0.66), "")

    def test_objects_unless_the_detector_was_very_sure(self):
        self.assertEqual(self.kind("person", 0.55, person=0.1, object=0.9), "object")
        self.assertEqual(self.kind("person", 0.85, person=0.34, object=0.64), "")  # someone right at the lens

    def test_animals(self):
        self.assertEqual(self.kind("animal", 0.5, bird=0.1, cat=0.74, dog=0.16), "cat")  # "cow"/"bird" that's a cat
        self.assertEqual(self.kind("animal", 0.5, person=0.8, cat=0.1), "person")
        self.assertEqual(self.kind("animal", 0.5, cat=0.3, dog=0.3, person=0.3), "")

    def test_unsure_keeps_the_detector(self):
        self.assertEqual(self.kind("person", 0.6, person=0.47, cat=0.19, object=0.16), "")


class TestPipeline(unittest.TestCase):
    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.engine.objects import CameraObjectTracker
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-subject-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings["camera_profiles"] = {CAM_URL: {"source": CAM_URL, "display": "Garage"}}
        e = Engine(sm)
        e._build_cameras()
        self.p = e.cameras["192.168.0.9"]
        self.p.tracker = CameraObjectTracker(self.p.host)
        self.p.detector.recording = True
        self.events = []
        self.p._on_object = lambda pipe, ev: self.events.append(ev.description)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, checker, dets_per_frame):
        self.p._detections = types.SimpleNamespace(subject_checker=checker, vehicle_classifier=None)
        for i, dets in enumerate(dets_per_frame):
            self.p._handle_detections(dets, GREY, 1000.0 + i)

    def test_cat_called_person_alerts_once_as_a_cat(self):
        boxes = [(200, 200, 60, 40), (205, 200, 60, 40), (210, 200, 60, 40), (212, 200, 60, 40), (214, 201, 60, 40)]
        self.feed(_Fixed(person=0.26, cat=0.55), [[Detection("person", 0.69, b)] for b in boxes])
        self.assertEqual(self.events, ["🐈 A cat is at the Garage."])
        self.assertEqual(self.p._clip_labels, {"cat"})

    def test_bin_called_person_never_alerts(self):
        self.feed(_Fixed(object=0.9, person=0.05), [[Detection("person", 0.6, (500, 250, 40, 90))]] * 4)
        self.assertEqual(self.events, [])
        self.assertEqual(self.p._clip_labels, set())

    def test_parked_car_does_not_label_a_wind_clip(self):
        self.feed(None, [[Detection("car", 0.8, (560, 150, 70, 40))]] * 4)
        self.assertEqual(self.p._clip_labels, set())
        self.feed(None, [[Detection("car", 0.8, (100 + 40 * i, 150, 70, 40))] for i in range(4)])
        self.assertEqual(self.p._clip_labels, {"car"})  # a car that moved does



class TestParkedAndDoors(TestPipeline):
    def test_door_opening_is_not_driving(self):
        from sentinel.engine.objects import Track
        parked = Track(1, "truck", "vehicle", 0, 0, [(0, (500, 100, 300, 400)), (1, (450, 100, 350, 400))], 0.8, 2)
        self.assertFalse(parked.vehicle_moved(1280))  # box widened by an opening door: centre moved 25 px
        driving = Track(2, "car", "vehicle", 0, 0, [(0, (100, 100, 300, 200)), (1, (300, 100, 300, 200))], 0.8, 2)
        self.assertTrue(driving.vehicle_moved(1280))

    def park(self, *spots):
        """Spots the once-a-minute driveway check found (vehicle id or None for each box)."""
        from sentinel.engine.household import Parked, Vehicle
        parked = [Parked(box, 0, 0, seen=3, confirmed=True, vehicle_id=vid) for box, vid in spots]
        presence = types.SimpleNamespace(spots=lambda key: parked, home_ids=lambda: set())
        household = types.SimpleNamespace(vehicle=lambda vid: Vehicle(vid, "Luke's Truck", "Luke", [], "pickup"))
        self.p._detections = types.SimpleNamespace(subject_checker=None, vehicle_classifier=None, presence=presence,
                                                   household=household)

    def test_parked_car_stays_quiet_until_its_spot_is_empty(self):
        self.park(((300, 150, 200, 120), 4))
        jiggle = [(300, 150, 200, 120), (380, 150, 200, 120), (300, 150, 200, 120), (385, 152, 200, 120)]
        for i, b in enumerate(jiggle):  # people around it, doors opening: the box jumps about
            self.p._handle_detections([Detection("car", 0.8, b)], GREY, 2000.0 + i)
        self.assertEqual(self.events, [])
        for i, x in enumerate((420, 470)):  # drives out of the spot
            self.p._handle_detections([Detection("car", 0.8, (x, 150, 200, 120))], GREY, 2010.0 + i)
        self.assertEqual(self.events, ["🛻 Luke's Truck is leaving the Garage."])  # named by its spot

    def test_headlights_on_a_parked_car_the_last_scan_missed(self):
        from sentinel.engine.household import Parked
        spot = (912, 233, 366, 476)
        self.park((spot, 4))
        self.p._detections.presence.spots = lambda key: [Parked(spot, 0, 0, seen=5, missed=1, confirmed=True, vehicle_id=4)]
        merged = (528, 220, 746, 490)  # at night the detector sometimes boxes both parked cars as one "truck"
        frames = [[spot], [spot], [merged], [merged, spot], [merged]]
        big = np.zeros((720, 1280, 3), np.uint8)
        for i, boxes in enumerate(frames):
            self.p._handle_detections([Detection("car", 0.6, b) for b in boxes], big, 2000.0 + i)
        self.assertEqual(self.events, [])

    def test_tracker_hopping_to_the_next_parked_car_is_not_driving(self):
        a, b = (100, 150, 200, 120), (280, 150, 200, 120)
        self.park((a, None), (b, None))
        frames = [[a], [a], [b], [b], [b]]  # at night the detector sees one car, then the other
        for i, boxes in enumerate(frames):
            self.p._handle_detections([Detection("car", 0.8, x) for x in boxes], GREY, 2000.0 + i)
        self.assertEqual(self.events, [])

    def test_someone_in_front_of_a_parked_car_keeps_it_parked(self):
        spot = (300, 150, 200, 120)
        self.park((spot, None))
        frames = [[Detection("car", 0.8, spot)], [Detection("car", 0.8, spot)],
                  [Detection("car", 0.8, (360, 150, 150, 120)), Detection("person", 0.9, (330, 160, 60, 100))],
                  [Detection("person", 0.9, (330, 160, 60, 100))]]  # the car's box shrinks, then vanishes behind them
        for i, dets in enumerate(frames):
            self.p._handle_detections(dets, GREY, 2000.0 + i)
        self.assertEqual([e for e in self.events if "Someone" not in e], [])  # the person, yes; the car, no

    def test_arriving_car_still_alerts(self):
        self.park(((40, 200, 150, 100), None))  # someone else parked on the left
        for i, y in enumerate((20, 60, 100, 140)):  # a car comes down the driveway on the right
            self.p._handle_detections([Detection("car", 0.8, (40, 200, 150, 100)),
                                       Detection("car", 0.8, (420, y, 160, 100))], GREY, 2000.0 + i)
        self.assertEqual(len(self.events), 1)
        self.assertIn("pulled in", self.events[0])


class TestClipPerAlert(unittest.TestCase):
    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-clips-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings.update({"telegram_motion_alerts": True, "alert_mode": "objects"})
        self.e = Engine(sm)
        self.e.detection_worker = object()
        self.e.telegram.include_video = True
        self.sent = []
        self.e.telegram.trigger_alert = lambda message, video_path=None, **kw: self.sent.append(message)
        self.cam = types.SimpleNamespace(host="192.168.0.9", display_name="Garage", last_clip_labels=["car"])

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_clips_that_go_with_an_alert(self):
        import time as _t
        self.e._on_clip(self.cam, "a.mp4", "motion", [])
        self.assertEqual(self.sent, [])  # labelled "car" but no alert went out: no clip
        self.e._clip_for_alert["192.168.0.9"] = (_t.time(), "🛻 Luke's Truck pulled into the Driveway (Garage).")
        self.e._on_clip(self.cam, "b.mp4", "motion", [])
        self.e._on_clip(self.cam, "c.mp4", "motion", [])
        self.assertEqual(self.sent, ["🛻 Luke's Truck pulled into the Driveway (Garage). (clip)"])  # once

if __name__ == "__main__":
    unittest.main()
