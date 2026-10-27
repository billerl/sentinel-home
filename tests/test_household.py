"""Tests for household vehicles: recognising the family's cars, presence, and questions about them."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import types
import unittest

import numpy as np

from sentinel.detection.objects import Detection
from sentinel.engine.household import HouseholdStore, PresenceTracker, Vehicle
from sentinel.engine.questions import (HouseholdQuestion, VehicleStatus, answer_household, household_question,
                                       mentioned_vehicles)

COLOUR = np.zeros((360, 640, 3), dtype=np.uint8)
COLOUR[:] = (30, 120, 60)  # saturated = daytime colour picture
NIGHT = np.full((360, 640, 3), 90, dtype=np.uint8)  # grey = infrared


def unit(seed: int, base=None, noise=0.0) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=512) if base is None else base + np.random.default_rng(seed).normal(size=512) * noise
    return (v / np.linalg.norm(v)).astype(np.float32)


SIERRA, FUSION = unit(1), unit(2)


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-hh-")
        self.store = HouseholdStore(self.dir)
        self.sierra = self.store.save_vehicle("Luke's Sierra", "Luke", [], "pickup")
        self.fusion = self.store.save_vehicle("Abigail's Fusion", "Abigail", ["Abby"], "sedan")
        for i in range(4):
            self.store.add_sighting(0, "cam", "Garage", COLOUR, (10, 10, 100, 80), unit(10 + i, SIERRA, 0.008), self.sierra)
            self.store.add_sighting(0, "cam", "Garage", COLOUR, (10, 10, 100, 80), unit(20 + i, FUSION, 0.008), self.fusion)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_identifies_known_cars_and_refuses_strangers(self):
        self.assertEqual(self.store.identify(unit(99, SIERRA, 0.008), False)[0], self.sierra)
        self.assertEqual(self.store.identify(unit(98, FUSION, 0.008), False)[0], self.fusion)
        self.assertIsNone(self.store.identify(unit(97), False)[0])  # someone else's car
        between = (SIERRA + FUSION) / np.linalg.norm(SIERRA + FUSION)
        self.assertIsNone(self.store.identify(between, False)[0])  # too close to call

    def test_night_photos_are_used_at_night(self):
        night_look = unit(5)
        self.store.add_sighting(0, "cam", "Garage", NIGHT, (10, 10, 100, 80), night_look, self.sierra)
        self.assertEqual(self.store.identify(unit(96, night_look, 0.008), True)[0], self.sierra)
        self.assertIsNone(self.store.identify(unit(96, night_look, 0.008), False)[0])  # daytime refs don't look like that

    def test_groups_and_labelling(self):
        day = 86400
        ids = [self.store.add_sighting(time.time() - i * day, "cam", "Garage", COLOUR, (0, 0, 90, 70),
                                       unit(50 + i, FUSION, 0.006)) for i in range(3)]
        ids.append(self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(77)))
        groups = self.store.suggestion_groups()
        self.assertEqual(len(groups), 1)  # the one-off car (seen on one day) isn't asked about
        self.assertEqual(sorted(groups[0]["ids"]), sorted(ids[:3]))
        self.assertEqual(groups[0]["days"], 3)
        self.assertTrue(os.path.isfile(self.store.crop_path(ids[0])))
        self.store.assign(ids[:3], self.fusion)
        self.assertEqual(self.store.suggestion_groups(), [])

    def test_night_photos_are_not_asked_about(self):
        for i in range(3):
            self.store.add_sighting(time.time() - i * 86400, "cam", "Garage", NIGHT, (0, 0, 90, 70), unit(80 + i, unit(8), 0.005))
        self.assertEqual(self.store.suggestion_groups(), [])

    def test_clear_matches_are_filed_automatically(self):
        sure = self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(60, SIERRA, 0.004))
        unsure = self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(61))
        self.assertEqual(self.store.auto_file(), 1)
        labelled = {s["id"]: s["vehicle_id"] for s in self.store.sightings(labelled=True)}
        self.assertEqual(labelled.get(sure), self.sierra)
        self.assertNotIn(unsure, labelled)

    def test_not_ours_hides_look_alikes(self):
        rv = unit(70)
        first = self.store.add_sighting(time.time(), "cam", "Front Door", COLOUR, (0, 0, 90, 70), rv)
        self.store.assign([first], None, ignore=True)
        later = [self.store.add_sighting(time.time() - i * 86400, "cam", "Front Door", COLOUR, (0, 0, 90, 70),
                                         unit(71 + i, rv, 0.005)) for i in range(3)]
        self.assertEqual(self.store.auto_file(), 3)
        self.assertEqual(self.store.suggestion_groups(), [])
        self.assertEqual([s for s in self.store.sightings() if s["id"] in later], [])


class TestLookAlikes(unittest.TestCase):
    """Cars CLIP can't tell apart (same shape from above): colour and who's already parked decide."""

    def setUp(self):
        from sentinel.engine.household import crop_colour
        self.crop_colour = crop_colour
        self.dir = tempfile.mkdtemp(prefix="sentinel-hh-")
        self.store = HouseholdStore(self.dir)
        self.blue = np.zeros((360, 640, 3), np.uint8); self.blue[:] = (200, 70, 20)
        self.red = np.zeros((360, 640, 3), np.uint8); self.red[:] = (20, 40, 200)
        self.shape = unit(3)
        self.edge = self.store.save_vehicle("Stephanie's Edge", "Stephanie", [], "suv")
        self.fusion = self.store.save_vehicle("Abigail's Fusion", "Abigail", [], "sedan")
        for i in range(4):
            self.store.add_sighting(0, "cam", "Garage", self.blue, (10, 10, 100, 80), unit(10 + i, self.shape, 0.01), self.edge)
            self.store.add_sighting(0, "cam", "Garage", self.red, (10, 10, 100, 80), unit(20 + i, self.shape, 0.01), self.fusion)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_colour_breaks_a_tie(self):
        look = unit(30, self.shape, 0.01)
        self.assertIsNone(self.store.identify(look, False)[0])  # shape alone: too close to call
        self.assertEqual(self.store.identify(look, False, colour=self.crop_colour(self.blue, (10, 10, 100, 80)))[0], self.edge)
        self.assertEqual(self.store.identify(look, False, colour=self.crop_colour(self.red, (10, 10, 100, 80)))[0], self.fusion)
        self.assertIsNone(self.crop_colour(NIGHT, (10, 10, 100, 80)))  # no colour in infrared
        self.assertIsNone(self.store.identify(unit(31), False, colour=self.crop_colour(self.red, (10, 10, 100, 80)))[0])  # a stranger stays a stranger

    def test_a_car_parked_elsewhere_is_ruled_out(self):
        look = unit(30, self.shape, 0.01)
        self.assertEqual(self.store.identify(look, False, exclude={self.edge})[0], self.fusion)


class TestNotOursDoesntSnowball(TestMatching):
    def test_hidden_photos_dont_hide_more(self):
        stranger = unit(70)
        first = self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), stranger)
        self.store.assign([first], None, ignore=True)
        # Each photo is like the previous one but drifts away from the stranger the user marked.
        chain, look = [], stranger
        for i in range(6):
            look = unit(200 + i, look, 0.0145)  # ~0.95 like the one before
            chain.append(self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), look))
        self.assertLess(float(stranger @ look), 0.85)
        for _ in range(6):  # hourly
            self.store.auto_file()
        visible = {s["id"] for s in self.store.sightings()}
        self.assertIn(chain[-1], visible)  # far from what the user marked: not hidden by a hidden photo
        self.assertEqual(len(self.store.ignored_embeddings()), 1)  # only the user's own mark counts

    def test_family_car_isnt_hidden_by_a_look_alike_stranger(self):
        stranger = unit(71, SIERRA, 0.02)  # a neighbour's truck, very like the Sierra
        first = self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), stranger)
        self.store.assign([first], None, ignore=True)
        mine = self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(72, SIERRA, 0.004))
        self.store.auto_file()
        self.assertEqual({s["id"]: s["vehicle_id"] for s in self.store.sightings(labelled=True)}.get(mine), self.sierra)


class _Det:
    """Returns whatever boxes the test sets."""

    def __init__(self):
        self.boxes = []
        self.people = []

    def detect(self, frame, min_score=0.45):
        return [Detection("car", 0.9, b) for b in self.boxes] + [Detection("person", 0.9, b) for b in self.people]


class _Clf:
    def __init__(self):
        self.looks = {}  # box -> embedding

    def embed(self, crop):
        return self.next


class TestPresence(unittest.TestCase):
    BOX = (100, 100, 200, 150)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-pres-")
        self.store = HouseholdStore(self.dir)
        self.sierra = self.store.save_vehicle("Luke's Sierra", "Luke", [], "pickup")
        for i in range(3):
            self.store.add_sighting(0, "cam", "Garage", COLOUR, self.BOX, unit(10 + i, SIERRA, 0.008), self.sierra)
        self.det, self.clf = _Det(), _Clf()
        self.clf.next = unit(40, SIERRA, 0.008)
        self.changes = []
        self.cam = types.SimpleNamespace(host="192.168.0.9", display_name="Garage")
        self.tracker = PresenceTracker(self.store, self.det, self.clf, cameras=lambda: [self.cam],
                                       on_change=lambda kind, cam, v, ts, jpeg: self.changes.append((kind, v.name, ts)))
        self.t = 1000.0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, boxes, frame=COLOUR):
        self.det.boxes = boxes
        self.tracker.scan(self.cam, frame, self.t)
        self.t += 60

    def test_arrives_and_leaves(self):
        self.scan([])  # baseline: empty driveway
        self.scan([self.BOX])
        self.assertEqual(self.changes, [])  # one sighting isn't parked yet
        self.scan([self.BOX])
        self.assertEqual(self.changes, [("arrived", "Luke's Sierra", 1060.0)])
        self.assertEqual(self.tracker.where_is(self.sierra)[0], "192.168.0.9")
        self.scan([])  # someone walks in front of it
        self.scan([self.BOX])
        self.assertEqual(len(self.changes), 1)  # still the same visit
        self.scan([]); self.scan([]); self.scan([])
        self.assertEqual(self.changes[-1], ("left", "Luke's Sierra", 1240.0))  # last time it was seen
        self.assertIsNone(self.tracker.where_is(self.sierra))

    def test_a_missed_scan_keeps_the_spot(self):
        self.scan([])
        for _ in range(3):
            self.scan([self.BOX])
        self.scan([])  # the night detector missed it once
        self.assertEqual([p.box for p in self.tracker.spots(self.cam.host)], [self.BOX])
        self.scan([]); self.scan([])
        self.assertEqual(self.tracker.spots(self.cam.host), [])  # three misses: it left

    def test_cars_already_parked_at_start_did_not_arrive(self):
        self.scan([self.BOX]); self.scan([self.BOX])
        self.assertEqual(self.changes, [])
        self.assertIsNotNone(self.tracker.where_is(self.sierra))

    def test_unknown_in_the_dark_is_named_later_with_its_arrival_time(self):
        self.scan([])
        self.clf.next = unit(60)  # infrared look it doesn't know yet
        self.scan([self.BOX], NIGHT); self.scan([self.BOX], NIGHT)
        self.assertEqual(self.changes, [])
        self.assertEqual(self.tracker.present()["192.168.0.9"][0].vehicle_id, None)
        self.clf.next = unit(41, SIERRA, 0.008)  # morning: colour again
        self.scan([self.BOX])
        self.assertEqual(self.changes, [])  # one match could be luck: wait for a second
        self.scan([self.BOX])
        self.assertEqual(self.changes, [("arrived", "Luke's Sierra", 1060.0)])

    def test_known_parked_car_files_its_night_photo(self):
        self.scan([]); self.scan([self.BOX]); self.scan([self.BOX]); self.scan([self.BOX])  # recognised by day
        self.clf.next = unit(62)  # dusk: infrared look it has never seen
        self.scan([self.BOX], NIGHT)
        night_refs = [s for s in self.store.sightings(labelled=True) if s["night"]]
        self.assertEqual(len(night_refs), 1)
        self.assertEqual(night_refs[0]["vehicle_id"], self.sierra)
        self.assertEqual(self.store.identify(unit(63, unit(62), 0.004), True)[0], self.sierra)  # next night: known

    def test_small_far_vehicles_are_ignored(self):
        self.scan([]); self.scan([(600, 10, 20, 15)]); self.scan([(600, 10, 20, 15)])
        self.assertEqual(self.tracker.present().get("192.168.0.9"), [])

    def test_state_survives_restart(self):
        path = os.path.join(self.dir, "presence.json")
        self.tracker.state_path = path
        self.t = time.time() - 300  # only recent parking is restored
        self.scan([]); self.scan([self.BOX]); self.scan([self.BOX])
        self.tracker._save()
        again = PresenceTracker(self.store, self.det, self.clf, cameras=lambda: [self.cam],
                                on_change=lambda *a: self.changes.append(a), state_path=path)
        self.assertIsNotNone(again.where_is(self.sierra))


class TestPresenceFlipFlops(TestPresence):
    """2026-10-25: cars 'got home' and 'left' over and over while parked in the Garage view."""

    def test_doors_open_and_people_loading_dont_make_it_leave(self):
        self.scan([]); self.scan([self.BOX]); self.scan([self.BOX])
        doors_open = (60, 100, 290, 160)  # box much wider: IoU with the parked box ~0.6 -> 0.3
        for _ in range(4):
            self.scan([doors_open])
        self.det.people = [(150, 120, 40, 100)]  # someone standing in front hides the car completely
        for _ in range(4):
            self.scan([])
        self.det.people = []
        self.scan([self.BOX])
        self.assertEqual([c[0] for c in self.changes], ["arrived"])

    def test_one_lucky_match_doesnt_name_a_car(self):
        self.scan([])
        looks = [unit(70), unit(41, SIERRA, 0.008), unit(71), unit(72)]  # matches the Sierra only once
        for look in looks:
            self.clf.next = look
            self.scan([self.BOX])
        self.assertEqual(self.changes, [])
        self.assertIsNone(self.tracker.present()["192.168.0.9"][0].vehicle_id)

    def test_same_car_in_two_spots_arrives_and_leaves_once(self):
        other = (400, 100, 200, 150)
        self.scan([]); self.scan([self.BOX]); self.scan([self.BOX])
        self.scan([self.BOX, other]); self.scan([self.BOX, other]); self.scan([self.BOX, other])
        self.assertEqual([c[0] for c in self.changes], ["arrived"])  # the second spot looks the same: not again
        self.scan([self.BOX]); self.scan([self.BOX]); self.scan([self.BOX])  # that one went
        self.assertEqual([c[0] for c in self.changes], ["arrived"])  # the car is still home
        self.scan([]); self.scan([]); self.scan([])
        self.assertEqual([c[0] for c in self.changes], ["arrived", "left"])

    def test_unsure_photos_arent_filed_under_the_car(self):
        self.scan([]); self.scan([self.BOX]); self.scan([self.BOX])  # the Sierra, recognised
        filed = len([x for x in self.store.sightings(labelled=True) if x["vehicle_id"] == self.sierra])
        self.clf.next = unit(80, SIERRA, 0.05)  # an hour later: only vaguely like it (another car in the spot?)
        self.t += 3600
        self.scan([self.BOX])
        self.assertEqual(len([x for x in self.store.sightings(labelled=True) if x["vehicle_id"] == self.sierra]), filed)


VEHICLES = [Vehicle(1, "Abigail's Fusion", "Abigail", ["Abby"], "sedan"),
            Vehicle(2, "Lincoln's Jeep", "Lincoln", [], "suv"),
            Vehicle(3, "Stephanie's Edge", "Stephanie", ["Steph"], "suv"),
            Vehicle(4, "Luke's Sierra", "Luke", [], "pickup"),
            Vehicle(5, "the blue Civic", "", [], "sedan")]


class TestHouseholdQuestions(unittest.TestCase):
    def q(self, text):
        return household_question(text, VEHICLES)

    def test_understands(self):
        self.assertEqual(self.q("When did Abigail get home?"), HouseholdQuestion("arrived", [1]))
        self.assertEqual(self.q("what time did abby get back"), HouseholdQuestion("arrived", [1]))
        self.assertEqual(self.q("When did Lincoln leave?"), HouseholdQuestion("left", [2]))
        self.assertEqual(self.q("did steph leave yet"), HouseholdQuestion("left", [3]))
        self.assertEqual(self.q("Is Steph home?"), HouseholdQuestion("present", [3]))
        self.assertEqual(self.q("is stephanie here"), HouseholdQuestion("present", [3]))
        self.assertEqual(self.q("where is the blue civic"), HouseholdQuestion("present", [5]))
        self.assertEqual(self.q("is Luke's truck home?"), HouseholdQuestion("present", [4]))
        self.assertEqual(self.q("when did abigail or lincoln get home?"), HouseholdQuestion("arrived", [1, 2]))
        for text in ("What cars are in the driveway?", "which vehicles are home", "who's home?", "Who is home",
                     "is anyone home?"):
            self.assertEqual(self.q(text), HouseholdQuestion("list"), text)

    def test_leaves_other_messages(self):
        for text in ("did anyone come by?", "videos today", "status", "any deliveries today?", "lucky day"):
            self.assertIsNone(self.q(text), text)
        self.assertEqual(mentioned_vehicles("I love lincolnshire", VEHICLES), [])

    def test_answers(self):
        now = time.mktime((2026, 10, 21, 20, 0, 0, 0, 0, -1))
        at = lambda h, m=0: time.mktime((2026, 10, 21, h, m, 0, 0, 0, -1))  # noqa: E731
        statuses = {
            1: VehicleStatus("Abigail's Fusion", True, "Garage", at(17, 42), at(17, 42), at(7, 8)),
            2: VehicleStatus("Lincoln's Jeep", False, None, None, at(12, 0), at(18, 5)),
            3: VehicleStatus("Stephanie's Edge", True, "Garage", None, None, None),
        }
        self.assertEqual(answer_household(HouseholdQuestion("arrived", [1]), statuses, [], now),
                         "Abigail's Fusion got home today at 5:42pm and is still here.")
        self.assertEqual(answer_household(HouseholdQuestion("arrived", [2]), statuses, [], now),
                         "Lincoln's Jeep got home today at 12:00pm, then left again today at 6:05pm.")
        self.assertIn("already parked when I started", answer_household(HouseholdQuestion("arrived", [3]), statuses, [], now))
        self.assertEqual(answer_household(HouseholdQuestion("left", [2]), statuses, [], now),
                         "Lincoln's Jeep left today at 6:05pm.")
        self.assertEqual(answer_household(HouseholdQuestion("present", [2]), statuses, [], now),
                         "No — Lincoln's Jeep isn't here; it left today at 6:05pm.")
        listing = answer_household(HouseholdQuestion("list"), statuses, ["Front Door"], now)
        self.assertIn("Home now: Abigail's Fusion (since today at 5:42pm), Stephanie's Edge.", listing)
        self.assertIn("Away: Lincoln's Jeep (left today at 6:05pm).", listing)
        self.assertIn("Also 1 car I don't recognise (Front Door).", listing)
        self.assertIn("Vehicles page", answer_household(HouseholdQuestion("list"), {}, [], now))


class TestEngineHousehold(unittest.TestCase):
    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-hhe-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        self.engine = Engine(sm)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_validates(self):
        e = self.engine
        self.assertFalse(e.household_save({"name": ""})[0])
        self.assertFalse(e.household_save({"name": "<script>"})[0])
        self.assertFalse(e.household_save({"name": "Ok", "kind": "tank"})[0])
        ok, vid = e.household_save({"name": "Luke's Sierra", "owner": "Luke", "aliases": "Lukey, ", "kind": "pickup"})
        self.assertTrue(ok)
        v = e.household.vehicle(int(vid))
        self.assertEqual((v.name, v.owner, v.aliases, v.kind), ("Luke's Sierra", "Luke", ["Lukey"], "pickup"))
        self.assertFalse(e.household_assign({"ids": "1", "vehicle": int(vid)})[0])
        self.assertFalse(e.household_assign({"ids": [1], "vehicle": 999})[0])
        view = e.household_view()
        self.assertEqual(view["vehicles"][0]["name"], "Luke's Sierra")
        self.assertTrue(e.household_delete(int(vid))[0])
        self.assertEqual(e.household_view()["vehicles"], [])


if __name__ == "__main__":
    unittest.main()



class TestFamilyCarIsNotADelivery(unittest.TestCase):
    def test_known_car_overrides_delivery_guess(self):
        from sentinel.engine.objects import Track
        from sentinel.detection.vehicle_type import VehicleType
        from sentinel.engine.camera import CameraPipeline

        d = tempfile.mkdtemp(prefix="sentinel-fam-")
        self.addCleanup(shutil.rmtree, d, True)
        store = HouseholdStore(d)
        truck = store.save_vehicle("Luke's Truck", "Luke", [], "pickup")
        for i in range(3):
            store.add_sighting(0, "cam", "Garage", COLOUR, (10, 10, 100, 80), unit(10 + i, SIERRA, 0.008), truck)
        clf = types.SimpleNamespace(embed=lambda crop: unit(55, SIERRA, 0.008),
                                    classify=lambda f, b, e=None: VehicleType("amazon", "Amazon van", True, 0.61, {}))
        pipe = CameraPipeline.__new__(CameraPipeline)
        pipe._detections = types.SimpleNamespace(household=store)
        pipe.detector = types.SimpleNamespace(recording=False)
        pipe._clip_labels, pipe.display_name = set(), "Garage"
        t = Track(1, "car", "vehicle", 0, 0, [(0, (10, 10, 100, 80))], 0.8, 2)
        t.see(COLOUR, (10, 10, 100, 80), 0)
        pipe._check_vehicle_type(clf, t)
        self.assertEqual(t.known_vehicle.name, "Luke's Truck")
        self.assertIsNone(t.vehicle_type)  # not "An Amazon van"

class TestAskWhoseCar(unittest.TestCase):
    BOX = (100, 100, 200, 150)

    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-ask-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        sm.settings.update({"household_ask_start_hour": 0, "household_ask_end_hour": 24})
        self.engine = Engine(sm)
        e = self.engine
        self.sierra = e.household.save_vehicle("Luke's Sierra", "Luke", [], "pickup")
        self.fusion = e.household.save_vehicle("Abigail's Fusion", "Abigail", [], "sedan")
        for i in range(3):
            e.household.add_sighting(0, "cam", "Garage", COLOUR, self.BOX, unit(10 + i, SIERRA, 0.008), self.sierra)
            e.household.add_sighting(0, "cam", "Garage", COLOUR, self.BOX, unit(20 + i, FUSION, 0.008), self.fusion)
        self.sent, self.edits, self.toasts = [], [], []
        tg = e.telegram
        tg.enabled, tg.bot_token, tg.chat_ids = True, "123:abc", ["111"]
        tg.send_photo_with_buttons = lambda chat, jpeg, caption, rows: (self.sent.append((chat, caption, rows)), 42)[1]
        tg.edit_caption = lambda chat, mid, caption: self.edits.append((chat, mid, caption))
        tg.answer_button = lambda cb, text="": self.toasts.append(text)
        self.det, self.clf = _Det(), _Clf()
        self.changes = []
        self.cam = types.SimpleNamespace(host="192.168.0.9", display_name="Garage")
        e.presence = PresenceTracker(e.household, self.det, self.clf, cameras=lambda: [self.cam],
                                     on_change=lambda kind, cam, v, ts, jpeg: self.changes.append((kind, v.name)),
                                     on_unknown=e._ask_whose_car)
        self.t = time.time() - 600

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, boxes, frame=COLOUR):
        self.det.boxes = boxes
        self.engine.presence.scan(self.cam, frame, self.t)
        self.t += 60

    def park_stranger(self, look=unit(90, (SIERRA + FUSION * 1.4) / np.linalg.norm(SIERRA + FUSION * 1.4), 0.002), frame=COLOUR):
        self.clf.next = look
        self.scan([])
        for _ in range(4):
            self.scan([self.BOX], frame)

    def test_asks_once_with_likeliest_first(self):
        self.park_stranger()
        self.assertEqual(len(self.sent), 1)
        chat, caption, rows = self.sent[0]
        self.assertIn("Whose car is this?", caption)
        self.assertEqual(rows[0][0][0], "Abigail's Fusion")  # closer look than the Sierra
        self.assertEqual([b[0] for b in rows[-1]], ["Not ours", "Skip"])
        self.scan([self.BOX]); self.scan([self.BOX])
        self.assertEqual(len(self.sent), 1)  # never twice for the same parked car

    def test_answer_names_it_and_records_the_arrival(self):
        self.park_stranger()
        data = self.sent[0][2][0][0][1]  # first button: Abigail's Fusion
        self.engine.handle_button("111", 42, data, "cb1")
        self.assertEqual(self.toasts, ["Thanks!"])
        self.assertIn("That's Abigail's Fusion", self.edits[0][2])
        self.scan([self.BOX])
        self.assertEqual(self.changes, [("arrived", "Abigail's Fusion")])
        self.assertEqual(self.engine.presence.where_is(self.fusion)[0], "192.168.0.9")
        sid = int(data.split(":")[1])
        self.assertEqual({s["id"]: s["vehicle_id"] for s in self.engine.household.sightings(labelled=True)}[sid], self.fusion)

    def test_not_ours_stops_questions_and_hides_it(self):
        self.park_stranger()
        sid = int(self.sent[0][2][-1][0][1].split(":")[1])
        self.engine.handle_button("111", 42, f"hh:{sid}:x", "cb2")
        self.assertIn("not one of ours", self.edits[0][2])
        self.assertNotIn(sid, [s["id"] for s in self.engine.household.sightings()])
        self.assertTrue(self.engine.presence.present()["192.168.0.9"][0].ignored)

    def test_limits(self):
        self.engine.settings["household_ask_per_day"] = 0
        self.park_stranger()
        self.assertEqual(self.sent, [])
        self.engine.settings["household_ask_per_day"] = 3
        self.engine.presence.parked.clear()
        self.park_stranger(frame=NIGHT)
        self.assertEqual(self.sent, [])  # not at night: the photo wouldn't help anyone
        self.engine.presence.parked.clear()
        self.clf.next = unit(91, SIERRA, 0.004)
        self.scan([]); [self.scan([self.BOX]) for _ in range(4)]
        self.assertEqual(self.sent, [])  # recognised: nothing to ask

    def test_stale_or_foreign_buttons(self):
        self.engine.handle_button("111", 7, "hh:99999:s", "cb3")
        self.assertEqual(self.toasts, ["Skipped"])
        self.engine.handle_button("111", 7, "rm -rf", "cb4")
        self.assertEqual(self.toasts[-1], "")


class TestAskSkipsCutOffCars(TestAskWhoseCar):
    BOX = (0, 100, 200, 150)  # touching the left edge of the picture

    def test_asks_once_with_likeliest_first(self):
        self.park_stranger()
        self.assertEqual(self.sent, [])

    test_answer_names_it_and_records_the_arrival = None
    test_not_ours_stops_questions_and_hides_it = None
    test_limits = None
    test_stale_or_foreign_buttons = None


class TestWrongCarButton(TestAskWhoseCar):
    """A car alert carries a button; the family says whose car it really was."""

    def alert(self, named=None):
        from sentinel.engine.objects import ObjectEvent, Track
        track = Track(1, "car", "vehicle", 0, 0, [(0, self.BOX)], 0.9, 3, best_frame=COLOUR, best_box=self.BOX)
        track.see(COLOUR, self.BOX)
        track.known_vehicle = self.engine.household.vehicle(named) if named else None
        event = ObjectEvent("192.168.0.9", "Garage", track, None, "🛻 Luke's Sierra is leaving the Garage.", COLOUR, time.time())
        pipeline = types.SimpleNamespace(host="192.168.0.9", display_name="Garage")
        return self.engine._car_fix_button(pipeline, event)

    def setUp(self):
        super().setUp()
        self.clf.next = unit(95, FUSION, 0.004)
        self.engine.detection_worker = types.SimpleNamespace(vehicle_classifier=self.clf)
        self.buttons = []
        self.engine.telegram.edit_buttons = lambda chat, mid, rows: self.buttons.append(rows)

    def test_correct_the_name(self):
        rows = self.alert(named=self.sierra)
        self.assertEqual(rows[0][0][0], "✏️ Wrong car?")
        self.engine.handle_button("111", 42, rows[0][0][1], "cb1")
        choices = self.buttons[-1]
        self.assertEqual([b[0] for b in choices[0]], ["Abigail's Fusion"])  # the car it was called isn't offered
        self.assertEqual([b[0] for b in choices[-1]], ["Not ours", "Cancel"])
        self.engine.handle_button("111", 42, choices[0][0][1], "cb2")
        self.assertEqual(self.toasts[-1], "Thanks!")
        self.assertIn("Corrected: that was Abigail's Fusion", self.edits[-1][2])
        self.assertTrue(self.edits[-1][2].startswith("🛻 Luke's Sierra is leaving"))  # the alert itself is kept
        filed = [s for s in self.engine.household.sightings(labelled=True) if s["auto"] == 0 and s["ts"] > 0]
        self.assertEqual([s["vehicle_id"] for s in filed], [self.fusion])
        self.engine.handle_button("111", 42, choices[0][0][1], "cb3")  # tapped twice
        self.assertEqual(self.toasts[-1], "That alert is too old to correct")

    def test_not_ours_and_cancel(self):
        rows = self.alert()
        self.assertEqual(rows[0][0][0], "🚗 Whose car is this?")
        data = rows[0][0][1]
        self.engine.handle_button("111", 42, data + ":c", "cb1")
        self.assertEqual(self.buttons[-1], rows)  # back to the single button
        self.engine.handle_button("111", 42, data + ":x", "cb2")
        self.assertIn("not one of ours", self.edits[-1][2])
        self.assertEqual(len(self.engine.household.ignored_embeddings()), 1)  # a "not ours" reference now

    def test_no_button_without_family_cars_or_for_bicycles(self):
        from sentinel.engine.objects import ObjectEvent, Track
        bike = Track(2, "bicycle", "vehicle", 0, 0, [(0, self.BOX)], 0.9, 3, best_frame=COLOUR, best_box=self.BOX)
        event = ObjectEvent("192.168.0.9", "Garage", bike, None, "🚲 Someone on a bicycle", COLOUR, time.time())
        self.assertIsNone(self.engine._car_fix_button(types.SimpleNamespace(host="h", display_name="G"), event))

    def test_ask_limit_survives_a_restart(self):
        from sentinel.engine import Engine
        self.engine.settings["household_ask_per_day"] = 1
        self.park_stranger()
        self.assertEqual(len(self.sent), 1)
        again = Engine(self.engine.settings_manager)
        self.assertEqual(again._car_ask_days, self.engine._car_ask_days)

    test_asks_once_with_likeliest_first = None
    test_answer_names_it_and_records_the_arrival = None
    test_not_ours_stops_questions_and_hides_it = None
    test_limits = None
    test_stale_or_foreign_buttons = None
