"""A TV in the house that opens Sentinel through the tunnel is moved, signed in, to the home-network address."""

import _test_env  # noqa: F401  (isolates the event log)

import ipaddress
import os
import shutil
import socket
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import bcrypt

from sentinel.settings.manager import SettingsManager
from sentinel.web import server as web
from sentinel.web.frame_broker import FrameBroker

FIRE_TV = "Mozilla/5.0 (Linux; Android 7.1.2; AFTEAMR311 Build/NS6711; wv) AppleWebKit/537.36 Chrome/118.0.0.0"
PHONE = "Mozilla/5.0 (Linux; Android 14; SM-S918U) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36"
HOME_NET = ipaddress.ip_network("2600:1f18:aa:bb::/64")
PAGE = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


class TestHelpers(unittest.TestCase):
    def test_lan_address(self):
        self.assertTrue(web._home_lan_address("192.168.1.45"))
        for host in ("127.0.0.1", "100.124.125.122", "8.8.8.8", "::1", "localhost"):
            self.assertFalse(web._home_lan_address(host), host)

    def test_home_prefix_from_this_pcs_addresses(self):
        path = os.path.join(tempfile.mkdtemp(prefix="sentinel-inet6-"), "if_inet6")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w") as f:
            f.write("26001f1800aa00bb0000000000000007 02 40 00 00 enp1s0\n"   # public, global
                    "fd7a115ca1e000000000000034017dca 05 80 00 80 tailscale0\n"  # private (ULA)
                    "fe80000000000000978a667e2f8e8255 02 40 20 80 enp1s0\n")  # link-local
        web._home_nets[:] = [0.0, []]
        self.addCleanup(web._home_nets.__setitem__, slice(None), [0.0, []])
        self.assertTrue(web._from_this_home("2600:1f18:aa:bb::99", path))
        self.assertFalse(web._from_this_home("2600:1f18:aa:cc::99", path))
        self.assertFalse(web._from_this_home("fd7a:115c:a1e0::5", path))
        self.assertFalse(web._from_this_home("203.0.113.9", path))
        self.assertFalse(web._from_this_home("nonsense", path))


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-handoff-")
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        # The second address isn't this machine's: its listener just fails to bind (logged), as in any test box.
        manager.settings["web"].update({"enabled": True, "port": self.port, "bind_host": "127.0.0.1, 10.254.254.254"})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        web._home_nets[:] = [time.time() + 1e9, [HOME_NET]]
        self.addCleanup(web._home_nets.__setitem__, slice(None), [0.0, []])
        self.server = web.WebServer(manager, FrameBroker(), engine=mock.Mock())
        with mock.patch("builtins.print"):
            self.assertTrue(self.server.start())
        self.auth = self.server._auth
        self.client = self.server._app.test_client()
        self.token = self.auth.create_session("alex", "2600:1f18:aa:bb::42", remember=True)
        self.client.set_cookie("sentinel_session", self.token)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def get(self, path, ua=FIRE_TV, ip="2600:1f18:aa:bb::42", **kw):
        return self.client.get(path, headers={**PAGE, "User-Agent": ua, "Cf-Connecting-Ip": ip}, **kw)

    def test_tv_at_home_moves_to_the_home_address_signed_in(self):
        r = self.get("/house?view=top")
        self.assertEqual(r.status_code, 302)
        to = urlparse(r.headers["Location"])
        self.assertEqual((to.scheme, to.netloc, to.path), ("http", f"10.254.254.254:{self.port}", "/handoff"))
        query = parse_qs(to.query)
        self.assertEqual(query["next"], ["/house?view=top"])
        tv = self.server._app.test_client()
        r = tv.get(f"/handoff?{to.query}", environ_base={"REMOTE_ADDR": "192.168.1.157"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/house?view=top"))
        cookie = tv.get_cookie("sentinel_session")
        self.assertIsNotNone(cookie)
        self.assertNotEqual(cookie.value, self.token)
        self.assertEqual(self.auth.validate_session(cookie.value, "192.168.1.157").username, "alex")
        r = tv.get(f"/handoff?{to.query}", environ_base={"REMOTE_ADDR": "192.168.1.158"})  # once only
        self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_left_alone(self):
        self.assertEqual(self.get("/house", ua=PHONE).status_code, 200)  # a phone may leave the house
        self.assertEqual(self.get("/house", ip="2600:1f18:aa:cc::1").status_code, 200)  # a TV somewhere else
        self.assertEqual(self.client.get("/house", headers={"User-Agent": FIRE_TV, "Cf-Connecting-Ip": "2600:1f18:aa:bb::42",
                                                            "Accept": "image/*"}).status_code, 200)  # not a page
        self.client.delete_cookie("sentinel_session")
        self.assertIn("/login", self.get("/house").headers["Location"])  # not signed in: the usual login

    def test_tickets_only_from_the_home_network(self):
        ticket = self.auth.create_handoff("alex")
        r = self.client.get(f"/handoff?t={ticket}&next=//evil.example/")  # through the tunnel (loopback)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        r = self.client.get(f"/handoff?t={ticket}&next=//evil.example/", environ_base={"REMOTE_ADDR": "192.168.1.157"})
        self.assertEqual(r.headers["Location"], "/")  # still unused; and never sent off-site


if __name__ == "__main__":
    unittest.main()
