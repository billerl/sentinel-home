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
        ids = [self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(50 + i, SIERRA, 0.008))
               for i in range(3)]
        ids.append(self.store.add_sighting(time.time(), "cam", "Garage", COLOUR, (0, 0, 90, 70), unit(77)))
        groups = self.store.suggestion_groups()
        big = max(groups, key=lambda g: len(g["ids"]))
        self.assertEqual(sorted(big["ids"]), sorted(ids[:3]))
        self.assertEqual(big["suggested"], self.sierra)
        self.assertTrue(os.path.isfile(self.store.crop_path(ids[0])))
        self.store.assign([ids[3]], None, ignore=True)
        self.assertEqual(sum(len(g["ids"]) for g in self.store.suggestion_groups()), 3)
        self.store.assign(ids[:3], self.sierra)
        self.assertEqual(self.store.suggestion_groups(), [])


class _Det:
    """Returns whatever boxes the test sets."""

    def __init__(self):
        self.boxes = []

    def detect(self, frame, min_score=0.45):
        return [Detection("car", 0.9, b) for b in self.boxes]


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
        self.assertEqual(self.changes, [("arrived", "Luke's Sierra", 1060.0)])

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
