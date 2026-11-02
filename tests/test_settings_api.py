"""Tests for the settings API, camera management, and the Telegram access command."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sentinel.engine import Engine, admin
from sentinel.notifications.telegram_notifier import TelegramNotifier
from sentinel.settings.manager import SettingsManager

TOKEN = "123456789:" + "A" * 35


class TestAccessCommand(unittest.TestCase):
    def test_phrases(self):
        for text in ["Access", "remote", "How do I connect?", "WEB", "link", "remote desktop"]:
            self.assertTrue(TelegramNotifier._is_access_request(text), text)
        for text in ["clip", "status", "accessible camera", "disable recording"]:
            self.assertFalse(TelegramNotifier._is_access_request(text), text)

    def test_message_mentions_url_tailscale_and_rdp(self):
        fake_ts = {"installed": True, "running": True, "ip": "100.1.2.3", "name": "sentinel.tail.ts.net"}
        with patch.object(admin, "tailscale_status", return_value=fake_ts):
            msg = admin.access_message({"remote_web_url": "https://cams.example.com", "remote_rdp_user": "bob"})
        self.assertIn("https://cams.example.com", msg)
        self.assertIn("100.1.2.3", msg)
        self.assertIn("Log in as bob", msg)
        self.assertIn("connected", msg)


class TestSettingsApi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-settings-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": TOKEN, "telegram_chat_id": "111", "volume": 0})
        self.engine = Engine(sm)
        self.engine._build_cameras()

    def tearDown(self):
        for pipeline in list(self.engine.cameras.values()):
            pipeline.stop(timeout=2)
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_view_never_contains_secrets(self):
        view = self.engine.settings_view()
        self.assertNotIn(TOKEN, repr(view))
        self.assertTrue(view["secrets"]["telegram_bot_token"])
        self.assertEqual(view["values"]["telegram_chat_ids"], "111")  # legacy field surfaced

    def test_update_validates(self):
        self.assertFalse(self.engine.update_settings({"telegram_chat_ids": "abc"})[0])
        self.assertFalse(self.engine.update_settings({"telegram_bot_token": "nope"})[0])
        self.assertFalse(self.engine.update_settings({"telegram_enabled": "yes"})[0])
        self.assertFalse(self.engine.update_settings({"camera_profiles": {}})[0])
        self.assertFalse(self.engine.update_settings({"remote_web_url": "javascript:alert(1)"})[0])
        ok, _ = self.engine.update_settings({"recording_retention_days": 999, "web.max_fps": 5})
        self.assertTrue(ok)
        self.assertEqual(self.engine.settings["recording_retention_days"], 365)
        self.assertEqual(self.engine.settings["web"]["max_fps"], 5)

    def test_chat_ids_update_reaches_notifier(self):
        ok, _ = self.engine.update_settings({"telegram_chat_ids": "222, 333 222", "telegram_enabled": False})
        self.assertTrue(ok)
        self.assertEqual(self.engine.settings["telegram_chat_ids"], "222, 333")
        self.assertEqual(self.engine.settings["telegram_chat_id"], "")
        self.assertEqual(self.engine.telegram.chat_ids, ["222", "333"])

    def test_add_rename_disable_remove_camera(self):
        ok, key = self.engine.add_camera(
            {"name": "Shed", "host": "127.0.0.1", "port": 9, "path": "/live", "username": "u", "password": "p@ss"}
        )
        self.assertTrue(ok, key)
        profile = next(iter(self.engine.settings["camera_profiles"].values()))
        self.assertEqual(profile["source"], "rtsp://u:p%40ss@127.0.0.1:9/live")
        configs = self.engine.camera_configs()
        self.assertNotIn("p%40ss", repr(configs))
        self.assertEqual(configs[0]["username"], "u")

        self.assertFalse(self.engine.add_camera({"host": "127.0.0.1"})[0])  # duplicate
        self.assertFalse(self.engine.add_camera({"host": "bad host!"})[0])

        self.assertTrue(self.engine.update_camera_config(key, {"display": "  Garden   shed ", "enabled": False})[0])
        self.assertEqual(self.engine.cameras[key].display_name, "Garden shed")
        self.assertIn("Garden shed: disabled", self.engine.status_message("1"))

        self.assertTrue(self.engine.remove_camera(key)[0])
        self.assertEqual(self.engine.settings["camera_profiles"], {})
        self.assertFalse(self.engine.remove_camera(key)[0])

    def test_status_shape(self):
        status = self.engine.system_status()
        for field in ("uptime_seconds", "disk", "recordings", "cameras", "telegram", "remote", "events"):
            self.assertIn(field, status)


if __name__ == "__main__":
    unittest.main()
