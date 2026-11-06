"""The TV remote's screen mirror: records only while someone watches, turns H.264 into pictures, stops after."""

import _test_env  # noqa: F401  (isolates the event log)

import subprocess
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np

from sentinel.engine import tv_mirror as M


def _h264(frames: int = 6) -> bytes:
    """A short H.264 stream like the Fire TV's screenrecord sends."""
    from sentinel.recordings.store import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise unittest.SkipTest("no ffmpeg")
    raw = b"".join(np.full((90, 160, 3), 40 * i, np.uint8).tobytes() for i in range(frames))
    return subprocess.run([ffmpeg, "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "160x90", "-r", "5",
                           "-i", "-", "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-f", "h264", "-"],
                          input=raw, capture_output=True, check=True).stdout


class FakeFireTV:
    def __init__(self, stream: bytes):
        self.stream = stream
        self.calls = []
        self.stopped = threading.Event()

    def mirror_start(self, host):
        self.calls.append("start")

    def mirror_read(self, host, offset):
        return self.stream[offset:offset + 500]  # dribbles in, a piece per poll

    def mirror_stop(self, host):
        self.calls.append("stop")
        self.stopped.set()


class TestTVMirror(unittest.TestCase):
    def test_split_jpegs(self):
        a, b = b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"
        self.assertEqual(M.split_jpegs(a + b + b"\xff\xd8thr"), ([a, b], b"\xff\xd8thr"))
        self.assertEqual(M.split_jpegs(b"junk"), ([], b""))

    def test_pictures_while_watched_then_it_stops(self):
        from sentinel.recordings.store import find_ffmpeg

        tv = FakeFireTV(_h264())
        now = [0.0]
        old = M.POLL_SECONDS
        M.POLL_SECONDS = 0.01
        self.addCleanup(setattr, M, "POLL_SECONDS", old)
        mirror = M.TVMirror(tv, "10.0.0.5", find_ffmpeg(), clock=lambda: now[0])
        jpeg, count = mirror.latest()
        self.assertIsNone(jpeg)  # nothing yet: the TV starts recording
        deadline = time.time() + 10
        picture = None
        while time.time() < deadline and (picture is None or picture.mean() < 190):
            time.sleep(0.05)
            jpeg = mirror.latest()[0]
            picture = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
        # the last frame (brightness 200), although nothing follows it: the screen then stays still
        self.assertAlmostEqual(picture.mean(), 200, delta=8)
        self.assertEqual(picture.shape[:2], (90, 160))
        now[0] += M.IDLE_SECONDS + 1  # the remote was closed
        self.assertTrue(tv.stopped.wait(5))
        self.assertEqual(tv.calls[0], "start")
        self.assertEqual(tv.calls[-1], "stop")
        mirror._thread.join(5)
        self.assertIsNone(mirror._jpeg)  # no stale picture next time


if __name__ == "__main__":
    unittest.main()
