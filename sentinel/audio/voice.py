"""
"Hey Jarvis" voice assistant: listens on the PC's microphone, wakes on the wake word,
writes down the question with Whisper and speaks the answer with Piper.

Everything runs on this PC; nothing is recorded to disk. Only the wake word is listened
for continuously (openWakeWord, ~15 ms per 80 ms of audio); speech recognition runs only
after it. The mic is a weak, rumbly one 20 ft away, so audio goes through a voice-band
filter (150-4000 Hz) first and is normalised before Whisper.

Answers come from the engine (``answer(text, progress)`` -> ``VoiceReply``); anything that
changes settings is only done after a spoken "yes".
"""

import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

from sentinel.logging_utils import app_logger

RATE = 16000
CHUNK = 1280  # 80 ms: what openWakeWord expects per call
WAKE_MODEL = "hey_jarvis_v0.1"  # bundled with openWakeWord; a custom word = models/wakeword/<name>.onnx
WHISPER_DIR = os.path.join("models", "whisper", "base.en")
DEVICE = "plughw:0,0"
# The front jack's mic needs these; ALSA forgets them without `alsactl store` (sudo), so set them each start.
MIXER = (("Input Source", "Headphone Mic"), ("Headphone Mic Boost", "3"), ("Capture", "63"), ("Capture", "cap"))
STARTUP_SKIP_SECONDS = 1.0  # opening the mic gives a loud thump
WAIT_FOR_SPEECH = 5.0  # after the chime
MAX_COMMAND = 10.0
END_SILENCE = 1.0
# Dictation ("take a note for Claude"): longer, with room to pause and think.
DICTATE_WAIT, DICTATE_SILENCE, DICTATE_MAX = 8.0, 2.5, 90.0
DICTATE_PROMPT = "A note for Claude about changes to the Sentinel home security program, Jezebel, Spotify, cameras."
FOLLOW_UP_WAIT = 5.0  # after an answer, a follow-up question needs no wake word...
MAX_TURNS = 5  # ...but a TV can't keep it talking forever
NO_SPEECH_MAX = 0.6  # Whisper's own "this was noise" score; above it the words are usually invented
# Whisper's favourite inventions on silence and noise.
HALLUCINATIONS = {"you", "thank you", "thanks", "thank you very much", "thanks for watching", "bye", "okay", "so",
                  "thank you for watching", "the end", "hmm", "uh", "um"}
# Words Whisper should expect. "Who's home?" must be here or the family names pull it to "Luke home"
# (2026-10-27); with it, quiet "who's home" variants went from 7/24 to 15/24 right in a synthetic test.
PROMPT = ("Sentinel. Who's home? Is Steph home? When did Abi get home? Did Lincoln, Luke or Karleigh leave? "
          "Did anyone come by today? Any deliveries? What's the weather? Front door, back yard, garage, driveway. "
          "Enable messages. Play some Tom Petty. Pause. Skip. Volume up. What's playing? Take a note for Claude. "
          "Never mind. Cancel.")


@dataclass
class VoiceReply:
    text: str
    confirm: Optional[Callable[[], str]] = None  # run only after a spoken "yes"; returns what to say
    dictate: Optional[Callable[[str], str]] = None  # take a long message next ("note for Claude"); returns what to say


class BandPass:
    """Voice-band filter that keeps its state between 80 ms chunks (no clicks at the joins)."""

    def __init__(self, low: float = 150.0, high: float = 4000.0) -> None:
        from scipy.signal import butter, sosfilt_zi

        self._sos = butter(4, [low, high], btype="bandpass", fs=RATE, output="sos")
        self._zi = sosfilt_zi(self._sos) * 0.0

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        from scipy.signal import sosfilt

        out, self._zi = sosfilt(self._sos, audio, zi=self._zi)
        return out.astype(np.float32)


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0


class SpeechEnd:
    """Decides when a spoken command is over, from 80 ms loudness values and the room's noise level."""

    def __init__(self, noise: float, wait: float = WAIT_FOR_SPEECH, max_len: float = MAX_COMMAND,
                 silence: float = END_SILENCE) -> None:
        self.start_level = max(noise * 3.0, 0.004)
        self.quiet_level = max(noise * 2.0, 0.003)
        self.wait, self.max_len, self.silence = wait, max_len, silence
        self.elapsed = 0.0
        self.quiet_for = 0.0
        self.heard = False

    def add(self, level: float, seconds: float = CHUNK / RATE) -> bool:
        """True when listening should stop (speech ended, never started, or too long)."""
        self.elapsed += seconds
        if level >= self.start_level:
            self.heard = True
            self.quiet_for = 0.0
        elif self.heard and level < self.quiet_level:
            self.quiet_for += seconds
        if not self.heard:
            return self.elapsed >= self.wait
        return self.quiet_for >= self.silence or self.elapsed >= self.max_len


def normalise(audio: np.ndarray, peak: float = 0.9) -> np.ndarray:
    top = float(np.abs(audio).max()) if len(audio) else 0.0
    return (audio * (peak / top)).astype(np.float32) if top > 1e-4 else audio.astype(np.float32)


def clean_transcript(segments) -> str:
    """Whisper segments -> the words actually said (noise segments and stock inventions dropped)."""
    words = " ".join(s.text.strip() for s in segments if s.no_speech_prob <= NO_SPEECH_MAX).strip()
    words = re.sub(r"^\W*(hey|hi|okay|ok)?,?\s*(jarvis|travis|service|jezebel|jezabel|jessabelle|jess a bell)\b[\s,.!]*",
                   "", words, flags=re.IGNORECASE)
    if re.sub(r"[^a-z ]", "", words.lower()).strip() in HALLUCINATIONS:
        return ""
    return words.strip()


# "Cancel" ends a conversation: at the end of anything (she woke by accident mid-chat, then heard
# "...Jezebel, cancel"), or as the whole reply. Not "stop"/"be quiet": those stop the music.
_CANCEL_AT_END = r"cancel|never ?mind|forget (it|that)|stop listening|go (back )?to sleep|(i'?m |i was )?not talking to you"
_CANCEL_ALONE = r"nothing|that'?s all|no thanks?( you)?|not you|ignore (that|me)"


def is_cancel(text: str) -> bool:
    low = re.sub(r"\s+", " ", re.sub(r"[^a-z' ]", " ", (text or "").lower())).strip()
    return bool(re.search(rf"(^|\s)({_CANCEL_AT_END})$", low) or re.fullmatch(_CANCEL_ALONE, low))


def is_yes(text: str) -> bool:
    return bool(re.match(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|please|correct|absolutely)\b",
                         text or "", re.IGNORECASE))


def to_speech(text: str, max_lines: int = 4, max_chars: int = 400) -> str:
    """Telegram-style text -> something worth hearing: no emoji, links, bullets or long lists."""
    text = re.sub(r"\s*\(?https?://\S+\)?", "", text or "")
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️‍]", "", text)
    lines = []
    for line in text.split("\n"):
        line = line.strip().lstrip("•-*").strip()
        if not line or line.startswith("(") or line.lower().startswith("for the clip"):
            continue
        line = line.replace(" — ", ", ").replace("—", ", ").replace("…", "")
        lines.append(line if line[-1] in ".?!:" else line + ".")
    if len(lines) > max_lines:  # "here's what happened (12 visits):" + the latest few
        lines = lines[:1] + lines[-(max_lines - 1):]
    spoken = " ".join(lines)
    if len(spoken) > max_chars:
        cut = spoken[:max_chars]
        spoken = cut[:cut.rfind(". ") + 1] if ". " in cut else cut
    return re.sub(r"\s+", " ", spoken).strip()


def set_mixer(card: str = "0", run: Callable = subprocess.run) -> List[str]:
    """Turn on the front mic at full boost; returns the controls that failed."""
    failed = []
    for control, value in MIXER:
        try:
            r = run(["amixer", "-q", "-c", card, "sset", control, value], capture_output=True, timeout=5)
            if r.returncode != 0:
                failed.append(control)
        except (OSError, subprocess.TimeoutExpired):
            failed.append(control)
    return failed


class VoiceAssistant:
    def __init__(self, settings, answer: Callable[[str, Callable[[str], None]], VoiceReply], speaker,
                 popen: Callable = subprocess.Popen) -> None:
        self.settings = settings
        self.answer = answer
        self.speaker = speaker  # AudioAlertSystem: speak(text), tone(kind)
        self._popen = popen
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._mode = "wake"  # wake -> busy -> listen -> busy -> wake
        self._queue: "queue.Queue[tuple]" = queue.Queue()  # (time, audio) while listening
        self._ignore_until = 0.0  # chunks before this are the chime: kept for Whisper, not judged as speech
        self._noise = 0.003
        self._wake = None
        self._wake_name = WAKE_MODEL
        # Second wake word (setting voice_trial_model): logs its scores, and also wakes the assistant
        # when voice_trial_wakes is on (at voice_trial_threshold) - "Jezebel" next to "Hey Jarvis".
        self._trial_name = ""
        self._trial_peak = 0.0
        self._whisper = None
        self._whisper_lock = threading.Lock()
        self.last_error = ""

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _threshold(self) -> float:
        return float(self.settings.get("voice_wake_threshold", 0.5))

    def _gain(self) -> float:
        return float(self.settings.get("voice_gain", 4.0))

    def _wake_path(self) -> str:
        import openwakeword

        custom = str(self.settings.get("voice_wake_model") or "")
        if custom and os.path.isfile(custom):
            return custom
        return os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", WAKE_MODEL + ".onnx")

    def _load_models(self) -> None:
        from openwakeword.model import Model

        path = self._wake_path()
        self._wake_name = os.path.splitext(os.path.basename(path))[0]
        paths = [path]
        trial = str(self.settings.get("voice_trial_model") or "")
        if trial and os.path.isfile(trial) and os.path.abspath(trial) != os.path.abspath(path):
            self._trial_name = os.path.splitext(os.path.basename(trial))[0]
            paths.append(trial)  # same Model: shares the audio features, so it costs little extra
        self._wake = Model(wakeword_model_paths=paths)
        self._load_whisper()

    def _load_whisper(self):
        with self._whisper_lock:
            if self._whisper is None:
                from faster_whisper import WhisperModel

                self._whisper = WhisperModel(WHISPER_DIR, device="cpu", compute_type="int8", cpu_threads=3)
            return self._whisper

    def _run(self) -> None:
        try:
            self._load_models()
        except Exception as e:
            self.last_error = f"couldn't load the speech models ({e})"
            app_logger.log("error", "voice_unavailable", error_message=self.last_error, component="voice",
                           action="start")
            return
        failed = set_mixer()
        if failed:
            app_logger.log("warning", "voice_mixer_failed", controls=failed, component="voice", action="start")
        backoff = 5.0
        while not self._stop.is_set():
            started = time.time()
            self._listen_to_mic()
            if self._stop.is_set():
                break
            backoff = 5.0 if time.time() - started > 60 else min(backoff * 2, 300.0)
            app_logger.log("warning", "voice_mic_stopped", error_message=self.last_error, retry_seconds=backoff,
                           component="voice", action="listen")
            self._stop.wait(backoff)

    def _listen_to_mic(self) -> None:
        device = str(self.settings.get("voice_device") or DEVICE)
        try:
            self._proc = self._popen(["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                                      "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self.last_error = f"couldn't open the microphone ({e})"
            return
        app_logger.log("info", "voice_ready", device=device, component="voice", action="listen")
        band = BandPass()
        skip = int(STARTUP_SKIP_SECONDS * RATE / CHUNK)
        self._mode, self._wake_reset = "wake", True
        try:
            while not self._stop.is_set():
                raw = self._proc.stdout.read(CHUNK * 2)
                if not raw or len(raw) < CHUNK * 2:
                    err = self._proc.stderr.read().decode(errors="replace").strip() if self._proc.stderr else ""
                    self.last_error = err[-200:] or "the microphone stopped"
                    return
                audio = band(np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0)
                if skip > 0:
                    skip -= 1
                    continue
                self._on_chunk(audio)
        finally:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------ audio
    def _on_chunk(self, audio: np.ndarray) -> None:
        if self._mode == "listen":
            self._queue.put((time.time(), audio))
            return
        if self._mode != "wake":
            return
        level = rms(audio)
        if level < self._noise * 2:  # follow the room's background level, ignoring speech
            self._noise += (level - self._noise) * 0.02
        if self._wake_reset:
            # reset() only clears the scores; the model still holds the last ~2 s of audio, which
            # (being "Hey Jarvis") would wake it again. Push silence through first.
            silence = np.zeros(CHUNK, np.int16)
            for _ in range(int(2.0 * RATE / CHUNK)):
                self._wake.predict(silence)
            self._wake.reset()
            self._wake_reset = False
        pcm = (np.clip(audio * self._gain(), -1, 1) * 32767).astype(np.int16)
        scores = self._wake.predict(pcm)
        score = float(scores.get(self._wake_name, 0.0))
        word = self._wake_name
        if self._trial_name:
            trial = float(scores.get(self._trial_name, 0.0))
            self._track_trial(trial, score)
            if self.settings.get("voice_trial_wakes") and trial >= float(self.settings.get("voice_trial_threshold", 0.45)) \
                    and trial > score:
                score, word = 1.0, self._trial_name
        if score >= self._threshold():
            while not self._queue.empty():
                self._queue.get_nowait()
            self._ignore_until = time.time() + 5.0  # until the chime has played
            self._mode = "listen"  # keep recording: people say "Hey Jarvis, who's home?" in one breath
            app_logger.log("info", "voice_wake", score=round(score, 2), word=word, noise=round(self._noise, 4),
                           component="voice", action="wake")
            self._trial_peak, self._trial_main = 0.0, 0.0  # this utterance woke it; don't log it as a near-miss
            threading.Thread(target=self._conversation, name="voice-talk", daemon=True).start()

    def _track_trial(self, score: float, main_score: float) -> None:
        """Log the trial word's peak score once per utterance (anything that looked a bit like it)."""
        if score >= 0.1:
            self._trial_peak = max(self._trial_peak, score)
            self._trial_main = max(getattr(self, "_trial_main", 0.0), main_score)
        elif self._trial_peak and score < 0.05:
            app_logger.log("info", "voice_trial_score", word=self._trial_name, score=round(self._trial_peak, 2),
                           would_wake=self._trial_peak >= self._threshold(), noise=round(self._noise, 4),
                           main_score=round(getattr(self, "_trial_main", 0.0), 2), component="voice", action="trial")
            self._trial_peak, self._trial_main = 0.0, 0.0

    def _hear(self, wait: float = WAIT_FOR_SPEECH, fresh: bool = True, silence: float = END_SILENCE,
              max_len: float = MAX_COMMAND, prompt: str = PROMPT) -> Optional[str]:
        """Record until the speaker stops, then transcribe. None = nothing said."""
        if fresh:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._mode = "listen"
        end = SpeechEnd(self._noise, wait=wait, max_len=max_len, silence=silence)
        chunks: List[np.ndarray] = []
        try:
            while not self._stop.is_set():
                try:
                    at, chunk = self._queue.get(timeout=2.0)
                except queue.Empty:
                    return None  # mic died
                chunks.append(chunk)
                if at >= self._ignore_until and end.add(rms(chunk)):
                    break
        finally:
            self._mode = "busy"
        if not end.heard:
            return None
        audio = normalise(np.concatenate(chunks))
        started = time.time()
        segments, _ = self._load_whisper().transcribe(audio, beam_size=1, language="en", initial_prompt=prompt,
                                                      vad_filter=False, condition_on_previous_text=False)
        segments = list(segments)
        text = clean_transcript(segments)
        app_logger.log("info", "voice_heard", text=text[:200], seconds=round(time.time() - started, 1),
                       audio_seconds=round(len(audio) / RATE, 1),
                       no_speech=[round(s.no_speech_prob, 2) for s in segments], component="voice", action="hear")
        return text

    # ------------------------------------------------------------ talking
    def _say(self, text: str) -> None:
        if text:
            self.speaker.speak(text)

    def _respond(self, text: str) -> None:
        reply = self.answer(text, self._say)
        app_logger.log("info", "voice_replied", text=(reply.text or "")[:300], confirm=reply.confirm is not None,
                       component="voice", action="respond")
        self._say(reply.text)
        if reply.dictate is not None:
            self.speaker.tone("listen")
            said = self._hear(wait=DICTATE_WAIT, silence=DICTATE_SILENCE, max_len=DICTATE_MAX, prompt=DICTATE_PROMPT)
            self._say("Okay, cancelled." if is_cancel(said) else reply.dictate(said or ""))
            return
        if reply.confirm is None:
            return
        answer = self._hear(wait=4.0) or ""
        if is_yes(answer):
            self._say(reply.confirm())
        else:
            self._say("Okay, I won't.")

    def _conversation(self) -> None:
        self.speaker.busy = True
        try:
            self.speaker.tone("listen")
            self._ignore_until = time.time() + 0.15  # the chime's echo
            text = self._hear(fresh=False)
            for turn in range(MAX_TURNS):
                if text and is_cancel(text):
                    app_logger.log("info", "voice_cancelled", component="voice", action="conversation")
                    self.speaker.tone("cancel")
                    return
                if not text:  # silence, or noise Whisper made nothing of
                    if text == "" and turn == 0:
                        self._say("Sorry, I didn't catch that.")
                    else:
                        self.speaker.tone("cancel")
                    return
                self._respond(text)
                if turn == MAX_TURNS - 1 or not self.settings.get("voice_follow_up", True):
                    return
                self.speaker.tone("listen")
                text = self._hear(wait=FOLLOW_UP_WAIT)
        except Exception as e:
            self.last_error = str(e)
            app_logger.log("error", "voice_failed", error_message=str(e), component="voice", action="conversation")
            try:
                self._say("Sorry, something went wrong.")
            except Exception:
                pass
        finally:
            time.sleep(0.3)  # let the speaker's echo die away before listening for the wake word again
            self.speaker.busy = False
            self._wake_reset = True
            self._mode = "wake"
