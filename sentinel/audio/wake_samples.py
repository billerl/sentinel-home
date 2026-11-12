"""
Real recordings of a wake word ("Jezebel"), for retraining its model on the family's own voices.

A custom wake word starts out trained on computer-made voices only, and hears real people badly. "Hey Jarvis,
train Jezebel" (VoiceAssistant._practice) records the word after each beep, as the wake model itself hears it
(same microphone, filter and gain), and keeps each one here: recorded_videos/wake_samples/<word>/*.wav plus
index.jsonl (what Whisper thought it was, for weeding out mistakes before training). They never leave this
computer; the training itself happens outside Sentinel.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import wave
from typing import Callable

import numpy as np

FOLDER = "wake_samples"
RATE = 16000


class WakeSamples:
    def __init__(self, root: str, clock: Callable[[], float] = time.time) -> None:
        self.root = os.path.join(os.path.abspath(root), FOLDER)
        self._clock = clock
        self._lock = threading.Lock()

    def _dir(self, word: str) -> str:
        return os.path.join(self.root, re.sub(r"[^a-z0-9_]+", "_", word.lower()).strip("_") or "word")

    def count(self, word: str) -> int:
        try:
            return sum(1 for name in os.listdir(self._dir(word)) if name.endswith(".wav"))
        except OSError:
            return 0

    def add(self, word: str, pcm: np.ndarray, heard: str = "") -> int:
        """Keep one recording (16 kHz mono int16); how many there are of this word now."""
        folder = self._dir(word)
        with self._lock:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            now = self._clock()
            name = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}.wav"
            with wave.open(os.path.join(folder, name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
            with open(os.path.join(folder, "index.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"file": name, "at": round(now), "seconds": round(len(pcm) / RATE, 2),
                                    "heard": heard[:80]}) + "\n")
            return self.count(word)
