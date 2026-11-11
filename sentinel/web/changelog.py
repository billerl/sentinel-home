"""
The change log: who changed what in Sentinel's setup, for the owner and admins (Settings page).
One line per change, written by sentinel/web/roles.py after a request that worked. Names of things only,
never the values (no passwords, addresses or positions).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

DB_NAME = "changes.db"
KEEP_DAYS = 365
SAME_WITHIN = 600.0  # the same person doing the same thing again (dragging devices on the map): one line


class ChangeLog:
    def __init__(self, root: str) -> None:
        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                         " who TEXT NOT NULL, role TEXT NOT NULL, what TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',"
                         " times INTEGER NOT NULL DEFAULT 1)")
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, who: str, role: str, what: str, detail: str = "", now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        who, what, detail = str(who)[:60], str(what)[:80], str(detail or "")[:120]
        with self._lock, self._connect() as conn:
            last = conn.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 1").fetchone()
            if (last is not None and (last["who"], last["what"], last["detail"]) == (who, what, detail)
                    and now - last["ts"] < SAME_WITHIN):
                conn.execute("UPDATE changes SET ts = ?, times = times + 1 WHERE id = ?", (now, last["id"]))
            else:
                conn.execute("INSERT INTO changes (ts, who, role, what, detail) VALUES (?, ?, ?, ?, ?)",
                             (now, who, role, what, detail))
            conn.execute("DELETE FROM changes WHERE ts < ?", (now - KEEP_DAYS * 86400,))

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT ts, who, role, what, detail, times FROM changes"
                                                  " ORDER BY id DESC LIMIT ?", (max(1, min(1000, int(limit))),))]
