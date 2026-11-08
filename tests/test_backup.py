"""Nightly backup: finding the USB drive, what goes on it (and what doesn't), the video days that fit, the copy to
another computer, and when it runs."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from sentinel.engine import backup as B
from sentinel.engine.admin import SETTINGS_SCHEMA


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class _Disks:
    """Stands in for lsblk/udisksctl/ssh/rsync: one USB stick (a temp folder) and the PC's own disk."""

    def __init__(self, mount, mounted=True):
        self.mount, self.mounted, self.calls = mount, mounted, []
        self.remote_listing = ""

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        if cmd[0] == "lsblk":
            tree = {"blockdevices": [
                {"path": "/dev/nvme0n1", "tran": "nvme", "type": "disk", "children": [
                    {"path": "/dev/nvme0n1p2", "tran": None, "type": "part", "fstype": "ext4", "mountpoint": "/"}]},
                {"path": "/dev/sda", "tran": "usb", "type": "disk", "size": 0},  # an empty card-reader slot
                {"path": "/dev/sdb", "tran": "usb", "type": "disk", "size": 128_000_000_000, "children": [
                    {"path": "/dev/sdb1", "tran": None, "type": "part", "fstype": "exfat", "label": "STICK",
                     "size": 128_000_000_000, "mountpoint": self.mount if self.mounted else None}]}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(tree), stderr="")
        if cmd[0] == "udisksctl":
            return SimpleNamespace(returncode=1, stdout="", stderr="Not authorized to perform operation")
        if cmd[0] == "ssh":
            return SimpleNamespace(returncode=0, stdout=self.remote_listing if "ls" in cmd[-1] else "", stderr="")
        if cmd[0] == "rsync":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(cmd)


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.data = os.path.join(self.tmp, "recorded_videos")
        self.app = os.path.join(self.tmp, "app")
        self.stick = os.path.join(self.tmp, "stick")
        os.makedirs(self.stick)
        self.now = time.mktime((2026, 11, 9, 3, 30, 0, 0, 0, -1))
        os.makedirs(self.data)
        with sqlite3.connect(os.path.join(self.data, "x.db")) as db:
            db.execute("CREATE TABLE t (v TEXT)")
            db.execute("INSERT INTO t VALUES ('kept')")
        _write(os.path.join(self.data, "x.db-wal"), b"wal")
        _write(os.path.join(self.data, "household.db.bak-20261025"))
        _write(os.path.join(self.data, "switches.json"), b"{}")
        _write(os.path.join(self.data, ".household", "1000.jpg"), b"photo")
        _write(os.path.join(self.data, ".web_cache", "big.mp4"))
        _write(os.path.join(self.data, "wifi_motion", "2026-11-08.csv"), b"csv")
        for day in ("2026-11-01", "2026-11-05", "2026-11-06", "2026-11-08", "2026-11-09"):
            _write(os.path.join(self.data, day, "cam_000416.avi"), b"video " + day.encode())
        _write(os.path.join(self.app, "settings.json"), b'{"secret": 1}')
        _write(os.path.join(self.app, "models", "wakeword", "custom.onnx"), b"model")
        _write(os.path.join(self.app, "firmware", "board", "board.ino"), b"code")
        _write(os.path.join(self.app, "firmware", "board", "build-xiao", "board.bin"), b"build")
        self.settings = {"backup_video_days": 3}
        self.warnings = []
        self.disks = _Disks(self.stick)
        self.backup = B.Backup(self.settings, self.data, self.app, self.warnings.append, run=self.disks,
                               clock=lambda: self.now)

    def _use_stick(self):
        ok, message = self.backup.use_drive("/dev/sdb1")
        self.assertTrue(ok, message)
        return os.path.join(self.stick, B.FOLDER)

    def test_finds_only_usb_filesystems(self):
        drives = self.backup.drives()
        self.assertEqual([d["path"] for d in drives], ["/dev/sdb1"])
        self.assertEqual(drives[0]["label"], "STICK")
        self.assertFalse(drives[0]["ours"])
        self._use_stick()
        self.assertTrue(self.backup.drives()[0]["ours"])

    def test_a_drive_it_isnt_allowed_to_open_is_reported_not_used(self):
        self.disks.mounted = False
        drives = self.backup.drives()
        self.assertFalse(drives[0]["mounted"])
        self.assertIn(["udisksctl", "mount", "--no-user-interaction", "-b", "/dev/sdb1"], self.disks.calls)
        ok, message = self.backup.use_drive("/dev/sdb1")
        self.assertFalse(ok)
        self.assertIn("isn't allowed", message)

    def test_a_drive_that_isnt_ours_is_left_alone(self):
        result = self.backup.run()
        self.assertEqual(os.listdir(self.stick), [])
        self.assertEqual(result["problems"], [])
        self.assertEqual(self.warnings, [])  # no drive was ever set up: nothing to nag about

    def test_backup_holds_the_important_part_and_recent_video(self):
        root = self._use_stick()
        result = self.backup.run()
        self.assertEqual(result["problems"], [])
        snap = os.path.join(root, "snapshots", "2026-11-09")
        with sqlite3.connect(os.path.join(snap, "x.db")) as db:
            self.assertEqual(db.execute("SELECT v FROM t").fetchall(), [("kept",)])
        for kept in ("switches.json", "settings.json", ".household/1000.jpg"):
            self.assertTrue(os.path.exists(os.path.join(snap, kept)), kept)
        for left_out in ("x.db-wal", "household.db.bak-20261025", ".web_cache", "wifi_motion", "2026-11-09"):
            self.assertFalse(os.path.exists(os.path.join(snap, left_out)), left_out)
        mirror = os.path.join(root, "mirror")
        self.assertTrue(os.path.exists(os.path.join(mirror, "recorded_videos", "wifi_motion", "2026-11-08.csv")))
        self.assertTrue(os.path.exists(os.path.join(mirror, "models", "wakeword", "custom.onnx")))
        self.assertTrue(os.path.exists(os.path.join(mirror, "firmware", "board", "board.ino")))
        self.assertFalse(os.path.exists(os.path.join(mirror, "firmware", "board", "build-xiao")))
        self.assertEqual(sorted(os.listdir(os.path.join(root, "videos"))), ["2026-11-06", "2026-11-08", "2026-11-09"])
        self.assertEqual(result["video_days"], 3)
        self.assertTrue(self.backup.state()["last_ok"])

    def test_the_owners_own_files_are_never_touched(self):
        _write(os.path.join(self.stick, "Photos", "2019", "wedding.jpg"), b"precious")
        _write(os.path.join(self.stick, "2026-11-01", "not ours.txt"), b"mine")  # even if it looks like a day folder
        root = self._use_stick()
        self.backup.run()
        self.now += 86400 * 30
        self.backup.run()
        B._remove(os.path.join(self.stick, "Photos"))  # the last guard, if a bug ever pointed it there
        with open(os.path.join(self.stick, "Photos", "2019", "wedding.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"precious")
        self.assertTrue(os.path.exists(os.path.join(self.stick, "2026-11-01", "not ours.txt")))
        self.assertEqual(sorted(os.listdir(self.stick)), ["2026-11-01", "Photos", B.FOLDER])
        self.assertTrue(os.path.isdir(os.path.join(root, "snapshots")))

    def test_old_nights_and_days_roll_off(self):
        root = self._use_stick()
        self.backup.run()
        for n in range(1, 10):
            os.makedirs(os.path.join(root, "snapshots", "2026-10-%02d" % n))
        _write(os.path.join(root, "videos", "2026-11-01", "cam_000416.avi"))
        os.remove(os.path.join(self.data, ".household", "1000.jpg"))
        self.now += 86400
        self.backup.run()
        snaps = sorted(os.listdir(os.path.join(root, "snapshots")))
        self.assertEqual(len(snaps), B.SNAPSHOTS_KEPT)
        self.assertEqual(snaps[-2:], ["2026-11-09", "2026-11-10"])
        self.assertNotIn("2026-11-01", os.listdir(os.path.join(root, "videos")))
        self.assertNotIn("2026-11-06", os.listdir(os.path.join(root, "videos")))  # now 4 days back
        self.assertTrue(os.path.exists(os.path.join(root, "snapshots", "2026-11-09", ".household", "1000.jpg")))
        self.assertFalse(os.path.exists(os.path.join(root, "snapshots", "2026-11-10", ".household", "1000.jpg")))

    def test_a_full_drive_keeps_the_newest_video(self):
        root = self._use_stick()
        _write(os.path.join(root, "videos", "2026-11-06", "cam_000416.avi"), b"from last night")
        real = shutil.disk_usage
        free = {"left": 2}  # room for two more clips

        def usage(path):
            u = real(path)
            return u._replace(free=B.DRIVE_RESERVE + free["left"] * 64) if path == root else u

        def copy(src, dst, **kw):
            free["left"] -= 1
            return shutil.copyfile(src, dst)

        with mock.patch.object(B.shutil, "disk_usage", usage), mock.patch.object(B.shutil, "copy2", copy):
            result = self.backup.run()
        self.assertTrue(result["video_full"])
        days = sorted(os.listdir(os.path.join(root, "videos")))
        self.assertNotIn("2026-11-06", days)  # an older day gives way
        self.assertIn("2026-11-09", days)

    def test_unplugged_drive_is_reported(self):
        self._use_stick()
        self.backup.run()
        shutil.rmtree(os.path.join(self.stick, B.FOLDER))  # as good as a different stick
        self.now += 86400
        result = self.backup.run()
        self.assertEqual(result["problems"], ["the backup drive isn't plugged in"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("isn't plugged in", self.warnings[0])

    def test_copy_to_another_computer(self):
        self.settings["backup_remote"] = "pi@backup-pi"
        self.disks.remote_listing = "\n".join("2026-11-%02d" % n for n in range(1, 9)) + "\nnotes.txt\n"
        result = self.backup.run()
        self.assertEqual(result["problems"], [])
        rsync = next(c for c in self.disks.calls if c[0] == "rsync")
        self.assertIn("--link-dest=../2026-11-08", rsync)
        self.assertEqual(rsync[-1], "pi@backup-pi:sentinel-backup/2026-11-09/")
        rm = [c for c in self.disks.calls if c[0] == "ssh" and "rm -rf" in c[-1]]
        self.assertEqual(rm[0][-1], "cd sentinel-backup && rm -rf 2026-11-01 2026-11-02")
        self.assertTrue(self.backup.state()["remote_ok"])

    def test_remote_host_cant_smuggle_ssh_options(self):
        check = SETTINGS_SCHEMA["backup_remote"]
        self.assertEqual(check(" backup-pi "), "backup-pi")
        self.assertEqual(check(""), "")
        for bad in ("-oProxyCommand=touch /tmp/x", "pi@host; rm -rf ~", "host name"):
            with self.assertRaises(ValueError):
                check(bad)

    def test_runs_once_a_night_at_the_set_time(self):
        self.settings["backup_time"] = "03:30"
        at = lambda h, m, d=9: time.mktime((2026, 11, d, h, m, 0, 0, 0, -1))  # noqa: E731
        self.assertFalse(self.backup.due(at(3, 29)))
        self.assertTrue(self.backup.due(at(3, 30)))
        self.assertTrue(self.backup.due(at(6, 0)))  # Sentinel was restarting at 3:30
        self.assertFalse(self.backup.due(at(7, 0)))
        self.backup.run()  # made at 3:30 on the 9th
        self.assertFalse(self.backup.due(at(4, 0)))
        self.assertTrue(self.backup.due(at(3, 31, 10)))
        self.settings["backup_enabled"] = False
        self.assertFalse(self.backup.due(at(3, 31, 10)))


if __name__ == "__main__":
    unittest.main()
