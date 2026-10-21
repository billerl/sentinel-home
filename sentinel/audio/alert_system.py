"""
Audio alert system for Sentinel Home.
Supports Piper TTS (high-quality neural) with pyttsx3 as fallback.
"""

import io
import os
import time
import wave
import threading
import subprocess
import audioop
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

import pyttsx3

from sentinel.constants import (
    DEFAULT_SPEECH_SPEED,
    DEFAULT_VOLUME,
    MOTION_TIMEOUT,
)


@dataclass
class VoiceInfo:
    """Voice info container compatible with pyttsx3 voice objects."""
    id: str
    name: str


class AudioAlertSystem:
    """Handles text-to-speech alerts for motion detection."""

    def __init__(self, settings: Dict[str, Any]):
        self.last_alert_time = 0
        self.piper_voice = None
        self.tts_engine = None
        self.voices: List[VoiceInfo] = []
        self.voice_ids: List[str] = []
        self._tts_thread: Optional[threading.Thread] = None
        self._speech_rate: int = settings.get("speech_speed", DEFAULT_SPEECH_SPEED)
        self._volume: float = settings.get("volume", DEFAULT_VOLUME)

        # Try Piper first (neural, natural-sounding)
        self._try_init_piper(settings)

        # Fall back to pyttsx3 if no Piper models found
        if not self.piper_voice:
            self._try_init_pyttsx3(settings)

    # ------------------------------------------------------------------
    # Piper TTS
    # ------------------------------------------------------------------

    def _try_init_piper(self, settings: Dict[str, Any]) -> None:
        """Attempt to initialise the Piper TTS engine."""
        try:
            from piper.voice import PiperVoice  # noqa: F401 – verify importable
        except ImportError:
            print("Piper TTS not installed, falling back to pyttsx3")
            return

        model_dir = "models"
        if not os.path.exists(model_dir):
            return

        onnx_files = sorted(f for f in os.listdir(model_dir) if f.endswith(".onnx"))
        if not onnx_files:
            return

        for onnx_file in onnx_files:
            voice_id = onnx_file[:-5]  # strip .onnx
            self.voices.append(VoiceInfo(id=voice_id, name=self._piper_display_name(voice_id)))
            self.voice_ids.append(voice_id)

        selected_id = settings.get("voice_id", self.voice_ids[0])
        if selected_id not in self.voice_ids:
            selected_id = self.voice_ids[0]

        try:
            self._load_piper_model(selected_id)
            print(f"Piper TTS initialised with voice: {selected_id}")
        except Exception as e:
            print(f"Error loading Piper model '{selected_id}': {e}")
            self.piper_voice = None

    def _load_piper_model(self, voice_id: str) -> None:
        """Load a Piper .onnx model by voice_id."""
        from piper.voice import PiperVoice
        model_path = os.path.join("models", f"{voice_id}.onnx")
        config_path = model_path + ".json"
        config = config_path if os.path.exists(config_path) else None
        self.piper_voice = PiperVoice.load(model_path, config_path=config)

    @staticmethod
    def _piper_display_name(voice_id: str) -> str:
        """
        Convert a Piper model ID to a readable label.
        e.g. en_US-lessac-medium  -> Lessac (US, Medium)
             en_GB-cori-high      -> Cori (GB, High)
        """
        try:
            lang_part, rest = voice_id.split("-", 1)
            region = lang_part.split("_")[1] if "_" in lang_part else ""
            parts = rest.rsplit("-", 1)
            speaker = parts[0].title()
            quality = parts[1].title() if len(parts) > 1 else ""
            label = speaker
            extras = ", ".join(filter(None, [region, quality]))
            if extras:
                label += f" ({extras})"
            return label
        except Exception:
            return voice_id

    # ------------------------------------------------------------------
    # pyttsx3 fallback
    # ------------------------------------------------------------------

    def _try_init_pyttsx3(self, settings: Dict[str, Any]) -> None:
        """Initialise pyttsx3 as a fallback TTS engine."""
        try:
            self.tts_engine = pyttsx3.init()
            raw_voices = self.tts_engine.getProperty("voices")
            self.voices = [
                VoiceInfo(id=v.id, name=getattr(v, "name", str(v.id)))
                for v in raw_voices
            ]
            self.voice_ids = [v.id for v in self.voices]

            self.tts_engine.setProperty("rate", settings.get("speech_speed", DEFAULT_SPEECH_SPEED))
            self.tts_engine.setProperty("volume", settings.get("volume", DEFAULT_VOLUME))

            voice_id = settings.get("voice_id", self.voice_ids[0] if self.voice_ids else None)
            if voice_id and voice_id in self.voice_ids:
                self.tts_engine.setProperty("voice", voice_id)
            elif self.voice_ids:
                self.tts_engine.setProperty("voice", self.voice_ids[0])

            print("pyttsx3 TTS engine initialised successfully")
        except Exception as e:
            print(f"Error initialising pyttsx3: {e}")
            self.tts_engine = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_alert(self, message: str = "Motion detected!") -> bool:
        """Speak an alert if the cooldown has elapsed."""
        current_time = time.time()
        if (current_time - self.last_alert_time) <= MOTION_TIMEOUT:
            return False
        # Don't stack TTS calls
        if self._tts_thread and self._tts_thread.is_alive():
            return False

        self.last_alert_time = current_time

        if self.piper_voice:
            self._tts_thread = threading.Thread(
                target=self._speak_piper,
                args=(message,),
                daemon=True,
            )
            self._tts_thread.start()
            return True

        if self.tts_engine:
            try:
                self.tts_engine.say(message)
                self.tts_engine.runAndWait()
                return True
            except Exception as e:
                print(f"Error triggering alert: {e}")

        return False

    def _speak_piper(self, text: str) -> None:
        """Synthesise speech with Piper and play it via aplay (background thread)."""
        try:
            from piper.config import SynthesisConfig
            length_scale = 150.0 / max(self._speech_rate, 1)
            syn_config = SynthesisConfig(length_scale=length_scale)

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wav_file:
                # set_wav_format=True lets piper configure nchannels/samplerate/etc.
                self.piper_voice.synthesize_wav(
                    text, wav_file, syn_config=syn_config, set_wav_format=True
                )

            wav_payload = self._apply_volume_to_wav(wav_buffer.getvalue(), self._volume)
            proc = subprocess.Popen(
                ["aplay", "-q", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(wav_payload)
        except Exception as e:
            print(f"Piper TTS playback error: {e}")

    def _apply_volume_to_wav(self, wav_bytes: bytes, volume: float) -> bytes:
        """Scale WAV PCM amplitude to honor volume slider for Piper output."""
        volume_scale = max(0.0, min(1.0, float(volume)))
        if volume_scale == 1.0:
            return wav_bytes
        try:
            source = io.BytesIO(wav_bytes)
            target = io.BytesIO()
            with wave.open(source, "rb") as in_wav:
                params = in_wav.getparams()
                raw_frames = in_wav.readframes(in_wav.getnframes())
                scaled_frames = audioop.mul(
                    raw_frames,
                    in_wav.getsampwidth(),
                    volume_scale,
                )
            with wave.open(target, "wb") as out_wav:
                out_wav.setparams(params)
                out_wav.writeframes(scaled_frames)
            return target.getvalue()
        except Exception:
            return wav_bytes

    def update_settings(self, settings: Dict[str, Any]) -> None:
        """Apply updated settings to the active TTS engine."""
        if "speech_speed" in settings:
            self._speech_rate = settings["speech_speed"]
            if self.tts_engine:
                try:
                    self.tts_engine.setProperty("rate", self._speech_rate)
                except Exception:
                    pass

        if "volume" in settings:
            self._volume = settings["volume"]
            if self.tts_engine:
                try:
                    self.tts_engine.setProperty("volume", self._volume)
                except Exception:
                    pass

        if "voice_id" in settings:
            new_id = settings["voice_id"]
            if self.piper_voice is not None and new_id in self.voice_ids:
                try:
                    self._load_piper_model(new_id)
                    print(f"Switched Piper voice to: {new_id}")
                except Exception as e:
                    print(f"Error switching Piper voice: {e}")
            elif self.tts_engine and new_id in self.voice_ids:
                try:
                    self.tts_engine.setProperty("voice", new_id)
                except Exception:
                    pass

    def cleanup(self) -> None:
        """Clean up TTS resources."""
        if self.tts_engine:
            try:
                self.tts_engine.stop()
                self.tts_engine = None
            except Exception as e:
                print(f"Error cleaning up TTS: {e}")
