"""A camera keeps running when something on its thread fails (bug hunt 2026-11-13: one error used to end the thread
for good while the camera still showed as online with a frozen picture)."""

import _test_env  # noqa: F401  (isolates the event log)

import sqlite3
import time
import unittest

import numpy as np

from sentinel.engine.camera import CameraPipeline


class _Store:  # stands in for RecordingStore
    root = "."

    def new_clip_path(self, *a):
        return "x.avi"

    def set_labels(self, *a):
        raise sqlite3.OperationalError("database or disk is full")

    def mark_checked(self, *a):
        pass

    def attach_hd(self, *a):
        pass


def _pipeline(status, on_motion=lambda p: None):
    p = CameraPipeline("rtsp://10.9.9.9/x", "rtsp://10.9.9.9/x", "Front Door", {"hd_recording": False},
                       recording_store=_Store(), frame_broker=None, settings=lambda: {},
                       on_motion=on_motion, on_clip=lambda *a: None, on_status=lambda p, up, err: status.append(up))
    frame = np.zeros((90, 160, 3), np.uint8)
    d = p.detector
    d.start_camera = lambda *a, **k: setattr(d, "feed_active", True) or True  # (no real camera)
    d.stop_camera = lambda *a, **k: setattr(d, "feed_active", False)
    d.detect_motion = lambda: (frame, frame, [], True, None)
    return p, d


class TestCameraKeepsRunning(unittest.TestCase):
    def test_a_failing_clip_label_write_doesnt_stop_the_camera(self):
        status = []
        p, d = _pipeline(status)
        d.last_completed_video_path, d.last_completed_span = "x.avi", (time.time() - 10, time.time(), 7)
        p._clip_labels = {"person"}
        p.start()
        try:
            time.sleep(0.6)
            self.assertTrue(p._thread.is_alive())
            self.assertLess(time.time() - p.last_frame_time, 0.5)  # still taking frames
        finally:
            p.stop()

    def test_a_failing_motion_handler_doesnt_stop_the_camera(self):
        calls = []

        def boom(_p):
            calls.append(1)
            raise RuntimeError("a routine broke")

        p, _d = _pipeline([], on_motion=boom)
        p.start()
        try:
            time.sleep(0.6)
            self.assertTrue(p._thread.is_alive())
            self.assertGreater(len(calls), 3)  # called again frame after frame
        finally:
            p.stop()

    def test_an_error_outside_a_frame_shows_the_camera_down_and_reconnects(self):
        status, connects = [], []
        p, d = _pipeline(status)
        real_start = d.start_camera

        def flaky_start(*a, **k):
            connects.append(1)
            if len(connects) == 1:
                raise RuntimeError("driver blew up")
            return real_start(*a, **k)

        d.start_camera = flaky_start
        p._stop.wait = lambda t=None: time.sleep(min(t or 0, 0.05))  # (don't wait the real 5 s)
        p.start()
        try:
            time.sleep(0.6)
            self.assertTrue(p._thread.is_alive())
            self.assertGreaterEqual(len(connects), 2)  # it tried again
            self.assertEqual(status[:2], [False, True])  # shown down, then back
        finally:
            p._stop.set()
            p.stop()


if __name__ == "__main__":
    unittest.main()
