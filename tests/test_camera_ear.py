import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import tempfile
import time
import types
import unittest

import numpy as np

from sentinel.audio.camera_ear import CHUNK, WAKE_MODEL, CameraEar
from sentinel.engine.camera_light import WHITE, CameraLights
from sentinel.engine.engine import Engine
from sentinel.engine.voice_log import VoiceLog


class FakeWake:
    def __init__(self):
        self.scores = []  # what the next predicts say

    def predict(self, pcm):
        return {WAKE_MODEL: self.scores.pop(0) if self.scores else 0.0}

    def reset(self):
        pass


class FakeLight:
    def __init__(self):
        self.calls = []

    def listening(self, host):
        self.calls.append(("listening", host))
        return True

    def answer(self, host, flashes):
        self.calls.append(("answer", host, flashes))


def quiet():
    return np.zeros(CHUNK, np.float32)


def loud():
    return (0.2 * np.sin(np.arange(CHUNK) / 3.0)).astype(np.float32)


class TestCameraEar(unittest.TestCase):
    def make(self, act):
        self.heard, self.light, self.wake = [], FakeLight(), FakeWake()

        def transcribe(audio):
            self.heard.append(len(audio))
            return "turn on the patio lights"
        ear = CameraEar("192.0.2.10", "rtsp://cam/main", transcribe, act, light=self.light, wake_model=self.wake,
                        threshold=lambda: 0.89, ffmpeg="ffmpeg")
        ear._flush = False
        return ear

    def say_command(self, ear):
        self.wake.scores = [0.95]
        ear._on_chunk(loud())  # "Hey Jarvis"
        self.assertEqual(ear._mode, "listen")
        for _ in range(10):
            ear._on_chunk(loud())  # "turn on the patio lights"
        for _ in range(20):
            ear._on_chunk(quiet())
        deadline = time.time() + 5
        while not any(c[0] == "answer" for c in self.light.calls) and time.time() < deadline:
            time.sleep(0.05)

    def test_a_command_is_done_and_the_light_flashes_once(self):
        acted = []
        ear = self.make(lambda text: acted.append(text) or True)
        self.say_command(ear)
        self.assertEqual(acted, ["turn on the patio lights"])
        self.assertEqual(self.light.calls, [("listening", "192.0.2.10"), ("answer", "192.0.2.10", 1)])
        self.assertEqual(ear._mode, "wake")

    def test_not_understood_flashes_twice(self):
        # (the listener itself writes no words: Engine._ear_command notes what was said after the wake word)
        ear = self.make(lambda text: False)
        from sentinel.logging_utils import app_logger
        logged = []
        real = app_logger.log
        app_logger.log = lambda *a, **k: logged.append((a, k))
        try:
            self.say_command(ear)
        finally:
            app_logger.log = real
        self.assertEqual(self.light.calls[-1], ("answer", "192.0.2.10", 2))
        self.assertTrue(logged)
        self.assertNotIn("patio", json.dumps([k for _, k in logged]))

    def test_nothing_said_after_the_wake_word(self):
        acted = []
        ear = self.make(lambda text: acted.append(text) or True)
        self.wake.scores = [0.95]
        ear._on_chunk(loud())
        for _ in range(60):  # past WAIT_FOR_SPEECH
            ear._on_chunk(quiet())
        deadline = time.time() + 5
        while not self.light.calls[1:] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(acted, [])
        self.assertEqual(self.heard, [])  # (nothing to turn into words)
        self.assertEqual(self.light.calls[-1], ("answer", "192.0.2.10", 2))

    def test_below_the_threshold_it_stays_asleep(self):
        ear = self.make(lambda text: True)
        self.wake.scores = [0.6, 0.7]
        ear._on_chunk(loud())
        ear._on_chunk(loud())
        self.assertEqual(ear._mode, "wake")
        self.assertEqual(self.light.calls, [])

    def test_ffmpeg_asks_for_the_sound_only_into_memory(self):
        started = []

        class Proc:
            stdout = types.SimpleNamespace(read=lambda n: b"")
            stderr = types.SimpleNamespace(read=lambda: b"rtsp://admin:secret@cam: 404")

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0
        ear = CameraEar("192.0.2.10", "rtsp://admin:secret@cam/main", lambda a: "", lambda t: False,
                        popen=lambda args, **kw: started.append(args) or Proc(), wake_model=FakeWake(), ffmpeg="ffmpeg")
        ear._listen()
        args = started[0]
        self.assertIn("-allowed_media_types", args)
        self.assertEqual(args[args.index("-allowed_media_types") + 1], "audio")
        self.assertEqual(args[-1], "pipe:1")  # never a file
        self.assertNotIn("secret", ear.last_error)


class TestOutsideCommands(unittest.TestCase):
    """Engine._ear_command: from the yard, only plugs and lights."""

    def setUp(self):
        self.log = VoiceLog(tempfile.mkdtemp(prefix="sentinel-voicelog-"))

    def run_it(self, parsed):
        sets = []
        sw = types.SimpleNamespace(parse=lambda text: parsed, set=lambda sid, on, src: sets.append((sid, on, src)) or (True, "ok"))
        fake = types.SimpleNamespace(switches=sw, voice_log=self.log)
        return Engine._ear_command(fake, "turn on the patio lights", "Back Yard"), sets

    def test_lights_and_plugs_only(self):
        patio = types.SimpleNamespace(id="kasa-1", name="patio lights", icon="light")
        tv = types.SimpleNamespace(id="samsung-1", name="TV", icon="tv")
        self.assertEqual(self.run_it((True, [patio])), (True, [("kasa-1", True, "voice (Back Yard)")]))
        self.assertEqual(self.run_it((True, [tv])), (False, []))  # the TVs: not from outside
        self.assertEqual(self.run_it((None, [patio])), (False, []))  # "are the patio lights on?": can't answer
        self.assertEqual(self.run_it(None), (False, []))  # "quiet the cameras", anything else: no
        # the timeline's Voice row: where, what was said to Jarvis (after the wake word only) and what was done
        rows = self.log.between(0, time.time() + 1)
        self.assertEqual([(r["place"], r["said"], r["did"], r["ok"]) for r in rows][0],
                         ("Back Yard", "turn on the patio lights", "patio lights on", True))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["said"] == "turn on the patio lights" for r in rows))
        self.assertFalse(rows[1]["ok"])


class TestVoiceLog(unittest.TestCase):
    def test_requests_by_day_and_old_ones_go(self):
        now = [1_800_000_000.0]
        log = VoiceLog(tempfile.mkdtemp(prefix="sentinel-voicelog-"), clock=lambda: now[0])
        log.add("Inside", "turn on the patio lights", "Okay, turning on the patio lights.")
        log.add("Phone", "x" * 500, "ok", ts=now[0] - 3600)
        rows = log.between(now[0] - 7200, now[0] + 1)
        self.assertEqual([r["place"] for r in rows], ["Phone", "Inside"])
        self.assertEqual(len(rows[0]["said"]), 200)
        now[0] += 100 * 86400
        log.add("Inside", "hello", "hi")  # (a day later than the last tidy: the old ones go)
        self.assertEqual(len(log.between(0, now[0] + 1)), 1)


class TestAnswerFrom(unittest.TestCase):
    def test_the_house_mic_and_phone_note_where_a_request_came_from(self):
        log = VoiceLog(tempfile.mkdtemp(prefix="sentinel-voicelog-"))
        fake = types.SimpleNamespace(voice_log=log,
                                     answer_voice=lambda text, say, doubtful=False: types.SimpleNamespace(text="Okay."))
        answer = Engine._answer_from(fake, "Inside")
        self.assertEqual(answer("turn on the patio lights", print).text, "Okay.")
        Engine._answer_from(fake, "Phone")("what's the weather", print)
        rows = log.between(0, time.time() + 1)
        self.assertEqual([(r["place"], r["said"], r["did"]) for r in rows],
                         [("Inside", "turn on the patio lights", "Okay."), ("Phone", "what's the weather", "Okay.")])


class TestLightSignals(unittest.TestCase):
    ORIGINAL = "<SupplementLight><supplementLightMode>eventIntelligence</supplementLightMode></SupplementLight>"

    def make(self, logins):
        d = tempfile.mkdtemp(prefix="sentinel-sig-")
        admin = os.path.join(d, "admin.json")
        with open(admin, "w") as f:
            json.dump(logins, f)
        self.puts = []

        def http(host, method, body):
            if method == "PUT":
                self.puts.append(body.split("<supplementLightMode>")[1].split("<")[0])
            return self.ORIGINAL
        lights = CameraLights(admin_path=admin, state_path=os.path.join(d, "state.json"), http=http)
        lights._sleep = lambda s: None
        return lights, os.path.join(d, "state.json")

    def test_listening_then_one_or_two_flashes_then_back(self):
        lights, state = self.make({"192.0.2.10": {"user": "admin", "password": "x", "car_light": False}})
        self.assertTrue(lights.listening("192.0.2.10"))
        self.assertEqual(self.puts, [WHITE])
        self.assertTrue(os.path.exists(state))  # (put back after a crash)
        lights.answer("192.0.2.10", 2)
        self.assertEqual(self.puts, [WHITE, "close", WHITE, "close", WHITE, "close", "eventIntelligence"])
        self.assertFalse(os.path.exists(state))
        self.puts.clear()
        lights.listening("192.0.2.10")
        lights.answer("192.0.2.10", 1)
        self.assertEqual(self.puts, [WHITE, "close", WHITE, "close", "eventIntelligence"])

    def test_a_signals_only_camera_is_not_lit_for_cars(self):
        lights, _ = self.make({"192.0.2.10": {"user": "admin", "password": "x", "car_light": False},
                               "192.0.2.9": {"user": "admin", "password": "y"}})
        self.assertFalse(lights.available("192.0.2.10"))
        self.assertTrue(lights.can_signal("192.0.2.10"))
        self.assertTrue(lights.available("192.0.2.9"))
        self.assertFalse(lights.listening("192.0.2.77"))  # (no login)

    def test_sound_is_turned_on_in_the_stream(self):
        lights, _ = self.make({"192.0.2.10": {"user": "admin", "password": "x"}})
        sent = []
        xml = ("<StreamingChannel><Video><enabled>true</enabled></Video><Audio><enabled>false</enabled>"
               "<audioCompressionType>G.711ulaw</audioCompressionType></Audio></StreamingChannel>")

        def request(host, method, body, path):
            sent.append((method, path, body))
            return xml
        lights._request = request
        self.assertTrue(lights.ensure_sound("192.0.2.10"))
        method, path, body = sent[-1]
        self.assertEqual((method, path), ("PUT", "/ISAPI/Streaming/channels/101"))
        self.assertIn("<Video><enabled>true</enabled></Video><Audio><enabled>true</enabled>", body)


if __name__ == "__main__":
    unittest.main()
