""""Hey Jarvis": end-of-speech, transcript cleaning, spoken answers and the yes-before-changes rule."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

from sentinel.audio import voice as v
from sentinel.audio.voice import SpeechEnd, VoiceAssistant, VoiceReply, clean_transcript, is_yes, to_speech
from sentinel.engine import Engine
from sentinel.settings.manager import SettingsManager


def seg(text, no_speech=0.1):
    return types.SimpleNamespace(text=text, no_speech_prob=no_speech)


class TestSpeechEnd(unittest.TestCase):
    def feed(self, end, levels):
        for i, level in enumerate(levels):
            if end.add(level):
                return i
        return None

    def test_stops_after_a_second_of_quiet(self):
        end = SpeechEnd(noise=0.003)
        stop = self.feed(end, [0.003] * 5 + [0.02] * 15 + [0.003] * 30)
        self.assertTrue(end.heard)
        self.assertEqual(stop, 5 + 15 + 12)  # 13th quiet chunk = 1.04 s

    def test_short_pauses_between_words_dont_end_it(self):
        end = SpeechEnd(noise=0.003)
        self.assertIsNone(self.feed(end, ([0.02] * 5 + [0.003] * 6) * 4))

    def test_gives_up_when_nobody_speaks(self):
        end = SpeechEnd(noise=0.003, wait=2.0)
        self.assertEqual(self.feed(end, [0.004] * 100), 24)
        self.assertFalse(end.heard)

    def test_never_longer_than_max(self):
        end = SpeechEnd(noise=0.003, max_len=3.0)
        self.assertEqual(self.feed(end, [0.02] * 100), 37)

    def test_noisy_room_raises_the_bar(self):
        end = SpeechEnd(noise=0.01)
        self.feed(end, [0.02] * 10)
        self.assertFalse(end.heard)


class TestTranscript(unittest.TestCase):
    def test_drops_noise_segments_and_wake_word(self):
        self.assertEqual(clean_transcript([seg(" Hey Jarvis, who's home?"), seg(" Thank you.", 0.8)]), "who's home?")

    def test_whisper_inventions_are_nothing(self):
        for text in (" You", " Thank you very much.", " Thanks for watching!"):
            self.assertEqual(clean_transcript([seg(text)]), "", text)

    def test_yes(self):
        for text in ("Yes.", "yeah do it", "Sure!", "Okay", "go ahead"):
            self.assertTrue(is_yes(text), text)
        for text in ("No.", "nope", "", "yesterday?"):
            self.assertFalse(is_yes(text), text)


class TestToSpeech(unittest.TestCase):
    def test_strips_emoji_links_bullets_and_clip_hints(self):
        text = ("Yes — 2 visits today:\n• 9:40 am — 🚶 A person at the Front Door.\n"
                "• 2:15 pm — 📦 A UPS van in the Driveway.\nFor the clips, reply \"videos 2pm\".\n"
                "Name them on the Vehicles page (https://example.com/vehicles).")
        spoken = to_speech(text)
        self.assertNotIn("•", spoken)
        self.assertNotIn("http", spoken)
        self.assertNotIn("📦", spoken)
        self.assertNotIn("clips", spoken)
        self.assertIn("9:40 am, A person at the Front Door.", spoken)

    def test_long_lists_keep_the_lead_and_the_latest(self):
        text = "Here's what happened today (8 visits):\n" + "\n".join(f"• {h}:00 am — A car." for h in range(1, 9))
        spoken = to_speech(text, max_lines=3)
        self.assertTrue(spoken.startswith("Here's what happened today"))
        self.assertIn("8:00 am", spoken)
        self.assertNotIn("1:00 am", spoken)


class FakeSpeaker:
    def __init__(self):
        self.said, self.busy = [], False

    def speak(self, text):
        self.said.append(text)

    def tone(self, kind):
        self.said.append(f"<{kind}>")


class TestConversation(unittest.TestCase):
    def run_talk(self, heard, reply):
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda text, say: reply, speaker)
        with mock.patch.object(va, "_hear", side_effect=heard):
            va._conversation()
        return speaker.said, va

    def test_answers_and_returns_to_listening(self):
        said, va = self.run_talk(["who's home"], VoiceReply("Steph is home."))
        self.assertEqual(said, ["<listen>", "Steph is home."])
        self.assertEqual(va._mode, "wake")
        self.assertTrue(va._wake_reset)

    def test_silence_just_chimes_off(self):
        said, _ = self.run_talk([None], VoiceReply("unused"))
        self.assertEqual(said, ["<listen>", "<cancel>"])

    def test_change_needs_a_spoken_yes(self):
        done = []
        reply = VoiceReply("Should I turn on alerts?", lambda: done.append(1) or "Done.")
        said, _ = self.run_talk(["enable alerts", "yes please"], reply)
        self.assertEqual(done, [1])
        self.assertEqual(said[-1], "Done.")

    def test_anything_but_yes_does_nothing(self):
        done = []
        for answer in ("no", None, "what?"):
            reply = VoiceReply("Should I?", lambda: done.append(1) or "Done.")
            said, _ = self.run_talk(["enable alerts", answer], reply)
            self.assertEqual(said[-1], "Okay, I won't.")
        self.assertEqual(done, [])

    def test_alerts_stay_quiet_during_a_conversation(self):
        speaker = FakeSpeaker()
        seen = []
        va = VoiceAssistant({}, lambda text, say: seen.append(speaker.busy) or VoiceReply("ok"), speaker)
        with mock.patch.object(va, "_hear", return_value="hi"):
            va._conversation()
        self.assertEqual(seen, [True])
        self.assertFalse(speaker.busy)


class TestEngineVoiceAnswers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-voice-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings["volume"] = 0
        self.engine = Engine(manager)
        self.said = []

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def ask(self, text):
        return self.engine.answer_voice(text, self.said.append)

    def test_time(self):
        self.assertTrue(self.ask("What time is it?").text.startswith("It's "))

    def test_toggle_asks_first_and_applies_on_confirm(self):
        self.engine.settings["telegram_motion_alerts"] = False
        reply = self.ask("Enable messages.")
        self.assertIn("Should I turn on Telegram motion alerts", reply.text)
        self.assertFalse(self.engine.settings["telegram_motion_alerts"])
        self.assertIn("Telegram motion alerts: on", reply.confirm())
        self.assertTrue(self.engine.settings["telegram_motion_alerts"])

    def test_event_question_answered_locally(self):
        reply = self.ask("Did anyone come by today?")
        self.assertIsNone(reply.confirm)
        self.assertNotIn("•", reply.text)
        self.assertEqual(self.said, [])  # no "let me think": Claude wasn't needed

    def test_claude_answers_the_rest_in_voice_mode(self):
        self.engine.settings["assistant_enabled"] = True
        with mock.patch.object(self.engine.assistant, "ask", return_value="It was quiet out back. 🌙") as ask, \
                mock.patch.object(self.engine, "assistant_context", return_value="DATA"):
            reply = self.ask("Was it busy out back this afternoon or pretty quiet?")
        self.assertEqual(reply.text, "It was quiet out back.")
        self.assertEqual(self.said, ["Let me think."])
        self.assertTrue(ask.call_args.kwargs["voice"])

    def test_claude_command_needs_yes_too(self):
        self.engine.settings["assistant_enabled"] = True
        with mock.patch.object(self.engine.assistant, "ask",
                               return_value="I can turn alerts back on.\nCOMMAND: Enable messages"), \
                mock.patch.object(self.engine, "assistant_context", return_value="DATA"):
            reply = self.ask("can you start pinging my phone again")
        self.assertIn("Say yes or no", reply.text)
        self.assertIsNotNone(reply.confirm)

    def test_claude_off_gives_a_hint(self):
        self.engine.settings["assistant_enabled"] = False
        self.assertIn("who's home", self.ask("tell me a joke").text)

    def test_voice_needs_the_speaker(self):
        self.engine.settings["voice_enabled"] = True
        self.engine._start_voice()  # volume 0 -> no audio -> stays off instead of listening silently
        self.assertIsNone(self.engine.voice)


if __name__ == "__main__":
    unittest.main()
