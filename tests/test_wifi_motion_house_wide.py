import _test_env  # noqa: F401  (keeps tests off the production log)

import tempfile
import unittest

from sentinel.engine.wifi_motion import WifiMotion


class TestPathsStartingTogether(unittest.TestCase):
    def test_counts_different_paths_near_a_moment(self):
        wm = WifiMotion.__new__(WifiMotion)  # (only what paths_starting uses)
        import threading
        from collections import deque
        wm._lock, wm._starts = threading.RLock(), deque(maxlen=400)
        for t, p in ((100.0, "a-b"), (100.2, "a-b"), (100.5, "a-c"), (101.9, "c-d"), (105.0, "d-e"), (100.3, "a-b")):
            wm._starts.append((t, p))
        self.assertEqual(wm.paths_starting(100.0), 3)  # a-b counted once; c-d is 1.9 s later: in
        self.assertEqual(wm.paths_starting(100.0, window=1.0), 2)
        self.assertEqual(wm.paths_starting(200.0), 0)


if __name__ == "__main__":
    unittest.main()
