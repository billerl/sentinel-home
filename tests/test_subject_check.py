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


if __name__ == "__main__":
    unittest.main()
