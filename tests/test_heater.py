"""The heater thermostat: stepping a remote-only heater's setting toward a target, and its safety steps."""

import _test_env  # noqa: F401  (isolates the event log)

import tempfile
import unittest

from sentinel.engine import heater as H


class FakeHeater:
    """The EdenPURE behind the BroadLink: a setting of 1..13 that Temp up/down move, and a room that drifts
    toward (setting * 1.5 + 55)°F."""

    def __init__(self, level=9):
        self.level, self.presses, self.room_f, self.mode_high = level, [], 64.0, False
        self.reachable, self.sensor = True, True

    def buttons(self):
        return {"EdenPURE heater": ["Power", "Mode", "Temp up", "Temp down"], "Soundbar": ["Volume up", "Volume down"]}

    def press(self, remote, button, times=1, source="web", gap=0.3):
        if not self.reachable:
            return False, "The BroadLink isn't answering."
        self.presses.append((remote, button, times))
        assert button in ("Temp up", "Temp down"), button  # never Power or Mode
        if not self.mode_high:
            self.level = max(1, min(13, self.level + (times if button == "Temp up" else -times)))
        return True, button

    def climate(self):
        if not self.sensor:
            return None
        return {"temperature": (self.room_f - 32) * 5 / 9, "humidity": 40.0}

    def settle(self, minutes):
        goal = 80.0 if self.mode_high else self.level * 1.5 + 55
        for _ in range(minutes):
            self.room_f += (goal - self.room_f) * 0.03


class TestHeater(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.heater = FakeHeater()
        self.sent = []
        self.t = 100000.0
        self.control = H.HeaterControl(self.tmp.name, self.heater, {"weather_units": "fahrenheit"}, self.sent.append,
                                       clock=lambda: self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def run_minutes(self, minutes):
        for _ in range(minutes):
            self.t += 60
            self.heater.settle(1)
            self.control.tick()

    def test_finds_the_buttons_by_name(self):
        self.assertEqual(self.control.buttons(), ("EdenPURE heater", "Temp up", "Temp down"))

    def test_counts_the_setting_from_the_bottom_then_holds_the_target(self):
        ok, message = self.control.hold(69)
        self.assertTrue(ok, message)
        self.run_minutes(1)
        self.assertEqual(self.heater.presses[0], ("EdenPURE heater", "Temp down", H.STEPS + 1))  # to the bottom
        self.assertEqual(self.control.state["level"], self.heater.level)  # counted right
        self.run_minutes(8 * 60)
        self.assertAlmostEqual(self.heater.room_f, 69, delta=1.2)
        self.assertEqual(self.control.state["level"], self.heater.level)
        self.assertEqual(self.control.state["held"], 9)  # 9 * 1.5 + 55 = 68.5: the step that holds it
        self.assertEqual(self.sent, [])

    def test_warm_up_then_back_to_the_held_step(self):
        self.control.state["held"] = 9
        self.control.hold(69)
        self.run_minutes(1)
        self.run_minutes(16)
        self.assertEqual(self.heater.level, H.STEPS)  # 5° short: as warm as it goes
        top_until = None
        for minute in range(6 * 60):
            self.run_minutes(1)
            if self.heater.level < H.STEPS and top_until is None:
                top_until = minute
        self.assertIsNotNone(top_until)
        self.assertLess(self.heater.room_f, 70.5)  # didn't overshoot far
        self.assertAlmostEqual(self.heater.room_f, 69, delta=1.2)

    def test_too_warm_turns_it_all_the_way_down(self):
        self.control.hold(70)
        self.run_minutes(1)
        for _ in range(5):  # (readings are smoothed: a few minutes of it)
            self.heater.room_f = 80
            self.run_minutes(1)
        self.assertEqual(self.heater.level, 1)
        self.assertTrue(any("all the way down" in s for s in self.sent), self.sent)

    def test_stuck_on_high_is_reported_not_fixed(self):
        self.control.hold(68)
        self.run_minutes(1)
        self.heater.mode_high = True  # someone pressed Mode on the panel
        self.run_minutes(3 * 60)
        self.assertTrue(any("may be on High" in s for s in self.sent), self.sent)
        self.assertTrue(all(b in ("Temp up", "Temp down") for _r, b, _n in self.heater.presses))

    def test_no_thermometer_reading(self):
        self.control.hold(70)
        self.run_minutes(1)
        self.heater.level = self.control.state["level"] = 8
        self.heater.sensor = False
        self.run_minutes(12)
        self.assertEqual(self.heater.level, 1)
        self.assertTrue(any("can't read the room temperature" in s for s in self.sent), self.sent)

    def test_broadlink_gone_counts_again_after(self):
        self.control.hold(70)
        self.run_minutes(1)
        self.heater.reachable = False
        self.run_minutes(20)
        self.assertIsNone(self.control.state["level"])
        self.assertTrue(any("couldn't reach the BroadLink" in s for s in self.sent))
        self.heater.reachable = True
        self.heater.level = 4  # someone used the panel meanwhile
        self.run_minutes(1)
        self.assertEqual(self.control.state["level"], self.heater.level)

    def test_release_and_kept_through_a_restart(self):
        self.control.hold(71)
        self.run_minutes(1)
        again = H.HeaterControl(self.tmp.name, self.heater, {}, self.sent.append, clock=lambda: self.t)
        self.assertTrue(again.state["enabled"])
        self.assertEqual(again.state["target_f"], 71)
        self.control.release()
        n = len(self.heater.presses)
        self.run_minutes(60)
        self.assertEqual(len(self.heater.presses), n)  # hands off

    def test_words(self):
        p = self.control.parse
        self.assertEqual(p("set the heater to 70"), ("hold", 70.0))
        self.assertEqual(p("heater to 68 degrees"), ("hold", 68.0))
        self.assertEqual(p("keep the room at 69"), ("hold", 69.0))
        self.assertEqual(p("set the heater to 21 c"), ("hold", 69.8))
        self.assertEqual(p("stop holding the temperature"), ("release", None))
        self.assertEqual(p("heater thermostat off"), ("release", None))
        self.assertEqual(p("how warm is it inside?"), ("ask", None))
        for not_it in ("heater power", "what's the temperature", "set the timer to 10", "heater off"):
            self.assertIsNone(p(not_it), not_it)
        self.control.temp_f = 67.2
        self.assertEqual(self.control.command("set the heater to 70", "voice"),
                         "Holding the heater at 70°F. The room is 67°F.")
        self.assertIn("holding 70°F, room 67.2°F", self.control.command("heater status", "telegram"))


if __name__ == "__main__":
    unittest.main()
