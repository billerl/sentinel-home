import io
import math
import wave
import unittest

import numpy as np

from sentinel.audio.alert_system import AudioAlertSystem
from sentinel.detection.motion_detector import MotionDetector


class RecordingAndAudioTests(unittest.TestCase):
    def test_recording_disabled_blocks_start(self):
        detector = MotionDetector()
        detector.recording_enabled = False
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        status, path = detector.check_recording_status(True, frame)
        self.assertIsNone(status)
        self.assertIsNone(path)
        self.assertFalse(detector.recording)

    def test_piper_wav_volume_scaling(self):
        system = AudioAlertSystem(
            {
                "speech_speed": 150,
                "volume": 1.0,
                "voice_id": "",
            }
        )
        wav_data = self._make_wav_samples()
        quieter = system._apply_volume_to_wav(wav_data, 0.3)
        self.assertNotEqual(wav_data, quieter)
        self.assertLess(self._rms_from_wav(quieter), self._rms_from_wav(wav_data))

    def _make_wav_samples(self) -> bytes:
        sample_rate = 16000
        duration_s = 0.15
        frequency = 440
        total_samples = int(sample_rate * duration_s)
        frames = []
        for i in range(total_samples):
            value = int(12000 * math.sin(2 * math.pi * frequency * i / sample_rate))
            frames.append(value.to_bytes(2, byteorder="little", signed=True))
        raw = b"".join(frames)
        payload = io.BytesIO()
        with wave.open(payload, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(raw)
        return payload.getvalue()

    def _rms_from_wav(self, wav_payload: bytes) -> float:
        with wave.open(io.BytesIO(wav_payload), "rb") as wav_file:
            raw = wav_file.readframes(wav_file.getnframes())
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            return float(np.sqrt(np.mean(np.square(data))))


if __name__ == "__main__":
    unittest.main()
