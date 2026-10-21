"""The site icon, touch icon and manifest load without logging in, and every page links them."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import socket
import tempfile
import unittest

from sentinel.settings.manager import SettingsManager
from sentinel.web.frame_broker import FrameBroker
from sentinel.web.server import WebServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sentinel-icons-")
        sm = SettingsManager(settings_file=os.path.join(cls.dir, "settings.json"))
        sm.settings["web"].update({"enabled": True, "port": _free_port()})
        sm.settings["web"]["auth"]["password_hash"] = "not-a-real-hash"
        cls.server = WebServer(sm, FrameBroker())
        assert cls.server.start()
        cls.client = cls.server._app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_icons_are_public(self):
        for path, kind in (("/favicon.ico", "image/vnd.microsoft.icon"), ("/static/icon.svg", "image/svg+xml"),
                           ("/static/apple-touch-icon.png", "image/png"),
                           ("/static/manifest.webmanifest", "application/manifest+json")):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(resp.content_type.startswith(kind), (path, resp.content_type))
            self.assertGreater(len(resp.data), 100)
            resp.close()

    def test_pages_link_the_icon(self):
        login = self.client.get("/login").get_data(as_text=True)
        self.assertIn('rel="apple-touch-icon"', login)
        templates = os.path.join(os.path.dirname(__file__), "..", "sentinel", "web", "templates")
        for name in ("grid.html", "zones.html", "settings.html", "login.html"):
            with open(os.path.join(templates, name)) as handle:
                self.assertIn('href="/favicon.ico"', handle.read(), name)


if __name__ == "__main__":
    unittest.main()
