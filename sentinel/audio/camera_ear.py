"""
"Hey Jarvis" at a camera: a camera's own microphone listened to like the house microphone, outdoors.

Tried 2026-11-17 at the back yard camera: the wake word scored 0.91-0.99 on all six tries and Whisper heard
"Turn on the patio lights" on five. The family didn't want the yard's sound kept ("that's violating other
people's conversational privacy"), so:

- The camera's sound is read 80 ms at a time and checked for the wake word, then dropped. Nothing is written
  anywhere, and the camera's recordings stay silent (the HD recorder takes only the picture, -map 0:v:0).
- Only after "Hey Jarvis" are the next few seconds turned into words, to find the command. Those words (what was
  said to Jarvis, like the house mic) are noted with what was done; nothing said without the wake word ever is.

The camera has no speaker, so it answers with its white light (sentinel/engine/camera_light.py): on while it
listens, one flash for done, two for "didn't understand" or "not from out here". Out here only plugs and lights
can be switched (``act`` decides): someone in the yard mustn't be able to quiet the cameras.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Callable, List, Optional

import numpy as np

from sentinel.audio.voice import CHUNK, RATE, BandPass, SpeechEnd, normalise, rms, trim_speech
from sentinel.logging_utils import app_logger, redact

WAKE_MODEL = "hey_jarvis_v0.1"  # the one tested at the camera (the house's own word may be another)
WAIT_FOR_SPEECH = 4.0  # after the wake word (most say it in one breath: "Hey Jarvis, turn on the...")
MAX_COMMAND = 7.0
RETRY_SECONDS = (5.0, 300.0)  # the camera's sound stopped: try again, backing off to this


class CameraEar:
    def __init__(self, host: str, source: str, transcribe: Callable[[np.ndarray], str],
                 act: Callable[[str, np.ndarray], bool], light: Any = None, threshold: Callable[[], float] = lambda: 0.89,
                 gain: Callable[[], float] = lambda: 4.0, popen: Callable[..., Any] = subprocess.Popen,
                 wake_model: Any = None, ffmpeg: str = "") -> None:
        self.host, self.source = host, source
        self._transcribe, self._act, self._light = transcribe, act, light
        self._threshold, self._gain = threshold, gain
        self._popen, self._wake, self._ffmpeg = popen, wake_model, ffmpeg
        self._proc: Optional[Any] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._noise = 0.003
        self._mode = "wake"  # wake | listen | busy
        self._end: Optional[SpeechEnd] = None
        self._heard: List[np.ndarray] = []
        self._flush = False
        self.last_error = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"camera-ear-{self.host}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _load(self) -> None:
        if self._wake is None:
            import os

            import openwakeword
            from openwakeword.model import Model

            path = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", WAKE_MODEL + ".onnx")
            self._wake = Model(wakeword_model_paths=[path])
        if not self._ffmpeg:
            import imageio_ffmpeg

            self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def _run(self) -> None:
        try:
            self._load()
        except Exception as e:
            self.last_error = f"couldn't load the wake word ({e})"
            app_logger.log("error", "camera_ear_unavailable", camera_ip=self.host, error_message=self.last_error,
                           component="camera_ear", action="start")
            return
        backoff = RETRY_SECONDS[0]
        while not self._stop.is_set():
            started = time.time()
            self._listen()
            if self._stop.is_set():
                break
            backoff = RETRY_SECONDS[0] if time.time() - started > 60 else min(backoff * 2, RETRY_SECONDS[1])
            app_logger.log("warning", "camera_ear_stopped", camera_ip=self.host, error_message=self.last_error,
                           retry_seconds=backoff, component="camera_ear", action="listen")
            self._stop.wait(backoff)

    def _listen(self) -> None:
        # only the sound track is asked for (no second copy of the picture); straight into memory, never a file
        args = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
                "-allowed_media_types", "audio", "-timeout", "10000000", "-i", self.source,
                "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
        try:
            self._proc = self._popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self.last_error = f"couldn't start ffmpeg ({e})"
            return
        app_logger.log("info", "camera_ear_ready", camera_ip=self.host, component="camera_ear", action="listen")
        band = BandPass()
        self._mode, self._flush = "wake", True
        try:
            while not self._stop.is_set():
                raw = self._proc.stdout.read(CHUNK * 2)
                if not raw or len(raw) < CHUNK * 2:
                    err = self._proc.stderr.read().decode(errors="replace").strip() if self._proc.stderr else ""
                    self.last_error = redact(err[-200:]) or "the camera's sound stopped"
                    return
                self._on_chunk(band(np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0))
        finally:
            self._end_ffmpeg()

    def _end_ffmpeg(self) -> None:
        """Stop ffmpeg for sure: it can ignore a polite stop while the camera holds it, and a leftover keeps one of
        the camera's few connections."""
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------------ the sound
    def _on_chunk(self, audio: np.ndarray) -> None:
        if self._mode == "listen":
            self._heard.append(audio)
            if self._end is not None and self._end.add(rms(audio)):
                chunks, end = self._heard, self._end
                self._heard, self._end, self._mode = [], None, "busy"
                threading.Thread(target=self._handle, args=(chunks, end), daemon=True,
                                 name=f"camera-ear-cmd-{self.host}").start()
            return
        if self._mode != "wake":
            return  # (busy: the light is answering; what's said meanwhile is dropped)
        level = rms(audio)
        if level < self._noise * 2:  # the yard's background level, ignoring speech
            self._noise += (level - self._noise) * 0.02
        if self._flush:  # the model holds ~2 s: push quiet through so the last "Hey Jarvis" can't wake it again
            for _ in range(int(2.0 * RATE / CHUNK)):
                self._wake.predict(np.zeros(CHUNK, np.int16))
            self._wake.reset()
            self._flush = False
        pcm = (np.clip(audio * self._gain(), -1, 1) * 32767).astype(np.int16)
        score = float(self._wake.predict(pcm).get(WAKE_MODEL, 0.0))
        if score >= self._threshold():
            app_logger.log("info", "camera_ear_wake", camera_ip=self.host, score=round(score, 2),
                           component="camera_ear", action="wake")
            self._mode = "listen"
            self._end = SpeechEnd(self._noise, wait=WAIT_FOR_SPEECH, max_len=MAX_COMMAND)
            self._heard = []
            if self._light is not None:
                threading.Thread(target=self._light.listening, args=(self.host,), daemon=True).start()

    def _handle(self, chunks: List[np.ndarray], end: SpeechEnd) -> None:
        """What was said after the wake word: do it if it's allowed out here, and answer with the light."""
        done = False
        try:
            if end.heard:
                audio, _talking = trim_speech(chunks, end.start_level, end.quiet_level)
                if len(audio):
                    audio = normalise(audio)
                    text = self._transcribe(audio)
                    done = bool(text) and self._act(text, audio)  # (the sound too: the Voice row's clip)
            if not done:
                app_logger.log("info", "camera_ear_not_done", camera_ip=self.host, component="camera_ear",
                               action="command")
        except Exception as e:
            app_logger.log("warning", "camera_ear_failed", camera_ip=self.host, error_message=str(e)[:200],
                           component="camera_ear", action="command")
        finally:
            del chunks[:]  # (gone from memory too, not just unused)
            if self._light is not None:
                time.sleep(0.3)  # (the "listening" light may still be on its way)
                self._light.answer(self.host, 1 if done else 2)
            self._flush, self._mode = True, "wake"
