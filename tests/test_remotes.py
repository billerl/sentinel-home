"""Learned remotes: teaching IR and RF buttons to a BroadLink, pressing them, and the words that press them."""

import _test_env  # noqa: F401  (isolates the event log)

import tempfile
import types
import unittest

from sentinel.engine.remotes import LOCKED, Remotes


class ReadError(Exception):
    pass


class FakeBroadLink:
    """An RM4 Pro: check_data/check_frequency raise until the remote's button is 'pressed'."""

    def __init__(self, host="192.168.1.60", mac="a1b2c3d4e5f6", locked=False):
        self.host, self.mac, self.is_locked = (host, 80), bytes.fromhex(mac), locked
        self.manufacturer, self.model = "Broadlink", "RM4 pro"
        self.sent, self.code, self.freq, self.authed = [], None, None, False
        self.calls = []

    def auth(self):
        self.authed = True

    def send_data(self, code):
        self.sent.append(code)

    def enter_learning(self):
        self.calls.append("learn")

    def check_data(self):
        if self.code is None:
            raise ReadError("nothing yet")
        return self.code

    def sweep_frequency(self):
        self.calls.append("sweep")

    def check_frequency(self):
        return (self.freq is not None, self.freq or 0.0)

    def find_rf_packet(self, frequency=None):
        self.calls.append(("find", frequency))

    def cancel_sweep_frequency(self):
        self.calls.append("cancel")


class NoRF(FakeBroadLink):
    """An RM4 mini: infrared only."""

    def __getattribute__(self, name):
        if name in ("sweep_frequency", "check_frequency", "find_rf_packet"):
            raise AttributeError(name)
        return super().__getattribute__(name)


class TestRemotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = FakeBroadLink()
        self.found = [self.dev]
        self.lib = types.SimpleNamespace(discover=lambda timeout: self.found,
                                         hello=lambda host, timeout: next(d for d in self.found if d.host[0] == host))
        self.t = 1000.0
        self.on_sleep = None
        self.remotes = Remotes(self.tmp.name, lib=self.lib, clock=lambda: self.t, sleep=self._sleep)

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep(self, seconds):
        self.t += seconds
        if self.on_sleep:
            self.on_sleep()

    def teach(self, remote, button, kind="ir", code=None):
        """Learn on this thread (the page starts a thread): the button is 'pressed' after 3 seconds."""
        start = self.t
        code = code or (b"\x26\x00IR" if button.lower() == "volume up" else b"\x26\x00" + button.encode())

        def press():
            if self.t - start >= 3:
                self.dev.freq = 433.92
                if kind == "ir" or ("find", 433.92) in self.dev.calls:
                    self.dev.code = code
        self.on_sleep = press
        self.remotes.learning = {"remote": remote, "button": button, "kind": kind}
        self.remotes._learn(remote, button, kind)
        self.on_sleep, self.dev.code, self.dev.freq = None, None, None
        return self.remotes.learning

    def test_teach_and_press_an_infrared_button(self):
        result = self.teach("Soundbar", "Volume up")
        self.assertEqual(result["step"], "done")
        self.assertEqual(self.remotes.view()["remotes"], [{"name": "Soundbar", "buttons": [{"name": "Volume up", "kind": "ir"}]}])
        self.assertEqual(self.remotes.view()["hub"]["host"], "192.168.1.60")
        ok, message = self.remotes.press("soundbar", "volume UP", times=3)
        self.assertTrue(ok, message)
        self.assertEqual(self.dev.sent, [b"\x26\x00IR"] * 3)
        again = Remotes(self.tmp.name, lib=self.lib)  # kept through a restart
        self.assertEqual(again.view()["remotes"][0]["buttons"][0]["name"], "Volume up")

    def test_teach_a_radio_button_in_two_steps(self):
        result = self.teach("Bedroom fan", "Fan high", kind="rf", code=b"\xb2RF")
        self.assertEqual(result["step"], "done")
        self.assertEqual(self.dev.calls[:2], ["sweep", ("find", 433.92)])
        self.remotes.press("Bedroom fan", "Fan high")
        self.assertEqual(self.dev.sent, [b"\xb2RF"])

    def test_nothing_heard(self):
        self.on_sleep = None
        self.remotes.learning = {"remote": "Heater", "button": "Power", "kind": "rf"}
        self.remotes._learn("Heater", "Power", "rf")
        self.assertEqual(self.remotes.learning["step"], "failed")
        self.assertIn("didn't hear a radio signal", self.remotes.learning["message"])
        self.assertIn("cancel", self.dev.calls)
        self.assertEqual(self.remotes.view()["remotes"], [])

    def test_an_infrared_only_broadlink(self):
        self.dev = NoRF()
        self.found = [self.dev]
        self.remotes.learning = {"remote": "Fan", "button": "High", "kind": "rf"}
        self.remotes._learn("Fan", "High", "rf")
        self.assertIn("RM4 Pro", self.remotes.learning["message"])

    def test_the_last_recording_is_not_the_new_button(self):
        # 2026-11-10: the RM4 handed back the light's recording, and Fan Power and Fan Low were saved as the light
        self.teach("Fan", "Light", kind="rf", code=b"\xb2LIGHT")
        self.dev.code = b"\xb2LIGHT"  # still in its memory: check_data gives it at once
        start = self.t

        def press():
            if self.t - start >= 3:
                self.dev.freq = 433.92
                if ("find", 433.92) in self.dev.calls:
                    self.dev.code = b"\xb2FANLOW"  # ...until the new press comes in
        self.on_sleep = press
        self.remotes.learning = {"remote": "Fan", "button": "Low", "kind": "rf"}
        self.remotes._learn("Fan", "Low", "rf")
        self.assertEqual(self.remotes.learning["step"], "done")
        self.remotes.press("Fan", "Low")
        self.assertEqual(self.dev.sent, [b"\xb2FANLOW"])

    def test_a_copy_of_another_button_is_refused(self):
        self.teach("Fan", "Light", kind="rf", code=b"\xb2LIGHT")
        self.dev.code = None
        result = self.teach("Fan", "Power", kind="rf", code=b"\xb2LIGHT")  # (somehow the same signal again)
        self.assertEqual(result["step"], "failed")
        self.assertIn("exactly like Light", result["message"])
        self.assertEqual([b["name"] for b in self.remotes.view()["remotes"][0]["buttons"]], ["Light"])

    def test_same_command_with_different_timing_is_a_copy(self):
        from sentinel.engine.remotes import command_of

        def rf(bits, wobble=0):
            """A BroadLink radio recording: the burst repeated 6 times, gaps between, a little timing wobble."""
            ticks = []
            for rep in range(6):
                for k, bit in enumerate(bits):
                    long_, short = 28 + (k + rep + wobble) % 3, 13 + (k * wobble) % 2  # (~850 / ~400 us)
                    ticks += [long_, short] if bit == "1" else [short, long_]
                ticks += [0, 1, 132]  # (0x00 then 2 bytes: a ~11.8 ms gap)
            body = bytes(ticks)
            return bytes([0x78, 0xC0]) + len(body).to_bytes(2, "little") + body

        light, light_again, fan = rf("110100101100"), rf("110100101100", wobble=1), rf("110100110010")
        self.assertNotEqual(light, light_again)
        self.assertEqual(command_of(light), command_of(light_again))
        self.assertNotEqual(command_of(light), command_of(fan))
        self.assertIsNone(command_of(b"\x26\x00IR"))
        self.teach("Fan", "Light", kind="rf", code=light)
        self.dev.code = None
        self.assertEqual(self.teach("Fan", "Power", kind="rf", code=light_again)["step"], "failed")
        self.dev.code = None
        self.assertEqual(self.teach("Fan", "Power", kind="rf", code=fan)["step"], "done")

    def test_the_remotes_frequency_is_remembered(self):
        # "Why do I need to hold the button?" (2026-11-10): only to find the frequency, once per remote
        self.teach("Fan", "Light On", kind="rf", code=b"\xb2ON")
        self.assertEqual(self.dev.calls.count("sweep"), 1)
        start, self.dev.calls = self.t, []

        def press():
            if self.t - start >= 2 and ("find", 433.92) in self.dev.calls:
                self.dev.code = b"\xb2LOW"
        self.on_sleep = press
        self.remotes.learning = {"remote": "Fan", "button": "Fan Low", "kind": "rf"}
        self.remotes._learn("Fan", "Fan Low", "rf")
        self.assertEqual(self.remotes.learning["step"], "done")
        self.assertNotIn("sweep", self.dev.calls)  # straight to listening on 433.92
        self.remotes.press("Fan", "Fan Low")
        self.assertEqual(self.dev.sent, [b"\xb2LOW"])

    def test_short_ways_to_say_a_fan_button(self):
        for button in ("Light On", "Light Off", "Fan Off", "Fan Low", "Fan Medium", "Fan High"):
            self.teach("Living Room Ceiling Fan", button)
        self.teach("Heater", "Power")
        p = self.remotes.parse
        for said, button in (("fan high", "Fan High"), ("turn off the ceiling fan", "Fan Off"),
                             ("set the fan to medium", "Fan Medium"), ("fan light on", "Light On"),
                             ("turn off the fan light", "Light Off"), ("living room fan low", "Fan Low")):
            self.assertEqual(p(said), ("Living Room Ceiling Fan", button, 1), said)
        for said in ("turn on the fan", "light on", "turn on the lights", "turn off the tv", "high"):
            self.assertIsNone(p(said), said)  # (not "Light On": the light wasn't asked for)
        self.teach("Bedroom Ceiling Fan", "Fan High")
        self.assertIsNone(p("fan high"))  # two fans now: which one?
        self.assertEqual(p("bedroom fan high"), ("Bedroom Ceiling Fan", "Fan High", 1))

    def test_teaching_again_replaces_the_button(self):
        self.teach("Heater", "Power", code=b"old")
        self.teach("heater", "power", code=b"new")
        self.assertEqual(len(self.remotes.view()["remotes"][0]["buttons"]), 1)
        self.remotes.press("Heater", "Power")
        self.assertEqual(self.dev.sent, [b"new"])

    def test_locked_broadlink(self):
        self.dev.is_locked = True
        ok, message = self.remotes.find_hub()
        self.assertFalse(ok)
        self.assertEqual(message, LOCKED)

    def test_moved_to_a_new_address(self):
        self.teach("Soundbar", "Mute")
        moved = FakeBroadLink(host="192.168.1.77")
        self.found = [moved]
        self.remotes._device = None
        ok, _ = self.remotes.press("Soundbar", "Mute")
        self.assertTrue(ok)
        self.assertEqual(moved.sent, [b"\x26\x00Mute"])
        self.assertEqual(self.remotes.view()["hub"]["host"], "192.168.1.77")

    def test_not_found_waits_before_searching_again(self):
        self.found = []
        searches = []
        self.lib.discover = lambda timeout: searches.append(1) or []
        self.assertFalse(self.remotes.press("x", "y")[0])  # unknown button: no search at all
        self.remotes.remotes = [{"name": "Soundbar", "buttons": [{"name": "Mute", "kind": "ir", "code": "AA=="}]}]
        self.assertFalse(self.remotes.press("Soundbar", "Mute")[0])
        n = len(searches)
        self.assertFalse(self.remotes.press("Soundbar", "Mute")[0])
        self.assertLessEqual(len(searches), n + 1)  # the forced retry only, not a search per press

    def test_words(self):
        self.teach("Soundbar", "Volume up")
        self.teach("Soundbar", "Volume down")
        self.teach("Soundbar", "Power")
        self.teach("Heater", "On")
        self.teach("Bedroom fan", "Fan high")
        p = self.remotes.parse
        self.assertEqual(p("soundbar volume up"), ("Soundbar", "Volume up", 1))
        self.assertEqual(p("Press the soundbar power button please"), ("Soundbar", "Power", 1))
        self.assertEqual(p("power on the soundbar"), ("Soundbar", "Power", 1))
        self.assertEqual(p("soundbar volume down 3 times"), ("Soundbar", "Volume down", 3))
        self.assertEqual(p("soundbar volume down twice"), ("Soundbar", "Volume down", 2))
        self.assertEqual(p("turn the soundbar up"), ("Soundbar", "Volume up", 1))
        self.assertEqual(p("turn up the soundbar five times"), ("Soundbar", "Volume up", 5))
        self.assertEqual(p("turn the heater on"), ("Heater", "On", 1))  # (a button named "On": the short way)
        self.assertEqual(p("heater on"), ("Heater", "On", 1))
        self.assertEqual(p("bedroom fan high"), ("Bedroom fan", "Fan high", 1))  # (said short, 2026-11-10)
        self.assertEqual(p("bedroom fan fan high"), ("Bedroom fan", "Fan high", 1))
        for other in ("what's the weather", "soundbar", "volume up", "turn on the patio lights", ""):
            self.assertIsNone(p(other), other)
        self.assertEqual(self.remotes.command("soundbar volume up 50 times", "voice"), "Okay, Soundbar Volume up (20 times).")

    def test_rename_and_delete(self):
        self.teach("Soundbar", "Vol up")
        self.teach("Soundbar", "Mute")
        self.assertTrue(self.remotes.rename("Soundbar", "Vol up", "Volume up")[0])
        self.assertFalse(self.remotes.rename("Soundbar", "Mute", "volume up")[0])  # taken
        self.assertFalse(self.remotes.rename("Soundbar", None, "bad/name")[0])
        seen = []
        self.remotes.renamed = lambda old, new: seen.append((old, new))  # (the House map follows a renamed remote)
        self.assertTrue(self.remotes.rename("Soundbar", None, "Living room soundbar")[0])
        self.assertEqual(seen, [("Soundbar", "Living room soundbar")])
        self.assertEqual(self.remotes.parse("living room soundbar volume up")[1], "Volume up")
        self.assertTrue(self.remotes.delete("Living room soundbar", "Mute")[0])
        self.assertTrue(self.remotes.delete("Living room soundbar", "Volume up")[0])
        self.assertEqual(self.remotes.view()["remotes"], [])  # the last button takes the remote with it

    def test_switches_hand_over_to_remotes(self):
        from sentinel.engine.switches import SmartSwitches

        self.teach("Soundbar", "Mute")
        switches = SmartSwitches(self.tmp.name, drivers=[])
        switches.extras.append(self.remotes)
        self.assertTrue(switches.understands("soundbar mute"))
        self.assertEqual(switches.command("soundbar mute", "telegram"), "Okay, Soundbar Mute.")
        self.assertIn("Soundbar remote buttons", switches.summary())
        self.assertFalse(switches.understands("kitchen mute"))


if __name__ == "__main__":
    unittest.main()


class TestTrimInfrared(unittest.TestCase):
    """A button taught while held is many copies of one message: sent as one quick tap."""

    @staticmethod
    def code(copies, other_last=False):
        frame = [146, 146, 16, 55, 16, 18] * 4  # (BroadLink ticks: header, then long/short bits)
        pulses = b""
        for i in range(copies):
            body = [146, 146, 16, 18, 16, 55] * 4 if other_last and i == copies - 1 else frame
            pulses += bytes(body) + bytes([0, 6, 0x50])  # then a ~49 ms gap (2 bytes, big-endian)
        return bytes([0x26, 0]) + len(pulses).to_bytes(2, "little") + pulses

    def test_held_button_is_cut_to_two_copies(self):
        from sentinel.engine.remotes import trim_ir
        held = self.code(17)
        self.assertEqual(trim_ir(held), self.code(2))

    def test_a_tap_and_odd_signals_are_left_alone(self):
        from sentinel.engine.remotes import trim_ir
        for code in (self.code(2), self.code(3), bytes([0xb2, 5, 4, 0, 1, 2, 3, 4]), bytes([0x26, 0])):
            self.assertEqual(trim_ir(code), code)  # a tap, a 3-copy tap (the TV's Power), radio, nonsense

    def test_copies_that_differ_are_left_alone(self):
        from sentinel.engine.remotes import trim_ir
        frame_a, frame_b = [146, 146, 16, 55, 16, 18] * 4, [146, 146, 16, 18, 16, 55] * 4
        pulses = b"".join(bytes(f) + bytes([0, 6, 0x50]) for f in (frame_a, frame_b, frame_a, frame_b))
        code = bytes([0x26, 0]) + len(pulses).to_bytes(2, "little") + pulses
        self.assertEqual(trim_ir(code), code)  # not one message repeated: sent as recorded

    def test_presses_send_the_trimmed_signal(self):
        import base64
        r = Remotes(tempfile.mkdtemp(prefix="sentinel-remotes-"))
        sent = []
        r._connect = lambda force=False: types.SimpleNamespace(send_data=sent.append)
        r.remotes = [{"name": "Sound Bar", "buttons": [{"name": "Volume Up", "kind": "ir",
                                                         "code": base64.b64encode(self.code(17)).decode()}]}]
        self.assertTrue(r.press("Sound Bar", "Volume Up")[0])
        self.assertEqual(sent, [self.code(2)])

    def test_quick_presses_are_spaced_out(self):
        import base64
        from sentinel.engine.remotes import IR_QUIET, ir_seconds
        now, slept, sent = [100.0], [], []
        def sleep(s):
            slept.append(round(s, 3)); now[0] += s
        r = Remotes(tempfile.mkdtemp(prefix="sentinel-remotes-"), clock=lambda: now[0], sleep=sleep)
        r._connect = lambda force=False: types.SimpleNamespace(send_data=sent.append)
        r.remotes = [{"name": "Sound Bar", "buttons": [{"name": "Volume Up", "kind": "ir",
                                                         "code": base64.b64encode(self.code(2)).decode()}]}]
        r.press("Sound Bar", "Volume Up")
        r.press("Sound Bar", "Volume Up")  # tapped again straight away: waits for the first, plus quiet
        self.assertEqual(len(sent), 2)
        self.assertEqual(slept, [round(ir_seconds(self.code(2)) + IR_QUIET, 3)])
        now[0] += 5
        r.press("Sound Bar", "Volume Up")  # a slow tap: no wait
        self.assertEqual(len(slept), 1)
