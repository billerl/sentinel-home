"""Family Timeline: stops from phone positions, kept history, personal logins, and the web pages."""

import _test_env  # noqa: F401  (isolates the event log)

import base64
import json
import os
import re
import shutil
import tempfile
import time
import unittest

import bcrypt

from sentinel.engine.location import LocationStore, Place
from sentinel.engine.timeline import day_view, day_window, describe, find_stops
from test_location import HOME, SALON, _free_port, fix

WORK = (41.1145, -88.1180)  # ~5 km east of home
DAY = "2026-10-29"


def stay(lat, lon, start, end, every=600, acc=10.0):
    """A phone sitting still: a position every ``every`` seconds."""
    return [(t, lat, lon, acc, 0.0) for t in range(int(start), int(end) + 1, every)]


def drive(a, b, start, minutes):
    """Positions once a minute along a straight line, at driving speed."""
    return [(start + 60 * i, a[0] + (b[0] - a[0]) * i / minutes, a[1] + (b[1] - a[1]) * i / minutes, 10.0, 50.0)
            for i in range(1, minutes)]


class TestStops(unittest.TestCase):
    def setUp(self):
        self.home = Place(1, "Home", *HOME, radius=150)
        self.salon = Place(2, "the salon", *SALON, radius=150)
        self.places = [self.home, self.salon]
        self.d0, self.d1 = day_window(DAY)

    def test_a_day_out_and_back(self):
        t = self.d0
        pts = (stay(*HOME, t - 8 * 3600, t + 7.5 * 3600) + drive(HOME, WORK, t + 7.5 * 3600, 12)
               + stay(*WORK, t + 7.75 * 3600, t + 16 * 3600) + drive(WORK, HOME, t + 16 * 3600, 12)
               + stay(*HOME, t + 16.25 * 3600, t + 23 * 3600))
        stops = find_stops(pts, self.places)
        self.assertEqual([s.place.name if s.place else None for s in stops], ["Home", None, "Home"])
        self.assertIsNone(stops[-1].end)  # the phone's newest position: still there
        self.assertAlmostEqual(stops[1].start, t + 7.75 * 3600)
        view = day_view(pts, self.places, self.d0, self.d1, now=t + 23 * 3600)
        kinds = [i["type"] for i in view["items"]]
        self.assertEqual(kinds, ["stop", "drive", "stop", "drive", "stop"])
        self.assertTrue(view["items"][0]["before_day"])
        self.assertEqual(view["stops"], 1)
        self.assertTrue(4 < view["miles"] < 9, view["miles"])
        self.assertAlmostEqual(view["away_s"], (16.25 - 7.5) * 3600, delta=1200)
        self.assertIn("east of Home", view["items"][2]["where"])

    def test_red_lights_and_gps_blips_are_not_stops(self):
        t = self.d0 + 9 * 3600
        pts = stay(*HOME, t - 3600, t)
        pts += [(t + 60 * i, HOME[0] + 0.002 * i, HOME[1], 10.0, 0.0 if i == 5 else 45.0) for i in range(1, 12)]  # stopped 1 min
        pts += stay(*SALON, t + 1200, t + 3600)
        pts.insert(3, (t - 3000, HOME[0] + 0.02, HOME[1], 10.0, None))  # one wild reading at home
        pts.sort()
        names = [s.place.name if s.place else None for s in find_stops(pts, self.places)]
        self.assertEqual(names, ["Home", "the salon"])

    def test_quiet_iphone_stayed_until_shortly_before_its_next_position(self):
        # An iPhone often sends one position on arrival and nothing more until it has moved.
        t = self.d0 + 10 * 3600
        pts = [(t, *SALON, 10.0, 0.0), (t + 3 * 3600, *HOME, 10.0, 0.0)]
        stops = find_stops(pts, self.places)
        self.assertEqual([s.place.name for s in stops], ["the salon", "Home"])
        self.assertTrue(t + 2.8 * 3600 < stops[0].end < t + 3 * 3600)

    def test_vague_positions_are_left_out(self):
        t = self.d0 + 10 * 3600
        pts = stay(*SALON, t, t + 3600, acc=1500.0)
        self.assertEqual(find_stops(pts, self.places), [])

    def test_describe_unnamed_stop(self):
        self.assertEqual(describe(HOME[0] + 0.001, HOME[1], self.places), "near Home")
        self.assertRegex(describe(*WORK, self.places), r"^3\.\d mi east of Home$")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-tl-")
        self.store = LocationStore(self.dir)
        self.liz = self.store.person(self.store.save_person("Elizabeth"))
        self.t = 1_800_000_000

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_position_is_kept_even_late_ones(self):
        self.store.record(self.liz, fix(*HOME, self.t), now=self.t)
        self.store.record(self.liz, fix(*SALON, self.t + 600), now=self.t + 600)
        self.store.record(self.liz, fix(*HOME, self.t + 300), now=self.t + 700)  # sent late
        self.store.record(self.liz, fix(*HOME, self.t + 300), now=self.t + 700)  # sent twice
        self.store.record(self.liz, {"_type": "transition", "event": "leave", "lat": 1, "lon": 2, "tst": self.t + 5},
                          now=self.t + 700)
        self.assertEqual([p[0] for p in self.store.points(self.liz.id, 0, 2e9)], [self.t, self.t + 300, self.t + 600])
        self.assertEqual(len(self.store.point_days(self.liz.id)), 1)
        self.assertEqual(self.store.delete_points(self.liz.id, before=self.t + 301), 2)
        self.store.forget(self.liz.id)
        self.assertEqual(self.store.points(self.liz.id, 0, 2e9), [])

    def test_personal_web_login(self):
        self.assertIsNone(self.store.web_login("elizabeth", "anything"))
        code, _ = self.store.new_login_code(self.liz.id)
        self.assertEqual(self.store.by_login_code(code).id, self.liz.id)
        self.assertIsNone(self.store.by_login_code(code[:-1] + ("A" if code[-1] != "A" else "B")))
        self.store.set_web_password(self.liz.id, "salon-days-2026")
        self.assertIsNone(self.store.by_login_code(code))  # one use
        self.assertEqual(self.store.web_login("Elizabeth ", "salon-days-2026").id, self.liz.id)
        self.assertIsNone(self.store.web_login("elizabeth", "salon-days-2027"))
        self.assertTrue(self.store.has_web_login(self.liz.id))
        self.store.remove_web_login(self.liz.id)
        self.assertIsNone(self.store.web_login("elizabeth", "salon-days-2026"))


class TestPages(unittest.TestCase):
    """Personal logins and the Timeline API on a real engine + web server."""

    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager
        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-tl-web-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": "123456789:" + "A" * 35, "telegram_chat_id": "111", "volume": 0,
                            "remote_web_url": "https://sentinel.example.org"})
        sm.settings["web"].update({"enabled": True, "port": _free_port()})
        sm.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"household-pw", bcrypt.gensalt(rounds=4)).decode()})
        self.engine = Engine(sm)
        self.server = WebServer(sm, FrameBroker(), engine=self.engine)
        assert self.server.start()
        loc = self.engine.locations
        loc.save_place("Home", *HOME)
        self.alex = loc.person(loc.save_person("Alex", ["Dad"]))
        self.liz = loc.person(loc.save_person("Elizabeth"))

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self):
        return self.server._app.test_client()

    def sign_in(self, c, user, pw):
        return c.post("/login", data={"username": user, "password": pw, "remember": "1"})

    def post(self, c, url, body):
        return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-Sentinel-Request": "1"})

    def phone(self, person, ts, where=HOME):
        auth = base64.b64encode(f"{person.username}:{person.token}".encode()).decode()
        self.client().post("/owntracks", data=json.dumps(fix(*where, ts)), content_type="application/json",
                           headers={"Authorization": "Basic " + auth})

    def test_login_link_then_own_login_then_timeline(self):
        house = self.client()
        self.assertEqual(self.sign_in(house, "alex", "household-pw").status_code, 302)
        link = self.post(house, f"/api/people/{self.liz.id}/login-link", {}).get_json()
        self.assertTrue(link["url"].startswith("https://sentinel.example.org/account/"))
        self.assertIn("<svg", link["qr"])
        path = "/account/" + link["url"].rsplit("/", 1)[1]

        phone = self.client()
        self.assertIn("Username: <span class=\"user\">elizabeth</span>", phone.get(path).get_data(as_text=True))
        r = phone.post(path, data={"password": "short", "password2": "short"})
        self.assertEqual(r.status_code, 400)
        r = phone.post(path, data={"password": "salon-days-2026", "password2": "salon-days-2026"})
        self.assertEqual((r.status_code, r.headers["Location"].endswith("/timeline")), (302, True))
        self.assertEqual(phone.get("/api/timeline").get_json()["me"], self.liz.id)
        self.assertEqual(phone.get(path).status_code, 404)  # the link worked once

        other = self.client()
        self.assertEqual(self.sign_in(other, "elizabeth", "salon-days-2026").status_code, 302)
        self.assertEqual(other.get("/api/timeline").get_json()["me"], self.liz.id)
        self.assertEqual(house.get("/api/timeline").get_json()["me"], None)  # the shared login is nobody in particular

    def test_alex_is_both_the_household_and_a_person(self):
        self.engine.locations.set_web_password(self.alex.id, "alexs-own-pw")
        c = self.client()
        self.sign_in(c, "alex", "alexs-own-pw")
        self.assertEqual(c.get("/api/timeline").get_json()["me"], self.alex.id)
        c2 = self.client()
        self.sign_in(c2, "alex", "household-pw")
        self.assertIsNone(c2.get("/api/timeline").get_json()["me"])
        self.assertEqual(self.sign_in(self.client(), "alex", "wrong-pw").status_code, 401)

    def test_everyone_sees_everyone_but_only_deletes_their_own(self):
        d0, _ = day_window(time.strftime("%Y-%m-%d"))
        now = time.time()
        for i, ts in enumerate(range(int(max(d0, now - 3 * 3600)), int(now), 600)):
            self.phone(self.liz, ts)
        self.engine.locations.set_web_password(self.alex.id, "alexs-own-pw")
        c = self.client()
        self.sign_in(c, "alex", "alexs-own-pw")
        people = {p["id"]: p for p in c.get("/api/timeline").get_json()["people"]}
        self.assertEqual(people[self.liz.id]["days"], [time.strftime("%Y-%m-%d")])
        day = c.get(f"/api/timeline/{self.liz.id}/{time.strftime('%Y-%m-%d')}").get_json()
        self.assertEqual(day["items"][0]["name"], "Home")
        self.assertTrue(day["items"][0]["ongoing"])
        self.assertEqual(self.post(c, f"/api/timeline/{self.liz.id}/delete", {}).status_code, 403)
        self.assertEqual(c.get(f"/api/timeline/{self.liz.id}/not-a-date").status_code, 404)

        self.engine.locations.set_web_password(self.liz.id, "salon-days-2026")
        a = self.client()
        self.sign_in(a, "elizabeth", "salon-days-2026")
        self.assertEqual(self.post(a, f"/api/timeline/{self.liz.id}/delete", {}).status_code, 200)
        self.assertEqual(self.engine.locations.points(self.liz.id, 0, 2e9), [])

    def test_pages_need_a_login(self):
        c = self.client()
        self.assertEqual(c.get("/timeline").status_code, 302)
        self.assertEqual(c.get("/api/timeline").status_code, 401)
        self.assertEqual(c.get("/account/" + "x" * 22).status_code, 404)
        self.sign_in(c, "alex", "household-pw")
        page = c.get("/timeline").get_data(as_text=True)
        self.assertIn("leaflet/leaflet.js", page)
        self.assertTrue(re.search(r"Family timeline", c.get("/").get_data(as_text=True)))


if __name__ == "__main__":
    unittest.main()
