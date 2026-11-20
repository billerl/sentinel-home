"""
Snore watch (trial, 2026-11-19): can the house microphone hear someone fall asleep in front of the TV?

The idea is a sleep timer that turns the TV off once someone has dozed off. Before anything acts on it, this only
notes what it hears: the microphone's sound, about a second at a time every half second, goes through YAMNet
(Google's AudioSet sound classifier, Apache-2.0, `models/sound/yamnet.tflite`, ~5 ms on the CPU with OpenVINO) and
is dropped. Nothing is recorded or kept. For each minute in which snoring scored at all, one `snore_heard` event is
logged: how many of that minute's checks sounded like snoring, the best score, the minute's commonest sounds (to
tell a person from snoring on the TV) and whether the Samsung was on.

Feed it the microphone's unfiltered sound: the voice band-pass (150 Hz and up) takes the low rumble out of snoring
and cut a test recording's snoring checks from 47% to 26%.
"""

from __future__ import annotations

import os
import time
import zipfile
from collections import Counter
from typing import Callable, List, Optional, Sequence

import numpy as np

from sentinel.logging_utils import app_logger

MODEL = os.path.join("models", "sound", "yamnet.tflite")
WINDOW = 15600  # 0.975 s at 16 kHz: what YAMNet takes in
HOP = 8000  # a check every half second
SNORING = 38  # "Snoring" in YAMNet's label list
HEARD = 0.3  # a check counts as snoring from here (snoring test recordings: 23-73% of checks; speech scored 0.0)
NOTE_FROM = 0.1  # minutes whose best snoring score stays under this aren't logged


def load_yamnet(path: str = MODEL):
    """(classify(window) -> 521 scores, labels). The label list travels inside the .tflite (a zip on its end)."""
    import openvino as ov

    compiled = ov.Core().compile_model(path, "CPU", {"INFERENCE_NUM_THREADS": 1})
    with zipfile.ZipFile(path) as z:
        labels = z.read("yamnet_label_list.txt").decode().splitlines()
    return (lambda window: compiled([window.astype(np.float32)])[0][0]), labels


class SnoreWatch:
    def __init__(self, enabled: Callable[[], bool], tv_on: Optional[Callable[[], bool]] = None,
                 model_path: str = MODEL, log: Callable = app_logger.log, clock: Callable[[], float] = time.time,
                 classify: Optional[Callable[[np.ndarray], Sequence[float]]] = None,
                 labels: Optional[List[str]] = None) -> None:
        self.enabled = enabled
        self.tv_on = tv_on
        self.model_path = model_path
        self._log = log
        self._clock = clock
        self._classify = classify
        self._labels = labels or []
        self._failed = False
        self._buf = np.zeros(0, np.float32)
        self._minute: Optional[int] = None
        self._checks = self._snores = 0
        self._best = 0.0
        self._sounds: Counter = Counter()

    def feed(self, audio: np.ndarray) -> None:
        """One chunk of the microphone (float, 16 kHz, unfiltered); called from the voice thread."""
        if self._failed or not self._on():
            self._flush()
            self._buf = np.zeros(0, np.float32)
            return
        self._buf = np.concatenate((self._buf, audio.astype(np.float32)))
        while len(self._buf) >= WINDOW:
            window, self._buf = self._buf[:WINDOW], self._buf[HOP:]
            scores = self._run(window)
            if scores is None:
                return
            self._count(scores)

    def _on(self) -> bool:
        try:
            return bool(self.enabled())
        except Exception:
            return False

    def _run(self, window: np.ndarray):
        if self._classify is None:
            try:
                self._classify, self._labels = load_yamnet(self.model_path)
            except Exception as e:
                self._failed = True
                self._log("warning", "snore_watch_unavailable", error_message=f"couldn't load {self.model_path} ({e})",
                          component="voice", action="snore_watch")
                return None
            self._log("info", "snore_watch_ready", component="voice", action="snore_watch")
        return self._classify(window)

    def _count(self, scores: Sequence[float]) -> None:
        minute = int(self._clock() // 60)
        if self._minute is not None and minute != self._minute:
            self._flush()
        self._minute = minute
        self._checks += 1
        snore = float(scores[SNORING])
        self._best = max(self._best, snore)
        if snore >= HEARD:
            self._snores += 1
        self._sounds[int(np.argmax(scores))] += 1

    def _flush(self) -> None:
        if self._minute is not None and self._best >= NOTE_FROM:
            tv = None
            if self.tv_on:
                try:
                    tv = bool(self.tv_on())
                except Exception:
                    tv = None
            self._log("info", "snore_heard", minute=time.strftime("%H:%M", time.localtime(self._minute * 60)),
                      checks=self._checks, snoring=self._snores, best=round(self._best, 2),
                      sounds=[self._labels[i] if i < len(self._labels) else str(i)
                              for i, _ in self._sounds.most_common(3)],
                      tv_on=tv, component="voice", action="snore_watch")
        self._minute = None
        self._checks = self._snores = 0
        self._best = 0.0
        self._sounds = Counter()
