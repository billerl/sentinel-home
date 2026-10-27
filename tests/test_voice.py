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

    def test_new_wake_word_is_dropped_too(self):
        self.assertEqual(clean_transcript([seg(" Jezebel, what's the weather?")]), "what's the weather?")

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
    def run_talk(self, heard, reply, **settings):
        speaker = FakeSpeaker()
        va = VoiceAssistant(settings, lambda text, say: reply, speaker)
        with mock.patch.object(va, "_hear", side_effect=heard):
            va._conversation()
        return speaker.said, va

    def test_answers_and_returns_to_listening(self):
        said, va = self.run_talk(["who's home", None], VoiceReply("Steph is home."))
        self.assertEqual(said, ["<listen>", "Steph is home.", "<listen>", "<cancel>"])
        self.assertEqual(va._mode, "wake")
        self.assertTrue(va._wake_reset)

    def test_silence_just_chimes_off(self):
        said, _ = self.run_talk([None], VoiceReply("unused"))
        self.assertEqual(said, ["<listen>", "<cancel>"])

    def test_change_needs_a_spoken_yes(self):
        done = []
        reply = VoiceReply("Should I turn on alerts?", lambda: done.append(1) or "Done.")
        said, _ = self.run_talk(["enable alerts", "yes please", None], reply)
        self.assertEqual(done, [1])
        self.assertEqual(said[-3], "Done.")

    def test_anything_but_yes_does_nothing(self):
        done = []
        for answer in ("no", None, "what?"):
            reply = VoiceReply("Should I?", lambda: done.append(1) or "Done.")
            said, _ = self.run_talk(["enable alerts", answer, None], reply)
            self.assertEqual(said[-3], "Okay, I won't.")
        self.assertEqual(done, [])

    def test_dictation_after_go_ahead(self):
        saved = []
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda t, s: VoiceReply("Go ahead.", dictate=lambda n: saved.append(n) or "Got it."), speaker)
        with mock.patch.object(va, "_hear", side_effect=["take a note for claude", "make the music bar bigger", None]) as hear:
            va._conversation()
        self.assertEqual(saved, ["make the music bar bigger"])
        self.assertEqual(hear.call_args_list[1].kwargs["silence"], v.DICTATE_SILENCE)
        self.assertIn("Got it.", speaker.said)

    def test_follow_up_question_needs_no_wake_word(self):
        asked = []
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda text, say: asked.append(text) or VoiceReply("ok"), speaker)
        with mock.patch.object(va, "_hear", side_effect=["who's home", "and the weather", None]):
            va._conversation()
        self.assertEqual(asked, ["who's home", "and the weather"])
        self.assertEqual(speaker.said[-1], "<cancel>")

    def test_follow_ups_are_capped(self):
        with mock.patch.object(v, "MAX_TURNS", 3):
            said, _ = self.run_talk(["blah"] * 10, VoiceReply("ok"))
        self.assertEqual(said.count("ok"), 3)

    def test_follow_up_can_be_turned_off(self):
        said, _ = self.run_talk(["who's home"], VoiceReply("Steph is home."), voice_follow_up=False)
        self.assertEqual(said, ["<listen>", "Steph is home."])

    def test_alerts_stay_quiet_during_a_conversation(self):
        speaker = FakeSpeaker()
        seen = []
        va = VoiceAssistant({}, lambda text, say: seen.append(speaker.busy) or VoiceReply("ok"), speaker)
        with mock.patch.object(va, "_hear", side_effect=["hi", None]):
            va._conversation()
        self.assertEqual(seen, [True])
        self.assertFalse(speaker.busy)


class TestSoundOutput(unittest.TestCase):
    def make(self, output):
        from sentinel.audio.alert_system import AudioAlertSystem

        audio = AudioAlertSystem.__new__(AudioAlertSystem)
        audio._play_lock, audio._output = __import__("threading").Lock(), output
        return audio

    def test_remote_desktop_session_when_connected(self):
        audio = self.make("remote")
        with mock.patch.object(audio, "_remote_desktop_sound", return_value="/run/user/1000/pulse/native"), \
                mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0)) as run, \
                mock.patch("subprocess.Popen") as popen:
            audio._play(b"wav")
        self.assertEqual(run.call_args.args[0], ["paplay"])
        self.assertEqual(run.call_args.kwargs["env"], {"PULSE_SERVER": "unix:/run/user/1000/pulse/native"})
        popen.assert_not_called()

    def test_falls_back_to_the_pc_speakers(self):
        for output, server, rc in (("remote", "/x", 1), ("remote", None, 0), ("speakers", "/x", 0)):
            audio = self.make(output)
            with mock.patch.object(audio, "_remote_desktop_sound", return_value=server), \
                    mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=rc)), \
                    mock.patch("subprocess.Popen") as popen:
                audio._play(b"wav")
            self.assertEqual(popen.call_args.args[0][0], "aplay", (output, server, rc))


class TestTrialWakeWord(unittest.TestCase):
    def test_logs_one_peak_per_utterance_and_never_wakes(self):
        va = VoiceAssistant({}, lambda t, s: VoiceReply("x"), FakeSpeaker())
        va._trial_name = "jezebel"
        with mock.patch.object(v.app_logger, "log") as log:
            for score in (0.0, 0.2, 0.7, 0.4, 0.02, 0.0, 0.03):
                va._track_trial(score, 0.01)
        trial = [c for c in log.call_args_list if c.args[1] == "voice_trial_score"]
        self.assertEqual(len(trial), 1)
        self.assertEqual(trial[0].kwargs["score"], 0.7)
        self.assertTrue(trial[0].kwargs["would_wake"])
        self.assertEqual(va._mode, "wake")  # the trial word never starts a conversation


class TestSecondWakeWord(unittest.TestCase):
    def run_chunk(self, settings, jarvis, jezebel):
        va = VoiceAssistant(settings, lambda t, s: VoiceReply("x"), FakeSpeaker())
        va._wake_name, va._trial_name, va._wake_reset = "hey_jarvis_v0.1", "jezebel", False
        va._wake = mock.Mock(predict=mock.Mock(return_value={"hey_jarvis_v0.1": jarvis, "jezebel": jezebel}))
        with mock.patch.object(v.threading, "Thread") as thread, mock.patch.object(v.app_logger, "log") as log:
            va._on_chunk(np.zeros(v.CHUNK, np.float32))
        woke = [c for c in log.call_args_list if c.args[1] == "voice_wake"]
        return thread.called, woke[0].kwargs["word"] if woke else None

    def test_either_word_wakes_when_on(self):
        on = {"voice_trial_wakes": True, "voice_trial_threshold": 0.45}
        self.assertEqual(self.run_chunk(on, 0.0, 0.5), (True, "jezebel"))
        self.assertEqual(self.run_chunk(on, 0.9, 0.0), (True, "hey_jarvis_v0.1"))
        self.assertEqual(self.run_chunk(on, 0.0, 0.4), (False, None))

    def test_log_only_when_off(self):
        self.assertEqual(self.run_chunk({}, 0.0, 0.9), (False, None))


class TestKokoro(unittest.TestCase):
    def test_sentences(self):
        from sentinel.audio.alert_system import split_sentences

        self.assertEqual(split_sentences("Steph is home. Abi left at 7:30 am! Anything else?"),
                         ["Steph is home.", "Abi left at 7:30 am!", "Anything else?"])
        self.assertEqual(split_sentences(""), [])

    def make(self):
        import threading
        from sentinel.audio.alert_system import AudioAlertSystem

        audio = AudioAlertSystem.__new__(AudioAlertSystem)
        audio._play_lock, audio._output, audio._volume, audio._speech_rate = threading.Lock(), "speakers", 0.5, 220
        audio.piper_voice, audio.tts_engine = None, None
        audio.kokoro_voice = "af_heart"
        made = []

        class FakeKokoro:
            def create(self, text, voice, speed, lang):
                made.append((text, voice, round(speed, 2), lang))
                return np.full(2400, 0.5, np.float32), 24000

        audio.kokoro = FakeKokoro()
        return audio, made

    def test_streams_every_sentence_into_one_player(self):
        audio, made = self.make()
        written = []
        player = mock.MagicMock()
        player.stdin.write.side_effect = written.append
        with mock.patch.object(audio, "_open_raw_player", return_value=player) as opened:
            self.assertTrue(audio.speak("One. Two. Three."))
        opened.assert_called_once_with(24000)
        self.assertEqual([m[0] for m in made], ["One.", "Two.", "Three."])
        self.assertEqual(made[0][1:], ("af_heart", 1.0, "en-us"))
        self.assertEqual(len(written), 3)
        self.assertEqual(np.frombuffer(written[0], np.int16)[0], int(0.5 * 0.5 * 32767))  # volume applied

    def test_falls_back_to_piper_when_kokoro_fails(self):
        audio, _ = self.make()
        audio.kokoro.create = mock.Mock(side_effect=RuntimeError("boom"))
        audio.piper_voice = object()
        with mock.patch.object(audio, "_speak_piper", return_value=True) as piper:
            self.assertTrue(audio.speak("Hello."))
        piper.assert_called_once_with("Hello.")

    def test_british_voices_use_british_english(self):
        audio, made = self.make()
        audio.kokoro_voice = "bf_emma"
        with mock.patch.object(audio, "_open_raw_player", return_value=mock.MagicMock()):
            audio.speak("Garage.")
        self.assertEqual(made[0][3], "en-gb")

    def test_kokoro_voices_listed_only_when_installed(self):
        from sentinel.engine import admin

        d = tempfile.mkdtemp()
        try:
            self.assertFalse(any(v.startswith("kokoro:") for v in admin.available_voices(d)))
            os.makedirs(os.path.join(d, "kokoro"))
            open(os.path.join(d, "kokoro", "kokoro-v1.0.onnx"), "w").close()
            self.assertIn("kokoro:af_heart", admin.available_voices(d))
        finally:
            shutil.rmtree(d)


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

    def test_notes_for_claude(self):
        self.engine.NOTES_FILE = os.path.join(self.dir, "notes", "claude-notes.md")
        reply = self.ask("Take a note for Claude.")
        self.assertEqual(reply.text, "Go ahead.")
        self.assertEqual(reply.dictate("Make the music bar bigger."), "Got it, I've saved that for Claude.")
        self.assertIn("I've saved", self.ask("Note for Claude: add a snow alert.").text)
        self.assertIn("I've saved", self.ask("tell cloud that the garage camera is blurry").text)
        self.assertEqual(self.ask("Take a note for Pause.").text, "Go ahead.")  # misheard "Claude" (2026-10-27)
        self.assertEqual(self.ask("take a note for cloud").text, "Go ahead.")
        notes = open(self.engine.NOTES_FILE).read()
        self.assertIn("Make the music bar bigger.", notes)
        self.assertIn("add a snow alert", notes)
        self.assertIn("the garage camera is blurry", notes)
        self.assertIn("didn't save", reply.dictate(""))

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
