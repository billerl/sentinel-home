"""
What was asked of Jarvis, and where: the events timeline's Voice row (pink), next to the camera clips.

One row per request: when, where it was heard (Inside, Phone, or a camera's name), what was said and what
Jarvis did. A camera's microphone keeps only what's said after "Hey Jarvis", like the house mic: conversations
outside are never kept (sentinel/audio/camera_ear.py). Kept KEEP_DAYS (voice_requests.db beside the other stores).

The sound of the request itself (what was said after the wake word, as Jarvis heard it) is kept too, CLIP_DAYS,
as a small WAV in CLIP_DIR, so the family can hear what they sounded like (2026-11-17: "might be helpful for me to
hear what I sound like"): the pink entries get a play button.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import wave
from typing import Any, Dict, List, Optional

KEEP_DAYS = 90
CLIP_DAYS = 30
CLIP_DIR = "voice_clips"
CLIP_MAX_SECONDS = 20  # (a dictated note can run 90 s: the start is enough to hear how it sounded)
RATE = 16000
TEXT_MAX = 200


class VoiceLog:
    def __init__(self, root: str, clock=time.time) -> None:
        self.db_path = os.path.join(os.path.abspath(root), "voice_requests.db")
        self.clips = os.path.join(os.path.abspath(root), CLIP_DIR)  # (absolute: Flask's send_file)
        self._clock = clock
        self._lock = threading.Lock()
        self._pruned = 0.0
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                         "place TEXT NOT NULL, said TEXT NOT NULL DEFAULT '', did TEXT NOT NULL DEFAULT '', "
                         "ok INTEGER NOT NULL DEFAULT 1)")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts)")
            if "clip" not in [r[1] for r in conn.execute("PRAGMA table_info(requests)")]:
                conn.execute("ALTER TABLE requests ADD COLUMN clip TEXT")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def add(self, place: str, said: str = "", did: str = "", ok: bool = True, ts: Optional[float] = None,
            audio: Any = None) -> None:
        """One request; audio (16 kHz mono floats, what Jarvis heard) becomes its clip."""
        now = self._clock()
        with self._lock, self._connect() as conn:
            cur = conn.execute("INSERT INTO requests (ts, place, said, did, ok) VALUES (?, ?, ?, ?, ?)",
                               (now if ts is None else ts, str(place)[:40], str(said or "")[:TEXT_MAX],
                                str(did or "")[:TEXT_MAX], 1 if ok else 0))
            name = self._save_clip(cur.lastrowid, audio) if audio is not None else None
            if name:
                conn.execute("UPDATE requests SET clip = ? WHERE id = ?", (name, cur.lastrowid))
            if now - self._pruned > 86400:
                for (old,) in conn.execute("SELECT clip FROM requests WHERE clip IS NOT NULL AND ts < ?",
                                           (now - CLIP_DAYS * 86400,)).fetchall():
                    try:
                        os.remove(os.path.join(self.clips, old))
                    except OSError:
                        pass
                conn.execute("UPDATE requests SET clip = NULL WHERE ts < ?", (now - CLIP_DAYS * 86400,))
                conn.execute("DELETE FROM requests WHERE ts < ?", (now - KEEP_DAYS * 86400,))
                self._pruned = now

    def _save_clip(self, row_id: int, audio: Any) -> Optional[str]:
        try:
            import numpy as np

            pcm = (np.clip(np.asarray(audio, dtype=np.float32)[:CLIP_MAX_SECONDS * RATE], -1, 1) * 32767).astype("<i2")
            if not len(pcm):
                return None
            os.makedirs(self.clips, exist_ok=True)
            name = f"{row_id}.wav"
            with wave.open(os.path.join(self.clips, name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(pcm.tobytes())
            return name
        except Exception:
            return None  # (a clip that can't be kept never costs the request)

    def clip_path(self, row_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT clip FROM requests WHERE id = ?", (int(row_id),)).fetchone()
        path = os.path.join(self.clips, row[0]) if row and row[0] else None
        return path if path and os.path.isfile(path) else None

    def between(self, start: float, end: float) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, ts, place, said, did, ok, clip FROM requests WHERE ts >= ? AND ts < ? "
                                "ORDER BY ts", (start, end)).fetchall()
        return [{"id": i, "ts": ts, "place": place, "said": said, "did": did, "ok": bool(ok), "clip": bool(clip)}
                for i, ts, place, said, did, ok, clip in rows]
