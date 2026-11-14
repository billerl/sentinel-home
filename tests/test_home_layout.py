"""Home page tile order, one per login (sentinel/engine/home_layout.py)."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest

from sentinel.engine.home_layout import HomeLayouts


class TestHomeLayouts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-layout-")
        self.store = HomeLayouts(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_each_login_keeps_its_own_order(self):
        self.assertEqual(self.store.get(None), [])
        self.assertTrue(self.store.save(3, ["planes", "cameras"])[0])
        self.assertTrue(self.store.save(None, ["help", "cameras"])[0])
        self.assertEqual(self.store.get(3), ["planes", "cameras"])
        self.assertEqual(self.store.get(None), ["help", "cameras"])
        self.assertEqual(self.store.get(4), [])
        self.assertEqual(HomeLayouts(self.dir).get(3), ["planes", "cameras"])  # (kept across a restart)

    def test_junk_is_dropped_never_saved_as_none(self):
        ok, _ = self.store.save(1, ["cameras", None, 5, "cameras", "Bad Key", "<x>", "notes", "a" * 40])
        self.assertTrue(ok)
        self.assertEqual(self.store.get(1), ["cameras", "notes"])
        self.assertFalse(self.store.save(1, "cameras")[0])
        self.assertEqual(self.store.get(1), ["cameras", "notes"])

    def test_empty_list_resets(self):
        self.store.save(1, ["notes"])
        self.store.save(1, [])
        self.assertEqual(self.store.get(1), [])
        with open(os.path.join(self.dir, "home_layouts.json")) as fh:
            self.assertEqual(json.load(fh), {})

    def test_a_broken_file_reads_as_no_layout(self):
        with open(os.path.join(self.dir, "home_layouts.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.store.get(1), [])
        self.assertTrue(self.store.save(1, ["notes"])[0])
        self.assertEqual(self.store.get(1), ["notes"])


if __name__ == "__main__":
    unittest.main()
