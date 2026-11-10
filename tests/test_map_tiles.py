import _test_env  # noqa: F401  (keeps tests off the production log)

import os
import shutil
import tempfile
import time
import unittest

from sentinel.web.map_tiles import KEEP_SECONDS, MapTiles


class TestMapTiles(unittest.TestCase):
    """The location map's pictures, fetched by Sentinel (the TV's browser was turned away) and kept a week."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-tiles-")
        self.tiles, self.asked = MapTiles(self.dir), []
        self.tiles._fetch = lambda url: self.asked.append(url) or b"PNGDATA"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fetched_once_then_from_disk(self):
        self.assertEqual(self.tiles.get("street", 12, 1000, 1500), (b"PNGDATA", "image/png"))
        self.assertEqual(self.tiles.get("street", 12, 1000, 1500), (b"PNGDATA", "image/png"))
        self.assertEqual(self.asked, ["https://tile.openstreetmap.org/12/1000/1500.png"])
        self.assertEqual(self.tiles.get("aerial", 12, 1000, 1500)[1], "image/jpeg")
        self.assertEqual(self.asked[-1], "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
                                         "MapServer/tile/12/1500/1000")  # (y before x there)

    def test_a_week_old_picture_is_fetched_again_but_used_when_offline(self):
        self.tiles.get("street", 3, 1, 2)
        path = os.path.join(self.dir, "street", "3", "1", "2.tile")
        old = time.time() - KEEP_SECONDS - 60
        os.utime(path, (old, old))
        self.tiles.get("street", 3, 1, 2)
        self.assertEqual(len(self.asked), 2)
        os.utime(path, (old, old))

        def offline(url):
            raise OSError("no internet")
        self.tiles._fetch = offline
        self.assertEqual(self.tiles.get("street", 3, 1, 2), (b"PNGDATA", "image/png"))
        self.assertIsNone(self.tiles.get("street", 3, 0, 0))  # never fetched: nothing to show

    def test_only_real_map_squares(self):
        for args in (("other", 3, 1, 1), ("street", 20, 1, 1), ("street", 3, 8, 1), ("street", 3, 1, -1)):
            self.assertIsNone(self.tiles.get(*args), args)
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
