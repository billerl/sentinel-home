"""
Alerts read out in a browser: the home page's "Hear alerts" switch (static/hear.js, sentinel/web/routes/talk.py).

The last few alerts Sentinel raised (someone at the door, a car pulling in, a package, a weather warning), kept in
memory only. A page that has the switch on asks every few seconds for what's new and plays each one in Jarvis's
voice; the WAV is made once, the first time a page asks for it.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

KEEP = 30  # alerts remembered
FRESH_SECONDS = 90  # an older one isn't read out (a page that was asleep doesn't recite the afternoon)
HELLO = "I'll read alerts out here."
HELLO_ID = 0


def spoken(text: str) -> str:
    """An alert's words as said aloud: no emoji or symbols, one line."""
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Sk", "Mn", "Cf", "Cs"))
    return re.sub(r"\s+", " ", text).strip(" -—:")[:300]


class HeardAlerts:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._alerts: Deque[Dict[str, Any]] = deque(maxlen=KEEP)
        self._next = int(clock() * 1000)  # so numbers keep rising across a restart (pages remember the last one)
        self._voices: Dict[int, bytes] = {}
        self._making = threading.Lock()

    def add(self, text: str) -> None:
        words = spoken(text or "")
        if not words:
            return
        with self._lock:
            self._next += 1
            self._alerts.append({"id": self._next, "at": self._clock(), "text": words})
            kept = {a["id"] for a in self._alerts} | {HELLO_ID}
            self._voices = {k: v for k, v in self._voices.items() if k in kept}

    def since(self, after: Optional[int]) -> Dict[str, Any]:
        """{"latest": the newest alert's number, "items": the fresh ones after ``after``}; nothing to read on a
        page's first ask (after=None): it only learns where to start."""
        now = self._clock()
        with self._lock:
            latest = self._next
            items: List[Dict[str, Any]] = [] if after is None else [
                {"id": a["id"], "text": a["text"]} for a in self._alerts
                if a["id"] > after and now - a["at"] <= FRESH_SECONDS]
        return {"latest": latest, "items": items}

    def voice(self, alert_id: int, synthesize: Callable[[str], Optional[bytes]]) -> Optional[bytes]:
        """The alert as a WAV in Jarvis's voice, or None (no such alert, or no neural voice: the page then
        uses the browser's own voice)."""
        with self._lock:
            text = HELLO if alert_id == HELLO_ID else next((a["text"] for a in self._alerts if a["id"] == alert_id), None)
        if text is None:
            return None
        with self._making:  # two pages asking at once: made once
            wav = self._voices.get(alert_id)
            if wav is None:
                wav = synthesize(text)
                if wav:
                    with self._lock:
                        self._voices[alert_id] = wav
        return wav
