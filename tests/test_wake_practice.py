""""Train Jezebel": the wake word recorded after each beep, kept for retraining its model."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest
import wave
from unittest import mock

import numpy as np

from sentinel.audio.voice import CHUNK, PRACTICE_CLIPS, RATE, VoiceAssistant, VoiceReply, practice_over, trim_speech
from sentinel.audio.wake_samples import WakeSamples


class FakeSpeaker:
    busy = False

    def __init__(self):
        self.said = []

    def speak(self, text, voice=""):
        self.said.append(text)

    def tone(self, kind):
        self.said.append(f"<{kind}>")


def chunk(level):
    return np.full(CHUNK, level, np.float32)


class TestTrim(unittest.TestCase):
    def test_keeps_the_word_and_a_little_either_side(self):
        chunks = [chunk(0.001)] * 20 + [chunk(0.05)] * 8 + [chunk(0.001)] * 10
        audio, talking = trim_speech(chunks, 0.004, 0.003)
        self.assertAlmostEqual(talking, 8 * CHUNK / RATE)
        self.assertEqual(len(audio), (3 + 8 + 4) * CHUNK)

    def test_nothing_said(self):
        audio, talking = trim_speech([chunk(0.001)] * 10, 0.004, 0.003)
        self.assertEqual((len(audio), talking), (0, 0.0))

    def test_over(self):
        for text in ("Stop.", "Okay, done", "that's enough", "I'm done", "Never mind."):
            self.assertTrue(practice_over(text), text)
        for text in ("Jezebel", "Jezebel.", "Jessabelle", "", "Just a bell"):
            self.assertFalse(practice_over(text), text)


class TestSamples(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-wake-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_kept_as_wav_with_what_was_heard(self):
        now = [1000.0]
        samples = WakeSamples(self.dir, clock=lambda: now[0])
        self.assertEqual(samples.count("jezebel"), 0)
        self.assertEqual(samples.add("jezebel", np.arange(RATE, dtype=np.int16), "Jezebel."), 1)
        now[0] += 3
        self.assertEqual(samples.add("jezebel", np.zeros(800, np.int16), "Jessabelle"), 2)
        folder = os.path.join(self.dir, "wake_samples", "jezebel")
        wavs = sorted(n for n in os.listdir(folder) if n.endswith(".wav"))
        with wave.open(os.path.join(folder, wavs[0])) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()), (RATE, 1, 2, RATE))
        with open(os.path.join(folder, "index.jsonl")) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([(r["file"], r["heard"], r["seconds"]) for r in rows],
                         [(wavs[0], "Jezebel.", 1.0), (wavs[1], "Jessabelle", 0.05)])


class TestSitting(unittest.TestCase):
    def sit(self, takes, total_before=0):
        """takes: (seconds of talking, what Whisper heard), or None for silence."""
        speaker, kept = FakeSpeaker(), []
        va = VoiceAssistant({"voice_gain": 2.0}, lambda text, say: None, speaker)
        captures = [(None, 0.0) if t is None else (np.full(4000, 0.1, np.float32), t[0]) for t in takes]
        heard = [t[1] for t in takes if t is not None]
        with mock.patch.object(va, "_capture", side_effect=captures), \
                mock.patch.object(va, "_transcribe", side_effect=[(h, []) for h in heard]):
            va._practice(lambda pcm, text: kept.append((pcm, text)) or total_before + len(kept))
        return speaker.said, kept

    def test_records_until_stop(self):
        said, kept = self.sit([(0.6, "Jezebel."), (0.7, "Jessabelle"), (0.4, "Stop.")], total_before=10)
        self.assertEqual([text for _pcm, text in kept], ["Jezebel.", "Jessabelle"])
        self.assertEqual(said, ["<listen>", "<listen>", "<listen>", "Got 2. That makes 12 altogether."])
        pcm = kept[0][0]
        self.assertEqual((pcm.dtype, int(pcm[0])), (np.int16, int(0.1 * 2.0 * 32767)))  # (as the wake model hears it)

    def test_a_cough_or_a_sentence_is_not_kept_and_silence_ends_it(self):
        said, kept = self.sit([(0.08, ""), (0.6, "Jezebel"), (2.6, "I wonder what's for dinner tonight"), None])
        self.assertEqual(len(kept), 1)
        self.assertEqual(said[-1], "Got 1. Thanks.")

    def test_three_odd_ones_in_a_row_end_it(self):
        said, kept = self.sit([(5.0, "the TV talking")] * 3)
        self.assertEqual((kept, said[-1]), ([], "I didn't catch any. Try again when you're ready."))

    def test_a_sitting_has_an_end(self):
        said, kept = self.sit([(0.6, "Jezebel")] * (PRACTICE_CLIPS + 5))
        self.assertEqual(len(kept), PRACTICE_CLIPS)

    def test_the_conversation_starts_it(self):
        speaker, kept = FakeSpeaker(), []
        reply = VoiceReply("Okay. After each beep, say Jezebel.", practice=lambda pcm, text: kept.append(text) or len(kept))
        va = VoiceAssistant({}, lambda text, say: reply, speaker)
        with mock.patch.object(va, "_hear", side_effect=["train Jezebel"]), \
                mock.patch.object(va, "_capture", side_effect=[(np.full(4000, 0.1, np.float32), 0.6), (None, 0.0)]), \
                mock.patch.object(va, "_transcribe", side_effect=[("Jezebel", [])]):
            va._conversation()
        self.assertEqual(kept, ["Jezebel"])
        self.assertEqual(speaker.said, ["<listen>", "Okay. After each beep, say Jezebel.", "<listen>", "<listen>",
                                        "Got 1. Thanks.", "<cancel>"])
        self.assertEqual(va._mode, "wake")


class TestAsked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        cls.dir = tempfile.mkdtemp(prefix="sentinel-practice-")
        cls.old_cwd = os.getcwd()
        os.chdir(cls.dir)
        sm = SettingsManager(settings_file=os.path.join(cls.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": "123456789:" + "A" * 35, "telegram_chat_id": "111", "volume": 0,
                            "telegram_enabled": False, "voice_name": "Jezebel",
                            "voice_trial_model": "/somewhere/models/wakeword/jezebel.onnx"})
        cls.engine = Engine(sm)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_train_jezebel_starts_a_sitting_and_keeps_the_clips(self):
        for text in ("Train Jezebel.", "teach Jezebel my voice", "practice the wake word", "Let's train your name"):
            reply = self.engine.answer_voice(text, lambda _t: None)
            self.assertIsNotNone(reply.practice, text)
        self.assertIn("say Jezebel", reply.text)
        self.assertEqual(reply.practice(np.zeros(8000, np.int16), "Jezebel"), 1)
        self.assertEqual(self.engine.wake_samples.count("jezebel"), 1)

    def test_other_talk_is_not_training(self):
        with mock.patch.object(self.engine, "ask_assistant", return_value=None, create=True):
            for text in ("what's the name of the train station", "record the garage camera"):
                self.assertIsNone(self.engine._practice_reply(text.lower()), text)

    def test_no_second_wake_word(self):
        with mock.patch.dict(self.engine.settings, {"voice_trial_model": ""}):
            reply = self.engine.answer_voice("train the wake word", lambda _t: None)
        self.assertIsNone(reply.practice)


if __name__ == "__main__":
    unittest.main()
