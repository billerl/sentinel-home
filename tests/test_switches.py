"""Smart plugs: the Kasa wire format, outlets as switches, words -> switches, and the voice/Telegram/web paths."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sentinel.engine import Engine
from sentinel.engine.switches import (SmartSwitches, _kasa_scramble, _kasa_unscramble, guess_icon)
from sentinel.notifications.telegram_notifier import TelegramNotifier
from sentinel.settings.manager import SettingsManager

NOW = 1_800_000_000.0


def ep40(lights=0, outlet=1):
    """An EP40 outdoor plug's answer (two outlets)."""
    return {"model": "EP40(US)", "alias": "TP-LINK_Smart Plug_00AA", "mac": "9C:53:22:00:00:AA", "children": [
        {"id": "80060000", "state": lights, "alias": "patio lights", "on_time": 0},
        {"id": "80060001", "state": outlet, "alias": "patio outlet", "on_time": 3600 if outlet else 0}]}


def hs103(on=0):
    return {"model": "HS103(US)", "alias": "Porch lamp", "mac": "50:C7:BF:00:00:01", "relay_state": on,
            "on_time": 60 if on else 0}


class FakeKasa:
    kind = "kasa"

    def __init__(self):
        self.devices = {"192.168.1.40": ep40(), "192.168.1.50": hs103()}
        self.calls = []
        self.dead = set()

    def discover(self, wait=2.0):
        return [(h, json.loads(json.dumps(i))) for h, i in self.devices.items() if h not in self.dead]

    def status(self, host):
        if host in self.dead:
            raise OSError("timed out")
        return json.loads(json.dumps(self.devices[host]))

    def set(self, host, child_id, on):
        if host in self.dead:
            raise OSError("timed out")
        self.calls.append((host, child_id, on))
        info = self.devices[host]
        if child_id:
            next(c for c in info["children"] if c["id"] == child_id)["state"] = int(on)
        else:
            info["relay_state"] = int(on)

    def rename(self, host, child_id, name):
        info = self.devices[host]
        (next(c for c in info["children"] if c["id"] == child_id) if child_id else info)["alias"] = name


class TestKasa(unittest.TestCase):
    def test_scramble_round_trip(self):
        text = '{"system":{"get_sysinfo":{}}}'
        self.assertNotEqual(_kasa_scramble(text), text.encode())
        self.assertEqual(_kasa_unscramble(_kasa_scramble(text)), text)
        self.assertEqual(_kasa_scramble("{")[0], 171 ^ ord("{"))  # the protocol's starting key

    def test_icons(self):
        self.assertEqual(guess_icon("patio lights"), "light")
        self.assertEqual(guess_icon("Christmas tree"), "tree")
        self.assertEqual(guess_icon("patio outlet"), "plug")
        self.assertEqual(guess_icon("Box fan"), "fan")


class TestSwitches(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.kasa = FakeKasa()
        self.sw = SmartSwitches(self.dir, drivers=[self.kasa], clock=lambda: NOW)
        self.sw.discover()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_outlets_are_switches(self):
        items = {s.name: s for s in self.sw.switches()}
        self.assertEqual(set(items), {"patio lights", "patio outlet", "Porch lamp"})
        self.assertEqual(items["patio lights"].device, "Kasa EP40")  # the plug's own default name is hidden
        self.assertEqual(items["patio lights"].id, "kasa-9c53220000aa-1")
        self.assertEqual(items["Porch lamp"].id, "kasa-50c7bf000001")
        self.assertEqual(items["patio outlet"].since, NOW - 3600)
        self.assertIsNone(items["patio lights"].since)

    def test_set_one_outlet(self):
        ok, said = self.sw.set("kasa-9c53220000aa-1", True)
        self.assertTrue(ok, said)
        self.assertEqual(self.kasa.calls, [("192.168.1.40", "80060000", True)])
        self.assertTrue(next(s for s in self.sw.switches() if s.name == "patio lights").on)
        self.assertFalse(self.sw.set("kasa-9c53220000aa-9", True)[0])
        self.assertFalse(self.sw.set("kasa-9c53220000aa", True)[0])  # a two-outlet plug needs the outlet
        self.assertFalse(self.sw.set("../etc", True)[0])

    def test_known_devices_survive_restart_and_moves(self):
        self.assertEqual(os.stat(os.path.join(self.dir, "switches.json")).st_mode & 0o777, 0o600)
        again = SmartSwitches(self.dir, drivers=[self.kasa], clock=lambda: NOW)
        again.poll()  # no broadcast needed: it remembers where they were
        self.assertEqual(len(again.switches()), 3)
        self.kasa.devices["192.168.1.99"] = self.kasa.devices.pop("192.168.1.40")  # router gave it a new address
        again.discover()
        self.assertTrue(again.set("kasa-9c53220000aa-2", False)[0])
        self.assertEqual(self.kasa.calls[-1][0], "192.168.1.99")

    def test_offline_after_three_misses(self):
        self.kasa.dead.add("192.168.1.50")
        self.sw.poll()
        self.sw.poll()
        self.assertTrue(next(s for s in self.sw.switches() if s.name == "Porch lamp").online)
        self.sw.poll()
        self.assertFalse(next(s for s in self.sw.switches() if s.name == "Porch lamp").online)
        ok, said = self.sw.set("kasa-50c7bf000001", True)
        self.assertFalse(ok)
        self.assertIn("isn't answering", said)

    def test_words(self):
        def names(text):
            parsed = self.sw.parse(text)
            return parsed and (parsed[0], sorted(s.name for s in parsed[1]))

        self.assertEqual(names("turn on the patio lights"), (True, ["patio lights"]))
        self.assertEqual(names("Patio light off"), (False, ["patio lights"]))
        self.assertEqual(names("switch the patio outlet off please"), (False, ["patio outlet"]))
        self.assertEqual(names("are the patio lights on"), (None, ["patio lights"]))
        self.assertEqual(names("is the porch lamp on or off"), (None, ["Porch lamp"]))
        self.assertEqual(names("lights on"), (True, ["Porch lamp", "patio lights"]))
        self.assertEqual(names("turn off patio"), (False, ["patio lights", "patio outlet"]))
        for other in ("turn on alerts", "turn the music off", "enable recording", "come on", "is anyone home"):
            self.assertIsNone(self.sw.parse(other), other)

    def test_command_reply(self):
        self.assertEqual(self.sw.command("turn on the patio lights", "voice"), "Okay, patio lights on.")
        self.assertEqual(self.sw.command("are the patio lights on", "voice"), "patio lights are on.")
        self.assertEqual(self.sw.command("is the porch lamp on", "voice"), "Porch lamp is off.")
        self.assertIsNone(self.sw.command("what time is it", "voice"))

    def test_rename_writes_to_the_plug(self):
        ok, name = self.sw.rename("kasa-9c53220000aa-1", "  String  lights ")
        self.assertTrue(ok)
        self.assertEqual(name, "String lights")
        self.assertEqual(self.kasa.devices["192.168.1.40"]["children"][0]["alias"], "String lights")
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "porch lamp")[0])  # taken
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "<script>")[0])
        self.assertFalse(self.sw.rename("kasa-9c53220000aa-2", "x" * 31)[0])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings.update({"volume": 0, "assistant_enabled": True})
        self.engine = Engine(manager)
        self.kasa = FakeKasa()
        self.engine.switches = SmartSwitches(self.dir, drivers=[self.kasa])
        self.engine.switches.discover()
        self.engine.telegram.switch_filter = lambda text: self.engine.switches.parse(text) is not None
        self.sent = []
        self.engine.telegram.reply_to_chat = lambda chat, text: self.sent.append((chat, text)) or True

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_voice_switches_straight_away(self):
        reply = self.engine.answer_voice("Turn on the patio lights.", lambda _s: None)
        self.assertEqual(reply.text, "Okay, patio lights on.")
        self.assertIsNone(reply.confirm)
        self.assertEqual(self.kasa.calls, [("192.168.1.40", "80060000", True)])

    def test_claude_suggestion_asks_first(self):
        with mock.patch.object(self.engine.assistant, "ask",
                               return_value="I can turn the porch lamp on for you.\nCOMMAND: Turn on porch lamp"), \
                mock.patch.object(self.engine.assistant, "remaining_today", return_value=5):
            reply = self.engine.answer_voice("it's dark at the front door", lambda _s: None)
        self.assertIn("Should I do that?", reply.text)
        self.assertEqual(self.kasa.calls, [])
        self.assertEqual(reply.confirm(), "Okay, Porch lamp on.")

    def test_telegram(self):
        t = self.engine.telegram
        t.route_text("111", "Are the patio lights on?")  # not taken for a camera question
        t.route_text("111", "patio outlet off")
        self.assertEqual(t.consume_questions(), [])
        for chat, text in t.consume_switch_commands():
            self.engine._switch_reply(chat, text)
        self.assertEqual(self.sent, [("111", "💡 patio lights are off."), ("111", "💡 Okay, patio outlet off.")])
        self.assertTrue(TelegramNotifier.is_command("Turn on patio lights"))  # Claude may offer it as a button
        self.assertFalse(TelegramNotifier.is_command("turn on the oven and burn the house down; rm -rf"))

    def test_claude_sees_them(self):
        self.assertIn("SMART PLUGS AND SWITCHES", self.engine.assistant_context())
        self.assertIn("patio outlet: on", self.engine.assistant_context())


class TestWeb(unittest.TestCase):
    def setUp(self):
        import socket

        import bcrypt

        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.kasa = FakeKasa()
        self.sw = SmartSwitches(self.dir, drivers=[self.kasa])
        self.sw.discover()
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        manager.settings["web"].update({"enabled": True, "port": port})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.server = WebServer(manager, FrameBroker(), engine=mock.Mock(switches=self.sw))
        self.assertTrue(self.server.start())
        self.server._auth.validate_session = lambda token, ip: mock.Mock(username="alex")
        self.client = self.server._app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body, header=True):
        return self.client.post(path, json=body, headers={"X-Sentinel-Request": "1"} if header else {})

    def test_list_and_set(self):
        data = self.client.get("/api/switches").get_json()
        self.assertEqual(len(data["switches"]), 3)
        r = self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(next(s for s in r.get_json()["switches"] if s["name"] == "Porch lamp")["on"])
        self.assertEqual(self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": "yes"}).status_code, 400)
        self.assertEqual(self.post("/api/switches/set", {"id": "kasa-50c7bf000001", "on": False}, header=False)
                         .status_code, 400)  # no cross-site requests
        self.assertEqual(len(self.kasa.calls), 1)



class FakeTV:
    """A Samsung TV as SamsungTVDriver presents it to SmartSwitches."""
    kind = "samsung"

    def __init__(self):
        self.on, self.calls, self.visible = False, [], None
        self.info = {"mac": "00:11:22:33:44:55", "model": "Samsung UN65NU6900", "alias": "TV"}

    def discover(self, wait=2.0):
        return [("192.168.1.60", self.status("192.168.1.60"))]

    def status(self, host):
        return {**self.info, "relay_state": int(self.on), "on_time": 0}

    def set(self, host, child_id, on):
        self.calls.append(("power", on))
        self.on = on

    def rename(self, host, child_id, name):
        self.info["alias"] = name

    def apps(self, host):
        return {"11101200001": "Netflix", "3201901017640": "Disney+", "3201707014489": "YouTube TV",
                "111299001912": "YouTube", "3201606009684": "Spotify - Music and Podcasts",
                "3201808016802": "Pluto TV: Free Movies & Shows"}

    def launch(self, host, app_id):
        self.calls.append(("launch", app_id))
        self.visible = self.apps(host)[app_id]

    def press(self, host, *keys):
        self.calls.append(("press",) + keys)

    def showing(self, host):
        return self.visible

    def type_text(self, host, text):
        self.calls.append(("type", text))

    def icon(self, host, app_id):
        return b"\x89PNG fake" if app_id == "11101200001" else None


class TestTV(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.kasa, self.tv = FakeKasa(), FakeTV()
        self.sw = SmartSwitches(self.dir, drivers=[self.kasa, self.tv], clock=lambda: NOW)
        self.sw.discover()
        sleep = mock.patch("sentinel.engine.switches.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_tv_is_a_switch(self):
        tv = next(s for s in self.sw.switches() if s.name == "TV")
        self.assertEqual(tv.icon, "tv")
        self.assertEqual(self.sw.command("turn on the TV", "voice"), "Okay, TV on.")
        self.assertEqual(self.tv.calls, [("power", True)])
        self.assertEqual(self.sw.command("is the tv on", "telegram"), "TV is on.")

    def test_apps_by_name(self):
        self.assertEqual(self.sw.command("put on Netflix", "voice"), "Okay, Netflix on the TV.")
        self.assertEqual(self.tv.calls, [("power", True), ("launch", "11101200001")])  # it was off: on first
        self.assertEqual(self.sw.command("open disney plus on the tv", "voice"), "Okay, Disney+ on the TV.")
        self.assertEqual(self.sw.command("turn on youtube tv", "voice"), "Okay, YouTube TV on the TV.")
        self.assertEqual(self.sw.command("open youtube", "voice"), "Okay, YouTube on the TV.")
        self.assertEqual(self.sw.command("launch pluto", "voice"), "Okay, Pluto TV: Free Movies & Shows on the TV.")
        self.assertEqual(self.sw.command("what's on the tv", "voice"), "Pluto TV: Free Movies & Shows is on the TV.")

    def test_turn_on_the_tv_to_an_app(self):
        for text, app in (("turn on the TV to Netflix", "Netflix"), ("turn the tv on to disney plus", "Disney+"),
                          ("put the TV on YouTube TV", "YouTube TV"), ("switch the TV to youtube", "YouTube"),
                          ("Turn on the TV to Pluto TV please", "Pluto TV: Free Movies & Shows")):
            self.assertEqual(self.sw.command(text, "voice"), f"Okay, {app} on the TV.", text)
        self.assertEqual(self.tv.calls[:2], [("power", True), ("launch", "11101200001")])
        said = self.sw.command("turn on the TV to Judge Judy", "voice")
        self.assertTrue(said.startswith("I couldn't find Judge Judy on the TV. I can open its apps, like Netflix"), said)
        self.assertEqual(self.sw.command("turn the TV on", "voice"), "Okay, TV on.")  # no app: just power
        self.assertNotIn("Gallery", self.sw.tv_apps())

    def test_volume_and_mute(self):
        self.assertEqual(self.sw.command("tv volume up", "voice"), "The TV is off.")
        self.tv.on = True
        self.sw.poll()
        self.assertEqual(self.sw.command("turn the tv down", "voice"), "Okay, TV quieter.")
        self.assertEqual(self.tv.calls[-1], ("press", "KEY_VOLDOWN", "KEY_VOLDOWN", "KEY_VOLDOWN"))
        self.assertEqual(self.sw.command("mute the TV", "telegram"), "Okay, TV muted.")
        self.assertEqual(self.tv.calls[-1], ("press", "KEY_MUTE"))

    def test_other_words_are_left_alone(self):
        self.assertEqual(self.sw.command("turn on the patio lights", "voice"), "Okay, patio lights on.")
        self.assertNotIn(("launch",), [c[:1] for c in self.tv.calls])
        for text in ("put on some music", "play some Tom Petty", "open the garage", "turn on alerts"):
            self.assertIsNone(self.sw.command(text, "voice"), text)
            self.assertFalse(self.sw.understands(text), text)
        self.assertTrue(self.sw.understands("put on Netflix"))
        self.assertIn("TV apps (say 'open <app>'):", self.sw.summary())
        self.assertIn("Pluto TV", self.sw.summary())


class TestSamsungDriver(unittest.TestCase):
    """The power dance: a TV that's off doesn't answer; the wake packet wakes only its network (picture
    still dark) and the power button lights the picture. Tested on a UN65NU6900, 2026-11-03."""

    def setUp(self):
        from sentinel.engine.samsung_tv import SamsungTVDriver
        self.dir = tempfile.mkdtemp(prefix="sentinel-tv-")
        self.drv = SamsungTVDriver(self.dir)
        self.net, self.picture, self.log = False, False, []
        self.drv._rest = self.rest
        self.drv._wake = lambda mac, host: self.log.append("wake") or setattr(self, "net", True)
        self.drv.press = lambda host, *keys: self.log.append(keys) or self.power()
        patches = [mock.patch("sentinel.engine.samsung_tv.time.sleep")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.drv._learn("192.168.1.60", {"device": {"type": "Samsung SmartTV", "name": "[TV] TV",
                                                    "modelName": "UN65NU6900", "wifiMac": "00:11:22:33:44:55"}})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rest(self, host, path="", method="GET", timeout=None):
        if not self.net:
            raise OSError("timed out")
        return {"device": {"type": "Samsung SmartTV"}}

    def power(self):
        self.picture = not self.picture
        self.net = self.picture

    def test_learns_the_tv(self):
        info = self.drv.status("192.168.1.60")
        self.assertEqual((info["alias"], info["model"], info["mac"], info["relay_state"]),
                         ("TV", "Samsung UN65NU6900", "001122334455", 0))  # off: not answering, no error
        with open(os.path.join(self.dir, "samsung_tv.json")) as f:
            self.assertIn("001122334455", json.load(f))
        self.assertEqual(oct(os.stat(os.path.join(self.dir, "samsung_tv.json")).st_mode & 0o777), "0o600")

    def test_on_wakes_then_presses_power(self):
        self.drv.set("192.168.1.60", None, True)
        self.assertEqual(self.log, ["wake", ("KEY_POWER",)])
        self.assertTrue(self.picture)
        self.log.clear()
        self.drv.set("192.168.1.60", None, True)  # already on: no button (it would switch it off)
        self.assertEqual(self.log, [])

    def test_off_presses_power_once(self):
        self.net = self.picture = True
        self.drv._screen("001122334455", True)  # known to be on
        self.drv.set("192.168.1.60", None, False)
        self.drv.set("192.168.1.60", None, False)
        self.assertEqual(self.log, [("KEY_POWER",)])
        self.assertFalse(self.picture)

    def test_a_dark_wake_up_isnt_on(self):
        """Switched off, the TV wakes its network for ~2 minutes now and then with the screen dark."""
        clock = [NOW]
        with mock.patch("sentinel.engine.samsung_tv.time.time", side_effect=lambda: clock[0]):
            self.net = True  # awake, picture dark
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 0)
            clock[0] += 130
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 0)
            self.drv.set("192.168.1.60", None, False)  # "off" now must not press power (it would light it)
            self.assertEqual(self.log, [])
            self.drv.set("192.168.1.60", None, True)  # 2026-11-03 13:18: this used to be taken for "already on"
            self.assertEqual(self.log, [("KEY_POWER",)])  # no wake packet needed: it's awake
            self.assertTrue(self.picture)
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 1)

    def test_on_with_its_own_remote_shows_after_a_while(self):
        clock = [NOW]
        with mock.patch("sentinel.engine.samsung_tv.time.time", side_effect=lambda: clock[0]):
            self.net = self.picture = True
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 0)
            clock[0] += 250  # longer than any dark wake-up
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 1)
            self.net = self.picture = False  # off with its own remote: gone at once
            clock[0] += 10
            self.assertEqual(self.drv.status("192.168.1.60")["relay_state"], 0)

    def test_never_woken_tv_says_so(self):
        self.drv._wake = lambda mac, host: self.log.append("wake")  # "Power On with Mobile" is off
        with mock.patch("sentinel.engine.samsung_tv.time.time", side_effect=[NOW + i for i in range(200)]):
            with self.assertRaises(OSError):
                self.drv.set("192.168.1.60", None, True)
        self.assertNotIn(("KEY_POWER",), self.log)

    def test_app_names(self):
        from sentinel.engine.switches import _app_words
        self.assertEqual(_app_words("Disney+"), _app_words("disney plus"))
        self.assertEqual(_app_words("Spotify - Music and Podcasts"), ["spotify"])
        self.assertEqual(_app_words("Pluto TV: Free Movies & Shows"), ["pluto", "tv"])
        self.assertNotEqual(_app_words("YouTube TV"), _app_words("YouTube"))


class TestVoiceHint(unittest.TestCase):
    def test_tv_apps_in_the_hint(self):
        from sentinel.audio.voice import whisper_prompt
        hint = whisper_prompt(["Kate"], ["patio lights"], ["Netflix", "Disney+", "Paramount+"])
        self.assertIn("Turn on the TV to Netflix. Put on Disney Plus, Paramount Plus.", hint)
        self.assertNotIn("TV to", whisper_prompt(["Kate"], ["patio lights"]))


class TestRemote(unittest.TestCase):
    """The web remote: buttons from a fixed list, apps by id, typing, the TV's own app icons."""

    def setUp(self):
        import socket

        import bcrypt

        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-switches-")
        self.tv = FakeTV()
        self.sw = SmartSwitches(self.dir, drivers=[FakeKasa(), self.tv])
        self.sw.discover()
        sleep = mock.patch("sentinel.engine.switches.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        manager.settings["web"].update({"enabled": True, "port": port})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.server = WebServer(manager, FrameBroker(), engine=mock.Mock(switches=self.sw))
        self.assertTrue(self.server.start())
        self.server._auth.validate_session = lambda token, ip: mock.Mock(username="alex")
        self.client = self.server._app.test_client()
        self.tv_id = "samsung-001122334455"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body, header=True):
        return self.client.post(path, json=body, headers={"X-Sentinel-Request": "1"} if header else {})

    def test_view_lists_apps_without_system_ones(self):
        self.tv.apps = lambda host: {"11101200001": "Netflix", "x1": "Gallery", "3201808016802": "Pluto TV: Free Movies & Shows"}
        v = self.client.get(f"/api/tv/{self.tv_id}").get_json()
        self.assertEqual(v["name"], "TV")
        self.assertEqual([a["name"] for a in v["apps"]], ["Netflix", "Pluto TV"])
        self.assertEqual(self.client.get("/api/tv/kasa-50c7bf000001").status_code, 404)  # a plug has no remote

    def test_buttons(self):
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/key", {"key": "up"}).status_code, 200)
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/key", {"key": "vol_down"}).status_code, 200)
        self.assertEqual(self.tv.calls, [("press", "KEY_UP"), ("press", "KEY_VOLDOWN")])
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/key", {"key": "KEY_FACTORY"}).status_code, 400)  # not on the list
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/key", {"key": "up"}, header=False).status_code, 400)
        self.assertEqual(len(self.tv.calls), 2)

    def test_apps_and_typing(self):
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/open", {"app": "11101200001"}).status_code, 200)
        self.assertEqual(self.tv.calls, [("power", True), ("launch", "11101200001")])  # it was off
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/open", {"app": "999"}).status_code, 400)
        self.assertEqual(self.post(f"/api/tv/{self.tv_id}/type", {"text": "judge judy"}).status_code, 200)
        self.assertEqual(self.tv.calls[-1], ("type", "judge judy"))

    def test_icons(self):
        r = self.client.get(f"/api/tv/{self.tv_id}/icon/11101200001")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/png", b"\x89PNG fake"))
        self.assertEqual(self.client.get(f"/api/tv/{self.tv_id}/icon/3201808016802").status_code, 404)



class FakeFireShell:
    """What a Fire TV answers to the ADB shell commands FireTVDriver sends."""

    def __init__(self):
        self.awake, self.focus, self.commands = True, "de.ozerov.fully", []

    def __call__(self, host, command, timeout=None):
        self.commands.append(command)
        if command.startswith("getprop ro.product.manufacturer"):
            return "Amazon\nAFTEAMR311\nAlex's 2nd TV\nG000SERIAL00001\n"
        if command.startswith("dumpsys power"):
            return "  mWakefulness=" + ("Awake" if self.awake else "Asleep") + "\n"
        if command.startswith("pm list packages -3"):
            return ("package:de.ozerov.fully\npackage:com.netflix.ninja\n---\npackage:com.amazon.avod\n"
                    "package:com.amazon.firebat\npackage:com.amazon.tv.launcher\npackage:com.droidlogic.factory\n"
                    "package:de.ozerov.fully\npackage:com.netflix.ninja\n")
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{7c9a212 u0 {self.focus}/{self.focus}.MainActivity}}\n"
        if "KEYCODE_WAKEUP" in command and "monkey" not in command:
            self.awake = True
        if "KEYCODE_SLEEP" in command:
            self.awake = False
        return ""


class TestFireTV(unittest.TestCase):
    def setUp(self):
        from sentinel.engine.fire_tv import FireTVDriver
        self.dir = tempfile.mkdtemp(prefix="sentinel-fire-")
        self.fire = FireTVDriver(self.dir)
        self.shell = FakeFireShell()
        self.fire._shell = self.shell
        self.fire._scan = lambda: ["192.168.1.70"]
        self.tv = FakeTV()
        self.sw = SmartSwitches(self.dir, drivers=[FakeKasa(), self.tv, self.fire], clock=lambda: NOW)
        self.sw.discover()
        sleeps = [mock.patch("sentinel.engine.switches.time.sleep"), mock.patch("sentinel.engine.fire_tv.time.sleep")]
        for p in sleeps:
            p.start()
            self.addCleanup(p.stop)
        fire = next(s for s in self.sw.switches() if s.device.startswith("Fire") or s.model.startswith("Fire"))
        self.assertTrue(self.sw.rename(fire.id, "Sentinel TV")[0])
        self.fire_id = fire.id

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_found_with_its_real_screen_state(self):
        fire = next(s for s in self.sw.switches() if s.id == self.fire_id)
        self.assertEqual((fire.name, fire.icon, fire.on, fire.model), ("Sentinel TV", "tv", True, "Fire TV AFTEAMR311"))
        self.assertTrue(self.fire_id.startswith("firetv-") and len(self.fire_id) == len("firetv-") + 12)
        self.assertEqual(self.sw.command("turn off the sentinel tv", "voice"), "Okay, Sentinel TV off.")
        self.assertIn("input keyevent KEYCODE_SLEEP", self.shell.commands)
        self.assertFalse(next(s for s in self.sw.switches() if s.id == self.fire_id).on)
        self.assertEqual(oct(os.stat(os.path.join(self.dir, "fire_tv.json")).st_mode & 0o777), "0o600")

    def test_apps_are_what_people_open(self):
        self.assertEqual(self.fire.apps("192.168.1.70"), {"de.ozerov.fully": "Sentinel live view",
                                                          "com.netflix.ninja": "Netflix", "com.amazon.avod": "Prime Video"})

    def test_commands_go_to_the_tv_named(self):
        self.assertEqual(self.sw.command("open prime video on the sentinel tv", "voice"), "Okay, Prime Video on the Sentinel TV.")
        self.assertIn("monkey -p com.amazon.avod", self.shell.commands[-1])
        self.assertEqual(self.sw.command("put on Netflix", "voice"), "Okay, Netflix on the TV.")  # the plain TV: the Samsung
        self.assertEqual(self.tv.calls[-1], ("launch", "11101200001"))
        self.assertEqual(self.sw.command("turn the fire tv down", "voice"), "Okay, Sentinel TV quieter.")
        self.assertIn("KEYCODE_VOLUME_DOWN", self.shell.commands[-1])
        self.assertEqual(self.sw.command("what's on the sentinel tv", "telegram"), "Sentinel live view is on the Sentinel TV.")

    def test_cameras_on_the_tv(self):
        self.assertEqual(self.sw.command("put the cameras on the TV", "voice"), "Okay, showing the cameras on the Sentinel TV.")
        self.assertIn("monkey -p de.ozerov.fully", self.shell.commands[-1])
        self.assertEqual(self.sw.command("reload the cameras", "telegram"), "Okay, reloading the cameras on the Sentinel TV.")
        self.assertTrue(any(c == "am force-stop de.ozerov.fully" for c in self.shell.commands))
        self.assertTrue(self.sw.understands("show the live view on the sentinel tv"))
        self.assertFalse(self.sw.understands("show the cameras"))  # no TV named: left to the camera questions

    def test_remote(self):
        v = self.sw.tv_view(self.fire_id)
        self.assertTrue(v["live"])
        self.assertNotIn("source", v["keys"])  # no such button on a Fire TV
        self.assertIn("up", v["keys"])
        self.assertEqual([a["name"] for a in v["apps"]], ["Netflix", "Prime Video"])  # the live view has its own buttons
        self.assertEqual(self.sw.tv_key(self.fire_id, "source"), (False, "I don't know that button."))
        self.assertTrue(self.sw.tv_key(self.fire_id, "ok")[0])
        self.assertEqual(self.shell.commands[-1], "input keyevent KEYCODE_DPAD_CENTER")
        self.assertTrue(self.sw.tv_type(self.fire_id, "judge judy; rm -rf /")[0])
        self.assertEqual(self.shell.commands[-1], "input text judge%sjudy%srm%s-rf%s")  # no shell tricks get through
        self.sw.tv_type(self.fire_id, "don't stop")
        self.assertEqual(self.shell.commands[-1], "input text 'don'\"'\"'t%sstop'")
        self.assertTrue(self.sw.tv_live(self.fire_id, reload=False)[0])
        with self.assertRaises(ValueError):
            self.fire.launch("192.168.1.70", "x; reboot")


if __name__ == "__main__":
    unittest.main()
