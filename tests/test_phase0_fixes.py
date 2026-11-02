"""Regression tests for logging redaction/rotation, settings saves, and detector stop."""

import _test_env  # noqa: F401  (isolates the event log)
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest

import numpy as np

from sentinel.detection.motion_detector import MotionDetector
from sentinel.logging_utils import StructuredLogger, redact
from sentinel.settings.manager import SettingsManager


class TestRedaction(unittest.TestCase):
    def test_masks_url_passwords(self) -> None:
        self.assertEqual(
            redact("rtsp://hass:S3cr3t!@192.168.0.10:554/h264/ch1/sub"),
            "rtsp://hass:***@192.168.0.10:554/h264/ch1/sub",
        )
        self.assertEqual(redact("no creds rtsp://10.0.0.1/x"), "no creds rtsp://10.0.0.1/x")
        self.assertEqual(
            redact({"a": ["Failed to open rtsp://u:p@h/s"], "n": 3}),
            {"a": ["Failed to open rtsp://u:***@h/s"], "n": 3},
        )


class TestStructuredLogger(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="sentinel-log-")
        self.path = os.path.join(self.dir, "events.log")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_log_records_are_redacted(self) -> None:
        StructuredLogger(self.path).log("info", "x", camera_id="rtsp://u:pw@h/s")
        with open(self.path) as handle:
            record = json.loads(handle.readline())
        self.assertEqual(record["camera_id"], "rtsp://u:***@h/s")

    def test_rotation_keeps_backup_count(self) -> None:
        logger = StructuredLogger(self.path, max_bytes=200, backup_count=2)
        for i in range(50):
            logger.log("info", "event", i=i, pad="x" * 40)
        names = sorted(os.listdir(self.dir))
        self.assertEqual(names, ["events.log", "events.log.1", "events.log.2"])
        self.assertLess(os.path.getsize(self.path), 400)


class TestSettingsSave(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="sentinel-set-")
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_saved_file_is_owner_only(self) -> None:
        manager = SettingsManager(settings_file=self.path)
        self.assertTrue(manager.save_settings(force=True))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_debounced_set_is_saved_later_not_dropped(self) -> None:
        manager = SettingsManager(settings_file=self.path)
        manager.save_settings(force=True)
        manager.set("recording_retention_days", 45)  # inside debounce window
        timer = manager._deferred_save
        self.assertIsNotNone(timer)
        timer.join(timeout=10)
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["recording_retention_days"], 45)

    def test_concurrent_saves_do_not_corrupt(self) -> None:
        manager = SettingsManager(settings_file=self.path)
        errors = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    manager.settings[f"k{n}"] = i
                    manager.save_settings(force=True)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.path) as handle:
            json.load(handle)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])


class _FailingCapture:
    def __init__(self) -> None:
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self):
        return False, None

    def release(self) -> None:
        self.released = True


class TestDetectorStop(unittest.TestCase):
    def test_repeated_read_failures_stop_camera_without_deadlock(self) -> None:
        detector = MotionDetector()
        cap = _FailingCapture()
        detector.cap = cap
        detector.feed_active = True
        detector.max_consecutive_read_failures = 3

        done = threading.Event()

        def run() -> None:
            for _ in range(3):
                detector.detect_motion()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(5), "detect_motion deadlocked on stop_camera")
        self.assertFalse(detector.feed_active)
        self.assertTrue(cap.released)

    def test_stop_camera_finalizes_recording(self) -> None:
        detector = MotionDetector()
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        path = detector.start_recording((160, 120))
        detector.write_frame(frame)
        detector.stop_camera()
        self.assertFalse(detector.recording)
        video_path, _ = detector.consume_last_completed_recording()
        self.assertEqual(video_path, path)
        os.remove(path)

    def test_recording_fps_follows_camera(self) -> None:
        detector = MotionDetector()

        class _Cap:
            def get(self, prop):
                return 12.0

        detector.cap = _Cap()
        detector._update_recording_fps(20)
        self.assertEqual(detector.recording_fps, 12.0)
        self.assertEqual(detector.recording_target_frames, 120)
        detector._update_recording_fps(8)
        self.assertEqual(detector.recording_fps, 8.0)
        detector.cap = None


if __name__ == "__main__":
    unittest.main()
