"""
Reminders: "remind me Wednesday at 9 about my PT appointment" -> a Telegram message at that time.

Claude (the opt-in helper) turns the spoken or typed words into Sentinel's exact command
``Remind 2026-11-04 09:00 PT appointment at 11am``; Sentinel only saves it once the person taps the
button or says yes. Typing the exact command works without Claude. Kept in SQLite so restarts
don't lose them; one sent while Sentinel was down goes out as soon as it is back, marked late.
"""

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

DB_NAME = "reminders.db"
MAX_TEXT = 300
MAX_DAYS_AHEAD = 366
LATE_SECONDS = 5 * 60  # sent later than this after its time: say so

REMIND = re.compile(r"^remind\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s+(.+)$", re.IGNORECASE | re.DOTALL)
LIST = re.compile(r"^(?:what are )?(?:my |the )?reminders\??$", re.IGNORECASE)
CANCEL = re.compile(r"^cancel reminder\s+#?(\d{1,6})$", re.IGNORECASE)


@dataclass
class Reminder:
    id: int
    due: float
    text: str
    chat_id: str  # "" = every Sentinel chat (set by voice)


def is_reminder_command(text: str) -> bool:
    t = (text or "").strip()
    return bool(REMIND.match(t) or LIST.match(t) or CANCEL.match(t))


def parse_remind(text: str, now: Optional[float] = None) -> Tuple[Optional[Tuple[float, str]], str]:
    """``Remind YYYY-MM-DD HH:MM what`` -> ((due, what), "") or (None, why not)."""
    m = REMIND.match((text or "").strip())
    if not m:
        return None, "Write it as: Remind 2026-11-04 09:00 PT appointment"
    date, hour, minute, what = m.group(1), int(m.group(2)), int(m.group(3)), " ".join(m.group(4).split())
    if hour > 23 or minute > 59:
        return None, "That time doesn't exist."
    try:
        due = time.mktime(time.strptime(f"{date} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M"))
    except ValueError:
        return None, "That date doesn't exist."
    now = time.time() if now is None else now
    if due <= now:
        return None, "That time has already passed."
    if due > now + MAX_DAYS_AHEAD * 86400:
        return None, "That's more than a year away."
    if not what:
        return None, "What should I remind you about?"
    return (due, what[:MAX_TEXT]), ""


def when_words(ts: float, now: Optional[float] = None) -> str:
    """'today at 4:00 pm', 'tomorrow at 9:00 am', 'Wednesday November 4 at 9:00 am'."""
    now = time.time() if now is None else now
    t = time.localtime(ts)
    clock = f"{t.tm_hour % 12 or 12}:{t.tm_min:02d} {'am' if t.tm_hour < 12 else 'pm'}"
    days = (time.mktime(time.strptime(time.strftime("%Y-%m-%d", t), "%Y-%m-%d"))
            - time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"))) / 86400
    day = {0: "today", 1: "tomorrow"}.get(round(days)) or f"{time.strftime('%A %B', t)} {t.tm_mday}"
    return f"{day} at {clock}"


class ReminderStore:
    def __init__(self, root: str) -> None:
        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                         " due REAL NOT NULL, text TEXT NOT NULL, chat_id TEXT NOT NULL DEFAULT '',"
                         " created REAL NOT NULL, sent REAL)")
        try:
            os.chmod(self.db_path, 0o600)  # personal appointments
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(r) -> Reminder:
        return Reminder(r["id"], r["due"], r["text"], r["chat_id"])

    def add(self, due: float, text: str, chat_id: str = "") -> Reminder:
        with self._lock, self._connect() as conn:
            cur = conn.execute("INSERT INTO reminders (due, text, chat_id, created) VALUES (?, ?, ?, ?)",
                               (due, text, chat_id or "", time.time()))
            return Reminder(int(cur.lastrowid), due, text, chat_id or "")

    def pending(self, chat_id: Optional[str] = None) -> List[Reminder]:
        """Not yet sent, soonest first; for a chat: its own plus the voice ones (sent to every chat)."""
        sql, args = "SELECT * FROM reminders WHERE sent IS NULL", []
        if chat_id is not None:
            sql, args = sql + " AND chat_id IN (?, '')", [chat_id]
        with self._connect() as conn:
            return [self._row(r) for r in conn.execute(sql + " ORDER BY due", args)]

    def due(self, now: float) -> List[Reminder]:
        return [r for r in self.pending() if r.due <= now]

    def mark_sent(self, reminder_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE reminders SET sent = ? WHERE id = ?", (time.time(), reminder_id))

    def cancel(self, reminder_id: int, chat_id: Optional[str] = None) -> bool:
        sql, args = "DELETE FROM reminders WHERE id = ? AND sent IS NULL", [reminder_id]
        if chat_id is not None:
            sql, args = sql + " AND chat_id IN (?, '')", args + [chat_id]
        with self._lock, self._connect() as conn:
            return conn.execute(sql, args).rowcount > 0

    def prune(self, now: float, keep_days: int = 30) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM reminders WHERE sent IS NOT NULL AND sent < ?", (now - keep_days * 86400,))


def due_message(r: Reminder, now: float) -> str:
    text = f"⏰ Reminder: {r.text}"
    if now - r.due > LATE_SECONDS:
        text += f"\n(This was for {when_words(r.due, now)}; Sentinel wasn't running then.)"
    return text
