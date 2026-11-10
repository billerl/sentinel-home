"""The Talk button: a phone's recording in, Jarvis's words and voice out (sentinel/engine/phone_talk.py)."""

import _test_env  # noqa: F401  (isolates the event log)

import base64
import subprocess
import unittest

import numpy as np

from sentinel.audio.voice import RATE, VoiceReply, decode_audio
from sentinel.engine.phone_talk import PENDING_SECONDS, PhoneTalk


class FakeVoice:
    def __init__(self, words):
        self.words = list(words)

    def transcribe(self, audio):
        return self.words.pop(0), 0.0


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def synthesize(self, text, voice=""):
        self.said.append(text)
        return b"RIFF" + text.encode()


def make(words, answer, speaker=None, clock=None):
    speaker = speaker or FakeSpeaker()
    voice = FakeVoice(words)
    talk = PhoneTalk({}, voice=lambda: voice, speaker=lambda: speaker, answer=answer, decode=lambda data: data,
                     clock=clock or (lambda: 1000.0))
    return talk, speaker


class TestPhoneTalk(unittest.TestCase):
    def test_answers_with_words_and_voice(self):
        talk, speaker = make(["who's home"], lambda text, say: VoiceReply("Kate is home."))
        out = talk.talk(b"audio")
        self.assertEqual((out["heard"], out["reply"]), ("who's home", "Kate is home."))
        self.assertEqual(base64.b64decode(out["audio"]), b"RIFFKate is home.")
        self.assertNotIn("ask", out)

    def test_thinking_is_left_out_but_other_words_kept(self):
        def answer(text, say):
            say("Let me think.")
            return VoiceReply("Paris.")
        talk, _ = make(["capital of France"], answer)
        self.assertEqual(talk.talk(b"a")["reply"], "Paris.")

    def test_nothing_heard_or_nothing_to_do(self):
        talk, speaker = make([""], lambda text, say: VoiceReply("unused"))
        self.assertEqual(talk.talk(b"a")["reply"], "")
        talk, speaker = make(["stop"], lambda text, say: None)
        self.assertEqual(talk.talk(b"a")["reply"], "")
        self.assertEqual(speaker.said, [])  # no voice for nothing

    def test_yes_no_question(self):
        done = []
        talk, _ = make(["delete the porch routine"],
                       lambda text, say: VoiceReply("Should I delete it? Say yes or no.", lambda: done.append(1) or "Deleted."))
        out = talk.talk(b"a")
        self.assertEqual(out["ask"], "confirm")
        self.assertEqual(talk.answer(out["id"], True)["reply"], "Deleted.")
        self.assertEqual(done, [1])
        self.assertIn("error", talk.answer(out["id"], True))  # once only

    def test_no_and_expiry(self):
        now = [1000.0]
        talk, _ = make(["turn off alerts", "turn off alerts"],
                       lambda text, say: VoiceReply("Should I? Say yes or no.", lambda: "Done."), clock=lambda: now[0])
        out = talk.talk(b"a")
        self.assertEqual(talk.answer(out["id"], False)["reply"], "Okay, I won't.")
        out = talk.talk(b"a")
        now[0] += PENDING_SECONDS + 1
        self.assertIn("error", talk.answer(out["id"], True))

    def test_a_note_is_recorded_next(self):
        notes = []
        talk, _ = make(["take a note for Claude", "the porch light stays on too long"],
                       lambda text, say: VoiceReply("Go ahead.", dictate=lambda said: notes.append(said) or "Saved."))
        out = talk.talk(b"a")
        self.assertEqual((out["reply"], out["ask"]), ("Go ahead.", "dictate"))
        self.assertEqual(talk.talk(b"a", pending=out["id"])["reply"], "Saved.")
        self.assertEqual(notes, ["the porch light stays on too long"])

    def test_voice_off(self):
        talk = PhoneTalk({}, voice=lambda: None, speaker=lambda: None, answer=lambda t, s: None)
        self.assertIn("error", talk.talk(b"a"))

    def test_unreadable_recording(self):
        talk, _ = make(["x"], lambda text, say: VoiceReply("x"))
        talk._decode = lambda data: (_ for _ in ()).throw(ValueError("bad"))
        self.assertEqual(talk.talk(b"a"), {"error": "Couldn't read that recording."})


class TestDecode(unittest.TestCase):
    """What phone browsers send: webm/opus (Android Chrome) and mp4/aac (iPhone Safari)."""

    def encode(self, fmt, codec):
        import imageio_ffmpeg

        tone = (np.sin(np.arange(RATE) * 2 * np.pi * 440 / RATE) * 0.5 * 32767).astype(np.int16).tobytes()
        extra = ["-movflags", "frag_keyframe+empty_moov"] if fmt == "mp4" else []  # (mp4 to a pipe must be fragmented)
        out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1",
                              "-i", "-", "-c:a", codec] + extra + ["-f", fmt, "-"], input=tone, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_formats(self):
        for fmt, codec in (("webm", "libopus"), ("mp4", "aac")):
            audio = decode_audio(self.encode(fmt, codec))
            self.assertAlmostEqual(len(audio) / RATE, 1.0, delta=0.1, msg=fmt)
            self.assertGreater(float(np.abs(audio).max()), 0.3, fmt)

    def test_garbage(self):
        with self.assertRaises(ValueError):
            decode_audio(b"not audio at all")


class TestRoutes(unittest.TestCase):
    def test_signed_in_only_and_bigger_uploads(self):
        import os
        import shutil
        import socket
        import tempfile

        import bcrypt

        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager
        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        d = tempfile.mkdtemp(prefix="sentinel-talk-")
        self.addCleanup(shutil.rmtree, d, True)
        old = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, old)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        sm = SettingsManager(settings_file=os.path.join(d, "settings.json"))
        sm.settings.update({"volume": 0})
        sm.settings["web"].update({"enabled": True, "port": port})
        sm.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        server = WebServer(sm, FrameBroker(), engine=Engine(sm))
        assert server.start()
        client = server._app.test_client()
        h = {"X-Sentinel-Request": "1"}
        self.assertEqual(client.post("/api/talk", json={"audio": ""}, headers=h).status_code, 401)
        client.post("/login", data={"username": "alex", "password": "pw", "remember": "1"})
        big = base64.b64encode(b"x" * 600_000).decode()  # over the usual 256 KB cap, under the Talk button's
        r = client.post("/api/talk", json={"audio": big}, headers=h)
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "Jarvis is turned off (Settings → Audio → voice assistant)."))
        self.assertEqual(client.post("/api/talk", json={"audio": "!!"}, headers=h).status_code, 400)
        self.assertEqual(client.post("/api/talk/answer", json={"id": "nope", "yes": True}, headers=h).status_code, 400)


if __name__ == "__main__":
    unittest.main()
