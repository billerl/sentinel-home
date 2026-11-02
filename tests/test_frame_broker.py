"""Tests for sentinel.web.frame_broker.FrameBroker."""

import threading
import unittest

import numpy as np

from sentinel.web.frame_broker import FrameBroker


class TestFrameBroker(unittest.TestCase):
    def test_publish_get_unpublish(self) -> None:
        b = FrameBroker()
        f = np.zeros((10, 10, 3), dtype=np.uint8)
        f[0, 0, 0] = 42
        b.publish("rtsp://u:secret@10.0.0.5/s", "Front", f)
        wid = b.web_id("rtsp://u:secret@10.0.0.5/s")
        self.assertNotIn("secret", wid)
        got, ts = b.get_latest(wid)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(int(got[0, 0, 0]), 42)
        self.assertGreater(ts, 0.0)
        f[0, 0, 0] = 99
        got_stable, _ = b.get_latest(wid)
        assert got_stable is not None
        self.assertEqual(int(got_stable[0, 0, 0]), 42)
        cams = b.list_cameras()
        self.assertEqual(cams, [(wid, "Front")])
        self.assertEqual(b.list_camera_hosts(), [("10.0.0.5", "Front")])
        b.unpublish("rtsp://u:secret@10.0.0.5/s")
        got2, _ = b.get_latest(wid)
        self.assertIsNone(got2)

    def test_clear(self) -> None:
        b = FrameBroker()
        b.publish("a", "A", np.zeros((2, 2, 3), dtype=np.uint8))
        b.clear()
        self.assertEqual(b.list_cameras(), [])

    def test_concurrent_publish(self) -> None:
        b = FrameBroker()
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                for _ in range(50):
                    b.publish("x", "X", np.full((4, 4, 3), i, dtype=np.uint8))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        got, _ = b.get_latest(b.web_id("x"))
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(np.all(got[:, :, 0] == got[0, 0, 0]))



class TestSmallSnapshots(unittest.TestCase):
    def test_tile_sized_copies(self):
        import cv2
        from sentinel.web.frame_broker import FrameBroker

        b = FrameBroker()
        b.publish("rtsp://u:p@10.0.0.9/s", "Garage", np.zeros((720, 1280, 3), dtype=np.uint8))
        wid = b.web_id("rtsp://u:p@10.0.0.9/s")
        full, _ = b.get_latest_jpeg(wid)
        small, _ = b.get_latest_jpeg(wid, 400)
        self.assertEqual(cv2.imdecode(np.frombuffer(small, np.uint8), 1).shape[:2], (270, 480))  # rounded up to 480
        self.assertEqual(cv2.imdecode(np.frombuffer(full, np.uint8), 1).shape[:2], (720, 1280))
        self.assertIs(b.get_latest_jpeg(wid, 400)[0], small)  # cached until the next frame
        self.assertEqual(b.get_latest_jpeg(wid, 3000)[0], full)  # bigger than any copy: full size

if __name__ == "__main__":
    unittest.main()
