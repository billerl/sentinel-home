"""Signing in a TV by scanning a QR code with a phone that is already signed in."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import re
import shutil
import socket
import tempfile
import unittest

import bcrypt

from sentinel.settings.manager import SettingsManager
from sentinel.web.frame_broker import FrameBroker
from sentinel.web.server import WebServer

JSON = {"Content-Type": "application/json", "X-Sentinel-Request": "1"}
_NEXT_IP = iter(range(10, 250))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestQrLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sentinel-qr-")
        sm = SettingsManager(settings_file=os.path.join(cls.dir, "settings.json"))
        sm.settings["web"].update({"enabled": True, "port": _free_port()})
        sm.settings["web"]["auth"].update({"username": "luke", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        cls.server = WebServer(sm, FrameBroker())
        assert cls.server.start()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.ip = f"203.0.113.{next(_NEXT_IP)}"  # each test is its own home (codes are limited per address)
        self.home = {"Cf-Connecting-Ip": self.ip}

    def phone(self, ip=None):
        ip = ip or self.ip
        client = self.server._app.test_client()
        r = client.post("/login", data={"username": "luke", "password": "pw", "remember": "1"},
                        headers={"Cf-Connecting-Ip": ip})
        self.assertEqual(r.status_code, 302)
        return client

    def start(self, tv):
        r = tv.post("/login/pair", data="{}", headers={**JSON, **self.home, "User-Agent": "Mozilla/5.0 (Linux; Android 9; AFTMM) Silk"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn("<svg", d["qr"])
        self.assertRegex(d["short"], r"^[A-Z0-9]{4}$")
        return d["code"]

    def test_phone_approves_and_tv_is_signed_in(self):
        tv = self.server._app.test_client()
        self.assertEqual(tv.get("/", headers=self.home).status_code, 302)  # not signed in
        code = self.start(tv)
        self.assertEqual(tv.get(f"/login/pair/{code}", headers=self.home).get_json(), {"state": "waiting"})
        phone = self.phone()
        page = phone.get(f"/pair/{code}", headers=self.home).get_data(as_text=True)
        self.assertIn("Sign in this Fire TV?", page)
        self.assertIn("same internet connection", page)
        self.assertEqual(phone.post(f"/pair/{code}", data="{}", headers={**JSON, **self.home}).status_code, 200)
        r = tv.get(f"/login/pair/{code}", headers=self.home)
        self.assertEqual(r.get_json(), {"state": "approved"})
        self.assertIn("sentinel_session=", r.headers.get("Set-Cookie", ""))
        self.assertEqual(tv.get("/", headers=self.home).status_code, 200)  # signed in
        self.assertEqual(tv.get(f"/login/pair/{code}", headers=self.home).get_json(), {"state": "expired"})  # one use

    def test_needs_a_signed_in_phone_and_a_real_request(self):
        tv = self.server._app.test_client()
        code = self.start(tv)
        stranger = self.server._app.test_client()
        r = stranger.get(f"/pair/{code}", headers=self.home)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=/pair/", r.headers["Location"])
        self.assertEqual(stranger.post(f"/pair/{code}", data="{}", headers={**JSON, **self.home}).status_code, 302)
        phone = self.phone()
        self.assertEqual(phone.post(f"/pair/{code}", data={}, headers=self.home).status_code, 400)  # plain form post (CSRF)
        self.assertEqual(tv.get(f"/login/pair/{code}", headers=self.home).get_json(), {"state": "waiting"})

    def test_code_only_works_for_the_screen_that_asked(self):
        tv = self.server._app.test_client()
        code = self.start(tv)
        phone = self.phone()
        phone.post(f"/pair/{code}", data="{}", headers={**JSON, **self.home})
        thief = self.server._app.test_client()
        self.assertEqual(thief.get(f"/login/pair/{code}", headers={"Cf-Connecting-Ip": "198.51.100.9"}).get_json(),
                         {"state": "expired"})

    def test_other_network_is_flagged(self):
        tv = self.server._app.test_client()
        code = self.start(tv)
        page = self.phone("198.51.100.20").get(f"/pair/{code}", headers={"Cf-Connecting-Ip": "198.51.100.20"})
        self.assertIn("different internet connection", page.get_data(as_text=True))

    def test_limits_codes_per_screen(self):
        tv = self.server._app.test_client()
        ip = {"Cf-Connecting-Ip": "192.0.2.77"}
        codes = [tv.post("/login/pair", data="{}", headers={**JSON, **ip}).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_login_page_shows_the_qr_panel(self):
        html = self.server._app.test_client().get("/login").get_data(as_text=True)
        self.assertTrue(re.search(r'id="qr-card"', html))


class TestSessionsSurviveRestart(unittest.TestCase):
    def test_saved_as_hashes_and_reloaded(self):
        from sentinel.web.auth import WebAuthManager

        d = tempfile.mkdtemp(prefix="sentinel-sess-")
        try:
            path = os.path.join(d, "s.json")
            cfg = {"session_idle_minutes": 60, "session_max_days": 365}
            token = WebAuthManager(cfg, store_path=path).create_session("luke", "1.2.3.4")
            with open(path) as fh:
                self.assertNotIn(token, fh.read())  # only a hash is stored
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            restarted = WebAuthManager(cfg, store_path=path)
            self.assertIsNotNone(restarted.validate_session(token, "1.2.3.4"))
            self.assertIsNone(restarted.validate_session(token + "x", "1.2.3.4"))
            restarted.revoke_session(token)
            self.assertIsNone(WebAuthManager(cfg, store_path=path).validate_session(token, "1.2.3.4"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
