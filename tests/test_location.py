"""Family locations: OwnTracks positions, named places, questions, and the phone/web endpoints."""

import _test_env  # noqa: F401  (isolates the event log)

import base64
import json
import os
import shutil
import socket
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

import bcrypt

from sentinel.engine.household import Vehicle
from sentinel.engine.location import (
    CarLine, LocationQuestion, LocationStore, PersonView, answer_location, change_message, distance_m,
    location_question, owntracks_config, owntracks_link, parse_coordinates, resolve_map_link,
)

HOME = (41.1145, -83.1780)
SALON = (41.1570, -83.1780)  # ~4.7 km north


def fix(lat, lon, ts, acc=10, **extra):
    return {"_type": "location", "lat": lat, "lon": lon, "tst": ts, "acc": acc, **extra}


class TestGeofence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-")
        self.store = LocationStore(self.dir)
        self.home = self.store.place(self.store.save_place("Home", *HOME, radius=150))
        self.salon = self.store.place(self.store.save_place("the salon", *SALON, radius=150))
        self.abi = self.store.person(self.store.save_person("Abigail", ["Abi"]))
        self.t = 1_800_000_000

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def kinds(self, payload):
        return [(c.kind, c.place.name if c.place else None) for c in self.store.record(self.abi, payload, now=self.t + 10_000)]

    def test_first_position_starts_quietly_then_arrive_and_leave(self):
        self.assertEqual(self.kinds(fix(*HOME, self.t)), [("started", "Home")])
        self.assertIsNone(self.store.fix(self.abi.id)["place_since"])  # was there before tracking began
        # 230 m from the middle: inside the leaving margin, so still home (GPS wobble)
        self.assertEqual(self.kinds(fix(HOME[0] + 0.0021, HOME[1], self.t + 60)), [])
        self.assertEqual(self.kinds(fix(HOME[0] + 0.01, HOME[1], self.t + 120)), [("left", "Home")])
        self.assertEqual(self.kinds(fix(*SALON, self.t + 600)), [("arrived", "the salon")])
        self.assertEqual(self.store.fix(self.abi.id)["place_since"], self.t + 600)
        self.assertEqual([v["kind"] for v in self.store.visits(self.abi.id)], ["arrived", "left"])

    def test_jump_between_places_is_leave_then_arrive(self):
        self.kinds(fix(*HOME, self.t))
        self.assertEqual(self.kinds(fix(*SALON, self.t + 900)), [("left", "Home"), ("arrived", "the salon")])

    def test_vague_fixes_move_nobody(self):
        self.kinds(fix(*HOME, self.t))
        self.assertEqual(self.kinds(fix(*SALON, self.t + 60, acc=2000)), [])  # cell-tower guess
        self.assertEqual(self.store.fix(self.abi.id)["place_id"], self.home.id)

    def test_late_old_positions_are_ignored(self):
        self.kinds(fix(*HOME, self.t + 100))
        self.assertEqual(self.kinds(fix(*SALON, self.t)), [])
        self.assertEqual(self.store.fix(self.abi.id)["ts"], self.t + 100)

    def test_phone_geofence_events_are_trusted(self):
        self.kinds(fix(*HOME, self.t))
        leave = {"_type": "transition", "event": "leave", "desc": "Home", "lat": HOME[0], "lon": HOME[1],
                 "tst": self.t + 60, "acc": 30}
        self.assertEqual(self.kinds(leave), [("left", "Home")])

    def test_junk_is_ignored(self):
        for payload in (None, [], {"_type": "lwt"}, {"_type": "location", "lat": "x", "lon": 1},
                        {"_type": "location", "lat": 95, "lon": 0}):
            self.assertEqual(self.store.record(self.abi, payload), [])
        self.assertIsNone(self.store.fix(self.abi.id))

    def test_adding_or_moving_a_place_rechecks_everyone_at_once(self):
        gym = (41.20, -83.30)
        self.kinds(fix(*gym, self.t))  # phone connected before the place existed
        self.assertIsNone(self.store.fix(self.abi.id)["place_id"])
        gym_id = self.store.save_place("the gym", *gym)
        self.assertEqual(self.store.fix(self.abi.id)["place_id"], gym_id)
        self.store.save_place("the gym", *SALON, place_id=gym_id)  # moved away from her
        self.assertIsNone(self.store.fix(self.abi.id)["place_id"])
        self.assertEqual(self.store.visits(self.abi.id), [])  # no made-up arrivals/departures

    def test_deleting_a_place_clears_who_is_there(self):
        self.kinds(fix(*HOME, self.t))
        self.store.delete_place(self.home.id)
        self.assertIsNone(self.store.fix(self.abi.id)["place_id"])

    def test_logins_and_setup_links(self):
        other = self.store.person(self.store.save_person("Abigail"))
        self.assertNotEqual(other.username, self.abi.username)  # "abigail2"
        self.assertEqual(self.store.authenticate("Abigail", self.abi.token).id, self.abi.id)
        self.assertIsNone(self.store.authenticate("abigail", other.token))
        self.assertIsNone(self.store.authenticate("abigail", ""))
        code = self.store.new_invite(self.abi.id, now=self.t)
        self.assertEqual(self.store.by_invite(code, now=self.t + 60).id, self.abi.id)
        self.assertIsNone(self.store.by_invite(code, now=self.t + 8 * 86400))  # expired
        self.assertIsNone(self.store.by_invite("x" * 22, now=self.t))
        self.store.new_token(self.abi.id)  # lost phone: old password and link both stop working
        self.assertIsNone(self.store.authenticate("abigail", self.abi.token))
        self.assertIsNone(self.store.by_invite(code, now=self.t + 60))

    def test_forget_and_delete(self):
        self.kinds(fix(*HOME, self.t))
        self.kinds(fix(*SALON, self.t + 600))
        self.store.forget(self.abi.id)
        self.assertIsNone(self.store.fix(self.abi.id))
        self.assertEqual(self.store.visits(self.abi.id), [])
        self.store.delete_person(self.abi.id)
        self.assertIsNone(self.store.person(self.abi.id))


class TestHelpers(unittest.TestCase):
    def test_coordinates(self):
        cases = {
            "41.1145, -83.1780": (41.1145, -83.178),
            "41.1145 -83.1780": (41.1145, -83.178),
            "https://www.google.com/maps/place/Salon/@41.15,-83.17,17z/data=!3m1!4b1!4m6!3m5!3d41.1571!4d-83.1779": (41.1571, -83.1779),
            "https://www.google.com/maps/@41.15,-83.17,15z": (41.15, -83.17),
            "https://maps.apple.com/?ll=41.2,-83.1&q=Pin": (41.2, -83.1),
            "https://www.openstreetmap.org/?mlat=41.3&mlon=-83.2#map=17/41.3/-83.2": (41.3, -83.2),
            "https://www.google.com/maps/search/41.108764,+-83.112597?entry=tts": (41.108764, -83.112597),
            "https://maps.apple.com/place?coordinate=41.4,-83.3&name=Pin": (41.4, -83.3),
            '41°06\'31.8"N 83°06\'45.4"W': (41.108833, -83.112611),
        }
        for text, want in cases.items():
            self.assertEqual(parse_coordinates(text), want, text)
        for text in ("", "the salon", "https://maps.app.goo.gl/abc123", "95, 10", "0, 0"):
            self.assertIsNone(parse_coordinates(text), text)

    def test_short_share_links_are_followed_by_their_redirects_only(self):
        import urllib.error
        hops = {
            "https://maps.app.goo.gl/abc123": "https://maps.google.com/?q=x&ftid=1",
            "https://maps.google.com/?q=x&ftid=1": "https://www.google.com/maps/search/41.108764,+-83.112597?g_ep=1",
        }
        seen = []

        def fake_open(req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": hops[req.full_url]}, None)

        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = fake_open
            self.assertEqual(resolve_map_link("Dropped pin https://maps.app.goo.gl/abc123"), (41.108764, -83.112597))
            self.assertEqual(seen, list(hops))  # the google.com page itself is never fetched
            seen.clear()
            self.assertIsNone(resolve_map_link("https://example.com/maps/abc"))  # only map share hosts
            self.assertEqual(seen, [])

    def test_distance(self):
        self.assertAlmostEqual(distance_m(*HOME, *SALON), 4730, delta=30)

    def test_owntracks_setup_link_round_trips(self):
        store = LocationStore(tempfile.mkdtemp(prefix="sentinel-loc-"))
        try:
            p = store.person(store.save_person("Karleigh Biller"))
            store.save_place("Home", *HOME)
            cfg = owntracks_config(p, "https://example.org/owntracks", store.places())
            link = owntracks_link(cfg)
            self.assertTrue(link.startswith("owntracks:///config?inline="))
            back = json.loads(base64.b64decode(urllib.parse.unquote(link.split("inline=")[1])))
            self.assertEqual(back["mode"], 3)
            self.assertEqual(back["url"], "https://example.org/owntracks")
            self.assertEqual((back["username"], back["password"], back["tid"]), (p.username, p.token, "KB"))
            self.assertEqual(back["waypoints"][0]["desc"], "Home")
        finally:
            shutil.rmtree(os.path.dirname(store.db_path), ignore_errors=True)


class TestQuestions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-")
        s = self.store = LocationStore(self.dir)
        self.home = s.place(s.save_place("Home", *HOME))
        self.salon = s.place(s.save_place("the salon", *SALON))
        self.school = s.place(s.save_place("school", 41.12, -83.20, radius=400))
        self.abi = s.person(s.save_person("Abigail", ["Abi"]))
        self.mom = s.person(s.save_person("Stephanie", ["Steph", "Mom"]))
        self.vehicles = [Vehicle(1, "Abigail's Fusion", "Abigail", ["Abi"], "sedan"),
                         Vehicle(2, "Stephanie's Edge", "Stephanie", ["Mom"], "suv")]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def q(self, text):
        return location_question(text, self.store.people(), self.store.places(), self.vehicles)

    def test_parsing(self):
        cases = {
            "Where is Abigail?": ("where", [self.abi.id], None),
            "where's abi": ("where", [self.abi.id], None),
            "Is mom home?": ("at", [self.mom.id], None),
            "is mom at the salon": ("at", [self.mom.id], self.salon.id),
            "When did Abi get to school?": ("arrived", [self.abi.id], self.school.id),
            "when did abigail get home": ("arrived", [self.abi.id], None),
            "Did mom leave?": ("left", [self.mom.id], None),
            "when did steph leave the salon": ("left", [self.mom.id], self.salon.id),
            "Who's home?": ("list", [], None),
            "where is everyone": ("list", [], None),
        }
        for text, want in cases.items():
            q = self.q(text)
            self.assertIsNotNone(q, text)
            self.assertEqual((q.kind, q.person_ids, q.place_id), want, text)

    def test_car_questions_stay_car_questions(self):
        for text in ("Is Abigail's Fusion home?", "where is mom's car", "is the edge here", "videos today",
                     "did anyone come by", "what's the weather"):
            self.assertIsNone(self.q(text), text)

    def test_no_people_no_location_questions(self):
        self.assertIsNone(location_question("who's home?", [], [], self.vehicles))

    def view(self, person, place, lat_lon, ts, cars=(), visits=(), vel=None, batt=None, since=None):
        f = {"lat": lat_lon[0], "lon": lat_lon[1], "ts": ts, "acc": 10, "batt": batt, "vel": vel,
             "place_id": place.id if place else None, "place_since": since}
        return PersonView(person, f, place, list(visits), list(cars))

    def test_phone_and_car_are_separate(self):
        now = time.time()
        fusion = CarLine("Abigail's Fusion", True)
        views = {self.abi.id: self.view(self.abi, self.salon, SALON, now - 60, [fusion], since=now - 1800)}
        text, pins = answer_location(LocationQuestion("where", [self.abi.id]), views, self.store.places(), now)
        self.assertIn("Abigail is at the salon (since", text)
        self.assertIn("Abigail's Fusion is at home.", text)
        self.assertEqual(pins, [])
        text, _ = answer_location(LocationQuestion("at", [self.abi.id]), views, self.store.places(), now)
        self.assertTrue(text.startswith("No — Abigail is at the salon"), text)

    def test_between_places_gets_direction_speed_and_a_pin(self):
        now = time.time()
        views = {self.abi.id: self.view(self.abi, None, (HOME[0], HOME[1] + 0.04), now - 30, vel=64)}
        text, pins = answer_location(LocationQuestion("where", [self.abi.id]), views, self.store.places(), now)
        self.assertIn("about 2.1 miles east of home, on the move (about 40 mph)", text)
        self.assertEqual(len(pins), 1)

    def test_old_position_says_when(self):
        now = time.time()
        views = {self.abi.id: self.view(self.abi, self.salon, SALON, now - 3 * 3600)}
        text, _ = answer_location(LocationQuestion("where", [self.abi.id]), views, self.store.places(), now)
        self.assertIn("last update from the phone", text)

    def test_not_sharing_falls_back_to_the_car_and_says_so(self):
        views = {self.mom.id: PersonView(self.mom, None, None, [], [CarLine("Stephanie's Edge", False, None, time.time() - 600)])}
        text, _ = answer_location(LocationQuestion("at", [self.mom.id]), views, self.store.places())
        self.assertIn("isn't sharing its location", text)
        self.assertIn("Stephanie's Edge isn't home", text)

    def test_arrived_and_left(self):
        now = time.time()
        visits = [{"kind": "left", "place_id": self.school.id, "ts": now - 600},
                  {"kind": "arrived", "place_id": self.school.id, "ts": now - 7200}]
        views = {self.abi.id: self.view(self.abi, None, HOME, now, visits=visits)}
        text, _ = answer_location(LocationQuestion("arrived", [self.abi.id], self.school.id), views, self.store.places(), now)
        self.assertTrue(text.startswith("Abigail got to school at"), text)
        self.assertIn("then left", text)
        text, _ = answer_location(LocationQuestion("left", [self.abi.id], self.school.id), views, self.store.places(), now)
        self.assertTrue(text.startswith("Abigail left school at"), text)

    def test_who_is_home(self):
        now = time.time()
        views = {self.abi.id: self.view(self.abi, self.salon, SALON, now),
                 self.mom.id: self.view(self.mom, self.home, HOME, now)}
        text, _ = answer_location(LocationQuestion("list"), views, self.store.places(), now)
        self.assertIn("Home (by phone): Stephanie.", text)
        self.assertIn("Out: Abigail (at the salon)", text)

    def test_alert_text(self):
        from sentinel.engine.location import Change

        self.assertEqual(change_message(Change("arrived", self.abi, self.salon, 0), []), "📍 Abigail got to the salon.")
        self.assertEqual(change_message(Change("arrived", self.abi, self.home, 0), []), "🏠 Abigail got home.")
        text = change_message(Change("left", self.abi, self.home, 0), [CarLine("Abigail's Fusion", True)])
        self.assertEqual(text, "👋 Abigail left home. (Abigail's Fusion is still here.)")
        self.assertIn("now sharing", change_message(Change("started", self.abi, None, 0), []))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEndpoints(unittest.TestCase):
    """The phone's /owntracks posts, the setup link page, and the Telegram pin flow, on a real engine."""

    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager
        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-web-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": "123456789:" + "A" * 35, "telegram_chat_id": "111", "volume": 0,
                            "remote_web_url": "https://sentinel.example.org"})
        sm.settings["web"].update({"enabled": True, "port": _free_port()})
        sm.settings["web"]["auth"].update({"username": "luke", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.engine = Engine(sm)
        self.server = WebServer(sm, FrameBroker(), engine=self.engine)
        assert self.server.start()
        self.client = self.server._app.test_client()
        loc = self.engine.locations
        loc.save_place("Home", *HOME)
        self.abi = loc.person(loc.save_person("Abigail"))
        self.sent = []
        patcher = patch.object(type(self.engine.telegram), "reply_to_chat",
                               lambda _self, chat, text: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, user, password, body, ip="198.51.100.7"):
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        return self.client.post("/owntracks", data=json.dumps(body), content_type="application/json",
                                headers={"Authorization": "Basic " + auth, "Cf-Connecting-Ip": ip})

    def wait_for(self, n):
        for _ in range(50):
            if len(self.sent) >= n:
                return
            time.sleep(0.02)

    def test_owntracks_post(self):
        r = self.post("abigail", "wrong", fix(*HOME, time.time()))
        self.assertEqual(r.status_code, 401)
        r = self.post("abigail", self.abi.token, fix(*HOME, time.time()))
        self.assertEqual((r.status_code, r.get_json()), (200, []))
        self.assertEqual(self.engine.locations.fix(self.abi.id)["place_id"], self.engine.locations.places()[0].id)
        self.wait_for(1)
        self.assertTrue(any("now sharing" in t for t in self.sent), self.sent)
        self.post("abigail", self.abi.token, fix(HOME[0] + 0.05, HOME[1], time.time() + 1))
        self.wait_for(2)
        self.assertIn("👋 Abigail left home.", self.sent)
        self.assertIn("Abigail is about", self.engine.answer_location_question(self.engine.location_question("where is abigail"))[0])

    def test_stale_position_asks_phone_for_a_fresh_one(self):
        ask = [{"_type": "cmd", "action": "reportLocation"}]
        self.assertEqual(self.post("abigail", self.abi.token, fix(*HOME, time.time() - 3600)).get_json(), ask)
        # the phone's next check-in repeats the old reading: don't keep nagging it
        self.assertEqual(self.post("abigail", self.abi.token, fix(*HOME, time.time() - 3600)).get_json(), [])
        self.engine._location_asked.clear()
        self.assertEqual(self.post("abigail", self.abi.token, fix(*HOME, time.time())).get_json(), [])

    def test_car_seen_driving_off_is_not_still_here(self):
        # 2026-10-30 13:54: "Abigail's Fusion is leaving", then her phone left home a minute later, before
        # the driveway check's three missed scans had marked the car gone -> "(Fusion is still here.)"
        from types import SimpleNamespace
        from sentinel.engine.household import Parked
        from sentinel.engine.questions import VehicleStatus

        vid = self.engine.household.save_vehicle("Abigail's Fusion", owner="Abigail")
        spot = Parked(box=(0, 0, 10, 10), first_seen=0, last_seen=0, vehicle_id=vid, confirmed=True)
        self.engine.presence = SimpleNamespace(where_is=lambda v: ("garage", spot) if v == vid else None)
        statuses = {vid: VehicleStatus("Abigail's Fusion", True, "Garage", None, None, None)}
        self.assertTrue(self.engine._cars_of(self.abi, statuses)[0].here)
        spot.leaving_at, spot.empty_since = time.time(), time.time()  # camera: it pulled out, spot empty
        self.assertFalse(self.engine._cars_of(self.abi, statuses)[0].here)
        spot.empty_since = 0.0  # it came straight back (or it was a false "leaving")
        self.assertTrue(self.engine._cars_of(self.abi, statuses)[0].here)

    def test_wrong_logins_get_blocked(self):
        for _ in range(20):
            self.post("abigail", "guess", {}, ip="198.51.100.99")
        self.assertEqual(self.post("abigail", self.abi.token, {}, ip="198.51.100.99").status_code, 429)
        self.assertEqual(self.post("abigail", self.abi.token, {}, ip="198.51.100.8").status_code, 200)

    def test_setup_link_page(self):
        self.assertEqual(self.client.get("/join/" + "x" * 22).status_code, 404)
        ok, url = self.engine.people_action(self.abi.id, "invite")
        self.assertTrue(url.startswith("https://sentinel.example.org/join/"))
        code = url.rsplit("/", 1)[1]
        page = self.client.get("/join/" + code).get_data(as_text=True)
        self.assertIn("Hi Abigail", page)
        self.assertIn("I agree", page)
        self.assertIn("owntracks:///config?inline=", page)
        self.assertIn("https://sentinel.example.org/owntracks", page)
        self.assertEqual(self.client.get(f"/join/{code}/status").get_json(), {"sharing": False})
        cfg = json.loads(self.client.get(f"/join/{code}/sentinel.otrc").get_data(as_text=True))
        self.assertEqual(cfg["password"], self.abi.token)

    def test_setup_visits_are_logged_with_the_phone_type(self):
        from sentinel.logging_utils import app_logger
        ok, url = self.engine.people_action(self.abi.id, "invite")
        code = url.rsplit("/", 1)[1]
        google_app = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 GSA/350.0 Mobile/15E148"
        self.client.get("/join/" + code, headers={"User-Agent": google_app})
        with open(app_logger.log_path) as f:
            opened = [json.loads(line) for line in f if "location_setup_opened" in line]
        self.assertEqual((opened[-1]["person"], opened[-1]["phone"]), ("Abigail", "iPhone (inside another app)"))
        self.assertNotIn(self.abi.token, json.dumps(opened[-1]))

    def test_people_api_needs_sign_in(self):
        self.assertEqual(self.client.get("/api/people").status_code, 401)
        self.assertEqual(self.client.get("/api/family").status_code, 401)
        self.assertEqual(self.client.get("/people").status_code, 302)

    def test_people_and_places_from_the_page(self):
        self.assertFalse(self.engine.people_save({"name": "abigail"})[0])  # already there
        ok, _ = self.engine.people_save({"name": "Stephanie", "aliases": "Steph, Mom"})
        self.assertTrue(ok)
        self.assertFalse(self.engine.place_save({"name": "the salon", "where": "the salon"})[0])
        ok, _ = self.engine.place_save({"name": "the salon", "where": "41.157, -83.178", "radius": 5})
        self.assertTrue(ok)
        salon = next(p for p in self.engine.locations.places() if p.name == "the salon")
        self.assertEqual(salon.radius, 50)  # clamped
        ok, msg = self.engine.place_save({"name": "school", "where": "41°08'00\"N 83°10'40\"W"})
        self.assertEqual((ok, msg), (True, "Saved — about 1.3 miles north of home"))
        self.assertFalse(self.engine.place_save({"name": "home", "where": "41, -83"})[0])  # Home exists
        view = self.engine.people_view("http://localhost")
        self.assertEqual([p["name"] for p in view["people"]], ["Abigail", "Stephanie"])
        self.assertNotIn(self.abi.token, json.dumps(view))  # the phone password only goes in the setup link

    def test_telegram_pin_then_name(self):
        self.engine._on_telegram_pin("111", 41.12, -83.20)
        self.assertIn("What should I call this place?", self.sent[-1])
        self.assertFalse(self.engine._name_pinned_place("111", "did anyone come by?"))  # a question passes through
        self.assertTrue(self.engine._name_pinned_place("111", "School"))
        self.assertIn("Saved “School”", self.sent[-1])
        self.assertFalse(self.engine._name_pinned_place("111", "hello"))  # only one name per pin
        self.assertTrue(any(p.name == "School" for p in self.engine.locations.places()))


if __name__ == "__main__":
    unittest.main()
