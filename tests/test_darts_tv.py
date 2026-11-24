"""The darts scorer's way to a TV: a local-only route with a key of its own (sentinel/web/routes/live.py)."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

import bcrypt
import numpy as np

from sentinel.settings.manager import SettingsManager
from sentinel.web.frame_broker import FrameBroker
from sentinel.web.routes import live
from sentinel.web.server import WebServer


class TestDartsTv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-darts-tv-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patch = mock.patch.object(live, "DARTS_KEY_FILE", os.path.join(self.dir, "key"))
        patch.start()
        self.addCleanup(patch.stop)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        manager.settings["web"].update({"enabled": True, "port": port})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        manager.settings["darts_url"] = "https://games.example.com/darts"
        self.manager = manager
        broker = FrameBroker()
        broker.publish("rtsp://u:secret@10.0.0.5/s", "Front", np.zeros((2, 2, 3), dtype=np.uint8))
        self.engine = mock.Mock()
        self.engine.switches.camera_tvs.return_value = {"samsung-1": "Den TV"}
        self.engine.switches.tv_show_page.return_value = (True, "Okay, the darts scoreboard is on the Den TV.")
        self.engine.home_layouts.get.return_value = []
        self.server = WebServer(manager, broker, engine=self.engine)
        self.assertTrue(self.server.start())
        self.client = self.server._app.test_client()

    def key(self):
        self.server._auth.validate_session = lambda token, ip: mock.Mock(username="alex", person_id=None, role="owner")
        page = self.client.get("/").get_data(as_text=True)
        self.server._auth.validate_session = lambda token, ip: None
        self.assertIn("https://games.example.com/darts?k=", page)
        return page.split("https://games.example.com/darts?k=")[1].split('"')[0]

    def test_with_the_key_from_this_computer_only(self):
        key = self.key()
        self.assertGreaterEqual(len(key), 16)
        with open(live.DARTS_KEY_FILE) as f:
            self.assertEqual(f.read(), key)
        self.assertEqual(os.stat(live.DARTS_KEY_FILE).st_mode & 0o777, 0o600)
        ok = {"X-Darts-Key": key}
        self.assertEqual(self.client.get("/api/local/darts_tv", headers=ok).get_json(),
                         {"tvs": [{"id": "samsung-1", "name": "Den TV"}]})
        self.assertTrue(self.client.post("/api/local/darts_tv", headers=ok, json={"tv": "samsung-1"}).get_json()["ok"])
        self.engine.switches.tv_show_page.assert_called_once_with("samsung-1", "the darts scoreboard", "#map=darts", "darts")
        self.assertEqual(self.client.get("/api/local/darts_tv").status_code, 403)  # no key
        self.assertEqual(self.client.get("/api/local/darts_tv", headers={"X-Darts-Key": "x" * 24}).status_code, 403)
        # through the tunnel (Cloudflare's headers), even with the key: no
        self.assertEqual(self.client.get("/api/local/darts_tv", headers=dict(ok, **{"Cf-Ray": "1"})).status_code, 403)
        # from another computer on the network: no
        self.assertEqual(self.client.get("/api/local/darts_tv", headers=ok,
                                         environ_base={"REMOTE_ADDR": "192.168.1.50"}).status_code, 403)
        self.manager.settings["darts_url"] = ""  # no darts scorer set up: nothing to show
        self.assertEqual(self.client.get("/api/local/darts_tv", headers=ok).status_code, 403)


if __name__ == "__main__":
    unittest.main()
