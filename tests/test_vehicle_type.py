"""Tests for delivery / vehicle-type recognition and how it changes events and alerts."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest

import numpy as np

from sentinel.detection.objects import Detection
from sentinel.detection.vehicle_type import (VEHICLE_TYPES, VISION_MODEL, VehicleClassifier, VehicleType,
                                             crop_for_clip)
from sentinel.engine import Engine
from sentinel.engine.objects import CameraObjectTracker, Track, describe, wants_type_check
from sentinel.engine.queries import parse_video_query
from sentinel.settings.manager import SettingsManager

W, H = 640, 360
GREY = np.full((H, W, 3), 120, dtype=np.uint8)
CAM_URL = "rtsp://hass:secret@192.168.0.10:554/h264/ch1/sub/av_stream"


def _vt(key, prob=0.9):
    phrase, delivery, _ = VEHICLE_TYPES[key]
    return VehicleType(key, phrase, delivery, prob, {key: prob})


def _track(label, boxes, vt=None):
    t = Track(1, label, "vehicle", 0, 0, [(i, b) for i, b in enumerate(boxes)], 0.9, len(boxes))
    t.best_box = boxes[-1]
    t.best_frame = GREY
    t.see(GREY, boxes[-1])
    t.vehicle_type = vt
    return t


ARRIVING = [(200, 60, 200, 100), (200, 200, 200, 100)]


class TestWording(unittest.TestCase):
    def test_delivery_phrases(self):
        self.assertEqual(describe(_track("truck", ARRIVING, _vt("ups")), "Garage", "Driveway", GREY, W),
                         "📦 A UPS truck pulled into the Driveway (Garage).")
        self.assertEqual(describe(_track("car", ARRIVING, _vt("amazon")), "Front Door", None, GREY, W),
                         "📦 An Amazon van pulled in at the Front Door.")
        parked = _track("truck", [(200, 150, 200, 100)] * 2, _vt("usps"))
        self.assertEqual(describe(parked, "Front Door", None, GREY, W), "📦 A mail truck is at the Front Door.")

    def test_vehicle_type_keeps_colour(self):
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        frame[:] = (40, 40, 40)
        frame[200:300, 200:400] = (30, 30, 200)  # BGR red
        truck = _track("truck", ARRIVING, _vt("pickup"))
        self.assertEqual(describe(truck, "Garage", "Driveway", frame, W),
                         "🛻 A red pickup truck pulled into the Driveway (Garage).")
        self.assertEqual(describe(_track("car", ARRIVING, _vt("suv")), "Garage", None, GREY, W),
                         "🚙 An SUV pulled in at the Garage.")

    def test_type_overrides_generic_vehicle(self):
        bus = _track("bus", [(0, 150, 150, 90), (300, 150, 150, 90)], _vt("school_bus"))
        bus.best_score = 0.6  # would be "a vehicle" without a type
        self.assertEqual(describe(bus, "Garage", None, GREY, W), "🚌 A school bus is driving past the Garage.")

    def test_motorcycle_type_needs_detector_agreement(self):
        car = _track("car", ARRIVING, _vt("motorcycle"))
        self.assertEqual(describe(car, "Garage", None, GREY, W), "🚗 A car pulled in at the Garage.")


class TestRecheck(unittest.TestCase):
    def test_checks_until_delivery_or_limit(self):
        t = _track("truck", [(0, 0, 50, 40)])
        self.assertTrue(wants_type_check(t))  # first look
        t.type_checks, t.type_checked_area = 1, 50 * 40
        self.assertFalse(wants_type_check(t))  # same size: nothing new to see
        t.see(GREY, (0, 0, 80, 60))
        self.assertTrue(wants_type_check(t))  # came closer
        t.vehicle_type = _vt("fedex")
        self.assertFalse(wants_type_check(t))  # already known
        t.vehicle_type, t.type_checks = None, 3
        self.assertFalse(wants_type_check(t))

    def test_people_and_bicycles_are_skipped(self):
        self.assertFalse(wants_type_check(_track("bicycle", ARRIVING)))
        person = _track("person", ARRIVING)
        person.group = "person"
        self.assertFalse(wants_type_check(person))


class _FakeClassifier(VehicleClassifier):
    """Real thresholds, scripted probabilities."""

    def __init__(self, probs):
        self.available = True
        self.probs = probs

    def probabilities(self, frame, box, embedding=None):
        return dict(self.probs)

    def embed(self, crop):
        return np.zeros(512, dtype=np.float32)


class TestThresholds(unittest.TestCase):
    COLOUR = np.zeros((H, W, 3), dtype=np.uint8)
    COLOUR[:] = (20, 120, 40)

    def test_confident_delivery(self):
        vt = _FakeClassifier({"ups": 0.7, "car": 0.2, "van": 0.1}).classify(self.COLOUR, (0, 0, 50, 50))
        self.assertEqual((vt.key, vt.delivery, vt.phrase), ("ups", True, "UPS truck"))

    def test_close_call_is_not_a_delivery(self):
        self.assertIsNone(_FakeClassifier({"amazon": 0.5, "van": 0.4, "car": 0.1}).classify(self.COLOUR, (0, 0, 50, 50)))

    def test_night_needs_more_certainty(self):
        clf = _FakeClassifier({"usps": 0.6, "van": 0.2, "car": 0.2})
        self.assertIsNotNone(clf.classify(self.COLOUR, (0, 0, 50, 50)))
        self.assertIsNone(clf.classify(GREY, (0, 0, 50, 50)))

    def test_scenery_and_unsure_types_are_none(self):
        self.assertIsNone(_FakeClassifier({"scenery": 0.9, "car": 0.1}).classify(self.COLOUR, (0, 0, 9, 9)))
        self.assertIsNone(_FakeClassifier({"pickup": 0.3, "car": 0.3, "suv": 0.4 - 0.01}).classify(self.COLOUR, (0, 0, 9, 9)))

    def test_crop_is_square_and_safe_at_edges(self):
        crop = crop_for_clip(GREY, (600, 300, 100, 80))
        self.assertEqual(crop.shape[0], crop.shape[1])
        self.assertGreater(crop.shape[0], 100)

    def test_missing_model_is_unavailable(self):
        clf = VehicleClassifier(model_path="/nonexistent.onnx")
        self.assertFalse(clf.available)
        self.assertIsNone(clf.classify(GREY, (0, 0, 10, 10)))


@unittest.skipUnless(os.path.isfile(VISION_MODEL), "CLIP model not downloaded")
class TestClassifierRuns(unittest.TestCase):
    def test_probabilities_cover_all_types(self):
        clf = VehicleClassifier()
        self.assertTrue(clf.available, clf.error)
        probs = clf.probabilities(GREY, (100, 100, 200, 120))
        self.assertEqual(set(probs), set(VEHICLE_TYPES))
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=3)


class TestPipelineAndAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-vt-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        settings = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        settings.settings["camera_profiles"] = {
            CAM_URL: {"source": CAM_URL, "display": "Front", "active_zones": [], "entry_lines": []}
        }
        settings.settings["alert_mode"] = "objects"
        settings.settings["telegram_motion_alerts"] = True
        self.engine = Engine(settings)
        self.engine._build_cameras()
        self.pipeline = self.engine.cameras["192.168.0.10"]
        self.events = []
        self.pipeline._on_object = lambda p, e: self.events.append(e)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _drive(self, classifier, boxes, label="truck"):
        self.pipeline._detections = types.SimpleNamespace(vehicle_classifier=classifier)
        if self.pipeline.tracker is None:
            self.pipeline.tracker = CameraObjectTracker(self.pipeline.host)
        for i, box in enumerate(boxes):
            self.pipeline._handle_detections([Detection(label, 0.8, box)], GREY, 1000.0 + i)

    def test_vehicle_alert_names_the_delivery(self):
        clf = _FakeClassifier({"amazon": 0.8, "van": 0.2})
        clf.classify = lambda frame, box, embedding=None: _vt("amazon")
        self._drive(clf, [(100, 100, 60, 40), (150, 100, 60, 40)])
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0].description.startswith("📦 An Amazon van"))
        self.assertEqual(self.events[0].alert_group, "delivery")

    def test_delivery_found_when_closer_sends_a_second_event(self):
        answers = iter([None, _vt("ups")])
        clf = _FakeClassifier({})
        clf.classify = lambda frame, box, embedding=None: next(answers)
        self._drive(clf, [(100, 100, 60, 40), (150, 100, 60, 40), (160, 90, 100, 70)])
        self.assertEqual([e.alert_group for e in self.events], ["vehicle", "delivery"])
        self.assertTrue(self.events[1].description.startswith("📦 A UPS truck"))

    def test_without_classifier_nothing_changes(self):
        self._drive(None, [(100, 100, 60, 40), (150, 100, 60, 40)])
        self.assertEqual(self.events[0].alert_group, "vehicle")

    def _alert(self, groups, vt):
        sent = []
        self.engine.settings["object_alert_groups"] = groups
        self.engine.detection_worker = object()  # objects mode active
        self.engine.telegram.trigger_photo_alert = lambda text, jpeg: sent.append(text)
        self.engine._object_alert_times.clear()
        track = _track("truck", ARRIVING, vt)
        from sentinel.engine.objects import ObjectEvent
        event = ObjectEvent("192.168.0.10", "Front", track, None, "📦 test", GREY, 1000.0)
        self.engine._on_object(self.pipeline, event)
        return sent

    def test_alert_groups(self):
        self.assertEqual(len(self._alert(["vehicle"], _vt("ups"))), 1)       # vehicles include deliveries
        self.assertEqual(len(self._alert(["delivery"], _vt("ups"))), 1)      # deliveries only
        self.assertEqual(len(self._alert(["delivery"], _vt("pickup"))), 0)   # ...not other vehicles
        self.assertEqual(len(self._alert(["delivery"], None)), 0)


class _FakeHD:
    running = True

    def __init__(self, frame):
        self.frame, self.asked = frame, []

    def keyframe_near(self, ts):
        self.asked.append(ts)
        return self.frame


class _FakeWorker:
    """Runs 'later' jobs immediately; detector finds a vehicle near the expected HD spot."""

    def __init__(self, classifier, hd_box):
        self.vehicle_classifier = classifier
        self.hd_box = hd_box
        self.jobs = 0
        outer = self

        class _Det:
            def detect(self, region, min_score=0.3):
                outer.region = region.shape
                return [Detection("truck", 0.7, outer.hd_box)]
        self.detector = _Det()

    def run_later(self, job):
        self.jobs += 1
        job()
        return True


class TestHDView(TestPipelineAndAlerts):
    def _setup_hd(self, hd_box):
        self.seen = []
        clf = _FakeClassifier({})
        clf.classify = lambda frame, box, embedding=None: (self.seen.append((frame.shape, box)), _vt("ups"))[1]
        hd_frame = np.full((H * 4, W * 4, 3), 90, dtype=np.uint8)
        self.pipeline.hd = _FakeHD(hd_frame)
        self.pipeline._last_hd_grab = 0.0
        worker = _FakeWorker(clf, hd_box)
        if self.pipeline.tracker is None:
            self.pipeline.tracker = CameraObjectTracker(self.pipeline.host)
        self.pipeline._detections = worker
        return worker

    def _go(self, boxes):
        for i, box in enumerate(boxes):
            self.pipeline._handle_detections([Detection("truck", 0.8, box)], GREY, 1000.0 + i)

    def test_far_vehicle_is_classified_in_hd(self):
        # Closest sub box (320,100,44,33) is centred at HD (1368,466), 176x132; the search region
        # starts at (928,268), so a vehicle found at region (352,132) is right where expected.
        worker = self._setup_hd((352, 132, 176, 132))
        self._go([(290, 100, 40, 30), (320, 100, 44, 33)])
        self.assertEqual(worker.jobs, 1)
        self.assertEqual(self.seen[0][0], (H * 4, W * 4, 3))         # HD frame used
        self.assertEqual(self.seen[0][1], (1280, 400, 176, 132))     # re-found box, in HD coordinates
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0].description.startswith("📦 A UPS truck"))
        self.assertEqual(self.events[0].snapshot.shape[1], 960)       # zoomed close-up
        self.assertFalse(self.events[0].track.type_pending)

    def test_wrong_vehicle_in_hd_falls_back_to_substream(self):
        self._setup_hd((0, 0, 900, 500))  # huge box far from the expected spot
        self._go([(290, 100, 40, 30), (320, 100, 44, 33)])
        self.assertEqual(self.seen[0][0], (H, W, 3))
        self.assertEqual(self.events[0].snapshot.shape, (H, W, 3))

    def test_near_vehicle_skips_hd(self):
        worker = self._setup_hd((0, 0, 10, 10))
        self._go([(100, 100, 200, 120), (130, 100, 200, 120)])
        self.assertEqual(worker.jobs, 0)
        self.assertEqual(self.pipeline.hd.asked, [])
        self.assertEqual(len(self.events), 1)

    def test_zoomed_view_keeps_box(self):
        from sentinel.engine.camera import zoomed_view
        frame = np.zeros((1440, 2560, 3), dtype=np.uint8)
        img, (x, y, w, h) = zoomed_view(frame, (2400, 1300, 150, 100))  # near the corner
        self.assertEqual(img.shape[1], 960)
        self.assertTrue(0 <= x and x + w <= img.shape[1] + 1 and 0 <= y and y + h <= img.shape[0] + 1)


class TestKeyframeNear(unittest.TestCase):
    def test_picks_nearest_segment(self):
        import subprocess
        import time as _time
        from sentinel.recordings.hd_recorder import HDRecorder
        from sentinel.recordings.store import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg not available")
        root = tempfile.mkdtemp(prefix="sentinel-kf-")
        self.addCleanup(shutil.rmtree, root, True)
        rec = HDRecorder("rtsp://x/main/", "cam", buffer_root=root)
        os.makedirs(rec.buffer_dir)
        base = int(_time.time()) - 10
        for i, colour in enumerate(("red", "blue")):
            ts = base + 2 * i
            path = os.path.join(rec.buffer_dir, _time.strftime("%Y%m%d%H%M%S", _time.localtime(ts)) + ".mkv")
            subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", f"color={colour}:s=64x36:d=1",
                            "-c:v", "libx264", "-f", "matroska", path], check=True)
            os.utime(path, (ts + 2, ts + 2))  # last write = end of segment
        first = rec.keyframe_near(base + 0.3, wait=False)
        second = rec.keyframe_near(base + 2.4, wait=False)
        self.assertGreater(int(first[..., 2].mean()), 150)   # red (BGR)
        self.assertGreater(int(second[..., 0].mean()), 150)  # blue
        self.assertIsNone(rec.keyframe_near(base + 60, wait=False))


class TestQueries(unittest.TestCase):
    def test_delivery_words(self):
        q, _ = parse_video_query("videos yesterday deliveries", [])
        self.assertEqual(q.groups, {"delivery"})
        q, _ = parse_video_query("videos today amazon van", [])
        self.assertEqual(q.groups, {"delivery"})
        q, _ = parse_video_query("videos today vans", [])
        self.assertEqual(q.groups, {"vehicle"})


if __name__ == "__main__":
    unittest.main()
