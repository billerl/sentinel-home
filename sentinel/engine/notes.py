"""
Notes: each person's own notes and lists, private (a journal) or shared with the family (the grocery list).

A private note is its writer's alone: no other login sees it, whatever its role. The household login is nobody
in particular, so what it writes is always shared. Kept in recorded_videos/notes.db as plain text (the nightly
backup copies it); nothing here is ever sent to Telegram, Jarvis or Claude.

Who may do what is decided here, from the person asking (``who``: their people.id, None = the household login)
and whether they are an admin, so every way in gets the same answer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

DB_NAME = "notes.db"
MAX_TITLE, MAX_BODY, MAX_ITEMS, MAX_ITEM = 80, 20000, 300, 200
NOT_YOURS = "That note isn't yours to change."
GONE = "That note no longer exists."


def _text(value: Any, limit: int) -> str:
    return (value if isinstance(value, str) else "").replace("\r\n", "\n").replace("\x00", "").strip()[:limit]


class Notes:
    def __init__(self, root: str) -> None:
        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            # owner: people.id of the writer (NULL = the household login); items: a list's lines as JSON
            conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER,"
                         " kind TEXT NOT NULL DEFAULT 'note', title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
                         " items TEXT NOT NULL DEFAULT '[]', shared INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,"
                         " updated REAL NOT NULL, updated_by TEXT NOT NULL DEFAULT '')")
        try:
            os.chmod(self.db_path, 0o600)  # people's private notes
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ who may
    @staticmethod
    def _sees(row, who: Optional[int]) -> bool:
        return bool(row["shared"]) or (who is not None and row["owner"] == who)

    @staticmethod
    def _owns(row, who: Optional[int], admin: bool) -> bool:
        """May delete it or change who sees it: its writer; an admin for a shared note (never a private one)."""
        if who is not None and row["owner"] == who:
            return True
        return bool(row["shared"]) and admin

    def _view(self, row, who: Optional[int], admin: bool, names: Dict[int, str]) -> Dict[str, Any]:
        return {"id": row["id"], "kind": row["kind"], "title": row["title"], "body": row["body"],
                "items": json.loads(row["items"]), "shared": bool(row["shared"]),
                "by": names.get(row["owner"], "") if row["owner"] is not None else "Household",
                "mine": who is not None and row["owner"] == who, "can_manage": self._owns(row, who, admin),
                "created": row["created"], "updated": row["updated"], "updated_by": row["updated_by"]}

    # ------------------------------------------------------------ reading
    def visible(self, who: Optional[int], admin: bool = False, names: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
        """The notes this person may see, the latest changed first: the shared ones and their own."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notes WHERE shared = 1 OR owner = ? ORDER BY updated DESC, id DESC",
                                (who if who is not None else -1,)).fetchall()
        return [self._view(r, who, admin, names or {}) for r in rows if self._sees(r, who)]

    # ------------------------------------------------------------ writing
    def save(self, who: Optional[int], admin: bool, body: Dict[str, Any], by: str = "") -> Tuple[bool, str]:
        """Make a note ({"kind", "title", "body", "shared"}) or change one ({"id", ...}); (ok, its id or why not)."""
        note_id = body.get("id")
        title, text = _text(body.get("title"), MAX_TITLE), _text(body.get("body"), MAX_BODY)
        shared = body.get("shared")
        now = time.time()
        with self._lock, self._connect() as conn:
            if note_id is None:
                kind = "list" if body.get("kind") == "list" else "note"
                if not title and not text:
                    return False, "Give it a title." if kind == "list" else "Write something first."
                if who is None:
                    shared = True  # (the household login is everyone: nothing of its is private)
                cur = conn.execute("INSERT INTO notes (owner, kind, title, body, shared, created, updated, updated_by)"
                                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (who, kind, title, text, int(shared is True), now, now, by[:60]))
                return True, str(cur.lastrowid)
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone() \
                if isinstance(note_id, int) else None
            if row is None or not self._sees(row, who):
                return False, GONE  # (someone else's private note doesn't exist, as far as they can tell)
            if isinstance(shared, bool) and shared != bool(row["shared"]):
                if who is None or row["owner"] != who:
                    return False, "Only its writer can change who sees a note."
                if not shared and row["owner"] is None:
                    return False, "A note from the household login is always shared."
            else:
                shared = bool(row["shared"])
            if not title and not text and row["kind"] == "note":
                return False, "Write something first."
            conn.execute("UPDATE notes SET title = ?, body = ?, shared = ?, updated = ?, updated_by = ? WHERE id = ?",
                         (title, text, int(shared), now, by[:60], row["id"]))
            return True, str(row["id"])

    def items(self, who: Optional[int], note_id: int, change: Dict[str, Any], by: str = "") -> Tuple[bool, str]:
        """One change to a list, so two people shopping don't undo each other: {"add": "milk"},
        {"toggle": item id, "done": bool}, {"remove": item id} or {"clear_done": true}."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None or not self._sees(row, who):
                return False, GONE
            if row["kind"] != "list":
                return False, "That's a note, not a list."
            items = json.loads(row["items"])
            if "add" in change:
                text = _text(change.get("add"), MAX_ITEM).replace("\n", " ")
                if not text:
                    return False, "Type what to add."
                if len(items) >= MAX_ITEMS:
                    return False, "That list is full: clear the ticked ones first."
                items.append({"id": max([i["id"] for i in items], default=0) + 1, "text": text, "done": False})
            elif "toggle" in change:
                for i in items:
                    if i["id"] == change.get("toggle"):
                        i["done"] = change.get("done") is True
            elif "remove" in change:
                items = [i for i in items if i["id"] != change.get("remove")]
            elif change.get("clear_done") is True:
                items = [i for i in items if not i["done"]]
            else:
                return False, "Nothing to change."
            conn.execute("UPDATE notes SET items = ?, updated = ?, updated_by = ? WHERE id = ?",
                         (json.dumps(items), time.time(), by[:60], note_id))
        return True, "saved"

    def delete(self, who: Optional[int], admin: bool, note_id: int) -> Tuple[bool, str]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None or not self._sees(row, who):
                return False, GONE
            if not self._owns(row, who, admin):
                return False, NOT_YOURS
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return True, "deleted"

    def forget_person(self, person_id: int) -> None:
        """Someone taken off the Family page: their private notes go with them; what they shared stays, as the household's."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM notes WHERE owner = ? AND shared = 0", (person_id,))
            conn.execute("UPDATE notes SET owner = NULL WHERE owner = ?", (person_id,))
