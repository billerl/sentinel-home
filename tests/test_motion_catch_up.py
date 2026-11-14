"""A camera sending frames faster than they're processed must not fall further and further behind (bug hunt
2026-11-13): when the stream's clock says the picture is over a second behind, queued frames are skipped."""

import _test_env  # noqa: F401  (isolates the event log)

import unittest
from unittest import mock

import cv2

from sentinel.detection import motion_detector as md


class _Cap:
    """A stream at ``fps`` whose frames queue up (as OpenCV's FFmpeg reader keeps them); ``clock`` is real time."""

    def __init__(self, clock, fps=20.0, report=True):
        self.clock, self.fps, self.report = clock, fps, report
        self.frame = -1
        self.grabs = 0

    def _available(self):
        return int(self.clock[0] * self.fps)  # frames the camera has sent so far

    def grab(self):
        if self.frame + 1 > self._available():
            self.clock[0] = (self.frame + 1) / self.fps  # (waits for the camera)
        self.frame += 1
        self.grabs += 1
        return True

    def get(self, prop):
        assert prop == cv2.CAP_PROP_POS_MSEC
        return self.frame * 1000.0 / self.fps if self.report else 0.0


class TestCatchUp(unittest.TestCase):
    def run_loop(self, cap, clock, loop_fps, seconds):
        d = md.MotionDetector.__new__(md.MotionDetector)
        d._stream_ref, d._stream_pos, d._stream_moves = None, -1.0, False
        d.camera_name = "Test"
        self.loops = 0
        with mock.patch.object(md.time, "monotonic", lambda: clock[0]):
            while clock[0] < seconds:
                self.loops += 1
                d._catch_up(cap)
                cap.grab()  # (the read)
                d._note_stream_position(cap)
                clock[0] += 1.0 / loop_fps  # processing + the loop's wait
        return clock[0] - cap.frame / cap.fps  # how far behind the picture is

    def test_a_fast_camera_is_caught_up(self):
        clock = [0.0]
        behind = self.run_loop(_Cap(clock, fps=20.0), clock, loop_fps=10.0, seconds=30.0)
        self.assertLess(behind, md.CATCH_UP_LAG + 0.2)  # (it was 15 s behind after 30 s)

    def test_nothing_is_skipped_while_keeping_up(self):
        clock = [0.0]
        cap = _Cap(clock, fps=12.0)
        self.run_loop(cap, clock, loop_fps=15.0, seconds=20.0)
        reads = cap.grabs
        self.assertLessEqual(reads, int(20.0 * 12.0) + 2)  # every frame read once: none skipped by catch-up

    def test_a_camera_without_a_clock_is_left_as_it_was(self):
        clock = [0.0]
        cap = _Cap(clock, fps=20.0, report=False)
        self.run_loop(cap, clock, loop_fps=10.0, seconds=10.0)
        self.assertEqual(cap.grabs, self.loops)  # one read per loop, as before


if __name__ == "__main__":
    unittest.main()
