"""
The Talk button on the Sentinel web page: talk to Jarvis through a phone's (or its headset's) microphone.

The page records a few seconds, this turns them into words with the house assistant's own Whisper, asks the
same answers as the house mic (Engine.answer_voice) and sends back the words plus Jarvis speaking them, played on
the phone. Nothing comes out of the house speaker. A question back ("Should I do that? Say yes or no.") becomes
Yes/No buttons; "take a note for Claude" records the note next. Those wait here a couple of minutes, by id.
"""

import base64
import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from sentinel.logging_utils import app_logger

PENDING_SECONDS = 120  # a Yes/No or a note waits this long for the phone
THINKING = ("Let me think.",)  # said before asking Claude: the page shows its own "thinking"


class PhoneTalk:
    def __init__(self, settings: Dict[str, Any], voice: Callable[[], Any], speaker: Callable[[], Any],
                 answer: Callable, decode: Optional[Callable[[bytes], Any]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._voice = voice  # the VoiceAssistant (its Whisper), None while voice is off
        self._speaker = speaker  # AudioAlertSystem (synthesize), None when sound is off
        self._answer = answer  # Engine.answer_voice
        self._decode = decode
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[float, str, Callable]] = {}  # id -> (until, "confirm"|"dictate", what to run)

    def talk(self, audio: bytes, pending: str = "") -> Dict[str, Any]:
        """A recording from the phone -> {"heard", "reply", "audio" (WAV, base64), "ask": "confirm"|"dictate", "id"}
        or {"error"}. ``pending`` = the id of a note being dictated."""
        from sentinel.audio.voice import casual, decode_audio, is_cancel

        voice = self._voice()
        if voice is None:
            return {"error": "Jarvis is turned off (Settings → Audio → voice assistant)."}
        try:
            pcm = (self._decode or decode_audio)(audio)
        except (OSError, ValueError) as e:
            app_logger.log("warning", "phone_talk_unreadable", error_message=str(e), component="voice", action="phone")
            return {"error": "Couldn't read that recording."}
        text, _doubt = voice.transcribe(pcm)
        job = self._take(pending, "dictate") if pending else None
        if not text:
            return {"heard": "", "reply": "", "note": "I didn't hear anything. Tap and try again."}
        if job is not None:
            return self._out(text, "Okay, cancelled." if is_cancel(text) else job(text))
        said = []
        reply = self._answer(text, said.append)
        if reply is None:  # room talk / "stop" with nothing playing: nothing to say
            return {"heard": text, "reply": "", "note": "Nothing to do there."}
        words = " ".join([s for s in said if s not in THINKING] + [casual(reply.text)]).strip()
        if reply.confirm is not None:
            return self._out(text, words, "confirm", reply.confirm)
        if reply.dictate is not None:
            return self._out(text, words, "dictate", reply.dictate)
        return self._out(text, words)

    def answer(self, pending: str, yes: bool) -> Dict[str, Any]:
        """The Yes / No button under a question."""
        job = self._take(pending, "confirm")
        if job is None:
            return {"error": "That question has expired. Ask again."}
        return self._out("Yes" if yes else "No", job() if yes else "Okay, I won't.")

    def _out(self, heard: str, words: str, ask: str = "", then: Optional[Callable] = None) -> Dict[str, Any]:
        app_logger.log("info", "voice_replied", text=(words or "")[:300], confirm=ask == "confirm", source="phone",
                       component="voice", action="respond")
        out: Dict[str, Any] = {"heard": heard, "reply": words}
        speaker = self._speaker()
        wav = speaker.synthesize(words, str(self.settings.get("voice_jarvis_id") or "")) if speaker and words else None
        if wav:
            out["audio"] = base64.b64encode(wav).decode()
        if then is not None:
            pid = secrets.token_urlsafe(9)
            with self._lock:
                now = self._clock()
                self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
                self._pending[pid] = (now + PENDING_SECONDS, ask, then)
            out.update(ask=ask, id=pid)
        return out

    def _take(self, pid: str, kind: str) -> Optional[Callable]:
        with self._lock:
            item = self._pending.pop(pid, None)
        if item is None or item[1] != kind or item[0] < self._clock():
            return None
        return item[2]
