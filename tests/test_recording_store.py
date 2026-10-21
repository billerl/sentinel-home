"""Tests for sentinel.recordings.RecordingStore and detector integration."""

import _test_env  # noqa: F401  (isolates the event log)
import os
import shutil
import tempfile
import time
import unittest

import numpy as np

from sentinel.detection.motion_detector import MotionDetector
from sentinel.recordings.store import (
    UNKNOWN_CAMERA_KEY,
    RecordingStore,
    camera_slug,
    find_ffmpeg,
    local_day_bounds,
)


def _touch(path: str, mtime: float = None, size: int = 10) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestRecordingStore(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="sentinel-rec-")
        self.store = RecordingStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _add_clip(self, camera: str, start: float, reason: str = "motion") -> int:
        path = _touch(self.store.new_clip_path(camera, start), mtime=start)
        return self.store.add_recording(path, camera, camera.upper(), start, start + 10, reason)

    def test_camera_slug(self) -> None:
        self.assertEqual(camera_slug("192.168.0.10"), "192-168-0-10")
        self.assertEqual(camera_slug("../../etc"), "etc")
        self.assertEqual(camera_slug(""), UNKNOWN_CAMERA_KEY)

    def test_clip_paths_are_per_day_and_unique_per_camera(self) -> None:
        ts = time.mktime((2026, 10, 20, 14, 32, 5, 0, 0, -1)) + 0.25
        a = self.store.new_clip_path("192.168.0.10", ts)
        b = self.store.new_clip_path("192.168.0.11", ts)
        self.assertEqual(os.path.dirname(a), os.path.join(self.root, "2026-10-20"))
        self.assertEqual(os.path.basename(a), "192-168-0-10_143205_250.avi")
        self.assertNotEqual(a, b)

    def test_add_and_query_by_day(self) -> None:
        day_start, day_end = local_day_bounds("2026-10-20")
        self._add_clip("cam1", day_start + 3600)
        self._add_clip("cam2", day_start + 7200, reason="telegram_clip_request")
        self._add_clip("cam1", day_end + 60)  # next day
        clips = self.store.query(day_start, day_end)
        self.assertEqual([c["camera_key"] for c in clips], ["cam1", "cam2"])
        self.assertEqual(clips[1]["reason"], "telegram_clip_request")
        self.assertIsNotNone(self.store.absolute_path(clips[0]))

    def test_backfill_legacy_flat_files(self) -> None:
        _touch(os.path.join(self.root, "motion_20260601_120000.avi"))
        _touch(os.path.join(self.root, "not_a_clip.txt"))
        self.assertEqual(self.store.backfill_legacy(), 1)
        self.assertEqual(self.store.backfill_legacy(), 0)  # idempotent
        start, end = local_day_bounds("2026-06-01")
        clips = self.store.query(start, end)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["camera_key"], UNKNOWN_CAMERA_KEY)
        self.assertEqual(clips[0]["start_ts"], start + 12 * 3600)

    def test_retention_deletes_only_old_clips(self) -> None:
        now = time.time()
        old_id = self._add_clip("cam1", now - 40 * 86400)
        new_id = self._add_clip("cam1", now - 3600)
        stray = _touch(os.path.join(self.root, "2026-01-01", "stray.avi"), mtime=now - 50 * 86400)
        keep_db = self.store.db_path

        result = self.store.apply_retention(30, 0)

        self.assertEqual(result["deleted"], 2)
        self.assertIsNone(self.store.get(old_id))
        self.assertIsNotNone(self.store.absolute_path(self.store.get(new_id)))
        self.assertFalse(os.path.exists(stray))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "2026-01-01")))
        self.assertTrue(os.path.exists(keep_db))

    def test_low_disk_deletes_oldest_but_keeps_last_day(self) -> None:
        now = time.time()
        ids = [self._add_clip("cam1", now - d * 86400) for d in (5, 4, 3)]
        recent = self._add_clip("cam1", now - 3600)

        def fake_free() -> float:
            # Pretend each deleted clip frees 2% of the disk.
            remaining = len(self.store.query(0, now + 1))
            return 5.0 + 2.0 * (4 - remaining)

        self.store.apply_retention(0, 9.0, free_percent_fn=fake_free)
        self.assertEqual(
            [self.store.get(i) is None for i in ids + [recent]],
            [True, True, True, False],
        )

    def test_absolute_path_rejects_escape(self) -> None:
        record = {"path": "../outside.avi"}
        _touch(os.path.join(os.path.dirname(self.root), "outside.avi"))
        try:
            self.assertIsNone(self.store.absolute_path(record))
        finally:
            os.remove(os.path.join(os.path.dirname(self.root), "outside.avi"))

    @unittest.skipUnless(find_ffmpeg(), "ffmpeg not available")
    def test_web_playable_transcodes_to_mp4(self) -> None:
        import cv2

        start = time.time()
        path = self.store.new_clip_path("cam1", start)
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"XVID"), 15, (160, 120))
        for i in range(15):
            writer.write(np.full((120, 160, 3), i * 10, dtype=np.uint8))
        writer.release()
        rid = self.store.add_recording(path, "cam1", "Cam 1", start, start + 1)

        mp4 = self.store.web_playable_path(rid)
        self.assertTrue(mp4 and mp4.endswith(".mp4") and os.path.getsize(mp4) > 0)
        with open(mp4, "rb") as handle:
            self.assertIn(b"ftyp", handle.read(16))
        self.assertIsNone(self.store.web_playable_path(rid + 999))


class TestDetectorIndexesRecordings(unittest.TestCase):
    def test_completed_clip_is_named_and_indexed(self) -> None:
        root = tempfile.mkdtemp(prefix="sentinel-rec-")
        try:
            store = RecordingStore(root)
            detector = MotionDetector()
            detector.set_recording_identity(store, "192.168.0.10", "Front door")
            detector.recording_target_frames = 3
            frame = np.zeros((120, 160, 3), dtype=np.uint8)
            path = detector.start_recording((160, 120), reason="motion")
            self.assertTrue(os.path.basename(path).startswith("192-168-0-10_"))
            for _ in range(3):
                detector.write_frame(frame)
            self.assertFalse(detector.recording)

            clips = store.query(0, time.time() + 1)
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0]["camera_name"], "Front door")
            self.assertEqual(store.absolute_path(clips[0]), path)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
