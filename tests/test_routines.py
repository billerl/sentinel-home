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
              people={1: "Alex", 2: "Kate"}, cars={7: "Sierra"})
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


class FakeHooks:
    def __init__(self):
        self.switch = {"kasa-9c53220000aa-1": False, "kasa-50c7bf000001": False}
        self.calls, self.messages, self.said = [], [], []
        self.motion = {}
        self.home = SPRINGFIELD

    def hooks(self):
        def set_switch(sid, on, source):
            self.calls.append((sid, on))
            self.switch[sid] = on
            return True, "ok"
        return {"set_switch": set_switch, "switch_on": lambda sid: self.switch.get(sid),
                "telegram": lambda text, jpeg: self.messages.append((text, jpeg)), "say": self.said.append,
                "coords": lambda: self.home, "nobody_home": lambda: False,
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

    def test_paused_routine_does_nothing(self):
        rid = self.add(PATIO)
        self.store.set_enabled(rid, False)
        self.runner.reload()
        self.runner.seen("192.168.0.9", "person")
        self.assertEqual(self.fake.calls, [])

    def test_messages_not_more_than_every_5_minutes(self):
        self.add({"when": {"type": "seen", "what": ["delivery"]},
                  "then": [{"do": "telegram", "text": "Package!", "photo": True}, {"do": "say", "text": "Package"}]})
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.assertEqual(self.fake.messages, [("Package!", b"jpeg")])
        self.assertEqual(self.fake.said, ["Package"])
        self.now += 301
        self.runner.seen("192.168.0.10", "delivery", b"jpeg")
        self.assertEqual(len(self.fake.messages), 2)

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
