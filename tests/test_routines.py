"""Routines: checking and describing them, sunset maths, running them (lights off again when quiet), and saving
them by voice / Telegram / the web page."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from sentinel.engine import Engine
from sentinel.engine.routines import (Catalog, RoutineRunner, RoutineStore, describe, is_routine_command,
                                      normalize, sun_times)
from sentinel.engine.switches import SmartSwitches
from sentinel.notifications.telegram_notifier import TelegramNotifier
from sentinel.settings.manager import SettingsManager
from test_switches import FakeKasa

CAT = Catalog(cameras={"192.168.0.9": "Back Yard", "192.168.0.10": "Front Door"},
              switches={"kasa-9c53220000aa-1": "patio lights", "kasa-50c7bf000001": "Porch lamp"},
              people={1: "Alex", 2: "Kate"}, cars={7: "Sierra"},
              sensors={"beb4-df1c": "Living room", "2350-beb4": "Kitchen", "8d24-df1c": "Dining room"})
PATIO = {"name": "Patio at night", "when": {"type": "seen", "what": ["person"], "cameras": ["back yard"]},
         "only": {"dark": True}, "then": [{"do": "switch", "switch": "Patio lights", "on": True, "until_quiet": 2}]}
EVENING = time.mktime(time.strptime("2026-11-02 21:00", "%Y-%m-%d %H:%M"))
NOON = time.mktime(time.strptime("2026-11-02 12:00", "%Y-%m-%d %H:%M"))
SPRINGFIELD = (39.80, -89.64)


class TestCheck(unittest.TestCase):
    def test_names_become_ids_and_read_back(self):
        spec = normalize(PATIO, CAT)
        self.assertEqual(spec["when"], {"type": "seen", "what": ["person"], "cameras": ["192.168.0.9"]})
        self.assertEqual(spec["then"], [{"do": "switch", "switch": "kasa-9c53220000aa-1", "on": True, "until_quiet": 2}])
        self.assertEqual(describe(spec, CAT), "When the Back Yard camera sees a person, only after dark: turn on the "
                                              "patio lights until there's been no motion for 2 minutes.")
        renamed = Catalog(CAT.cameras, {**CAT.switches, "kasa-9c53220000aa-1": "string lights"}, CAT.people, CAT.cars)
        self.assertIn("string lights", describe(spec, renamed))  # stored by id: a rename doesn't break it

    def test_more_kinds(self):
        spec = normalize({"when": {"type": "time", "at": "sunset", "offset": -15}, "only": {"days": ["Mon", "tue", "wed", "thu", "fri"]},
                          "then": [{"do": "switch", "switch": "porch lamp", "on": True, "for": 120},
                                   {"do": "say", "text": "Good evening"}]}, CAT)
        self.assertEqual(describe(spec, CAT), "Every day 15 minutes before sunset, only on weekdays: turn on the "
                                              "Porch lamp for 2 hours and say “Good evening” out loud.")
        self.assertEqual(spec["name"], "Porch lamp — sunset")
        spec = normalize({"when": {"type": "car_arrive", "cars": ["Sierra"]}, "only": {"from": "22:00", "to": "6:00"},
                          "then": [{"do": "telegram", "text": "Sierra's home"}]}, CAT)
        self.assertEqual(describe(spec, CAT), "When Sierra pulls in, only between 10 pm and 6 am: send “Sierra's home” on Telegram.")
        spec = normalize({"when": {"type": "seen", "what": "something"}, "then": {"do": "say", "text": "hi"}}, CAT)
        self.assertTrue(describe(spec, CAT).startswith("When any camera sees anything"))

    def test_wifi_sensor_trigger(self):
        spec = normalize({"when": {"type": "sensor", "sensors": ["living room"]},
                          "then": [{"do": "switch", "switch": "porch lamp", "on": True, "until_quiet": 10}]}, CAT)
        self.assertEqual(spec["when"], {"type": "sensor", "sensors": ["beb4-df1c"]})
        self.assertEqual(describe(spec, CAT), "When the Living room Wi-Fi sensor notices movement: turn on the Porch lamp "
                                              "until there's been no motion for 10 minutes.")
        spec = normalize({"when": {"type": "sensor"}, "only": {"nobody_home": True},
                          "then": [{"do": "telegram", "text": "Movement inside"}]}, CAT)
        self.assertEqual(spec["name"], "Message — any Wi-Fi sensor")
        self.assertTrue(describe(spec, CAT).startswith("When any Wi-Fi sensor notices movement, only when nobody's home"))
        with self.assertRaises(ValueError):
            normalize({"when": {"type": "sensor", "sensors": ["attic"]}, "then": {"do": "say", "text": "hi"}}, CAT)

    def test_mistakes_say_what_is_wrong(self):
        cases = [
            ({**PATIO, "then": [{"do": "switch", "switch": "hot tub", "on": True}]}, "hot tub"),
            ({**PATIO, "when": {"type": "seen", "cameras": ["Attic"]}}, "Attic"),
            ({**PATIO, "when": {"type": "earthquake"}}, "what starts it"),
            ({**PATIO, "then": []}, "what it should do"),
            ({**PATIO, "when": {"type": "time", "at": "25:00"}}, "time"),
            ({"when": {"type": "time", "at": "sunset"}, "then": [{"do": "switch", "switch": "porch lamp", "until_quiet": 2}]},
             "needs a routine started by a camera"),
            ({**PATIO, "then": [{"do": "switch", "switch": "porch lamp", "for": 5000}]}, "12 hours"),
            ({**PATIO, "then": [{"do": "email", "text": "x"}]}, "Telegram"),
        ]
        for spec, words in cases:
            with self.assertRaises(ValueError) as e:
                normalize(spec, CAT)
            self.assertIn(words, str(e.exception))

    def test_commands(self):
        for t in ("Routines", "what are my routines?", "Routine 2 off", "turn on routine 3", "Delete routine 4",
                  'Routine {"when": {}}'):
            self.assertTrue(is_routine_command(t), t)
        for t in ("routine 2", "routine", "turn on the patio lights"):
            self.assertFalse(is_routine_command(t), t)

    def test_sun(self):
        rise, set_ = sun_times(*SPRINGFIELD, NOON)
        # Open-Meteo for Springfield, IL on 2026-11-02: 06:28 and 16:55 Central = 12:28 and 22:55 UTC.
        self.assertEqual(time.strftime("%m-%d %H:%M", time.gmtime(rise)), "11-02 12:28")
        self.assertEqual(time.strftime("%m-%d %H:%M", time.gmtime(set_)), "11-02 22:55")


class TestSensorRules(unittest.TestCase):
    """“This sensor and that one, but not that one”: a room from sensors that each hear more than one."""
    ROOM = {"when": {"type": "sensor", "sensors": ["Living room"], "none": ["Kitchen", "Dining room"]},
            "then": [{"do": "say", "text": "Someone is in the living room"}]}

    def test_groups_read_back(self):
        spec = normalize(self.ROOM, CAT)
        self.assertEqual(spec["when"], {"type": "sensor", "sensors": ["beb4-df1c"], "none": ["2350-beb4", "8d24-df1c"],
                                        "within": 5})
        self.assertEqual(describe(spec, CAT), "When the Living room Wi-Fi sensor notices movement, but not if the "
                                              "Kitchen or Dining room does too (within 5 seconds): say “Someone is in "
                                              "the living room” out loud.")
        both = normalize({"when": {"type": "sensor", "all": ["Living room", "Kitchen"], "within": 10},
                          "then": [{"do": "say", "text": "walking through"}]}, CAT)
        self.assertTrue(describe(both, CAT).startswith("When the Living room and Kitchen Wi-Fi sensors all notice "
                                                       "movement (within 10 seconds)"))
        self.assertEqual(both["name"], "Announcement — Living room, Kitchen")
        mixed = normalize({"when": {"type": "sensor", "sensors": ["Living room"], "all": ["Kitchen"], "none": ["Dining room"]},
                           "then": [{"do": "say", "text": "x"}]}, CAT)
        self.assertIn("When the Living room Wi-Fi sensor notices movement, and so does the Kitchen, but not if the "
                      "Dining room does too (within 5 seconds)", describe(mixed, CAT))
        plain = normalize({"when": {"type": "sensor", "sensors": ["Kitchen"]}, "then": [{"do": "say", "text": "x"}]}, CAT)
        self.assertNotIn("within", plain["when"])  # a plain one still goes off at once

    def test_rule_mistakes(self):
        for when, words in (({"sensors": ["Kitchen"], "none": ["Kitchen"]}, "only be in one group"),
                            ({"sensors": ["Kitchen"], "none": ["Living room"], "within": 90}, "between 2 and 60"),
                            ({"none": ["Kitchen", "Living room", "Dining room"]}, "nothing could start it")):
            with self.assertRaises(ValueError) as e:
                normalize({"when": {"type": "sensor", **when}, "then": [{"do": "say", "text": "x"}]}, CAT)
            self.assertIn(words, str(e.exception))


class FakeHooks:
    def __init__(self):
        self.switch = {"kasa-9c53220000aa-1": False, "kasa-50c7bf000001": False}
        self.calls, self.messages, self.said = [], [], []
        self.motion, self.wifi = {}, {}
        self.away = False
        self.home = SPRINGFIELD

    def hooks(self):
        def set_switch(sid, on, source):
            self.calls.append((sid, on))
            self.switch[sid] = on
            return True, "ok"
        return {"set_switch": set_switch, "switch_on": lambda sid: self.switch.get(sid),
                "telegram": lambda text, jpeg: self.messages.append((text, jpeg)), "say": self.said.append,
                "coords": lambda: self.home, "nobody_home": lambda: self.away,
                "last_sensor_motion": lambda p: max(self.wifi.values(), default=0) if p is None else self.wifi.get(p, 0),
                "last_motion": lambda cam: max(self.motion.values(), default=0) if cam is None else self.motion.get(cam, 0)}


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-routines-")
        self.now = EVENING
        self.fake = FakeHooks()
        self.store = RoutineStore(self.dir)
        self.runner = self.make()

    def make(self):
        return RoutineRunner(self.store, self.fake.hooks(), holds_path=os.path.join(self.dir, "holds.json"),
                             clock=lambda: self.now, spawn=lambda fn, *a: fn(*a))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, spec):
        rid = self.store.save(normalize(spec, CAT))
        self.runner.reload()
        return rid

    def test_a_package_announcement_repeats_until_it_is_taken_in(self):
        spec = {"when": {"type": "seen", "what": ["package"], "cameras": ["Front Door"], "repeat_hours": 2},
                "then": [{"do": "say", "text": "There's a package at the front door"}]}
        self.assertIn("again every 2 hours while the package is still there", describe(normalize(spec, CAT), CAT))
        with self.assertRaises(ValueError):  # a person can't "repeat until taken in"
            normalize({**spec, "when": {**spec["when"], "what": ["person"]}}, CAT)
        self.add(spec)
        self.runner.seen("192.168.0.10", "package")
        self.runner.package_waiting("192.168.0.10")  # the watch's next look: the clock starts
        self.assertEqual(len(self.fake.said), 1)
        self.now += 3600
        self.runner.package_waiting("192.168.0.10")
        self.assertEqual(len(self.fake.said), 1)  # an hour: not yet
        self.now += 3601
        self.runner.package_waiting("192.168.0.10")
        self.assertEqual(len(self.fake.said), 2)  # two hours: again
        self.runner.package_gone("192.168.0.10")  # taken in / "✅ Got it"
        self.now += 3 * 3600
        self.runner.package_waiting("192.168.0.10")  # (the next parcel's first look)
        self.assertEqual(len(self.fake.said), 2)

    def test_front_door_on_the_tv(self):
        cat = Catalog(CAT.cameras, CAT.switches, CAT.people, CAT.cars, CAT.sensors, tvs={"samsung-1": "TV"})
        spec = normalize({"when": {"type": "seen", "what": ["person"], "cameras": ["Front Door"]},
                          "then": [{"do": "tv_camera", "camera": ""}]}, cat)  # the one TV; the camera that saw it
        self.assertEqual(spec["then"], [{"do": "tv_camera", "tv": "samsung-1", "camera": "", "for": 1}])
        self.assertEqual(spec["name"], "Camera on TV — Front Door")
        self.assertEqual(describe(spec, cat), "When the Front Door camera sees a person: show that camera on the TV "
                                              "(if it's on) for 1 minute, then go back to what was on.")
        for bad, why in (({"do": "tv_camera", "for": 45}, "30 minutes"),
                         ({"do": "tv_camera", "tv": "Bedroom"}, "Bedroom")):
            with self.assertRaises(ValueError) as e:
                normalize({**spec, "then": [bad]}, cat)
            self.assertIn(why, str(e.exception))
        with self.assertRaises(ValueError):  # a timed routine has no "camera that saw it"
            normalize({"when": {"type": "time", "at": "18:00"}, "then": [{"do": "tv_camera"}]}, cat)
        self.store.save(spec)  # (already checked against the catalog with the TV in it)
        self.runner.reload()
        shown = []
        self.runner.hooks["tv_camera"] = lambda tv, camera, seconds: shown.append((tv, camera, seconds)) or True
        self.runner.seen("192.168.0.10", "person")
        self.assertEqual(shown, [("samsung-1", "192.168.0.10", 60)])

    def test_patio_lights_until_quiet(self):
        self.add(PATIO)
        self.runner.seen("192.168.0.10", "person")  # another camera
        self.runner.seen("192.168.0.9", "vehicle")  # not a person
        self.assertEqual(self.fake.calls, [])
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(self.fake.calls, [("kasa-9c53220000aa-1", True)])
        self.fake.motion["192.168.0.9"] = self.now + 60  # still moving about a minute later
        self.now += 150
        self.runner.tick()
        self.assertEqual(len(self.fake.calls), 1)  # 90 s since the last motion: stays on
        self.fake.motion["192.168.0.10"] = self.now  # motion on another camera doesn't count
        self.now += 31
        self.runner.tick()
        self.assertEqual(self.fake.calls[-1], ("kasa-9c53220000aa-1", False))
        self.assertIsNotNone(self.store.get(1).last_run)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "holds.json")))

    def test_not_in_daylight_and_hand_switched_light_is_left_alone(self):
        self.add(PATIO)
        self.now = NOON
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(self.fake.calls, [])
        self.now = EVENING
        self.fake.switch["kasa-9c53220000aa-1"] = True  # someone turned it on by hand
        self.runner.seen("192.168.0.9", "person")
        self.now += 3600
        self.runner.tick()
        self.assertEqual(self.fake.calls, [])

    def test_unsure_sighting_only_switches_lights(self):
        self.add(PATIO)
        self.add({"when": {"type": "seen", "what": ["person"], "cameras": ["Back Yard"]},
                  "then": [{"do": "telegram", "text": "Someone's out back"}]})
        self.runner.seen("192.168.0.9", "person", sure=False)
        self.assertEqual(self.fake.calls, [("kasa-9c53220000aa-1", True)])
        self.assertEqual(self.fake.messages, [])  # a message waits for a real sighting
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(self.fake.messages, [("Someone's out back", None)])

    def test_paused_routine_does_nothing(self):
        rid = self.add(PATIO)
        self.store.set_enabled(rid, False)
        self.runner.reload()
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(self.fake.calls, [])

    def test_messages_every_5_minutes_speaking_every_2(self):
        self.add({"when": {"type": "seen", "what": ["delivery"]},
                  "then": [{"do": "telegram", "text": "Package!", "photo": True}, {"do": "say", "text": "Package"}]})
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.assertEqual(self.fake.messages, [("Package!", b"jpeg")])
        self.assertEqual(self.fake.said, ["Package"])
        self.now += 121
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.assertEqual((len(self.fake.messages), len(self.fake.said)), (1, 2))
        self.now += 180
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.assertEqual(len(self.fake.messages), 2)

    def test_each_room_speaks_in_turn(self):
        """Walking kitchen -> living room -> kitchen: each room's routine says its own line."""
        for path, room in (("2350-beb4", "kitchen"), ("beb4-df1c", "living room")):
            self.add({"when": {"type": "sensor", "sensors": [path]}, "then": [{"do": "say", "text": room}]})
        for path in ("2350-beb4", "beb4-df1c", "2350-beb4"):
            self.runner.sensor(path)
            self.now += 10
        self.assertEqual(self.fake.said, ["kitchen", "living room"])  # the kitchen again waits 2 min
        self.now += 120
        self.runner.sensor("2350-beb4")
        self.assertEqual(self.fake.said[-1], "kitchen")

    def test_wifi_sensor_fires_when_movement_starts_not_all_through_it(self):
        self.add({"when": {"type": "sensor", "sensors": ["2350-beb4"]}, "then": [{"do": "say", "text": "front door"}]})
        for _ in range(60 * 10):  # ten minutes of moving readings, never 30 s apart
            self.runner.sensor("2350-beb4")
            self.now += 1
        self.assertEqual(self.fake.said, ["front door"])
        self.now += 60  # quiet a minute, then movement again: a new time
        self.runner.sensor("2350-beb4")
        self.assertEqual(self.fake.said, ["front door", "front door"])

    def run_due(self):
        """Run the decisions whose time has come (the runner's own timers, faked)."""
        due = [(t, fn) for t, fn in self.timers if t <= self.now]
        self.timers = [x for x in self.timers if x[0] > self.now]
        for _t, fn in due:
            fn()

    def rule_runner(self):
        self.timers = []
        self.runner = RoutineRunner(self.store, self.fake.hooks(), holds_path=os.path.join(self.dir, "holds.json"),
                                    clock=lambda: self.now, spawn=lambda fn, *a: fn(*a),
                                    later=lambda delay, fn: self.timers.append((self.now + delay, fn)))

    def test_a_house_wide_wifi_blip_is_not_the_front_door(self):
        # 2026-11-10 19:07: 15 of 21 paths jumped in the same second -> "someone opened the front door", nobody had
        self.rule_runner()
        together = {"n": 15}
        self.runner.house_wide = lambda at: together["n"]
        self.add({"when": {"type": "sensor", "sensors": ["2350-beb4"]},
                  "then": [{"do": "say", "text": "Someone has opened the front door"}]})
        self.runner.sensor("2350-beb4")
        self.assertEqual(self.fake.said, [])  # waits to see the rest of the house first
        self.now += 3
        self.run_due()
        self.assertEqual(self.fake.said, [])  # the whole house moved: a blip
        self.now += 60
        together["n"] = 4  # a real opening moved 4-5 paths near the door
        self.runner.sensor("2350-beb4")
        self.now += 3
        self.run_due()
        self.assertEqual(self.fake.said, ["Someone has opened the front door"])
        self.add(TestSensorRules.ROOM)  # rules ("and" / "but not") skip a blip too
        self.now += 300
        together["n"] = 14
        self.runner.sensor("beb4-df1c")
        self.now += 10
        self.run_due()
        self.assertEqual(self.fake.said, ["Someone has opened the front door"])

    def test_but_not_waits_for_the_other_sensors(self):
        self.rule_runner()
        self.add(TestSensorRules.ROOM)
        self.runner.sensor("beb4-df1c")  # living room...
        self.assertEqual(self.fake.said, [])  # ...decided a few seconds later, not at once
        self.now += 2
        self.runner.sensor("2350-beb4")  # ...and the kitchen 2 s later: walking through
        self.now += 3
        self.run_due()
        self.assertEqual(self.fake.said, [])
        self.now += 60
        self.runner.sensor("beb4-df1c")  # the living room alone
        self.runner.sensor("beb4-df1c")  # (one decision at a time)
        self.now += 5
        self.run_due()
        self.assertEqual(self.fake.said, ["Someone is in the living room"])
        self.assertEqual(self.timers, [])

    def test_kitchen_just_before_counts_too(self):
        self.rule_runner()
        self.add(TestSensorRules.ROOM)
        self.runner.sensor("8d24-df1c")  # the dining room first...
        self.now += 3
        self.runner.sensor("beb4-df1c")  # ...then the living room: coming in, not staying
        self.now += 5
        self.run_due()
        self.assertEqual(self.fake.said, [])

    def test_and_needs_both(self):
        self.rule_runner()
        self.add({"when": {"type": "sensor", "sensors": ["Living room"], "all": ["Kitchen"], "within": 8},
                  "then": [{"do": "say", "text": "walking through"}]})
        self.runner.sensor("2350-beb4")  # the kitchen alone doesn't start it...
        self.assertEqual(self.timers, [])
        self.runner.sensor("beb4-df1c")  # ...but counts for the living room just after
        self.now += 8
        self.run_due()
        self.assertEqual(self.fake.said, ["walking through"])
        self.now += 300
        self.runner.sensor("beb4-df1c")  # the living room alone
        self.now += 8
        self.run_due()
        self.assertEqual(self.fake.said, ["walking through"])

    def test_replay_the_last_day(self):
        spec = normalize(TestSensorRules.ROOM, CAT)
        t = EVENING
        moments = [("beb4-df1c", t, t + 20, 6.0),  # living room alone: goes off
                   ("beb4-df1c", t + 600, t + 610, 6.0), ("2350-beb4", t + 603, t + 606, 5.0),  # walking through: no
                   ("beb4-df1c", t + 1200, t + 1210, 6.0), ("2350-beb4", t + 1203, t + 1206, 2.0),  # kitchen too weak now
                   ("beb4-df1c", t + 1250, t + 1260, 6.0),  # 50 s later: the same time
                   ("beb4-df1c", t + 2400, t + 2410, 2.5)]  # below the living room's line now
        lines = {"beb4-df1c": 3.0, "2350-beb4": 3.0}.get
        times = self.runner.replay(spec, moments, lines, t - 3600, t + 3600)
        self.assertEqual(times, [(t, t + 20), (t + 1200, t + 1260)])
        late = normalize({**TestSensorRules.ROOM, "only": {"from": "06:00", "to": "20:00"}}, CAT)
        self.assertEqual(self.runner.replay(late, moments, lines, t - 3600, t + 3600), [])  # 9 pm: outside its hours
        plain = normalize({"when": {"type": "sensor", "sensors": ["Living room"]}, "then": [{"do": "say", "text": "x"}]}, CAT)
        self.assertEqual(len(self.runner.replay(plain, moments, lines, t - 3600, t + 3600)), 3)

    def test_motion_trigger_ignores_the_light_it_switched(self):
        self.add({"when": {"type": "motion", "cameras": ["Back Yard"]}, "only": {"dark": True},
                  "then": [{"do": "switch", "switch": "patio lights", "on": True, "for": 1}]})
        self.runner.motion("192.168.0.9")
        self.assertEqual(self.fake.calls, [("kasa-9c53220000aa-1", True)])
        self.now += 61
        self.runner.tick()
        self.assertEqual(self.fake.calls[-1], ("kasa-9c53220000aa-1", False))
        self.now += 5
        self.runner.motion("192.168.0.9")  # the picture changing as the light goes off
        self.assertEqual(len(self.fake.calls), 2)
        self.now += 30
        self.runner.motion("192.168.0.9")  # real motion later
        self.assertEqual(len(self.fake.calls), 3)

    def test_wifi_sensor_lights_until_quiet_and_away_alert(self):
        self.add({"when": {"type": "sensor", "sensors": ["Living room"]},
                  "then": [{"do": "switch", "switch": "porch lamp", "on": True, "until_quiet": 2}]})
        self.add({"when": {"type": "sensor"}, "only": {"nobody_home": True},
                  "then": [{"do": "telegram", "text": "Movement inside while nobody's home"}]})
        self.runner.sensor("2350-beb4")  # the kitchen: not the lamp's sensor; and people are home
        self.assertEqual((self.fake.calls, self.fake.messages), ([], []))
        self.runner.sensor("beb4-df1c")
        self.assertEqual(self.fake.calls, [("kasa-50c7bf000001", True)])
        self.fake.wifi["beb4-df1c"] = self.now + 100  # still moving in there
        self.fake.motion["192.168.0.9"] = self.now + 200  # camera motion doesn't keep an indoor light on
        self.now += 200
        self.runner.tick()
        self.assertEqual(len(self.fake.calls), 1)
        self.now += 21
        self.runner.tick()
        self.assertEqual(self.fake.calls[-1], ("kasa-50c7bf000001", False))
        self.fake.away = True
        self.runner.sensor("2350-beb4")
        self.assertEqual(self.fake.messages, [("Movement inside while nobody's home", None)])

    def test_sunset_routine_fires_once(self):
        self.add({"when": {"type": "time", "at": "sunset"}, "then": [{"do": "switch", "switch": "porch lamp", "on": True}]})
        sunset = sun_times(*SPRINGFIELD, NOON)[1]
        self.now = sunset - 30
        self.runner = self.make()
        self.runner.tick()
        self.assertEqual(self.fake.calls, [])
        self.now = sunset + 5
        self.runner.tick()
        self.now += 5
        self.runner.tick()
        self.assertEqual(self.fake.calls, [("kasa-50c7bf000001", True)])

    def test_arrive_and_cars(self):
        self.add({"when": {"type": "arrive", "people": ["Kate"]}, "then": [{"do": "say", "text": "Welcome home Kate"}]})
        self.add({"when": {"type": "car_leave"}, "then": [{"do": "telegram", "text": "A car left"}]})
        self.runner.person("arrive", 1)
        self.runner.person("leave", 2)
        self.runner.person("arrive", 2)
        self.runner.car("leave", 7)
        self.assertEqual(self.fake.said, ["Welcome home Kate"])
        self.assertEqual(self.fake.messages, [("A car left", None)])

    def test_light_left_on_at_a_restart_goes_off(self):
        self.add(PATIO)
        self.runner.seen("192.168.0.9", "person")
        restarted = self.make()  # Sentinel stopped while holding the light on
        restarted.tick()
        self.assertEqual(self.fake.calls[-1], ("kasa-9c53220000aa-1", False))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "holds.json")))

    def test_hours_across_midnight(self):
        self.add({**PATIO, "only": {"from": "22:00", "to": "06:00"}})
        self.runner.seen("192.168.0.9", "person")  # 21:00
        self.assertEqual(self.fake.calls, [])
        self.now += 2 * 3600  # 23:00
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(len(self.fake.calls), 1)


ROUTINE_JSON = "Routine " + json.dumps(PATIO)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-routines-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings.update({"volume": 0, "assistant_enabled": True})
        self.engine = Engine(manager)
        self.kasa = FakeKasa()
        self.engine.switches = SmartSwitches(self.dir, drivers=[self.kasa])
        self.engine.switches.discover()
        self.engine.cameras = {"192.168.0.9": types.SimpleNamespace(host="192.168.0.9", display_name="Back Yard", connected=True)}
        self.sent = []
        self.engine.telegram.reply_to_chat = lambda chat, text: self.sent.append((chat, text)) or True

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_said_to_jarvis_saved_after_yes(self):
        with mock.patch.object(self.engine.assistant, "ask", return_value="Sure.\nCOMMAND: " + ROUTINE_JSON), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            reply = self.engine.answer_voice("new routine when the back yard camera sees someone after dark turn on "
                                             "the patio lights until no motion for 2 minutes", lambda _s: None)
        self.assertIn("When the Back Yard camera sees a person, only after dark", reply.text)
        self.assertIn("Should I save it?", reply.text)
        self.assertEqual(self.engine.routines.store.all(), [])
        self.assertTrue(reply.confirm().startswith("Saved."))
        self.assertEqual([r.name for r in self.engine.routines.store.all()], ["Patio at night"])
        self.assertIn("1 routine", self.engine.answer_voice("what are my routines", lambda _s: None).text)

    def test_claude_mistake_is_explained(self):
        bad = "Routine " + json.dumps({**PATIO, "then": [{"do": "switch", "switch": "hot tub", "on": True}]})
        with mock.patch.object(self.engine.assistant, "ask", return_value="OK.\nCOMMAND: " + bad), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            reply = self.engine.answer_voice("new routine hot tub", lambda _s: None)
        self.assertIn("couldn't make that into a routine", reply.text)
        self.assertIsNone(reply.confirm)

    def test_telegram_button_then_list_pause_delete(self):
        t = self.engine.telegram
        sent_buttons = []
        t.send_text_with_buttons = lambda chat, text, rows: sent_buttons.append((text, rows))
        with mock.patch.object(self.engine.assistant, "ask", return_value="Sure.\nCOMMAND: " + ROUTINE_JSON), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            self.engine.answer_with_assistant("111", "new routine patio lights at night")
        text, rows = sent_buttons[0]
        self.assertIn("When the Back Yard camera sees a person", text)
        self.assertEqual(rows[0][0][0], "✅ Save routine")
        t.route_text("111", ROUTINE_JSON)  # typed by hand: not saved without the tap
        self.assertEqual(t.consume_routine_commands(), [])
        t.edit_text = t.answer_button = lambda *a, **k: None
        self.engine.handle_button("111", 5, rows[0][0][1], "cb")  # the ✅ tap
        for chat, cmd in t.consume_routine_commands():
            self.sent.append((chat, self.engine.routine_command(cmd)))
        self.assertIn("✅ Saved routine 1", self.sent[-1][1])
        self.assertTrue(TelegramNotifier.is_command(ROUTINE_JSON))
        for cmd in ("Routines", "Routine 1 off", "Delete routine 1"):
            t.route_text("111", cmd)
        replies = [self.engine.routine_command(c) for _chat, c in t.consume_routine_commands()]
        self.assertIn("1. Patio at night", replies[0])
        self.assertEqual(replies[1], "Routine 1, Patio at night, is off.")
        self.assertEqual(replies[2], "Deleted routine 1, Patio at night.")
        self.assertEqual(self.engine.routines.store.all(), [])

    def test_claude_knows_the_names_and_routines(self):
        self.engine.routine_command(ROUTINE_JSON)
        context = self.engine.assistant_context()
        self.assertIn("Camera names: Back Yard. Switch names: patio lights", context)
        self.assertIn("1. Patio at night: When the Back Yard camera", context)

    def test_web(self):
        ok, rid = self.engine.routine_save({"spec": PATIO})
        self.assertTrue(ok)
        view = self.engine.routines_view()
        self.assertEqual(view["routines"][0]["text"][:30], "When the Back Yard camera sees")
        self.assertEqual(view["catalog"]["cameras"], [{"id": "192.168.0.9", "name": "Back Yard"}])
        self.assertFalse(self.engine.routine_save({"spec": PATIO, "id": 99})[0])
        self.assertTrue(self.engine.routine_action(int(rid), "enable", False)[0])
        self.assertFalse(self.engine.routines_view()["routines"][0]["enabled"])
        self.assertFalse(self.engine.routine_action(int(rid), "enable", "no")[0])
        self.assertTrue(self.engine.routine_action(int(rid), "delete")[0])
        self.assertEqual(self.engine.routines_view()["routines"], [])


if __name__ == "__main__":
    unittest.main()
