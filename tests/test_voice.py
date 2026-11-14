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
from sentinel.audio.voice import (SpeechEnd, VoiceAssistant, VoiceReply, clean_transcript, is_cancel, is_yes,
                                  to_speech)
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
        for text in ("No.", "nope", "", "yesterday?",
                     # (heard as yes before the bug hunt of 2026-11-13: the first word decided)
                     "Please don't.", "Okay, never mind.", "Ok, no.", "Okay cancel", "Sure, wait no", "Yes... no, stop",
                     "Please do not", "Sure, hold on", "Ok forget it"):
            self.assertFalse(is_yes(text), text)
        for text in ("Yes please", "Please", "Sure thing", "Okay, go ahead"):
            self.assertTrue(is_yes(text), text)


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

    def speak(self, text, voice=""):
        self.said.append(f"{text} [{voice}]" if voice else text)

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
        said, va = self.run_talk(["who's home", None], VoiceReply("Kate is home.", chat=True))
        self.assertEqual(said, ["<listen>", "Kate is home.", "<listen>", "<cancel>"])
        self.assertEqual(va._mode, "wake")
        self.assertTrue(va._wake_reset)

    def test_silence_just_chimes_off(self):
        said, _ = self.run_talk([None], VoiceReply("unused"))
        self.assertEqual(said, ["<listen>", "<cancel>"])
        said, _ = self.run_talk([""], VoiceReply("unused"))  # noise Whisper made nothing of: no "Come again?"
        self.assertEqual(said, ["<listen>", "<cancel>"])

    def test_change_needs_a_spoken_yes(self):
        done = []
        reply = VoiceReply("Should I turn on alerts?", lambda: done.append(1) or "Done.")
        said, _ = self.run_talk(["enable alerts", "yes please", None], reply)
        self.assertEqual(done, [1])
        self.assertEqual(said[-1], "Done.")

    def test_anything_but_yes_does_nothing(self):
        done = []
        for answer in ("no", None, "what?"):
            reply = VoiceReply("Should I?", lambda: done.append(1) or "Done.")
            said, _ = self.run_talk(["enable alerts", answer, None], reply)
            self.assertEqual(said[-1], "Okay, I won't.")
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

    def test_a_plain_answer_is_the_last_word(self):
        said, _ = self.run_talk(["what time is it", "room talk that must not be heard"], VoiceReply("It's 3:40 pm."))
        self.assertEqual(said, ["<listen>", "It's 3:40 pm."])
        said, _ = self.run_talk(["thoughts on pineapple pizza", "why", None], VoiceReply("It's fine.", chat=True))
        self.assertEqual(said, ["<listen>", "It's fine.", "<listen>", "It's fine.", "<listen>", "<cancel>"])
        said, _ = self.run_talk(["thoughts on pineapple pizza", "why"], VoiceReply("It's fine.", chat=True), voice_follow_up=False)
        self.assertEqual(said, ["<listen>", "It's fine."])

    def test_a_command_gets_a_chime_and_no_follow_up(self):
        said, _ = self.run_talk(["turn on the porch lamp", "room talk that must not be heard"], VoiceReply("Okay, turning on the Porch lamp.", done=True))
        self.assertEqual(said, ["<listen>", "<done>"])
        said, _ = self.run_talk(["turn on the porch lamp", "room talk"], VoiceReply("Okay, turning on the Porch lamp.", done=True),
                                voice_quiet_commands=False)
        self.assertEqual(len(said), 2)
        self.assertTrue(said[1].endswith("turning on the Porch lamp."), said)
        said, _ = self.run_talk(["play tom petty", "lyrics"], VoiceReply("Playing Tom Petty.", final=True))
        self.assertEqual(said, ["<listen>", "Playing Tom Petty."])  # said, but nothing listened for after

    def test_follow_up_question_needs_no_wake_word(self):
        asked = []
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda text, say: asked.append(text) or VoiceReply("ok", chat=True), speaker)
        with mock.patch.object(va, "_hear", side_effect=["who's home", "and the weather", None]):
            va._conversation()
        self.assertEqual(asked, ["who's home", "and the weather"])
        self.assertEqual(speaker.said[-1], "<cancel>")

    def test_cancel_stops_without_answering(self):
        asked = []
        for heard in ("Cancel.", "so I told him the game was at, oh, Jezebel, cancel", "Never mind.", "nothing"):
            speaker = FakeSpeaker()
            va = VoiceAssistant({}, lambda text, say: asked.append(text) or VoiceReply("ok", chat=True), speaker)
            with mock.patch.object(va, "_hear", side_effect=[heard, "who's home", None]):
                va._conversation()
            self.assertEqual(speaker.said, ["<listen>", "<cancel>"], heard)
            self.assertEqual(va._mode, "wake")
        self.assertEqual(asked, [])
        said, _ = self.run_talk(["who's home", "never mind", "ignored"], VoiceReply("Kate is home.", chat=True))
        self.assertEqual(said, ["<listen>", "Kate is home.", "<listen>", "<cancel>"])  # a follow-up can cancel too

    def test_what_is_and_isnt_cancel(self):
        for text in ("Cancel", "forget it.", "Jezebel, never mind", "I'm not talking to you", "That's all.",
                     "no thank you", "go to sleep"):
            self.assertTrue(is_cancel(text), text)
        for text in ("", None, "stop", "be quiet", "pause the music", "who's home", "how do I cancel the alarm?",
                     "nothing on the front door?", "cancelled flights"):
            self.assertFalse(is_cancel(text), text)

    def test_a_confused_answer_ends_the_conversation(self):
        """2026-11-05 08:53: a false wake, then room talk answered "Huh?", "Huh?", "Come again?" in turn.
        Since 2026-11-10 not even the first "Huh?" is said: she wasn't listening for the answer anyway."""
        said, _ = self.run_talk(["If you say go to my mind", "Get into the front", None], VoiceReply("Huh?"))
        self.assertEqual(said, ["<listen>", "<cancel>"])  # just the closing chime
        replies = iter([VoiceReply("Kate is home.", chat=True), VoiceReply("Come again?")])
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda text, say: next(replies), speaker)
        with mock.patch.object(va, "_hear", side_effect=["who's home", "put it back towards the TV", "more talk"]):
            va._conversation()
        self.assertEqual(speaker.said, ["<listen>", "Kate is home.", "<listen>", "<cancel>"])  # silent on a follow-up

    def test_stop_inside_room_talk_ends_a_follow_up(self):
        asked = []
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda text, say: asked.append(text) or VoiceReply("ok", chat=True), speaker)
        with mock.patch.object(va, "_hear", side_effect=["who's home", "How did the save all this stuff? Stop. But.", None]):
            va._conversation()
        self.assertEqual(asked, ["who's home"])
        self.assertEqual(speaker.said[-1], "<cancel>")
        self.assertTrue(v.says_stop("Okay. Stop talking!"))
        for text in ("Stop.", "stop the music", "Don't stop. Keep going.", "", None):
            self.assertFalse(v.says_stop(text), text)

    def test_cancel_drops_a_note(self):
        saved = []
        speaker = FakeSpeaker()
        va = VoiceAssistant({}, lambda t, s: VoiceReply("Go ahead.", dictate=lambda n: saved.append(n) or "Got it."), speaker)
        with mock.patch.object(va, "_hear", side_effect=["take a note for claude", "uh, never mind", None]):
            va._conversation()
        self.assertEqual(saved, [])
        self.assertIn("Okay, cancelled.", speaker.said)

    def test_hey_jarvis_answers_as_jarvis_in_his_own_voice(self):
        speaker = FakeSpeaker()
        settings = {"voice_name": "Jezebel", "voice_jarvis_id": "kokoro:bm_george"}
        va = VoiceAssistant(settings, lambda text, say: say("Let me think.") or VoiceReply("Kate is home.", chat=True), speaker)
        va._persona("hey_jarvis_v0.1")
        self.assertEqual(va.speaking_as, "Jarvis")
        with mock.patch.object(va, "_hear", side_effect=["who's home", None]):
            va._conversation()
        self.assertIn("Kate is home. [kokoro:bm_george]", speaker.said)
        self.assertIn("Let me think. [kokoro:bm_george]", speaker.said)
        speaker.said.clear()
        va._persona("jezebel")  # the next conversation, woken the other way
        self.assertEqual(va.speaking_as, "Jezebel")
        with mock.patch.object(va, "_hear", side_effect=["who's home", None]):
            va._conversation()
        self.assertIn("Kate is home.", speaker.said)
        settings["voice_jarvis_id"] = ""  # "same as above": one voice for both
        va._persona("hey_jarvis_v0.1")
        self.assertEqual((va.speaking_as, va._reply_voice), ("Jezebel", ""))

    def test_doubtful_follow_up_only_for_sentinels_own_commands(self):
        asked = []

        def answer(text, say, doubtful=False):
            asked.append((text, doubtful))
            return None if doubtful and text != "pause" else VoiceReply("ok", chat=True)
        for follow_up, doubt_score, want in (("I don't want to get in the candy box", 0.42, ["ok", "<listen>", "<cancel>"]),
                                             ("I don't know", 0.2, ["ok", "<listen>", "ok", "<listen>", "<cancel>"]),
                                             ("Get into the front. Put it back towards the TV.", 0.29, ["ok", "<listen>", "<cancel>"]),
                                             ("pause", 0.58, ["ok", "<listen>", "ok", "<listen>", "<cancel>"]),
                                             ("and the weather", 0.05, ["ok", "<listen>", "ok", "<listen>", "<cancel>"])):
            speaker = FakeSpeaker()
            va = VoiceAssistant({}, answer, speaker)
            heard = iter([("who's home", 0.1), (follow_up, doubt_score), (None, 0.0)])

            def hear(**kwargs):
                text, va.last_doubt = next(heard)
                return text
            with mock.patch.object(va, "_hear", side_effect=hear):
                va._conversation()
            self.assertEqual(speaker.said[1:], want, follow_up)
        self.assertEqual(asked, [("who's home", False), ("I don't want to get in the candy box", True),
                                 ("who's home", False), ("I don't know", False),  # 0.2: fine for a follow-up
                                 ("who's home", False), ("Get into the front. Put it back towards the TV.", True),
                                 ("who's home", False), ("pause", True), ("who's home", False), ("and the weather", False)])

    def test_doubtful_words_right_after_a_wake_word(self):
        """2026-11-03 19:39: a false wake (Jarvis 0.91) heard "I'm not sure" (0.27) and Claude answered it."""
        for first, doubt_score, asked_claude in (("I'm not sure", 0.27, False), ("turn on the TV", 0.04, True)):
            speaker, asked = FakeSpeaker(), []
            va = VoiceAssistant({}, lambda text, say, doubtful=False: asked.append(doubtful) or (None if doubtful else VoiceReply("ok", chat=True)), speaker)
            heard = iter([(first, doubt_score), (None, 0.0)])

            def hear(**kwargs):
                text, va.last_doubt = next(heard)
                return text
            with mock.patch.object(va, "_hear", side_effect=hear):
                va._conversation()
            self.assertEqual(asked, [not asked_claude], first)
            self.assertEqual(speaker.said[-1], "<cancel>")

    def test_with_the_tv_on_doubtful_wake_words_are_ignored_entirely(self):
        """2026-11-03 19:51: "Pause." (0.42) after a weak wake while the TV was on."""
        for tv, first, doubt_score, answered in ((True, "Pause.", 0.42, False), (False, "Pause.", 0.42, True),
                                                 (True, "turn off the TV", 0.03, True)):
            speaker, asked = FakeSpeaker(), []
            va = VoiceAssistant({}, lambda text, say, doubtful=False: asked.append(text) or VoiceReply("ok", chat=True), speaker,
                                tv_on=lambda: tv)
            heard = iter([(first, doubt_score), (None, 0.0)])

            def hear(**kwargs):
                text, va.last_doubt = next(heard)
                return text
            with mock.patch.object(va, "_hear", side_effect=hear):
                va._conversation()
            self.assertEqual(asked, [first] if answered else [], (tv, first))
            self.assertEqual(speaker.said[-1], "<cancel>")

    def test_doubt_is_the_worst_kept_segment(self):
        self.assertEqual(v.doubt([seg("hi", 0.1), seg("there", 0.42), seg("noise", 0.9)]), 0.42)
        self.assertEqual(v.doubt([]), 0.0)

    def test_follow_ups_are_capped(self):
        with mock.patch.object(v, "MAX_TURNS", 3):
            said, _ = self.run_talk(["blah"] * 10, VoiceReply("ok", chat=True))
        self.assertEqual(said.count("ok"), 3)

    def test_follow_up_can_be_turned_off(self):
        said, _ = self.run_talk(["who's home"], VoiceReply("Kate is home.", chat=True), voice_follow_up=False)
        self.assertEqual(said, ["<listen>", "Kate is home."])

    def test_alerts_stay_quiet_during_a_conversation(self):
        speaker = FakeSpeaker()
        seen = []
        va = VoiceAssistant({}, lambda text, say: seen.append(speaker.busy) or VoiceReply("ok", chat=True), speaker)
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
            self.assertEqual(popen.call_args.args[0][:4], ["aplay", "-q", "-D", "sentinel_speaker"], (output, server, rc))
            self.assertIn("alsa-sentinel.conf", popen.call_args.kwargs["env"]["ALSA_CONFIG_PATH"])


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

        self.assertEqual(split_sentences("Kate is home. Liz left at 7:30 am! Anything else?"),
                         ["Kate is home.", "Liz left at 7:30 am!", "Anything else?"])
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

    def test_whisper_hears_the_familys_names(self):
        self.engine.locations.save_person("Katherine", ["Kate"])
        self.engine.household.save_vehicle("Ben's Jeep", owner="Ben")
        self.assertEqual(self.engine._voice_names(), ["Katherine", "Kate", "Ben"])

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

    def test_open_ended_answers_keep_the_conversation_and_remember_it(self):
        self.engine.settings["assistant_enabled"] = True
        with mock.patch.object(self.engine.assistant, "ask", side_effect=[
                "Honestly? It belongs on pizza.\nMORE", "Because sweet and salty works.\nMORE", "About 2,700 kilometres."]) as ask, \
                mock.patch.object(self.engine, "assistant_context", return_value="DATA"):
            first = self.ask("What are your thoughts on pineapple on pizza?")
            self.assertEqual((first.text, first.chat), ("Honestly? It belongs on pizza.", True))
            second = self.ask("Why do people like that?")
            self.assertTrue(second.chat)
            self.assertIn("EARLIER IN THIS CONVERSATION:\nThey said: What are your thoughts on pineapple on pizza?\n"
                          "You said: Honestly? It belongs on pizza.", ask.call_args.args[1])
            third = self.ask("How far is it from Chicago to Miami?")  # a plain fact: the last word
            self.assertEqual((third.text, third.chat), ("About 2,700 kilometres.", False))
            self.assertEqual(self.engine._voice_chat, [])

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


class TestWhisperPrompt(unittest.TestCase):
    def test_names_come_from_the_household(self):
        from sentinel.audio.voice import PROMPT, whisper_prompt

        self.assertEqual(whisper_prompt([]), PROMPT)
        text = whisper_prompt(["Emma", "Em", "Kate", "Ben", "Kate", " "])
        self.assertTrue(text.startswith("Sentinel. Who's home? Is Emma home? When did Em get home? Did Kate or Ben leave?"))
        self.assertTrue(text.endswith("Cancel."))
        self.assertIn("Did Ben leave?", whisper_prompt(["A", "B", "Ben"]))
        plugs = whisper_prompt([], ["patio lights", "patio outlet", "patio lights"])
        self.assertIn("Turn on the patio lights. Patio outlet off. ", plugs)  # what was misheard as "Turn Petty"
        pets = whisper_prompt(["Emma"], pets=["Grace", "Ben"])
        self.assertIn("The dogs are going out. Let Grace and Ben out. Recall Grace. ", pets)  # "Lean is going outside"

    def test_names_before_nicknames(self):
        """2026-11-07: nicknames came right after each name, so the 10th entry, the dog, never made the hint."""
        from sentinel.engine.engine import Engine

        people = [types.SimpleNamespace(name=n, aliases=a, tracker=t) for n, a, t in
                  (("Alex", ["Dad"], ""), ("Kate", ["Mom", "Katie"], ""), ("Ben", ["Benny"], ""), ("Grace", [], "!a"))]
        fake = types.SimpleNamespace(locations=types.SimpleNamespace(people=lambda: people),
                                     household=types.SimpleNamespace(vehicles=lambda: []))
        self.assertEqual(Engine._voice_names(fake)[:4], ["Alex", "Kate", "Ben", "Grace"])


if __name__ == "__main__":
    unittest.main()
