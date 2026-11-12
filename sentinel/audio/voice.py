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
import random
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

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
# "Train Jezebel": the wake word recorded after each beep, for retraining its model (sentinel/audio/wake_samples.py)
PRACTICE_CLIPS = 30  # a sitting: more than this and people start saying it oddly
PRACTICE_WAIT, PRACTICE_SILENCE, PRACTICE_MAX = 6.0, 0.5, 3.0
PRACTICE_SPEECH = (0.25, 2.0)  # seconds of talking a wake word takes; outside it wasn't one
FOLLOW_UP_WAIT = 5.0  # after an answer, a follow-up question needs no wake word...
MAX_TURNS = 5  # ...but a TV can't keep it talking forever
NO_SPEECH_MAX = 0.6  # Whisper's own "this was noise" score; above it the words are usually invented
# A follow-up (no wake word) scoring above this is doubtful: acted on only if Sentinel understands it itself,
# never sent to Claude. Room talk got answered ("I don't want to get in the candy box", 0.42, 2026-11-03), while
# real short follow-ups score high too ("Pause" 0.58, "Stop" 0.53-0.58), so they can't simply be dropped.
# Lowered from 0.3 on 2026-11-05: "Get into the front. Put it back towards the TV." (0.29) went to Claude;
# every real follow-up between 0.12 and 0.3 in the logs was one of Sentinel's own commands.
FOLLOW_UP_DOUBT = 0.2
# The same for the first words after a wake word, stricter: false wakes from room talk score high on the wake word
# too (0.76-1.0, overlapping real commands at 0.70-1.0), but every real command since 2026-11-01 had a no-speech
# score <= 0.06 and every false wake >= 0.11 ("I'm not sure", Jarvis 0.91, 2026-11-03 19:39).
WAKE_DOUBT = 0.15
# While a TV is on (its talk is what wakes Jarvis by mistake), doubtful words right after the wake word are ignored
# altogether, Sentinel's own commands too ("Pause." from the room at 0.42, 2026-11-03 19:51). Follow-ups keep the
# milder rule, so "pause"/"stop" right after an answer still work. A trial the user agreed to, 2026-11-03.
# Whisper's favourite inventions on silence and noise.
HALLUCINATIONS = {"you", "thank you", "thanks", "thank you very much", "thanks for watching", "bye", "okay", "so",
                  "thank you for watching", "the end", "hmm", "uh", "um"}
# Words Whisper should expect. "Who's home?" must come first or the family names pull it to "<name> home"
# (2026-10-27); with it, quiet "who's home" variants went from 7/24 to 15/24 right in a synthetic test.
# The names themselves come from the Family and Vehicles pages (whisper_prompt).
PROMPT_START = "Sentinel. Who's home? "
PROMPT_REST = ("Did anyone come by today? Any deliveries? What's the weather? Front door, back yard, garage, driveway. "
               "Enable messages. Play some Tom Petty. Pause. Skip. Volume up. What's playing? Take a note for Claude. "
               "Read my mesh messages. Next step. How much garlic? Set a timer for 10 minutes. Turn the Jabra down. "
               "Fan high. What plane is that? Never mind. Cancel.")  # ("Jabra" came out "job roast", 2026-11-10)
PROMPT = PROMPT_START + PROMPT_REST
MAX_PROMPT_NAMES = 10
MAX_PROMPT_PETS = 4
MAX_PROMPT_SWITCHES = 6
MAX_PROMPT_APPS = 12
MAX_PROMPT_ARTISTS = 12  # with everything else the hint stays well under Whisper's 224 tokens


def whisper_prompt(names: List[str], switches: Optional[List[str]] = None, apps: Optional[List[str]] = None,
                   artists: Optional[List[str]] = None, pets: Optional[List[str]] = None) -> str:
    """The hint for Whisper, with this household's names (and smart plugs) said the way people ask about them.
    Without the plugs, "turn the patio light on" came out as "Turn Petty, or light home." (2026-11-02)."""
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n][:MAX_PROMPT_NAMES]
    plugs = [s for s in dict.fromkeys((s or "").strip() for s in (switches or [])) if s][:MAX_PROMPT_SWITCHES]
    said = f"Is {names[0]} home? " if names else ""
    if len(names) > 1:
        said += f"When did {names[1]} get home? "
    if len(names) > 2:
        rest = names[2:]
        said += f"Did {', '.join(rest[:-1]) + ' or ' + rest[-1] if len(rest) > 1 else rest[0]} leave? "
    pets = [p for p in dict.fromkeys((p or "").strip() for p in (pets or [])) if p][:MAX_PROMPT_PETS]
    if pets:  # "Xena is going outside" was heard as "Lean is going outside" (2026-11-07)
        said += f"The dogs are going out. Let {' and '.join(pets)} out. Recall {pets[0]}. "
    if plugs:
        said += f"Turn on the {plugs[0]}. " + "".join(f"{p[:1].upper()}{p[1:]} off. " for p in plugs[1:])
    apps = [a.replace("+", " Plus") for a in dict.fromkeys((a or "").strip() for a in (apps or [])) if a][:MAX_PROMPT_APPS]
    if apps:  # TV apps: "Paramount Plus", "Peacock", "Fubo" are easy to mishear
        said += f"Turn on the TV to {apps[0]}. " + ("Put on " + ", ".join(apps[1:]) + ". " if len(apps) > 1 else "")
    artists = [a for a in dict.fromkeys((a or "").strip() for a in (artists or [])) if a][:MAX_PROMPT_ARTISTS]
    if artists:  # the family's Spotify artists: "Mötley Crüe" was heard as "monily crew" (2026-11-03)
        said += f"Play {artists[0]}. " + ("Play some " + ", ".join(artists[1:]) + ". " if len(artists) > 1 else "")
    return PROMPT_START + said + PROMPT_REST


@dataclass
class VoiceReply:
    text: str
    confirm: Optional[Callable[[], str]] = None  # run only after a spoken "yes"; returns what to say
    dictate: Optional[Callable[[str], str]] = None  # take a long message next ("note for Claude"); returns what to say
    # record the wake word after each beep ("train Jezebel"): (the clip, what Whisper heard) -> how many are kept now
    practice: Optional[Callable[[np.ndarray, str], int]] = None


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


PHONE_MAX_SECONDS = 30  # a Talk-button recording is cut here


def decode_audio(data: bytes, ffmpeg: str = "") -> np.ndarray:
    """A phone browser's recording (webm/opus from Android, mp4/aac from an iPhone) -> 16 kHz mono float32.
    Through a file: an iPhone's mp4 has its index at the end, which a pipe can't seek back to."""
    import tempfile

    if not ffmpeg:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.NamedTemporaryFile(prefix="sentinel-talk-", suffix=".audio") as f:
        f.write(data)
        f.flush()
        out = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", f.name, "-t", str(PHONE_MAX_SECONDS),
                              "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"], capture_output=True, timeout=30)
    if out.returncode != 0 or not out.stdout:
        raise ValueError((out.stderr or b"no audio").decode(errors="replace").strip()[:200])
    return np.frombuffer(out.stdout[:len(out.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768.0


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


def trim_speech(chunks: Sequence[np.ndarray], start_level: float, quiet_level: float,
                lead: int = 3, tail: int = 4) -> Tuple[np.ndarray, float]:
    """The spoken part of a recording, with a little room either side, and how many seconds of it were talking."""
    levels = [rms(c) for c in chunks]
    loud = [i for i, level in enumerate(levels) if level >= start_level]
    if not loud:
        return np.zeros(0, np.float32), 0.0
    first = loud[0]
    last = max(i for i, level in enumerate(levels) if level >= quiet_level)
    kept = chunks[max(0, first - lead):last + 1 + tail]
    return np.concatenate(kept).astype(np.float32), (last - first + 1) * CHUNK / RATE


_PRACTICE_OVER = r"stop|done|i'?m done|all done|that'?s (it|enough|all)|enough|finished|quit|cancel|never ?mind|end"


def practice_over(text: str) -> bool:
    """Said instead of the wake word during "train Jezebel": the sitting is over."""
    low = re.sub(r"\s+", " ", re.sub(r"[^a-z' ]", " ", (text or "").lower())).strip()
    return bool(re.fullmatch(rf"(okay |ok )?({_PRACTICE_OVER})( now| please| for now)?", low)) or is_cancel(text)


def normalise(audio: np.ndarray, peak: float = 0.9) -> np.ndarray:
    top = float(np.abs(audio).max()) if len(audio) else 0.0
    return (audio * (peak / top)).astype(np.float32) if top > 1e-4 else audio.astype(np.float32)


def doubt(segments) -> float:
    """How likely Whisper thinks the kept words were noise (the worst kept segment)."""
    return max([s.no_speech_prob for s in segments if s.no_speech_prob <= NO_SPEECH_MAX] or [0.0])


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


def says_stop(text: str) -> bool:
    """"Stop." as its own sentence inside longer talk: the user is telling her to stop, not the music
    ("How did the save all this stuff? Stop. But.", yelled during a false wake, 2026-11-05)."""
    sentences = [re.sub(r"[^a-z' ]", "", p.lower()).strip() for p in re.split(r"[.!?]+", text or "")]
    sentences = [p for p in sentences if p]
    return len(sentences) > 1 and any(re.fullmatch(r"(please )?stop( it| talking)?( please)?", p) for p in sentences)


# Claude's whole answer when the words made no sense (see assistant.py): she wasn't being talked to.
_CONFUSED = r"huh|what|come again|sorry|pardon|say again|sorry what"


def is_confused(text: str) -> bool:
    return bool(re.fullmatch(_CONFUSED, re.sub(r"[^a-z ]", "", (text or "").lower()).strip()))


def is_yes(text: str) -> bool:
    return bool(re.match(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|please|correct|absolutely)\b",
                         text or "", re.IGNORECASE))


def first_sentence(text: str) -> str:
    """Just the gist out loud ("Okay, watching Xena."); Telegram gets the rest."""
    m = re.match(r"(.+?[.!?])(?:\s|$)", re.sub(r"[\U0001F000-\U0001FFFF☀-➿️‍]", "", text or "").strip(), re.S)
    return m.group(1) if m else (text or "").strip()


OKAYS = ("Okay, ", "Okay, ", "Sure, ", "Alright, ")


def casual(text: str, pick: Callable[[Sequence[str]], str] = random.choice) -> str:
    """Not the same "Okay, ..." every time."""
    return pick(OKAYS) + text[6:] if text.startswith("Okay, ") else text


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
                 popen: Callable = subprocess.Popen, names: Optional[Callable[[], List[str]]] = None,
                 switches: Optional[Callable[[], List[str]]] = None,
                 apps: Optional[Callable[[], List[str]]] = None,
                 artists: Optional[Callable[[], List[str]]] = None,
                 tv_on: Optional[Callable[[], bool]] = None,
                 pets: Optional[Callable[[], List[str]]] = None) -> None:
        self.settings = settings
        self.names = names  # the family's names and nicknames, for Whisper's hint
        self.switches = switches  # smart plug names, likewise
        self.apps = apps  # TV app names, likewise
        self.artists = artists  # the family's Spotify artists, likewise
        self.pets = pets  # pets with collar trackers, likewise
        self.tv_on = tv_on  # a TV (with sound) is on: stricter right after the wake word
        self.answer = answer
        self.last_doubt = 0.0  # Whisper's noise score for the last words heard (doubt())
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
        self._transcribe_lock = threading.Lock()
        self.last_error = ""
        # Who is talking this conversation: "Hey Jarvis" gets Jarvis (a man's voice, voice_jarvis_id),
        # the other wake word the main voice and voice_name. Read by the engine for Claude's prompt.
        self.speaking_as = ""
        self._reply_voice = ""

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
        device = str(self.settings.get("voice_device") or DEVICE)
        failed = set_mixer() if device in (DEVICE, "plughw:CARD=PCH,DEV=0") else []  # the PC's front mic jack only
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
            self._persona(word)
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
              max_len: float = MAX_COMMAND, prompt: Optional[str] = None) -> Optional[str]:
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
        text, segments = self._transcribe(audio, prompt)
        self.last_doubt = doubt(segments)
        app_logger.log("info", "voice_heard", text=text[:200], seconds=round(time.time() - started, 1),
                       audio_seconds=round(len(audio) / RATE, 1),
                       no_speech=[round(s.no_speech_prob, 2) for s in segments], component="voice", action="hear")
        return text

    def _transcribe(self, audio: np.ndarray, prompt: Optional[str] = None):
        """(words, Whisper's segments) for 16 kHz mono audio, with the family's names etc. as the hint."""
        if prompt is None:
            try:
                prompt = whisper_prompt(self.names() if self.names else [], self.switches() if self.switches else [],
                                        self.apps() if self.apps else [], self.artists() if self.artists else [],
                                        self.pets() if self.pets else [])
            except Exception:  # a database hiccup shouldn't cost the question
                prompt = PROMPT
        whisper = self._load_whisper()
        with self._transcribe_lock:  # (the house mic and a phone at once: one at a time)
            segments, _ = whisper.transcribe(audio, beam_size=1, language="en", initial_prompt=prompt,
                                             vad_filter=False, condition_on_previous_text=False)
            segments = list(segments)
        return clean_transcript(segments), segments

    def transcribe(self, audio: np.ndarray) -> Tuple[str, float]:
        """A recording made elsewhere (the Talk button on a phone): (words, doubt)."""
        started = time.time()
        text, segments = self._transcribe(normalise(audio))
        app_logger.log("info", "voice_heard", text=text[:200], seconds=round(time.time() - started, 1),
                       audio_seconds=round(len(audio) / RATE, 1), source="phone",
                       no_speech=[round(s.no_speech_prob, 2) for s in segments], component="voice", action="hear")
        return text, doubt(segments)

    # ------------------------------------------------------------ talking
    def _capture(self) -> Tuple[Optional[np.ndarray], float]:
        """One short thing said after a beep, as recorded: (the trimmed audio, seconds of talking), or
        (None, 0) when nothing was said."""
        while not self._queue.empty():
            self._queue.get_nowait()
        self._mode = "listen"
        end = SpeechEnd(self._noise, wait=PRACTICE_WAIT, max_len=PRACTICE_MAX, silence=PRACTICE_SILENCE)
        chunks: List[np.ndarray] = []
        try:
            while not self._stop.is_set():
                try:
                    at, chunk = self._queue.get(timeout=2.0)
                except queue.Empty:
                    return None, 0.0  # mic died
                if at < self._ignore_until:
                    continue  # the beep's echo
                chunks.append(chunk)
                if end.add(rms(chunk)):
                    break
        finally:
            self._mode = "busy"
        if not end.heard:
            return None, 0.0
        return trim_speech(chunks, end.start_level, end.quiet_level)

    def _practice(self, keep: Callable[[np.ndarray, str], int]) -> None:
        """"Train Jezebel": beep, record the word, again, until "stop", silence or a sitting's worth. Each clip is
        kept the way the wake model hears the room (this microphone, filter and gain)."""
        got, total, odd = 0, 0, 0
        while got < PRACTICE_CLIPS and odd < 3 and not self._stop.is_set():
            self.speaker.tone("listen")
            self._ignore_until = time.time() + 0.15
            audio, talking = self._capture()
            if audio is None:
                break  # nobody there any more
            text, _segments = self._transcribe(normalise(audio))
            if practice_over(text):
                break
            if not PRACTICE_SPEECH[0] <= talking <= PRACTICE_SPEECH[1]:
                odd += 1  # a cough, or a sentence: not kept
                continue
            odd = 0
            total = keep((np.clip(audio * self._gain(), -1, 1) * 32767).astype(np.int16), text)
            got += 1
        app_logger.log("info", "voice_practice", kept=got, total=total, component="voice", action="practice")
        if not got:
            self._say("I didn't catch any. Try again when you're ready.")
        else:
            self._say(f"Got {got}. That makes {total} altogether." if total > got else f"Got {got}. Thanks.")

    def _persona(self, word: str) -> None:
        jarvis = str(self.settings.get("voice_jarvis_id") or "")
        if word.startswith("hey_jarvis") and jarvis:
            self.speaking_as, self._reply_voice = "Jarvis", jarvis
        else:
            self.speaking_as, self._reply_voice = str(self.settings.get("voice_name") or ""), ""

    def _say(self, text: str) -> None:
        if text and self._reply_voice:
            self.speaker.speak(text, voice=self._reply_voice)
        elif text:
            self.speaker.speak(text)

    def _tv_on(self) -> bool:
        try:
            return bool(self.tv_on and self.tv_on())
        except Exception:
            return False

    def _respond(self, text: str, doubtful: bool = False) -> bool:
        """Answer it; False when the conversation should end: a doubtful follow-up that wasn't one of
        Sentinel's own commands (ignored), or words that made no sense ("Huh?")."""
        reply = self.answer(text, self._say, doubtful=True) if doubtful else self.answer(text, self._say)
        if reply is None:
            app_logger.log("info", "voice_ignored", text=text[:200], doubt=round(self.last_doubt, 2),
                           component="voice", action="respond")
            return False
        app_logger.log("info", "voice_replied", text=(reply.text or "")[:300], confirm=reply.confirm is not None,
                       component="voice", action="respond")
        if is_confused(reply.text) and reply.confirm is None and reply.dictate is None:
            # Room talk, after a false wake or as a "follow-up": listening on only invites more of it
            # (four "Let me think"s in a row, 2026-11-05). Not even a "Huh?": she doesn't listen for the
            # answer anyway, so just the closing chime (the user's call, 2026-11-10).
            return False
        self._say(casual(reply.text))
        if reply.practice is not None:
            self._practice(reply.practice)
            return False
        if reply.dictate is not None:
            self.speaker.tone("listen")
            said = self._hear(wait=DICTATE_WAIT, silence=DICTATE_SILENCE, max_len=DICTATE_MAX, prompt=DICTATE_PROMPT)
            self._say("Okay, cancelled." if is_cancel(said) else reply.dictate(said or ""))
            return
        if reply.confirm is None:
            return True
        answer = self._hear(wait=4.0) or ""
        if is_yes(answer):
            self._say(reply.confirm())
        else:
            self._say("Okay, I won't.")
        return True

    def _conversation(self) -> None:
        self.speaker.busy = True
        try:
            self.speaker.tone("listen")
            self._ignore_until = time.time() + 0.15  # the chime's echo
            text = self._hear(fresh=False)
            for turn in range(MAX_TURNS):
                if text and (is_cancel(text) or (turn and says_stop(text))):
                    app_logger.log("info", "voice_cancelled", component="voice", action="conversation")
                    self.speaker.tone("cancel")
                    return
                if not text:  # silence, or noise Whisper made nothing of: stop without a word
                    self.speaker.tone("cancel")
                    return
                doubtful = self.last_doubt > (FOLLOW_UP_DOUBT if turn else WAKE_DOUBT)
                if doubtful and not turn and self._tv_on():
                    app_logger.log("info", "voice_ignored", text=text[:200], doubt=round(self.last_doubt, 2), tv=True,
                                   component="voice", action="respond")
                    self.speaker.tone("cancel")
                    return
                if not self._respond(text, doubtful=doubtful):
                    self.speaker.tone("cancel")
                    return
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
