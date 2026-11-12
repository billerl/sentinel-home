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
    CarLine, LocationQuestion, LocationStore, Person, PersonView, answer_location, change_message, distance_m,
    location_question, beep_command, lost_command, outing_command, owntracks_config, owntracks_link, WAYPOINT_TST_BASE, parse_coordinates, resolve_map_link,
    Place, Stop, frequent_spots, stops_in,
)

HOME = (41.1145, -88.1780)
SALON = (41.1570, -88.1780)  # ~4.7 km north


def fix(lat, lon, ts, acc=10, **extra):
    return {"_type": "location", "lat": lat, "lon": lon, "tst": ts, "acc": acc, **extra}


class TestGeofence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-")
        self.store = LocationStore(self.dir)
        self.home = self.store.place(self.store.save_place("Home", *HOME, radius=150))
        self.salon = self.store.place(self.store.save_place("the salon", *SALON, radius=150))
        self.liz = self.store.person(self.store.save_person("Elizabeth", ["Liz"]))
        self.t = 1_800_000_000

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def kinds(self, payload):
        return [(c.kind, c.place.name if c.place else None) for c in self.store.record(self.liz, payload, now=self.t + 10_000)]

    def test_first_position_starts_quietly_then_arrive_and_leave(self):
        self.assertEqual(self.kinds(fix(*HOME, self.t)), [("started", "Home")])
        self.assertIsNone(self.store.fix(self.liz.id)["place_since"])  # was there before tracking began
        # 230 m from the middle: inside the leaving margin, so still home (GPS wobble)
        self.assertEqual(self.kinds(fix(HOME[0] + 0.0021, HOME[1], self.t + 60)), [])
        self.assertEqual(self.kinds(fix(HOME[0] + 0.01, HOME[1], self.t + 120)), [("left", "Home")])
        self.assertEqual(self.kinds(fix(*SALON, self.t + 600)), [("arrived", "the salon")])
        self.assertEqual(self.store.fix(self.liz.id)["place_since"], self.t + 600)
        self.assertEqual([v["kind"] for v in self.store.visits(self.liz.id)], ["arrived", "left"])

    def test_jump_between_places_is_leave_then_arrive(self):
        self.kinds(fix(*HOME, self.t))
        self.assertEqual(self.kinds(fix(*SALON, self.t + 900)), [("left", "Home"), ("arrived", "the salon")])

    def test_vague_fixes_move_nobody(self):
        self.kinds(fix(*HOME, self.t))
        self.assertEqual(self.kinds(fix(*SALON, self.t + 60, acc=2000)), [])  # cell-tower guess
        self.assertEqual(self.store.fix(self.liz.id)["place_id"], self.home.id)

    def test_late_old_positions_are_ignored(self):
        self.kinds(fix(*HOME, self.t + 100))
        self.assertEqual(self.kinds(fix(*SALON, self.t)), [])
        self.assertEqual(self.store.fix(self.liz.id)["ts"], self.t + 100)

    def test_phone_geofence_events_are_trusted(self):
        self.kinds(fix(*HOME, self.t))
        leave = {"_type": "transition", "event": "leave", "desc": "Home", "lat": HOME[0], "lon": HOME[1],
                 "tst": self.t + 60, "acc": 30}
        self.assertEqual(self.kinds(leave), [("left", "Home")])

    def test_junk_is_ignored(self):
        for payload in (None, [], {"_type": "lwt"}, {"_type": "location", "lat": "x", "lon": 1},
                        {"_type": "location", "lat": 95, "lon": 0}):
            self.assertEqual(self.store.record(self.liz, payload), [])
        self.assertIsNone(self.store.fix(self.liz.id))

    def test_adding_or_moving_a_place_rechecks_everyone_at_once(self):
        gym = (41.20, -88.30)
        self.kinds(fix(*gym, self.t))  # phone connected before the place existed
        self.assertIsNone(self.store.fix(self.liz.id)["place_id"])
        gym_id = self.store.save_place("the gym", *gym)
        self.assertEqual(self.store.fix(self.liz.id)["place_id"], gym_id)
        self.store.save_place("the gym", *SALON, place_id=gym_id)  # moved away from her
        self.assertIsNone(self.store.fix(self.liz.id)["place_id"])
        self.assertEqual(self.store.visits(self.liz.id), [])  # no made-up arrivals/departures

    def test_deleting_a_place_clears_who_is_there(self):
        self.kinds(fix(*HOME, self.t))
        self.store.delete_place(self.home.id)
        self.assertIsNone(self.store.fix(self.liz.id)["place_id"])

    def test_logins_and_setup_links(self):
        other = self.store.person(self.store.save_person("Elizabeth"))
        self.assertNotEqual(other.username, self.liz.username)  # "elizabeth2"
        self.assertEqual(self.store.authenticate("Elizabeth", self.liz.token).id, self.liz.id)
        self.assertIsNone(self.store.authenticate("elizabeth", other.token))
        self.assertIsNone(self.store.authenticate("elizabeth", ""))
        code = self.store.new_invite(self.liz.id, now=self.t)
        self.assertEqual(self.store.by_invite(code, now=self.t + 60).id, self.liz.id)
        self.assertIsNone(self.store.by_invite(code, now=self.t + 8 * 86400))  # expired
        self.assertIsNone(self.store.by_invite("x" * 22, now=self.t))
        self.store.new_token(self.liz.id)  # lost phone: old password and link both stop working
        self.assertIsNone(self.store.authenticate("elizabeth", self.liz.token))
        self.assertIsNone(self.store.by_invite(code, now=self.t + 60))

    def test_forget_and_delete(self):
        self.kinds(fix(*HOME, self.t))
        self.kinds(fix(*SALON, self.t + 600))
        self.store.forget(self.liz.id)
        self.assertIsNone(self.store.fix(self.liz.id))
        self.assertEqual(self.store.visits(self.liz.id), [])
        self.store.delete_person(self.liz.id)
        self.assertIsNone(self.store.person(self.liz.id))


class TestYard(unittest.TestCase):
    """A dog's collar tracker and the yard: in/out within metres, not the 100+ m a phone needs to leave home."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-")
        self.store = LocationStore(self.dir)
        self.store.save_place("Home", *HOME, radius=150)
        self.dog = self.store.person(self.store.save_person("Grace"))
        self.store.set_tracker(self.dog.id, "!a1cacb2a")
        self.dog = self.store.person(self.dog.id)
        # a 40 m x 30 m yard around the house (~0.00036 deg lat, ~0.00036 deg lon at 41 N)
        self.store.set_yard([(HOME[0] - 0.00018, HOME[1] - 0.00024), (HOME[0] - 0.00018, HOME[1] + 0.00024),
                             (HOME[0] + 0.00018, HOME[1] + 0.00024), (HOME[0] + 0.00018, HOME[1] - 0.00024)])
        self.t = 1_800_000_000

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def at(self, dlat, ts, acc=5):
        return [c.kind for c in self.store.record(self.dog, fix(HOME[0] + dlat, HOME[1], self.t + ts, acc=acc),
                                                  now=self.t + 10_000)]

    def test_left_and_back(self):
        self.assertEqual(self.at(0, 0), ["started"])  # in the yard: noted quietly
        self.assertEqual(self.store.fix(self.dog.id)["in_yard"], 1)
        self.assertEqual(self.at(0.00022, 30), [])  # ~4 m past the edge: GPS wander at the fence
        self.assertEqual(self.at(0.00040, 60), [])  # ~24 m past it: one reading could be a stray one ...
        self.assertEqual(self.at(0.00050, 90), ["left_yard"])  # ... a second one agrees
        self.assertEqual(self.at(0.00060, 120), [])  # still out: said once
        self.assertEqual(self.at(0.00045, 150, acc=80), [])  # a vague fix moves nobody
        self.assertEqual(self.at(0.00010, 180), [])
        self.assertEqual(self.at(0.00005, 210), ["back_in_yard"])
        self.assertEqual(self.store.visits(self.dog.id), [])  # the yard isn't a place visit

    def test_one_stray_reading_is_not_an_escape(self):
        """2026-11-07: the collar sat in the house (by the yard's edge) and single readings 8-26 m outside
        sent "Xena left the yard!" three times, each followed by a normal one 30 s later."""
        self.at(0, 0)
        for t in (100, 300, 500):
            self.assertEqual(self.at(0.00045, t), [])  # a stray reading outside
            self.assertEqual(self.at(0.00005, t + 30), [])  # back to normal
        self.assertEqual(self.at(0.00045, 600), [])
        self.assertEqual(self.at(0.00021, 630), [])  # at the fence: doesn't decide either way
        self.assertEqual(self.at(0.00050, 660), ["left_yard"])

    def test_words(self):
        self.at(0, 0)
        self.at(0.00040, 60)
        self.at(0.00040, 90)
        view = PersonView(self.dog, self.store.fix(self.dog.id), None, yard=self.store.yard())
        text, pins = answer_location(LocationQuestion("where", [self.dog.id]), {self.dog.id: view},
                                     self.store.places(), now=self.t + 100)
        self.assertIn("Grace is outside the yard, about 80 ft north of it", text)
        self.assertEqual(change_message(type("C", (), {"kind": "left_yard", "person": self.dog, "place": None})(), []),
                         "🐕 Grace left the yard!")

    def test_redrawing_the_yard_starts_over(self):
        self.at(0, 0)
        self.store.set_yard([])
        self.assertIsNone(self.store.fix(self.dog.id)["in_yard"])
        self.assertEqual(self.at(0.001, 60), [])  # no yard: nothing to leave (and too close to leave home)


class TestOuting(unittest.TestCase):
    """"The dogs are going out" (their collars report often) / "the dogs are in"."""

    def test_phrases(self):
        people = [Person(1, "Alex"), Person(2, "Grace", tracker="!a"), Person(3, "Ben", tracker="!b")]

        def said(text):
            r = outing_command(text, people)
            return r and (r[0], [p.name for p in r[1]])

        self.assertEqual(said("The dogs are going out"), ("out", ["Grace", "Ben"]))
        self.assertEqual(said("let Grace out."), ("out", ["Grace"]))
        self.assertEqual(said("Grace's back inside"), ("in", ["Grace"]))
        self.assertEqual(said("bring the dogs in"), ("in", ["Grace", "Ben"]))
        # said short (2026-11-07: "Dogs outside." went nowhere)
        self.assertEqual(said("Dogs outside."), ("out", ["Grace", "Ben"]))
        self.assertEqual(said("Grace going out"), ("out", ["Grace"]))
        self.assertEqual(said("dogs in"), ("in", ["Grace", "Ben"]))
        for not_it in ("Alex outside", "lights out", "dogs out?"):
            self.assertIsNone(said(not_it), not_it)
        for not_it in ("is Grace outside?", "are the dogs out", "Alex is outside", "the lights are out"):
            self.assertIsNone(said(not_it), not_it)
        self.assertIsNone(outing_command("the dogs are out", people[:1]))  # no pets with trackers

    def test_recall(self):
        people = [Person(1, "Alex"), Person(2, "Grace", tracker="!a"), Person(3, "Ben", tracker="!b")]
        self.assertEqual([p.name for p in beep_command("Recall Grace", people)], ["Grace"])
        self.assertEqual([p.name for p in beep_command("beep the dogs", people)], ["Grace", "Ben"])
        self.assertEqual([p.name for p in beep_command("beep Grace's collar", people)], ["Grace"])
        for not_it in ("beep Alex", "recall the last message", "beep"):
            self.assertIsNone(beep_command(not_it, people), not_it)

    def test_lost_phrases(self):
        people = [Person(1, "Alex"), Person(2, "Grace", tracker="!a"), Person(3, "Ben", tracker="!b")]

        def said(text):
            r = lost_command(text, people)
            return r and (r[0], [p.name for p in r[1]])

        self.assertEqual(said("Grace is lost"), ("lost", ["Grace"]))
        self.assertEqual(said("The dogs are lost!"), ("lost", ["Grace", "Ben"]))
        self.assertEqual(said("Grace got out"), ("lost", ["Grace"]))  # loose, not "let her out"
        self.assertEqual(said("Grace's missing"), ("lost", ["Grace"]))
        self.assertEqual(said("Ben ran off"), ("lost", ["Ben"]))
        self.assertEqual(said("I can't find Grace"), ("lost", ["Grace"]))
        self.assertEqual(said("lost dogs"), ("lost", ["Grace", "Ben"]))
        self.assertEqual(said("We found Grace"), ("found", ["Grace"]))
        self.assertEqual(said("found the dogs"), ("found", ["Grace", "Ben"]))
        self.assertEqual(said("Grace is safe"), ("found", ["Grace"]))
        for not_it in ("is Grace lost?", "is Grace lost", "where is Grace", "Alex is lost", "I lost my keys",
                       "Grace is back inside", "found a recipe for chili"):
            self.assertIsNone(said(not_it), not_it)


class TestHelpers(unittest.TestCase):
    def test_coordinates(self):
        cases = {
            "41.1145, -88.1780": (41.1145, -88.178),
            "41.1145 -88.1780": (41.1145, -88.178),
            "https://www.google.com/maps/place/Salon/@41.15,-88.17,17z/data=!3m1!4b1!4m6!3m5!3d41.1571!4d-88.1779": (41.1571, -88.1779),
            "https://www.google.com/maps/@41.15,-88.17,15z": (41.15, -88.17),
            "https://maps.apple.com/?ll=41.2,-88.1&q=Pin": (41.2, -88.1),
            "https://www.openstreetmap.org/?mlat=41.3&mlon=-88.2#map=17/41.3/-88.2": (41.3, -88.2),
            "https://www.google.com/maps/search/41.108764,+-88.112597?entry=tts": (41.108764, -88.112597),
            "https://maps.apple.com/place?coordinate=41.4,-88.3&name=Pin": (41.4, -88.3),
            '41°06\'31.8"N 88°06\'45.4"W': (41.108833, -88.112611),
        }
        for text, want in cases.items():
            self.assertEqual(parse_coordinates(text), want, text)
        for text in ("", "the salon", "https://maps.app.goo.gl/abc123", "95, 10", "0, 0"):
            self.assertIsNone(parse_coordinates(text), text)

    def test_short_share_links_are_followed_by_their_redirects_only(self):
        import urllib.error
        hops = {
            "https://maps.app.goo.gl/abc123": "https://maps.google.com/?q=x&ftid=1",
            "https://maps.google.com/?q=x&ftid=1": "https://www.google.com/maps/search/41.108764,+-88.112597?g_ep=1",
        }
        seen = []

        def fake_open(req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": hops[req.full_url]}, None)

        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = fake_open
            self.assertEqual(resolve_map_link("Dropped pin https://maps.app.goo.gl/abc123"), (41.108764, -88.112597))
            self.assertEqual(seen, list(hops))  # the google.com page itself is never fetched
            seen.clear()
            self.assertIsNone(resolve_map_link("https://example.com/maps/abc"))  # only map share hosts
            self.assertEqual(seen, [])

    def test_distance(self):
        self.assertAlmostEqual(distance_m(*HOME, *SALON), 4730, delta=30)

    def test_owntracks_setup_link_round_trips(self):
        store = LocationStore(tempfile.mkdtemp(prefix="sentinel-loc-"))
        try:
            p = store.person(store.save_person("Grace Smith"))
            store.save_place("Home", *HOME)
            cfg = owntracks_config(p, "https://example.org/owntracks", store.places())
            link = owntracks_link(cfg)
            self.assertTrue(link.startswith("owntracks:///config?inline="))
            back = json.loads(base64.b64decode(urllib.parse.unquote(link.split("inline=")[1])))
            self.assertEqual(back["mode"], 3)
            self.assertEqual(back["url"], "https://example.org/owntracks")
            self.assertEqual((back["username"], back["password"], back["tid"]), (p.username, p.token, "GS"))
            self.assertEqual(back["waypoints"][0]["desc"], "Home")
        finally:
            shutil.rmtree(os.path.dirname(store.db_path), ignore_errors=True)


GYM = (HOME[0], HOME[1] + 0.03)  # ~2.5 km east


class TestSpots(unittest.TestCase):
    """A spot someone keeps going back to, offered as a new place."""

    def test_stops(self):
        day = 1_762_000_000.0
        iphone = [(day, *GYM, 6, 0), (day + 60, *GYM, 6, 0),  # arrives, then quiet while it sits there
                  (day + 3 * 3600, GYM[0] + 0.01, GYM[1], 6, 40)]  # next report: driving away
        self.assertEqual([(s.start, s.end) for s in stops_in(1, iphone)], [(day, day + 3 * 3600)])
        android = [(day + i * 900, GYM[0] + i * 0.0002, GYM[1], 20, 0) for i in range(5)]  # pings every 15 min, wandering a bit
        self.assertEqual([(s.start, s.end) for s in stops_in(1, android)], [(day, day + 3600)])
        quick = [(day, *GYM, 6, 0), (day + 600, *HOME, 6, 0)]  # 10 minutes: a stop at the shop
        self.assertEqual(stops_in(1, quick), [])
        dead = [(day, *GYM, 6, 0), (day + 60, *GYM, 6, 0), (day + 20 * 3600, *GYM, 6, 0)]  # phone off overnight?
        self.assertEqual(stops_in(1, dead), [])
        driving = [(day + i * 600, *GYM, 6, 50) for i in range(6)]
        self.assertEqual(stops_in(1, driving), [])

    def test_frequent_spots(self):
        day = 1_762_000_000.0
        stops = [Stop(1, day + d * 86400, day + d * 86400 + 3600, GYM[0] + d * 0.0003, GYM[1]) for d in range(3)]
        spots = frequent_spots(stops, [])
        self.assertEqual((len(spots), len(spots[0].stops)), (1, 3))
        self.assertAlmostEqual(spots[0].lat, GYM[0] + 0.0003)
        self.assertEqual(frequent_spots(stops[:2], []), [])  # two days isn't a habit yet
        twice_a_day = stops[:2] + [Stop(2, day + 3600 * 5, day + 3600 * 6, *GYM)]
        self.assertEqual(frequent_spots(twice_a_day, []), [])  # still two days
        self.assertEqual(frequent_spots(stops, [Place(1, "Gym", GYM[0], GYM[1] + 0.002, 150)]), [])  # already a place
        self.assertEqual(frequent_spots(stops, [], skip=[GYM]), [])  # already asked


class TestQuestions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-loc-")
        s = self.store = LocationStore(self.dir)
        self.home = s.place(s.save_place("Home", *HOME))
        self.salon = s.place(s.save_place("the salon", *SALON))
        self.school = s.place(s.save_place("school", 41.12, -88.20, radius=400))
        self.liz = s.person(s.save_person("Elizabeth", ["Liz"]))
        self.mom = s.person(s.save_person("Katherine", ["Kate", "Mom"]))
        self.vehicles = [Vehicle(1, "Elizabeth's Fusion", "Elizabeth", ["Liz"], "sedan"),
                         Vehicle(2, "Katherine's Edge", "Katherine", ["Mom"], "suv")]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def q(self, text):
        return location_question(text, self.store.people(), self.store.places(), self.vehicles)

    def test_parsing(self):
        cases = {
            "Where is Elizabeth?": ("where", [self.liz.id], None),
            "where's liz": ("where", [self.liz.id], None),
            "Is mom home?": ("at", [self.mom.id], None),
            "is mom at the salon": ("at", [self.mom.id], self.salon.id),
            "When did Liz get to school?": ("arrived", [self.liz.id], self.school.id),
            "when did elizabeth get home": ("arrived", [self.liz.id], None),
            "Did mom leave?": ("left", [self.mom.id], None),
            "when did kate leave the salon": ("left", [self.mom.id], self.salon.id),
            "Who's home?": ("list", [], None),
            "where is everyone": ("list", [], None),
        }
        for text, want in cases.items():
            q = self.q(text)
            self.assertIsNotNone(q, text)
            self.assertEqual((q.kind, q.person_ids, q.place_id), want, text)

    def test_car_questions_stay_car_questions(self):
        for text in ("Is Elizabeth's Fusion home?", "where is mom's car", "is the edge here", "videos today",
                     "did anyone come by", "what's the weather"):
            self.assertIsNone(self.q(text), text)

    def test_no_people_no_location_questions(self):
        self.assertIsNone(location_question("who's home?", [], [], self.vehicles))

    def view(self, person, place, lat_lon, ts, cars=(), visits=(), vel=None, batt=None, since=None):
        f = {"lat": lat_lon[0], "lon": lat_lon[1], "ts": ts, "acc": 10, "batt": batt, "vel": vel,
             "place_id": place.id if place else None, "place_since": since}
        return PersonView(person, f, place, list(visits), list(cars))

    def test_battery_questions(self):
        s = self.store
        dog = s.save_person("Grace")
        s.set_tracker(dog, "!a1cacb2a")
        dog = s.person(dog)
        for text, want in (("What's Grace's battery at?", [dog.id]), ("How much charge does Liz's phone have?", [self.liz.id]),
                           ("what is the tracker battery percentage", [dog.id]), ("is the collar charged", [dog.id])):
            q = location_question(text, s.people(), s.places(), self.vehicles)
            self.assertEqual((q.kind, q.person_ids), ("battery", want), text)
        self.assertIsNone(location_question("what's the battery on the door sensor", s.people(), s.places(), self.vehicles))
        now = time.time()
        views = {dog.id: self.view(dog, self.home, HOME, now - 60, batt=62),
                 self.liz.id: self.view(self.liz, self.home, HOME, now - 60)}
        text, pins = answer_location(LocationQuestion("battery", [dog.id, self.liz.id]), views, s.places(), now)
        self.assertEqual(text, "Grace's tracker battery is at 62%.\nElizabeth's phone hasn't told me its battery level yet.")
        self.assertEqual(pins, [])
        views = {dog.id: self.view(dog, self.home, HOME, now - 3 * 3600, batt=62)}
        text, _ = answer_location(LocationQuestion("battery", [dog.id]), views, s.places(), now)
        self.assertRegex(text, r"^Grace's tracker battery is at 62% \(as of (yesterday at )?\d+:\d\d[ap]m\)\.$")

    def test_phone_and_car_are_separate(self):
        now = time.time()
        fusion = CarLine("Elizabeth's Fusion", True)
        views = {self.liz.id: self.view(self.liz, self.salon, SALON, now - 60, [fusion], since=now - 1800)}
        text, pins = answer_location(LocationQuestion("where", [self.liz.id]), views, self.store.places(), now)
        self.assertIn("Elizabeth is at the salon (since", text)
        self.assertIn("Elizabeth's Fusion is at home.", text)
        self.assertEqual(pins, [])
        text, _ = answer_location(LocationQuestion("at", [self.liz.id]), views, self.store.places(), now)
        self.assertTrue(text.startswith("No — Elizabeth is at the salon"), text)

    def test_between_places_gets_direction_speed_and_a_pin(self):
        now = time.time()
        views = {self.liz.id: self.view(self.liz, None, (HOME[0], HOME[1] + 0.04), now - 30, vel=64)}
        text, pins = answer_location(LocationQuestion("where", [self.liz.id]), views, self.store.places(), now)
        self.assertIn("about 2.1 miles east of home, on the move (about 40 mph)", text)
        self.assertEqual(len(pins), 1)

    def test_old_position_says_when(self):
        now = time.time()
        views = {self.liz.id: self.view(self.liz, self.salon, SALON, now - 3 * 3600)}
        text, _ = answer_location(LocationQuestion("where", [self.liz.id]), views, self.store.places(), now)
        self.assertIn("last update from the phone", text)

    def test_not_sharing_falls_back_to_the_car_and_says_so(self):
        views = {self.mom.id: PersonView(self.mom, None, None, [], [CarLine("Katherine's Edge", False, None, time.time() - 600)])}
        text, _ = answer_location(LocationQuestion("at", [self.mom.id]), views, self.store.places())
        self.assertIn("isn't sharing its location", text)
        self.assertIn("Katherine's Edge isn't home", text)

    def test_arrived_and_left(self):
        now = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))  # midday: "2 hours ago" is still today
        visits = [{"kind": "left", "place_id": self.school.id, "ts": now - 600},
                  {"kind": "arrived", "place_id": self.school.id, "ts": now - 7200}]
        views = {self.liz.id: self.view(self.liz, None, HOME, now, visits=visits)}
        text, _ = answer_location(LocationQuestion("arrived", [self.liz.id], self.school.id), views, self.store.places(), now)
        self.assertTrue(text.startswith("Elizabeth got to school at"), text)
        self.assertIn("then left", text)
        text, _ = answer_location(LocationQuestion("left", [self.liz.id], self.school.id), views, self.store.places(), now)
        self.assertTrue(text.startswith("Elizabeth left school at"), text)

    def test_who_is_home(self):
        now = time.time()
        views = {self.liz.id: self.view(self.liz, self.salon, SALON, now),
                 self.mom.id: self.view(self.mom, self.home, HOME, now)}
        text, _ = answer_location(LocationQuestion("list"), views, self.store.places(), now)
        self.assertIn("Home (by phone): Katherine.", text)
        self.assertIn("Out: Elizabeth (at the salon)", text)

    def test_alert_text(self):
        from sentinel.engine.location import Change

        self.assertEqual(change_message(Change("arrived", self.liz, self.salon, 0), []), "📍 Elizabeth got to the salon.")
        self.assertEqual(change_message(Change("arrived", self.liz, self.home, 0), []), "🏠 Elizabeth got home.")
        text = change_message(Change("left", self.liz, self.home, 0), [CarLine("Elizabeth's Fusion", True)])
        self.assertEqual(text, "👋 Elizabeth left home. (Elizabeth's Fusion is still here.)")
        self.assertIn("now sharing", change_message(Change("started", self.liz, None, 0), []))


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
        sm.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.engine = Engine(sm)
        self.server = WebServer(sm, FrameBroker(), engine=self.engine)
        assert self.server.start()
        self.client = self.server._app.test_client()
        loc = self.engine.locations
        loc.save_place("Home", *HOME)
        self.liz = loc.person(loc.save_person("Elizabeth"))
        loc.phone_places_sent(self.liz.id, loc.places())  # her phone has the places (see test_places_reach_the_phones)
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
        r = self.post("elizabeth", "wrong", fix(*HOME, time.time()))
        self.assertEqual(r.status_code, 401)
        r = self.post("elizabeth", self.liz.token, fix(*HOME, time.time()))
        self.assertEqual((r.status_code, r.get_json()), (200, []))
        self.assertEqual(self.engine.locations.fix(self.liz.id)["place_id"], self.engine.locations.places()[0].id)
        self.wait_for(1)
        self.assertTrue(any("now sharing" in t for t in self.sent), self.sent)
        self.post("elizabeth", self.liz.token, fix(HOME[0] + 0.05, HOME[1], time.time() + 1))
        self.wait_for(2)
        self.assertIn("👋 Elizabeth left home.", self.sent)
        self.assertIn("Elizabeth is about", self.engine.family.answer_location_question(self.engine.family.location_question("where is elizabeth"))[0])

    def test_battery_question_by_voice(self):
        self.post("elizabeth", self.liz.token, dict(fix(*HOME, time.time()), batt=62))
        for text in ("What's Elizabeth's battery percentage?", "What is Elizabeth's phone battery at?"):
            self.assertEqual(self.engine.answer_voice(text, lambda _s: None).text, "Elizabeth's phone battery is at 62%.", text)

    def test_location_map_for_the_tv(self):
        """"Show me Elizabeth's location" puts her on a map on the TV (#map=<id>), like a camera."""
        lookup, liz = self.engine._camera_page, f"#map={self.liz.id}"
        self.assertEqual(lookup("Elizabeth's location"), ("Elizabeth's location", liz))
        self.assertEqual(lookup("where elizabeth is"), ("Elizabeth's location", liz))
        self.assertEqual(lookup("elizabeth on the map"), ("Elizabeth's location", liz))
        self.assertEqual(lookup("everyone's location"), ("everyone's location", "#map=all"))
        self.assertEqual(lookup("map"), ("everyone's location", "#map=all"))
        for not_it in ("weather", "front door", "grace's location", "elizabeth"):
            self.assertIsNone(lookup(not_it), not_it)
        self.post("elizabeth", self.liz.token, fix(*HOME, time.time()))
        view = self.engine.family.map_view(str(self.liz.id))
        self.assertEqual([(p["name"], p["label"], p["pet"]) for p in view["people"]], [("Elizabeth", "at home", False)])
        self.assertEqual(view["home"], list(HOME))
        self.assertEqual(len(self.engine.family.map_view("all")["people"]), 1)
        self.assertEqual(self.engine.family.map_view("99")["people"], [])
        self.assertEqual(self.client.get("/api/where?p=all").status_code, 401)  # signed in only

    def test_profile_picture(self):
        """People page "Add picture": kept as a small square JPEG, served to signed-in pages, gone with the person."""
        import cv2
        import numpy as np

        family = self.engine.family
        ok, jpeg = cv2.imencode(".png", np.full((300, 500, 3), 200, np.uint8))
        url = "data:image/png;base64," + base64.b64encode(jpeg.tobytes()).decode()
        self.assertEqual(family.set_photo(self.liz.id, url), (True, "Saved Elizabeth's picture."))
        saved = cv2.imread(self.engine.locations.photo_path(self.liz.id))
        self.assertEqual(saved.shape[:2], (256, 256))
        self.assertIsNotNone(family.map_view("all")["people"][0]["photo"])
        self.assertFalse(family.set_photo(self.liz.id, "data:image/png;base64,bm90IGEgcGljdHVyZQ==")[0])
        self.assertFalse(family.set_photo(self.liz.id, "<svg onload=...>")[0])
        self.assertFalse(family.set_photo(999, url)[0])
        self.assertEqual(self.client.get(f"/person_photo/{self.liz.id}.jpg").status_code, 401)  # signed in only
        self.assertEqual(family.set_photo(self.liz.id, None)[0], True)
        self.assertIsNone(self.engine.locations.photo_version(self.liz.id))
        family.set_photo(self.liz.id, url)
        self.engine.locations.delete_person(self.liz.id)
        self.assertIsNone(self.engine.locations.photo_version(self.liz.id))

    def test_lost_dog(self):
        loc = self.engine.locations
        dog = loc.person(loc.save_person("Grace"))
        loc.set_tracker(dog.id, "!a1cacb2a")
        pins = []
        patcher = patch.object(type(self.engine.telegram), "send_location",
                               lambda _self, chat, lat, lon: pins.append((lat, lon)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ask = [{"_type": "cmd", "action": "reportLocation"}]
        now = time.time()
        self.post(dog.username, dog.token, fix(*HOME, now - 600))
        self.assertEqual(self.post(dog.username, dog.token, {"_type": "checkin"}).get_json(), [])  # the base, every 5 s

        house = self.server._app.test_client()
        house.post("/login", data={"username": "alex", "password": "pw", "remember": "1"})
        r = house.post("/api/lost/start", json={"ids": [dog.id]}, headers={"X-Sentinel-Request": "1"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.wait_for(2)
        alert = next(t for t in self.sent if t.startswith("🚨"))
        self.assertIn("Grace is lost", alert)
        self.assertIn("https://sentinel.example.org/lost", alert)
        for _ in range(50):
            if pins:
                break
            time.sleep(0.02)
        self.assertEqual(pins, [HOME])  # where she was last heard
        view = house.get("/api/lost").get_json()
        self.assertEqual((view["pets"][0]["name"], bool(view["pets"][0]["lost_since"])), ("Grace", True))
        self.assertEqual(self.engine.family.settings["lost_pets"], {str(dog.id): view["pets"][0]["lost_since"]})  # kept
        self.assertEqual(self.post(dog.username, dog.token, {"_type": "checkin"}).get_json(), ask)  # ping the collar

        # she moves: the family out looking gets a fresh pin (not for every small step)
        n = len(self.sent)
        self.post(dog.username, dog.token, fix(HOME[0] + 0.0005, HOME[1], now - 500))
        self.post(dog.username, dog.token, fix(HOME[0] + 0.0006, HOME[1], now - 450))
        far = (HOME[0] + 0.003, HOME[1])
        self.post(dog.username, dog.token, fix(*far, now - 60))
        self.wait_for(n + 1)
        self.assertTrue(any(t.startswith("🐕 Grace is here now") for t in self.sent[n:]), self.sent[n:])
        self.assertEqual(len(house.get("/api/lost").get_json()["pets"][0]["trail"]), 4)

        r = house.post("/api/lost/found", json={"ids": [dog.id]}, headers={"X-Sentinel-Request": "1"})
        self.assertIn("is found", r.get_json()["message"])
        self.wait_for(len(self.sent) + 1)
        self.assertIn("✅ Grace found. Back to normal checking.", self.sent)
        self.assertEqual(self.engine.family.settings["lost_pets"], {})
        self.engine.family._location_asked.clear()
        self.assertEqual(self.post(dog.username, dog.token, {"_type": "checkin"}).get_json(), [])
        self.assertIn("wasn't marked lost", self.engine.family.set_lost("found", [loc.person(dog.id)]))

    def test_places_reach_the_phones(self):
        loc = self.engine.locations
        home = loc.places()[0]
        school = loc.save_place("School", HOME[0] + 0.02, HOME[1])
        ask = [{"_type": "cmd", "action": "waypoints"}]
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), ask)
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), [])  # asked once
        # an Android phone set up with Home under its own "tst": it keeps it (a new one would be a second Home)
        phone = {"_type": "waypoints", "waypoints": [{"_type": "waypoint", "desc": "home", "lat": HOME[0], "lon": HOME[1],
                                                      "rad": 150, "tst": 1762000000}]}
        reply = self.post("elizabeth", self.liz.token, phone).get_json()
        self.assertEqual(reply[0]["action"], "setWaypoints")
        sent = {w["desc"]: w["tst"] for w in reply[0]["waypoints"]["waypoints"]}
        self.assertEqual(sent, {"Home": 1762000000, "School": WAYPOINT_TST_BASE + school})
        self.engine.family._places_asked.clear()
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), [])  # up to date

        # renamed: the old name comes off (an iPhone deletes a place sent with impossible coordinates)
        loc.save_place("Lincoln High", HOME[0] + 0.02, HOME[1], place_id=school)
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), ask)
        phone["waypoints"].append({"_type": "waypoint", "desc": "School", "lat": HOME[0] + 0.02, "lon": HOME[1],
                                   "rad": 150, "tst": WAYPOINT_TST_BASE + school})
        phone["waypoints"].append({"_type": "waypoint", "desc": "Gym", "lat": 1, "lon": 1, "rad": 50, "tst": 5})
        out = self.post("elizabeth", self.liz.token, phone).get_json()[0]["waypoints"]["waypoints"]
        self.assertEqual([(w["desc"], w["lat"], w["tst"]) for w in out],
                         [("Home", round(home.lat, 6), 1762000000),
                          ("Lincoln High", round(HOME[0] + 0.02, 6), WAYPOINT_TST_BASE + school),  # Android: replaced
                          ("School", 360.0, WAYPOINT_TST_BASE + school)])  # iPhone: removed; her own "Gym" left alone
        # deleted: off the iPhone too
        self.engine.family._places_asked.clear()
        loc.delete_place(school)
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), ask)
        phone["waypoints"][1]["desc"] = "Lincoln High"
        out = self.post("elizabeth", self.liz.token, phone).get_json()[0]["waypoints"]["waypoints"]
        self.assertEqual([(w["desc"], w["lat"]) for w in out], [("Home", round(home.lat, 6)), ("Lincoln High", 360.0)])

    def test_a_spot_she_keeps_going_to_is_offered_as_a_place(self):
        loc, family = self.engine.locations, self.engine.family
        now = time.time()
        for d in (3, 2, 1):
            loc.record(self.liz, fix(*GYM, now - d * 86400))
            loc.record(self.liz, fix(*GYM, now - d * 86400 + 60))
            loc.record(self.liz, fix(*HOME, now - d * 86400 + 5400))
        pins, asks = [], []
        for name, fn in (("send_location", lambda _s, chat, lat, lon: pins.append((lat, lon)) or True),
                         ("send_text_with_buttons", lambda _s, chat, text, buttons: asks.append((text, buttons)) or True),
                         ("edit_text", lambda _s, chat, mid, text: self.sent.append(text) or True),
                         ("answer_button", lambda _s, cid, text="": True)):
            patcher = patch.object(type(self.engine.telegram), name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertTrue(family.suggest_spot(now))
        self.assertEqual(pins, [GYM])
        text, buttons = asks[0]
        self.assertIn("Elizabeth has stopped at this spot on 3 different days lately", text)
        self.assertFalse(family.suggest_spot(now))  # asked already: not again tomorrow

        self.engine.handle_button("111", 5, buttons[0][0][1], "cb")  # 📍 Save it
        self.assertIn("What should I call this place?", self.sent[-1])
        self.assertTrue(family.name_pinned_place("111", "the gym"))
        self.assertEqual([p.name for p in loc.places()], ["Home", "the gym"])
        self.assertFalse(family.suggest_spot(now))  # a place now

        loc.delete_place(loc.places()[1].id)
        with loc._connect() as conn:
            conn.execute("DELETE FROM spot_asks")
        self.assertTrue(family.suggest_spot(now))
        self.engine.handle_button("111", 6, asks[-1][1][0][1][1], "cb")  # No thanks
        self.assertIn("won't ask about that spot again", self.sent[-1])
        self.assertFalse(family.suggest_spot(now + 30 * 86400 + 1))  # not even weeks later

    def test_stale_position_asks_phone_for_a_fresh_one(self):
        ask = [{"_type": "cmd", "action": "reportLocation"}]
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time() - 3600)).get_json(), ask)
        # the phone's next check-in repeats the old reading: don't keep nagging it
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time() - 3600)).get_json(), [])
        self.engine.family._location_asked.clear()
        self.assertEqual(self.post("elizabeth", self.liz.token, fix(*HOME, time.time())).get_json(), [])

    def test_car_seen_driving_off_is_not_still_here(self):
        # 2026-10-30 13:54: "Elizabeth's Fusion is leaving", then her phone left home a minute later, before
        # the driveway check's three missed scans had marked the car gone -> "(Fusion is still here.)"
        from types import SimpleNamespace
        from sentinel.engine.household import Parked
        from sentinel.engine.questions import VehicleStatus

        vid = self.engine.household.save_vehicle("Elizabeth's Fusion", owner="Elizabeth")
        spot = Parked(box=(0, 0, 10, 10), first_seen=0, last_seen=0, vehicle_id=vid, confirmed=True)
        self.engine.presence = SimpleNamespace(where_is=lambda v: ("garage", spot) if v == vid else None)
        statuses = {vid: VehicleStatus("Elizabeth's Fusion", True, "Garage", None, None, None)}
        self.assertTrue(self.engine.family._cars_of(self.liz, statuses)[0].here)
        spot.leaving_at, spot.empty_since = time.time(), time.time()  # camera: it pulled out, spot empty
        self.assertFalse(self.engine.family._cars_of(self.liz, statuses)[0].here)
        spot.empty_since = 0.0  # it came straight back (or it was a false "leaving")
        self.assertTrue(self.engine.family._cars_of(self.liz, statuses)[0].here)

    def test_wrong_logins_get_blocked(self):
        for _ in range(20):
            self.post("elizabeth", "guess", {}, ip="198.51.100.99")
        self.assertEqual(self.post("elizabeth", self.liz.token, {}, ip="198.51.100.99").status_code, 429)
        self.assertEqual(self.post("elizabeth", self.liz.token, {}, ip="198.51.100.8").status_code, 200)

    def test_setup_link_page(self):
        self.assertEqual(self.client.get("/join/" + "x" * 22).status_code, 404)
        ok, url = self.engine.family.people_action(self.liz.id, "invite")
        self.assertTrue(url.startswith("https://sentinel.example.org/join/"))
        code = url.rsplit("/", 1)[1]
        page = self.client.get("/join/" + code).get_data(as_text=True)
        self.assertIn("Hi Elizabeth", page)
        self.assertIn("I agree", page)
        self.assertIn("owntracks:///config?inline=", page)
        self.assertIn("https://sentinel.example.org/owntracks", page)
        self.assertEqual(self.client.get(f"/join/{code}/status").get_json(), {"sharing": False})
        cfg = json.loads(self.client.get(f"/join/{code}/sentinel.otrc").get_data(as_text=True))
        self.assertEqual(cfg["password"], self.liz.token)

    def test_setup_visits_are_logged_with_the_phone_type(self):
        from sentinel.logging_utils import app_logger
        ok, url = self.engine.family.people_action(self.liz.id, "invite")
        code = url.rsplit("/", 1)[1]
        google_app = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 GSA/350.0 Mobile/15E148"
        self.client.get("/join/" + code, headers={"User-Agent": google_app})
        with open(app_logger.log_path) as f:
            opened = [json.loads(line) for line in f if "location_setup_opened" in line]
        self.assertEqual((opened[-1]["person"], opened[-1]["phone"]), ("Elizabeth", "iPhone (inside another app)"))
        self.assertNotIn(self.liz.token, json.dumps(opened[-1]))

    def test_people_api_needs_sign_in(self):
        self.assertEqual(self.client.get("/api/people").status_code, 401)
        self.assertEqual(self.client.get("/api/family").status_code, 401)
        self.assertEqual(self.client.get("/people").status_code, 302)

    def test_people_and_places_from_the_page(self):
        self.assertFalse(self.engine.family.people_save({"name": "elizabeth"})[0])  # already there
        ok, _ = self.engine.family.people_save({"name": "Katherine", "aliases": "Kate, Mom"})
        self.assertTrue(ok)
        self.assertFalse(self.engine.family.place_save({"name": "the salon", "where": "the salon"})[0])
        ok, _ = self.engine.family.place_save({"name": "the salon", "where": "41.157, -88.178", "radius": 5})
        self.assertTrue(ok)
        salon = next(p for p in self.engine.locations.places() if p.name == "the salon")
        self.assertEqual(salon.radius, 50)  # clamped
        ok, msg = self.engine.family.place_save({"name": "school", "where": "41°08'00\"N 88°10'40\"W"})
        self.assertEqual((ok, msg), (True, "Saved — about 1.3 miles north of home"))
        self.assertFalse(self.engine.family.place_save({"name": "home", "where": "41, -83"})[0])  # Home exists
        view = self.engine.family.people_view("http://localhost")
        self.assertEqual([p["name"] for p in view["people"]], ["Elizabeth", "Katherine"])
        self.assertNotIn(self.liz.token, json.dumps(view))  # the phone password only goes in the setup link

    def test_telegram_pin_then_name(self):
        self.engine.family.on_telegram_pin("111", 41.12, -88.20)
        self.assertIn("What should I call this place?", self.sent[-1])
        self.assertFalse(self.engine.family.name_pinned_place("111", "did anyone come by?"))  # a question passes through
        self.assertTrue(self.engine.family.name_pinned_place("111", "School"))
        self.assertIn("Saved “School”", self.sent[-1])
        self.assertFalse(self.engine.family.name_pinned_place("111", "hello"))  # only one name per pin
        self.assertTrue(any(p.name == "School" for p in self.engine.locations.places()))


if __name__ == "__main__":
    unittest.main()
