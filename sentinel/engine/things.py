"""
Where things are: "my darts are in the garage cabinet" now, "where are my darts?" months later.

The family tells Sentinel where a thing is kept (by voice, Telegram or the Things page) and asks later. One row per
thing in recorded_videos/things.db: its name as it was said, where it is ("in the red toolbox": the words after
the name, kept whole), who said so and when. Telling it again moves the thing.

Understood (parse_put / parse_find / parse_forget; everything else is somebody else's sentence):
  "my darts are in the garage cabinet" · "I put the 10 mm socket in the red toolbox" · "remember the stepper
  motors are in bin 4" · "remember my darts" (asks "Where are they?") · "where are my darts?" · "where did I put
  the socket?" · "forget where my darts are" · "what things do you know?"
Names are matched loosely: "10 millimeter socket" finds "ten mm sockets".

From a photo (PhotoScans): someone photographs a drawer or shelf on the Things page and says where it is; Claude
(the household's own subscription, sentinel/engine/assistant.py, only when "ask Claude" is on) names what it can
see and describes what it can't. Nothing is kept until the person has ticked what's right and named the
unknowns; the photo stays with those things (things_photos/), so the page can show the drawer again.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

NAME_MAX, PLACE_MAX, MAX_THINGS = 60, 120, 2000

_WHERE = (r"in|inside|on|on top of|under|underneath|beneath|below|above|behind|beside|next to|near|by|at|"
          r"in front of|between|with|over|hanging on|hanging in|out in|up in|down in|back in")
_OWNER = r"(?:my|our|the|a|an|some|those|these|that|this)\s+"
_NUMBERS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
            "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
            "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
            "twenty": "20"}
_UNITS = {"millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm", "mil": "mm",
          "centimeter": "cm", "centimeters": "cm", "inch": "in", "inches": "in"}
# Not things: people, pets and what Sentinel already answers for ("where is my car" is the Vehicles' question).
_NOT_THINGS = re.compile(r"^(?:car|truck|suv|van|vehicle|phone|wife|husband|mom|dad|son|daughter|kid|kids|dog|cat|"
                         r"package|packages|parcel|delivery|everyone|everybody|anyone|it|them|that|this|those|these|one|"
                         r"something|anything|everything|stuff|thing|here|there)$")

_PUT = [
    # "remember (that) my darts are in the garage cabinet" / "my darts are in ..." (my/our only: "the TV is on ...")
    re.compile(rf"^(?:please\s+)?(?:remember|note|know)\s+(?:that\s+)?(?:{_OWNER})?(?P<name>.+?)\s+(?P<verb>is|are)\s+"
               rf"(?:kept\s+|stored\s+|now\s+|sitting\s+|located\s+)?(?P<place>(?:{_WHERE})\s+.+)$"),
    re.compile(rf"^(?:my|our)\s+(?P<name>.+?)\s+(?P<verb>is|are)\s+(?:kept\s+|stored\s+|now\s+|sitting\s+|located\s+)?"
               rf"(?P<place>(?:{_WHERE})\s+.+)$"),
    # "I put / keep / left / moved the 10 mm socket in the red toolbox"
    re.compile(rf"^(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:put|keep|kept|left|leave|store|stored|moved|stuck|hung|"
               rf"placed|stashed)\s+(?:{_OWNER})?(?P<name>.+?)\s+(?:back\s+|away\s+)?(?P<place>(?:{_WHERE})\s+.+)$"),
]
# "remember my darts" / "keep track of the stepper motors": no place yet, so ask
_PUT_ASK = re.compile(rf"^(?:please\s+)?(?:remember where (?:i put |i keep )?|keep track of |track |"
                      rf"remember )(?:{_OWNER})?(?P<name>.+?)(?:\s+(?:is|are|go|goes))?$")
_FIND = [
    re.compile(rf"^(?:do you know |can you tell me |tell me |remind me )?where(?:'s| is| are| did (?:i|we) "
               rf"(?:put|leave|keep|store|stash)| do (?:i|we) keep| (?:i|we) (?:put|left|keep|kept))\s+"
               rf"(?:{_OWNER})?(?P<name>.+?)(?:\s+(?:is|are|at|go|goes|went|again))*$"),
    re.compile(rf"^(?:find|locate)\s+(?:{_OWNER})(?P<name>.+)$"),
    re.compile(rf"^(?:do you know|can you tell me|tell me|remind me)\s+where\s+(?:{_OWNER})?(?P<name>.+?)\s+(?:is|are)$"),
    re.compile(rf"^(?:where(?:'s| is| are)|have you seen)\s+(?:{_OWNER})?(?P<name>.+?)(?:\s+at)?$"),
]
_FORGET = re.compile(rf"^(?:please\s+)?(?:forget|remove|delete)\s+(?:about\s+)?(?:where\s+)?(?:{_OWNER})?(?P<name>.+?)"
                     rf"(?:\s+(?:is|are|from (?:my |the )?things|from (?:my |the )?list))?$")
_LIST = re.compile(r"^(?:what|which) things (?:do you (?:know|remember)|are (?:saved|stored))(?: where they are)?$|"
                   r"^(?:list|read) (?:my |the |our )?things$|^what do you know where it is$")
_ASKED = "explicit"  # parse_find's second value when the words can only be about a kept thing ("where did I put")


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.?!,]+$", "", (text or "").strip().lower()))


def key(name: str) -> str:
    """A name as it's compared: "My 10 Millimeter sockets" -> "10 mm socket"."""
    words = re.sub(r"[^a-z0-9/ ]+", " ", re.sub(r"(\d)\s*-\s*(?=[a-z])", r"\1 ", name.lower())).split()
    out = []
    for w in words:
        w = _UNITS.get(w, _NUMBERS.get(w, w))
        if w in ("my", "our", "the", "a", "an", "some", "of"):
            continue
        m = re.fullmatch(r"(\d+)(mm|cm|in)", w)
        if m:
            out += [m.group(1), m.group(2)]
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-3] + "y" if w.endswith("ies") else w[:-1]
        out.append(w)
    return " ".join(out)


def _name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" .,?!")[:NAME_MAX]


def parse_put(text: str) -> Optional[Tuple[str, Optional[str], str]]:
    """(the thing, where or None when it still has to be asked, "is"/"are")."""
    t = _plain(text)
    for pattern in _PUT:
        m = pattern.match(t)
        if m and key(m.group("name")) and not _NOT_THINGS.match(key(m.group("name"))):
            name = _name(m.group("name"))
            return name, m.group("place").strip()[:PLACE_MAX], m.groupdict().get("verb") or ""
    m = _PUT_ASK.match(t)
    if m and key(m.group("name")) and not _NOT_THINGS.match(key(m.group("name"))) \
            and not re.search(r"\b(?:to|that|when|if|is|are|was|were)\b", m.group("name")):  # ("remember to call Kate" is a reminder)
        name = _name(m.group("name"))
        return name, None, ""
    return None


def parse_find(text: str) -> Optional[Tuple[str, str]]:
    """(the thing asked for, "explicit" when it can only mean a kept thing, else "")."""
    t = _plain(text)
    for i, pattern in enumerate(_FIND):
        m = pattern.match(t)
        if m and key(m.group("name")) and not _NOT_THINGS.match(key(m.group("name"))):
            explicit = i in (1, 2) or bool(re.search(r"\b(?:put|leave|left|keep|kept|store|stash)\b", t))
            return _name(m.group("name")), _ASKED if explicit else ""
    return None


def parse_forget(text: str) -> Optional[str]:
    m = _FORGET.match(_plain(text))
    return _name(m.group("name")) if m and key(m.group("name")) and not _NOT_THINGS.match(key(m.group("name"))) else None


def is_list(text: str) -> bool:
    return bool(_LIST.match(_plain(text)))


def your(words: str) -> str:
    """Said back: "in my toolbox" -> "in your toolbox"."""
    return re.sub(r"\bmy\b", "your", re.sub(r"\bour\b", "your", re.sub(r"\bmine\b", "yours", words)))


class Things:
    def __init__(self, root: str, clock=time.time) -> None:
        self.db_path = os.path.join(os.path.abspath(root), "things.db")
        self._clock = clock
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS things (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                         "key TEXT NOT NULL, place TEXT NOT NULL, verb TEXT NOT NULL DEFAULT 'is', "
                         "by TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS things_key ON things (key)")
            if "photo" not in [r[1] for r in conn.execute("PRAGMA table_info(things)")]:
                conn.execute("ALTER TABLE things ADD COLUMN photo TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(r: sqlite3.Row) -> Dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "place": r["place"], "verb": r["verb"], "by": r["by"],
                "updated": r["updated"], "photo": r["photo"]}

    def all(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return [self._row(r) for r in conn.execute("SELECT * FROM things ORDER BY name COLLATE NOCASE")]

    def put(self, name: Any, place: Any, verb: str = "", by: str = "", thing_id: Optional[int] = None,
            photo: str = "", another: bool = False) -> Tuple[bool, str]:
        """Keep (or move) a thing. With thing_id: that row is changed (the page's edit), name and all.
        another: from a photo of one drawer, so a thing of the same name kept somewhere else stays where it is
        (there's a screwdriver in most drawers); only the same thing in the same place is the same row."""
        name = _name(str(name or ""))
        place = re.sub(r"\s+", " ", str(place or "")).strip(" .")[:PLACE_MAX]
        k = key(name)
        if not k:
            return False, "What is the thing called?"
        if not place:
            return False, "Where is it?"
        if not re.match(rf"^(?:{_WHERE})\b", place.lower()):
            place = "in " + place  # (typed on the page as just "garage cabinet")
        verb = verb if verb in ("is", "are") else ("are" if name.lower().endswith("s") and not name.lower().endswith("ss") else "is")
        now = self._clock()
        with self._lock, self._connect() as conn:
            if thing_id:
                row = conn.execute("SELECT id, photo FROM things WHERE id = ?", (thing_id,)).fetchone()
            elif another:
                row = conn.execute("SELECT id, photo FROM things WHERE key = ? AND lower(place) = ?",
                                   (k, place.lower())).fetchone()
            else:
                row = conn.execute("SELECT id, photo FROM things WHERE key = ?", (k,)).fetchone()
            if thing_id and row is None:
                return False, "That one is gone."
            if row is not None:
                # (moved by word of mouth: the old drawer's photo no longer shows where it is)
                conn.execute("UPDATE things SET name = ?, key = ?, place = ?, verb = ?, by = ?, updated = ?, photo = ? "
                             "WHERE id = ?", (name, k, place, verb, by[:40], now,
                                              photo or (row["photo"] if thing_id else ""), row["id"]))
            else:
                if conn.execute("SELECT COUNT(*) FROM things").fetchone()[0] >= MAX_THINGS:
                    return False, "That's a lot of things. Remove some first."
                conn.execute("INSERT INTO things (name, key, place, verb, by, updated, photo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (name, k, place, verb, by[:40], now, photo))
        return True, f"Okay, {self._subject(name)} {verb} {your(place)}."

    @staticmethod
    def _subject(name: str) -> str:
        return ("your " if re.match(r"^(?:my|our)\b", name.lower()) else "the ") + re.sub(r"^(?:my|our|the)\s+", "", name, flags=re.I)

    def find(self, name: str) -> List[Dict[str, Any]]:
        """The things this name could mean, best first: the same name, else every word of one in the other
        ("socket" finds "10 mm socket" and "socket wrench"), else a near spelling (Whisper's "stopper motor")."""
        k = key(name)
        if not k:
            return []
        things = [dict(t, key=key(t["name"])) for t in self.all()]
        same = [t for t in things if t["key"] == k]
        if same:
            return same
        want = set(k.split())
        part = [t for t in things if want <= set(t["key"].split()) or set(t["key"].split()) <= want]
        if part:
            return sorted(part, key=lambda t: abs(len(t["key"].split()) - len(want)))[:5]
        near = difflib.get_close_matches(k, [t["key"] for t in things], n=3, cutoff=0.78)
        return [t for n in near for t in things if t["key"] == n]

    def answer(self, name: str, explicit: bool = True) -> Optional[str]:
        """Where it is, in words; None when nothing like it is kept and the question might be somebody else's."""
        found = self.find(name)
        if not found:
            return f"I don't know where {self._subject(name)} is yet. Tell me, and I'll remember." if explicit else None
        said = [f"{self._subject(t['name'])} {t['verb']} {your(t['place'])}" for t in found[:3]]
        return (said[0][0].upper() + said[0][1:] + "." if len(said) == 1 else
                "I know of " + str(len(said)) + ": " + "; ".join(said) + ".")

    def forget(self, name: str = "", thing_id: Optional[int] = None) -> Tuple[bool, str]:
        with self._lock, self._connect() as conn:
            if thing_id is not None:
                row = conn.execute("SELECT id, name FROM things WHERE id = ?", (thing_id,)).fetchone()
            else:
                row = conn.execute("SELECT id, name FROM things WHERE key = ?", (key(name),)).fetchone()
            if row is None:
                return False, f"I don't have {self._subject(name)}." if name else "That one is gone."
            conn.execute("DELETE FROM things WHERE id = ?", (row["id"],))
            return True, f"Okay, I've forgotten where {self._subject(row['name'])} was."

    def summary(self) -> str:
        things = self.all()
        if not things:
            return "I don't know where anything is yet. Tell me, like: my darts are in the garage cabinet."
        names = [t["name"] for t in things]
        shown = ", ".join(names[:12])
        return f"I know where {len(things)} thing{'s are' if len(things) != 1 else ' is'}: {shown}" + \
            (f", and {len(names) - 12} more" if len(names) > 12 else "") + "."


# ------------------------------------------------------------------ from a photo
PHOTO_DIR = "things_photos"
PHOTO_MAX_PX = 1568  # (as much as Claude looks at; small parts need it)
PHOTO_MAX_BYTES = 6 * 1024 * 1024
SCAN_KEEP_SECONDS = 2 * 3600
MAX_SEEN = 60

LOOK_SYSTEM = """You help a family keep track of where their things are. You are shown ONE photo of a storage spot
(a drawer, shelf, bin, toolbox, cabinet) and told where it is. List the distinct kinds of things kept there.

Answer with ONLY a JSON object, no other words:
{"items": ["..."], "unsure": [{"looks_like": "...", "guess": "..."}]}

- "items": things you can name with confidence, as a person would ask for them later: short, lower case, specific
  when you can read or clearly see it ("10 mm socket", "needle-nose pliers", "AA batteries", "wood glue"), singular
  for one, plural for several of the same. One entry per kind, not per piece. Read labels and sizes printed on things.
- "unsure": things you can see but can't name confidently. "looks_like": a few words so the person can spot it in
  the photo (colour, shape, where in the picture). "guess": your best short name, or "" if you have none.
- Leave out the container itself, the furniture, and anything you'd be inventing. If the photo shows no stored
  things, answer {"items": [], "unsure": []}. At most 40 items and 10 unsure."""


def parse_seen(reply: Optional[str]) -> Optional[Dict[str, List[Any]]]:
    """Claude's answer -> {"items": [names], "unsure": [{"looks_like", "guess"}]}; None when it isn't one."""
    text = reply or ""
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    items: List[str] = []
    for raw in data.get("items") if isinstance(data.get("items"), list) else []:
        name = _name(str(raw)) if isinstance(raw, str) else ""
        if name and key(name) and key(name) not in [key(n) for n in items]:
            items.append(name)
    unsure = []
    for raw in data.get("unsure") if isinstance(data.get("unsure"), list) else []:
        if isinstance(raw, dict) and str(raw.get("looks_like") or "").strip():
            unsure.append({"looks_like": str(raw["looks_like"]).strip()[:120],
                           "guess": _name(str(raw.get("guess") or ""))})
    return {"items": items[:MAX_SEEN], "unsure": unsure[:15]}


class PhotoScans:
    """A photo being looked at, then waiting for the person's say-so. Kept in memory (and its photo on disk)
    until saved or a couple of hours old."""

    def __init__(self, things: Things, root: str, look: Callable[[str, str, bytes, str], Optional[str]],
                 why_not: Callable[[], str] = lambda: "", clock=time.time) -> None:
        self.things, self._look, self._why_not, self._clock = things, look, why_not, clock
        self.folder = os.path.join(os.path.abspath(root), PHOTO_DIR)
        self._lock = threading.Lock()
        self._scans: Dict[str, Dict[str, Any]] = {}

    def photo_path(self, name: str) -> Optional[str]:
        if not re.fullmatch(r"[a-f0-9]{16}\.jpg", str(name or "")):
            return None
        path = os.path.join(self.folder, name)
        return path if os.path.isfile(path) else None

    def _tidy(self) -> None:
        """Forget old scans, and delete photos no thing and no waiting scan points at."""
        now = self._clock()
        with self._lock:
            for sid in [s for s, scan in self._scans.items() if now - scan["at"] > SCAN_KEEP_SECONDS]:
                del self._scans[sid]
            wanted = {scan["photo"] for scan in self._scans.values()} | {t["photo"] for t in self.things.all()}
        try:
            for name in os.listdir(self.folder):
                if name not in wanted and now - os.path.getmtime(os.path.join(self.folder, name)) > 60:
                    os.remove(os.path.join(self.folder, name))
        except OSError:
            pass

    def start(self, picture: bytes, place: Any, by: str = "", wait: bool = False) -> Tuple[bool, str]:
        """Take the photo in and start looking -> (True, the scan's id) or (False, why not)."""
        from sentinel.engine.recipes import shrink_jpeg

        place = re.sub(r"\s+", " ", str(place or "")).strip(" .")[:PLACE_MAX]
        if not place:
            return False, "Say where this is first (like: top drawer of the red toolbox)."
        try:
            jpeg = shrink_jpeg(picture, PHOTO_MAX_PX) if picture and len(picture) <= PHOTO_MAX_BYTES else None
        except (ValueError, ImportError):
            jpeg = None
        if jpeg is None:
            return False, "That doesn't look like a photo."
        if not re.match(rf"^(?:{_WHERE})\b", place.lower()):
            place = "in " + place
        self._tidy()
        sid, photo = secrets.token_hex(8), secrets.token_hex(8) + ".jpg"
        os.makedirs(self.folder, mode=0o700, exist_ok=True)
        with open(os.path.join(self.folder, photo), "wb") as f:
            f.write(jpeg)
        scan = {"id": sid, "state": "looking", "place": place, "by": by[:40], "photo": photo, "at": self._clock(),
                "items": [], "unsure": [], "error": ""}
        with self._lock:
            self._scans[sid] = scan

        def work() -> None:
            reply = self._look(LOOK_SYSTEM, f"This is {place}. What is kept here?", jpeg, "things photo")
            seen = parse_seen(reply)
            with self._lock:
                if seen is None:
                    scan.update(state="failed", error=(self._why_not() if reply is None else "") or
                                "Claude's answer couldn't be read. Try the photo again.")
                else:
                    scan.update(state="ready", **seen)

        thread = threading.Thread(target=work, name="things-photo", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True, sid

    def view(self, sid: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            scan = self._scans.get(str(sid or ""))
            return None if scan is None else {k: scan[k] for k in ("id", "state", "place", "photo", "items", "unsure", "error")}

    def save(self, sid: Any, names: Any) -> Tuple[bool, str]:
        """Keep the names the person ticked or typed (theirs: not only the ones Claude offered), in that place."""
        with self._lock:
            scan = self._scans.get(str(sid or ""))
        if scan is None or scan["state"] != "ready":
            return False, "That photo has timed out. Send it again."
        kept: List[str] = []
        for raw in names if isinstance(names, list) else []:
            name = _name(str(raw)) if isinstance(raw, str) else ""
            if name and key(name) and key(name) not in [key(n) for n in kept] and len(kept) < MAX_SEEN:
                if self.things.put(name, scan["place"], by=scan["by"], photo=scan["photo"], another=True)[0]:
                    kept.append(name)
        with self._lock:
            self._scans.pop(scan["id"], None)
        self._tidy()
        if not kept:
            return True, "Nothing kept."
        return True, f"Kept {len(kept)} thing{'s' if len(kept) != 1 else ''} {your(scan['place'])}."

    def cancel(self, sid: Any) -> None:
        with self._lock:
            self._scans.pop(str(sid or ""), None)
        self._tidy()
