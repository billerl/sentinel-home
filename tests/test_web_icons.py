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

    def test_live_page_script_and_style_load(self):
        # served with nosniff, so a wrong type would stop the browser running them
        for path, kind in (("/static/grid.js", "text/javascript"), ("/static/grid.css", "text/css")):
            resp = self.client.get(path + "?v=1")
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(resp.content_type.startswith(kind), (path, resp.content_type))
            self.assertGreater(len(resp.data), 1000)
            resp.close()

    def test_pages_link_the_icon(self):
        login = self.client.get("/login").get_data(as_text=True)
        self.assertIn('rel="apple-touch-icon"', login)
        templates = os.path.join(os.path.dirname(__file__), "..", "sentinel", "web", "templates")
        for name in ("home.html", "grid.html", "zones.html", "settings.html", "login.html"):
            with open(os.path.join(templates, name)) as handle:
                self.assertIn('href="/favicon.ico"', handle.read(), name)

    def test_tvs_go_straight_to_the_cameras(self):
        from sentinel.web.server import TV_BROWSER

        tvs = ["Mozilla/5.0 (Linux; Android 7.1.2; AFTEAMR311 Build/NS6711; wv) AppleWebKit/537.36 Chrome/118.0.0.0",
               "Mozilla/5.0 (SMART-TV; Linux; Tizen 4.0) AppleWebKit/537.36 SamsungBrowser/2.1 TV Safari/537.36"]
        others = ["Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/155.0.0.0 Mobile Safari/537.36",
                  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36",
                  "Mozilla/5.0 (X11; Linux x86_64; Quest 3) AppleWebKit/537.36 OculusBrowser/154.0 VR Safari/537.36",
                  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"]
        self.assertTrue(all(TV_BROWSER.search(ua) for ua in tvs))
        self.assertFalse(any(TV_BROWSER.search(ua) for ua in others))


if __name__ == "__main__":
    unittest.main()
