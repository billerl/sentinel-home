"""Snore watch: notes minutes with snoring, keeps nothing, never stops the wake word."""

import _test_env  # noqa: F401  (isolates the event log)

import io
import types
import unittest
from unittest import mock

import numpy as np

from sentinel.audio import snore as sn
from sentinel.audio import voice as v
from sentinel.audio.snore import HOP, SNORING, WINDOW, SnoreWatch
from sentinel.audio.voice import VoiceAssistant

LABELS = [f"sound{i}" for i in range(521)]
LABELS[0], LABELS[SNORING], LABELS[519] = "Speech", "Snoring", "Television"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def scores(snoring=0.0, top=0):
    s = np.zeros(521, np.float32)
    s[top] = 0.9
    s[SNORING] = snoring
    return s


class SnoreWatchTest(unittest.TestCase):
    def make(self, results, enabled=True, tv=True):
        self.logs, self.clock, self.seen = [], Clock(), []
        it = iter(results)

        def classify(window):
            self.seen.append(len(window))
            self.clock.t += 0.5
            return next(it)

        self.on = [enabled]
        return SnoreWatch(lambda: self.on[0], tv_on=lambda: tv, log=lambda *a, **k: self.logs.append((a, k)),
                          clock=self.clock, classify=classify, labels=LABELS)

    def heard(self):
        return [k for a, k in self.logs if a[1] == "snore_heard"]

    def test_checks_a_second_every_half_second(self):
        w = self.make([scores()] * 10)
        w.feed(np.zeros(WINDOW + 3 * HOP, np.float32))
        self.assertEqual(self.seen, [WINDOW] * 4)

    def test_one_note_per_minute_with_snoring(self):
        results = [scores(0.8, top=SNORING)] * 5 + [scores(0.05, top=519)] * 5 + [scores()]
        w = self.make(results)
        self.clock.t = 60 * (self.clock.t // 60) + 54.5  # 10 checks end the minute, the 11th starts the next
        w.feed(np.zeros(WINDOW + 10 * HOP, np.float32))
        notes = self.heard()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["snoring"], 5)
        self.assertEqual(notes[0]["checks"], 10)
        self.assertEqual(notes[0]["best"], 0.8)
        self.assertEqual(notes[0]["sounds"][:2], ["Snoring", "Television"])
        self.assertTrue(notes[0]["tv_on"])

    def test_quiet_minutes_arent_noted(self):
        w = self.make([scores(0.02, top=0)] * 200)
        w.feed(np.zeros(WINDOW + 150 * HOP, np.float32))
        self.assertEqual(self.heard(), [])

    def test_off_does_nothing_and_ends_the_minute(self):
        w = self.make([scores(0.6)] * 3)
        w.feed(np.zeros(WINDOW, np.float32))
        self.on[0] = False
        w.feed(np.zeros(WINDOW, np.float32))
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(len(self.heard()), 1)

    def test_missing_model_is_reported_once(self):
        logs = []
        w = SnoreWatch(lambda: True, model_path="/nonexistent/yamnet.tflite",
                       log=lambda *a, **k: logs.append(a[1]))
        for _ in range(3):
            w.feed(np.zeros(WINDOW, np.float32))
        self.assertEqual(logs, ["snore_watch_unavailable"])

    def test_labels_come_from_the_model_file(self):
        if not __import__("os").path.isfile(sn.MODEL):
            self.skipTest("YAMNet not downloaded")
        classify, labels = sn.load_yamnet()
        self.assertEqual(labels[SNORING], "Snoring")
        self.assertEqual(len(classify(np.zeros(WINDOW, np.float32))), len(labels))


class MicLoopTest(unittest.TestCase):
    def test_snore_watch_gets_the_unfiltered_sound_and_cant_stop_the_wake_word(self):
        tone = (np.sin(np.arange(v.CHUNK * 4) * 2 * np.pi * 60 / v.RATE) * 3000).astype(np.int16)  # 60 Hz hum
        proc = types.SimpleNamespace(stdout=io.BytesIO(tone.tobytes()), stderr=io.BytesIO(b""),
                                     terminate=lambda: None, wait=lambda timeout=None: None)
        fed, chunks = [], []

        class Boom:
            def feed(self, audio):
                fed.append(audio)
                raise RuntimeError("model broke")

        va = VoiceAssistant({"voice_gain": 2.0}, lambda t, s: None, None, popen=lambda *a, **k: proc, snore=Boom())
        with mock.patch.object(v, "STARTUP_SKIP_SECONDS", 0), mock.patch.object(va, "_on_chunk", chunks.append):
            va._listen_to_mic()
        self.assertEqual(len(fed), 1)  # failed once, then left out
        self.assertIsNone(va.snore)
        self.assertEqual(len(chunks), 4)  # the wake word kept hearing every chunk
        expected = tone[:v.CHUNK].astype(np.float32) / 32768.0 * 2.0
        np.testing.assert_allclose(fed[0], expected, atol=1e-6)  # the 60 Hz hum the voice filter would remove


if __name__ == "__main__":
    unittest.main()
