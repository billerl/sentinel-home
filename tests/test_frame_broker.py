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
        b.publish("cam1", "Front", f)
        got, ts = b.get_latest("cam1")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(int(got[0, 0, 0]), 42)
        self.assertGreater(ts, 0.0)
        f[0, 0, 0] = 99
        got_stable, _ = b.get_latest("cam1")
        assert got_stable is not None
        self.assertEqual(int(got_stable[0, 0, 0]), 42)
        cams = b.list_cameras()
        self.assertEqual(cams, [("cam1", "Front")])
        b.unpublish("cam1")
        got2, _ = b.get_latest("cam1")
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
        got, _ = b.get_latest("x")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(np.all(got[:, :, 0] == got[0, 0, 0]))


if __name__ == "__main__":
    unittest.main()
