"""Tests for the main-stream HD recorder's clip assembly."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from sentinel.recordings.hd_recorder import HDRecorder, default_buffer_root, main_stream_url
from sentinel.recordings.store import find_ffmpeg


class TestBufferRoot(unittest.TestCase):
    def test_env_override_keeps_a_second_engine_off_the_live_buffers(self):
        old = os.environ.get("SENTINEL_BUFFER_ROOT")
        os.environ["SENTINEL_BUFFER_ROOT"] = "/tmp/elsewhere"
        try:
            self.assertEqual(default_buffer_root(), "/tmp/elsewhere")
            self.assertTrue(HDRecorder("rtsp://x/main/", "cam").buffer_dir.startswith("/tmp/elsewhere/"))
        finally:
            if old is None:
                del os.environ["SENTINEL_BUFFER_ROOT"]
            else:
                os.environ["SENTINEL_BUFFER_ROOT"] = old


class TestMainStreamUrl(unittest.TestCase):
    def test_known_patterns(self):
        self.assertEqual(
            main_stream_url("rtsp://u:p@h:554/h264/ch1/sub/av_stream"),
            "rtsp://u:p@h:554/h264/ch1/main/av_stream",
        )
        self.assertEqual(
            main_stream_url("rtsp://h/cam/realmonitor?channel=1&subtype=1"),
            "rtsp://h/cam/realmonitor?channel=1&subtype=0",
        )
        self.assertIsNone(main_stream_url("rtsp://h/live"))


@unittest.skipUnless(find_ffmpeg(), "ffmpeg not available")
class TestAssemble(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-hd-")
        self.rec = HDRecorder("rtsp://unused", "test", buffer_root=self.dir)
        os.makedirs(self.rec.buffer_dir)
        # Four 2-second segments starting at t0, t0+2, t0+4, t0+6.
        self.t0 = float(int(time.time()) - 60)
        for i in range(4):
            name = time.strftime("%Y%m%d%H%M%S", time.localtime(self.t0 + 2 * i)) + ".mkv"
            subprocess.run(
                [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15", "-t", "2",
                 "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
                 os.path.join(self.rec.buffer_dir, name)],
                check=True, timeout=60,
            )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _duration(self, path):
        out = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
        line = next(l for l in out.splitlines() if "Duration" in l)
        h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_joins_only_overlapping_segments(self):
        out = os.path.join(self.dir, "clip.mp4")
        # [t0+2.5, t0+4.5] overlaps the segments starting at t0+2 and t0+4.
        result = self.rec._assemble(self.t0 + 2.5, self.t0 + 4.5, out)
        self.assertEqual(result, out)
        self.assertAlmostEqual(self._duration(out), 4.0, delta=0.3)

    def test_no_overlap_returns_none(self):
        self.assertIsNone(self.rec._assemble(self.t0 + 100, self.t0 + 110, os.path.join(self.dir, "x.mp4")))


if __name__ == "__main__":
    unittest.main()
