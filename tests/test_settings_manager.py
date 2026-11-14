"""settings.json safety: a file that can't be read is never overwritten with the defaults Sentinel fell back to."""

import _test_env  # noqa: F401  (isolates the event log)

import glob
import json
import os
import shutil
import tempfile
import unittest

from sentinel.settings.manager import SettingsManager


class TestSettingsFileSafety(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-settings-")
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_broken_file_is_kept_and_never_saved_over(self):
        broken = '{"telegram_bot_token": "123:abc", "camera_profiles": {"cam": {"source": "rtsp://x"}},}'  # stray comma
        with open(self.path, "w") as f:
            f.write(broken)
        sm = SettingsManager(self.path)
        self.assertTrue(sm.load_failed)
        self.assertEqual(sm.settings.get("camera_profiles"), {})  # running on defaults for now
        self.assertFalse(sm.save_settings(force=True))  # (what every restart does)
        with open(self.path) as f:
            self.assertEqual(f.read(), broken)  # untouched
        copies = glob.glob(self.path + ".broken-*")
        self.assertEqual(len(copies), 1)
        with open(copies[0]) as f:
            self.assertEqual(f.read(), broken)
        self.assertEqual(os.stat(copies[0]).st_mode & 0o777, 0o600)

    def test_not_an_object_counts_as_broken(self):
        with open(self.path, "w") as f:
            f.write("[1, 2]")
        sm = SettingsManager(self.path)
        self.assertTrue(sm.load_failed)
        self.assertFalse(sm.save_settings(force=True))

    def test_missing_file_starts_fresh_and_saves(self):
        sm = SettingsManager(self.path)
        self.assertFalse(sm.load_failed)
        self.assertTrue(sm.save_settings(force=True))
        with open(self.path) as f:
            self.assertIn("web", json.load(f))

    def test_good_file_loads_and_saves(self):
        with open(self.path, "w") as f:
            json.dump({"telegram_bot_token": "123:abc"}, f)
        sm = SettingsManager(self.path)
        self.assertFalse(sm.load_failed)
        self.assertEqual(sm.settings["telegram_bot_token"], "123:abc")
        self.assertTrue(sm.save_settings(force=True))


if __name__ == "__main__":
    unittest.main()
