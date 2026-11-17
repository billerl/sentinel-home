"""
What was asked of Jarvis, and where: the events timeline's Voice row (pink), next to the camera clips.

One row per request: when, where it was heard (Inside, Phone, or a camera's name), what was said and what
Jarvis did. A camera's microphone keeps only what's said after "Hey Jarvis", like the house mic: conversations
outside are never kept (sentinel/audio/camera_ear.py). Kept KEEP_DAYS (voice_requests.db beside the other stores).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

KEEP_DAYS = 90
TEXT_MAX = 200


class VoiceLog:
    def __init__(self, root: str, clock=time.time) -> None:
        self.db_path = os.path.join(root, "voice_requests.db")
        self._clock = clock
        self._lock = threading.Lock()
        self._pruned = 0.0
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                         "place TEXT NOT NULL, said TEXT NOT NULL DEFAULT '', did TEXT NOT NULL DEFAULT '', "
                         "ok INTEGER NOT NULL DEFAULT 1)")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def add(self, place: str, said: str = "", did: str = "", ok: bool = True, ts: Optional[float] = None) -> None:
        now = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO requests (ts, place, said, did, ok) VALUES (?, ?, ?, ?, ?)",
                         (now if ts is None else ts, str(place)[:40], str(said or "")[:TEXT_MAX],
                          str(did or "")[:TEXT_MAX], 1 if ok else 0))
            if now - self._pruned > 86400:
                conn.execute("DELETE FROM requests WHERE ts < ?", (now - KEEP_DAYS * 86400,))
                self._pruned = now

    def between(self, start: float, end: float) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT ts, place, said, did, ok FROM requests WHERE ts >= ? AND ts < ? ORDER BY ts",
                                (start, end)).fetchall()
        return [{"ts": ts, "place": place, "said": said, "did": did, "ok": bool(ok)} for ts, place, said, did, ok in rows]
