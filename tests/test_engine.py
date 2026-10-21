"""Tests for the headless engine, camera config API, and motion gating."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import unittest

import numpy as np

from sentinel.detection.motion_detector import MotionDetector
from sentinel.engine import Engine
from sentinel.settings.manager import SettingsManager

CAM_URL = "rtsp://hass:secret@192.168.0.10:554/h264/ch1/sub/av_stream"


class _FrameCapture:
    """Fake cv2.VideoCapture that plays a list of frames."""

    def __init__(self, frames):
        self.frames = list(frames)

    def isOpened(self):
        return True

    def read(self):
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def get(self, _prop):
        return 12.0

    def release(self):
        pass


def _background():
    rng = np.random.default_rng(0)
    base = np.full((180, 320, 3), 90, dtype=np.uint8)
    noise = rng.integers(0, 6, size=base.shape, dtype=np.uint8)
    return base + noise


def _run(detector, frames):
    detector.cap = _FrameCapture(frames)
    detector.feed_active = True
    return [detector.detect_motion() for _ in range(len(frames))]


def _with_box(frame, x, y, w=40, h=60):
    out = frame.copy()
    out[y:y + h, x:x + w] = 250
    return out


class TestMotionGating(unittest.TestCase):
    def setUp(self):
        self.detector = MotionDetector()
        self.detector.min_contour_area = 400
        self.detector.set_night_bug_filter(False, 20, 6)
        self.detector.set_recording_enabled(False)

    def test_requires_consecutive_frames(self):
        bg = _background()
        results = _run(self.detector, [bg] * 40 + [_with_box(bg, 100 + i * 3, 60) for i in range(4)])
        gated = [r[3] for r in results[40:]]
        self.assertEqual(gated, [False, False, True, True])

    def test_full_size_frames_are_downscaled_but_boxes_are_full_size(self):
        rng = np.random.default_rng(1)
        bg = (np.full((360, 640, 3), 90, dtype=np.uint8)
              + rng.integers(0, 6, size=(360, 640, 3), dtype=np.uint8))
        self.detector.min_contour_area = 2000
        frames = [bg] * 40 + [_with_box(bg, 300 + i * 6, 150, 80, 120) for i in range(4)]
        results = _run(self.detector, frames)
        self.assertTrue(results[-1][3])
        x, y, w, h = results[-1][2][0]
        self.assertAlmostEqual(x, 318, delta=12)
        self.assertAlmostEqual(w, 80, delta=30)  # includes trailing motion; a scale bug would be 2x
        self.assertAlmostEqual(h, 120, delta=30)

    def test_single_frame_blip_is_ignored(self):
        bg = _background()
        results = _run(self.detector, [bg] * 40 + [_with_box(bg, 100, 60)] + [bg] * 3)
        self.assertFalse(any(r[3] for r in results[40:]))

    def test_whole_frame_lighting_change_is_ignored(self):
        bg = _background()
        bright = np.clip(bg.astype(np.int16) + 120, 0, 255).astype(np.uint8)
        results = _run(self.detector, [bg] * 40 + [bright] * 4)
        self.assertFalse(any(r[3] for r in results[40:]))

    def test_zone_uses_ground_point(self):
        # Zone covers only the bottom half; box straddles it with its feet inside.
        self.detector.set_active_zones([[[0, 0.5], [1, 0.5], [1, 1], [0, 1]]])
        self.assertTrue(self.detector._box_in_active_zone(100, 40, 40, 60, 320, 180))
        # Box entirely in the top half: out.
        self.assertFalse(self.detector._box_in_active_zone(100, 10, 40, 40, 320, 180))

    def test_recording_uses_clean_frames(self):
        self.detector.set_recording_enabled(True)
        written = []
        self.detector.start_recording = lambda size, reason="motion": setattr(self.detector, "recording", True)
        self.detector.write_frame = lambda frame: written.append(frame.copy())
        bg = _background()
        _run(self.detector, [bg] * 40 + [_with_box(bg, 100 + i * 3, 60) for i in range(5)])
        self.assertTrue(written)
        # No green annotation pixels burned into recorded frames.
        green = (written[-1][:, :, 1] == 255) & (written[-1][:, :, 0] == 0)
        self.assertFalse(green.any())


class TestEngineConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-engine-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        settings = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        settings.settings["camera_profiles"] = {
            CAM_URL: {"source": CAM_URL, "display": "Front", "active_zones": [], "entry_lines": []}
        }
        self.engine = Engine(settings)
        self.engine._build_cameras()

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_configs_are_keyed_by_host_without_credentials(self):
        configs = self.engine.camera_configs()
        self.assertEqual([c["key"] for c in configs], ["192.168.0.10"])
        self.assertNotIn("secret", repr(configs))

    def test_update_validates_and_applies(self):
        ok, _ = self.engine.update_camera_config(
            "192.168.0.10",
            {
                "active_zones": [[[0, 0], [1.5, 0], [1, 1]], [[0, 0], [1, 1]]],
                "entry_direction": "both",
                "min_contour_area": 5,
            },
        )
        self.assertTrue(ok)
        profile = self.engine.settings["camera_profiles"][CAM_URL]
        self.assertEqual(profile["active_zones"], [[[0, 0], [1.0, 0], [1, 1]]])  # clamped; 2-pt zone dropped
        self.assertEqual(profile["min_contour_area"], 100)
        detector = self.engine.cameras["192.168.0.10"].detector
        self.assertEqual(detector.entry_direction, "both")
        self.assertEqual(len(detector.active_zones), 1)
        with open(os.path.join(self.dir, "settings.json")) as handle:
            self.assertIn('"entry_direction": "both"', handle.read())

    def test_update_rejects_bad_input(self):
        self.assertFalse(self.engine.update_camera_config("10.9.9.9", {})[0])
        self.assertFalse(self.engine.update_camera_config("192.168.0.10", {"source": "x"})[0])
        self.assertFalse(
            self.engine.update_camera_config("192.168.0.10", {"entry_direction": "sideways"})[0]
        )
        self.assertFalse(
            self.engine.update_camera_config("192.168.0.10", {"min_contour_area": "lots"})[0]
        )

    def test_zone_outlines_can_be_hidden(self):
        pipeline = self.engine.cameras["192.168.0.10"]
        self.assertTrue(self.engine.update_camera_config(
            "192.168.0.10", {"active_zones": [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]]})[0])
        published = []
        pipeline._broker = type("B", (), {"publish": lambda self, cid, name, frame, jpeg_quality: published.append(frame)})()
        for show in (True, False):
            self.assertTrue(self.engine.update_settings({"web.show_zones": show})[0])
            pipeline._last_publish = 0.0
            pipeline._publish(np.zeros((90, 160, 3), dtype=np.uint8), None)
        self.assertGreater(int(published[0].sum()), 0)   # outline drawn
        self.assertEqual(int(published[1].sum()), 0)     # hidden
        self.assertEqual(len(pipeline.detector.active_zones), 1)  # still enforced

    def test_unnamed_zones_never_become_none(self):
        from sentinel.engine.engine import _clean_zone_names, _repair_zone_names

        self.assertEqual(_clean_zone_names([None, " Driveway ", 5]), ["", "Driveway", ""])
        profiles = {"a": {"zone_names": ["None", "Porch"]}, "b": {"zone_names": ["Yard"]}}
        self.assertTrue(_repair_zone_names(profiles))
        self.assertEqual(profiles["a"]["zone_names"], ["", "Porch"])
        self.assertFalse(_repair_zone_names(profiles))

    def test_feature_toggle_updates_profiles(self):
        replies = []
        self.engine.telegram.reply_to_chat = lambda chat, msg: replies.append(msg)
        self.engine.apply_feature_toggles("1", {"recording": False})
        self.assertFalse(self.engine.cameras["192.168.0.10"].detector.recording_enabled)
        self.assertIn("Recording: off", replies[0])
        self.assertIn("Front: OFFLINE, recording off", self.engine.status_message("1"))


if __name__ == "__main__":
    unittest.main()
