"""
Family locations ("Where is Abigail?") from the free OwnTracks phone app.

Each person who agrees installs OwnTracks and points it at Sentinel's /owntracks address
with their own username and password (the People page shows a QR code that sets it all
up). The phone posts its position now and then; Sentinel keeps only each person's latest
position and their arrivals/departures at named places (home, school, the salon), never
a trail of everywhere they've been.

A person's phone says where they are; their car is a separate thing. Family members swap
cars, so answers give both: "Abigail is at the salon. Abigail's Fusion is at home."
"""

import base64
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

DB_NAME = "location.db"

DEFAULT_RADIUS_M = 150
MIN_RADIUS_M, MAX_RADIUS_M = 50, 2000
# GPS jitters, so leaving needs a margin beyond the place's edge (scaled by how sure the phone
# is of its fix, within limits); fixes vaguer than these never move anyone in or out.
LEAVE_MARGIN_M = (100, 300)
ARRIVE_MAX_ACC_M = 250
LEAVE_MAX_ACC_M = 500
MOVING_KMH = 10  # OwnTracks reports speed in km/h; slower than this is walking around / GPS noise
FRESH_SECONDS = 3600  # older fixes get "last update ..." in answers
HISTORY_DAYS = 30
INVITE_DAYS = 7


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_word(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Compass direction from point 1 to point 2: 'north', 'southeast', ..."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(x, y)) + 360) % 360
    return ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")[int((deg + 22.5) // 45) % 8]


def parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """'41.11, -83.17' or a Google/Apple/OpenStreetMap link -> (lat, lon)."""
    t = urllib.parse.unquote(str(text or ""))
    patterns = (
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",  # Google place links (the pin, not the view)
        r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",  # Google map view
        r"[?&](?:q|ll|query|destination|center|daddr)=(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)",
        r"[?&]mlat=(-?\d+(?:\.\d+)?)&mlon=(-?\d+(?:\.\d+)?)",  # openstreetmap.org
        r"^\s*\(?(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\)?\s*$",  # plain numbers
    )
    for p in patterns:
        m = re.search(p, t)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
                return lat, lon
    return None


def _words_in(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![a-z']){re.escape(w)}(?![a-z])", text) for w in words if w)


@dataclass
class Person:
    id: int
    name: str  # "Abigail"
    aliases: List[str] = field(default_factory=list)  # "Abi", "Mom"
    username: str = ""  # what the phone app logs in with
    token: str = ""  # the phone app's password (a random device key, shown in the setup QR code)

    def words(self) -> List[str]:
        return [w for w in dict.fromkeys([self.name.lower()] + [a.lower() for a in self.aliases]) if w]


@dataclass
class Place:
    id: int
    name: str  # as said after "at": "the salon", "school", "Grandma's"; "Home" is special
    lat: float
    lon: float
    radius: float = DEFAULT_RADIUS_M
    alerts: bool = True  # Telegram message when someone arrives or leaves

    @property
    def is_home(self) -> bool:
        return self.name.strip().lower() == "home"

    def words(self) -> List[str]:
        n = self.name.strip().lower()
        out = [n, n[4:] if n.startswith("the ") else n]
        return [w for w in dict.fromkeys(out) if w]


@dataclass
class Change:
    kind: str  # "arrived", "left", "started" (first position from a phone)
    person: Person
    place: Optional[Place]
    ts: float


class LocationStore:
    """People, places, each person's latest position and their arrivals/departures (SQLite)."""

    def __init__(self, root: str) -> None:
        import os

        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._pruned = 0.0
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                         " aliases TEXT NOT NULL DEFAULT '', username TEXT NOT NULL UNIQUE, token TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS places (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                         " lat REAL NOT NULL, lon REAL NOT NULL, radius REAL NOT NULL, alerts INTEGER NOT NULL DEFAULT 1)")
            # One row per person: where their phone was last (place_since NULL = there when tracking began).
            conn.execute("CREATE TABLE IF NOT EXISTS fixes (person_id INTEGER PRIMARY KEY, ts REAL NOT NULL,"
                         " lat REAL NOT NULL, lon REAL NOT NULL, acc REAL, batt INTEGER, vel REAL,"
                         " place_id INTEGER, place_since REAL)")
            conn.execute("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL,"
                         " place_id INTEGER, place_name TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS visits_person_ts ON visits (person_id, ts)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
            if "invite" not in columns:  # setup link sent to the person's own phone (no Sentinel login needed)
                conn.execute("ALTER TABLE people ADD COLUMN invite TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE people ADD COLUMN invite_expires REAL NOT NULL DEFAULT 0")
        self.reassess()
        try:
            os.chmod(self.db_path, 0o600)  # holds the phones' passwords and the family's whereabouts
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ people
    @staticmethod
    def _person(r) -> Person:
        return Person(r["id"], r["name"], [a for a in r["aliases"].split(",") if a], r["username"], r["token"])

    def people(self) -> List[Person]:
        with self._connect() as conn:
            return [self._person(r) for r in conn.execute("SELECT * FROM people ORDER BY id")]

    def person(self, person_id: int) -> Optional[Person]:
        return next((p for p in self.people() if p.id == person_id), None)

    def _free_username(self, conn, name: str, person_id: Optional[int]) -> str:
        base = re.sub(r"[^a-z0-9]+", "", name.lower()) or "person"
        taken = {r[0] for r in conn.execute("SELECT username FROM people WHERE id != ?", (person_id or -1,))}
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}{n}", n + 1
        return candidate

    def save_person(self, name: str, aliases: Optional[List[str]] = None, person_id: Optional[int] = None) -> int:
        alias_text = ",".join(a.strip() for a in (aliases or []) if a.strip())
        with self._lock, self._connect() as conn:
            if person_id is None:
                cur = conn.execute("INSERT INTO people (name, aliases, username, token) VALUES (?, ?, ?, ?)",
                                   (name, alias_text, self._free_username(conn, name, None), secrets.token_urlsafe(18)))
                return int(cur.lastrowid)
            conn.execute("UPDATE people SET name = ?, aliases = ? WHERE id = ?", (name, alias_text, person_id))
            return person_id

    def new_token(self, person_id: int) -> None:
        """New phone password: the old setup (and any setup link) stops working (lost phone, shared by mistake)."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET token = ?, invite = '', invite_expires = 0 WHERE id = ?",
                         (secrets.token_urlsafe(18), person_id))

    def new_invite(self, person_id: int, now: Optional[float] = None) -> str:
        """A fresh setup link code for this person's phone, good for INVITE_DAYS."""
        now = time.time() if now is None else now
        code = secrets.token_urlsafe(16)
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET invite = ?, invite_expires = ? WHERE id = ?",
                         (code, now + INVITE_DAYS * 86400, person_id))
        return code

    def invite(self, person_id: int, now: Optional[float] = None) -> Optional[Tuple[str, float]]:
        """(code, expires) of this person's setup link while it still works."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute("SELECT invite, invite_expires FROM people WHERE id = ?", (person_id,)).fetchone()
        return (row[0], row[1]) if row and row[0] and row[1] > now else None

    def by_invite(self, code: str, now: Optional[float] = None) -> Optional[Person]:
        now = time.time() if now is None else now
        code = str(code or "")
        if len(code) < 16:
            return None
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM people WHERE invite != '' AND invite_expires > ?", (now,)).fetchall()
        found = None
        for r in rows:
            if secrets.compare_digest(r["invite"].encode(), code.encode()):
                found = self._person(r)
        return found

    def forget(self, person_id: int) -> None:
        """Drop where this person's phone is and has been (keeps the person and their login)."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM fixes WHERE person_id = ?", (person_id,))
            conn.execute("DELETE FROM visits WHERE person_id = ?", (person_id,))

    def delete_person(self, person_id: int) -> None:
        self.forget(person_id)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM people WHERE id = ?", (person_id,))

    def authenticate(self, username: str, password: str) -> Optional[Person]:
        """The person whose phone this is, or None. Compares every token so timing says nothing."""
        found = None
        for p in self.people():
            same_user = secrets.compare_digest(p.username.encode(), str(username or "").strip().lower().encode())
            same_token = secrets.compare_digest(p.token.encode(), str(password or "").encode())
            if same_user and same_token:
                found = p
        return found

    # ------------------------------------------------------------ places
    def places(self) -> List[Place]:
        with self._connect() as conn:
            return [Place(r["id"], r["name"], r["lat"], r["lon"], r["radius"], bool(r["alerts"]))
                    for r in conn.execute("SELECT * FROM places ORDER BY id")]

    def place(self, place_id: Optional[int]) -> Optional[Place]:
        return next((p for p in self.places() if p.id == place_id), None)

    def save_place(self, name: str, lat: float, lon: float, radius: float = DEFAULT_RADIUS_M, alerts: bool = True,
                   place_id: Optional[int] = None) -> int:
        radius = max(MIN_RADIUS_M, min(MAX_RADIUS_M, float(radius)))
        with self._lock, self._connect() as conn:
            if place_id is None:
                cur = conn.execute("INSERT INTO places (name, lat, lon, radius, alerts) VALUES (?, ?, ?, ?, ?)",
                                   (name, lat, lon, radius, int(alerts)))
                place_id = int(cur.lastrowid)
                conn.commit()
                self.reassess()
                return place_id
            conn.execute("UPDATE places SET name = ?, lat = ?, lon = ?, radius = ?, alerts = ? WHERE id = ?",
                         (name, lat, lon, radius, int(alerts), place_id))
        self.reassess()
        return place_id

    def reassess(self) -> None:
        """Places changed: check everyone's last position again now, not at the phone's next report
        (a phone sitting still at home may not report for hours). Quietly: no arrive/leave records,
        and a new place's start time is unknown."""
        with self._lock, self._connect() as conn:
            places = self.places()
            for row in conn.execute("SELECT * FROM fixes").fetchall():
                current = next((p for p in places if p.id == row["place_id"]), None)
                acc = 100.0 if row["acc"] is None else row["acc"]
                if current is not None and self._has_left(current, row["lat"], row["lon"], acc, None):
                    current = None
                if current is None:
                    current = self._place_at(places, row["lat"], row["lon"], acc, None)
                new_id = current.id if current else None
                if new_id != row["place_id"]:
                    conn.execute("UPDATE fixes SET place_id = ?, place_since = NULL WHERE person_id = ?",
                                 (new_id, row["person_id"]))

    def delete_place(self, place_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE fixes SET place_id = NULL, place_since = NULL WHERE place_id = ?", (place_id,))
            conn.execute("DELETE FROM places WHERE id = ?", (place_id,))

    # ------------------------------------------------------------ positions
    def fix(self, person_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fixes WHERE person_id = ?", (person_id,)).fetchone()
        return dict(row) if row else None

    def visits(self, person_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Newest first: {'kind': 'arrived'|'left', 'place_id', 'place_name', 'ts'}."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM visits WHERE person_id = ? ORDER BY ts DESC LIMIT ?",
                                (person_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def record(self, person: Person, payload: Dict[str, Any], now: Optional[float] = None) -> List[Change]:
        """Take one OwnTracks message; returns arrivals/departures it caused."""
        now = time.time() if now is None else now
        if not isinstance(payload, dict) or payload.get("_type") not in ("location", "transition"):
            return []
        try:
            lat, lon = float(payload["lat"]), float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return []
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return []

        def num(key):
            try:
                value = float(payload.get(key))
                return value if math.isfinite(value) else None
            except (TypeError, ValueError):
                return None

        ts = min(num("tst") or now, now)
        acc = num("acc")
        acc = 100.0 if acc is None else max(0.0, acc)
        batt, vel = num("batt"), num("vel")
        # A geofence event from the phone itself (it watches the places in its setup) is trusted
        # over our own sums, matched by name.
        hint = None
        if payload.get("_type") == "transition" and payload.get("event") in ("enter", "leave"):
            hint = (payload["event"], str(payload.get("desc") or "").strip().lower())

        changes: List[Change] = []
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM fixes WHERE person_id = ?", (person.id,)).fetchone()
            if row is not None and ts < row["ts"]:
                return []  # an old position the phone sent late; we already know a newer one
            places = self.places()
            current = next((p for p in places if row is not None and p.id == row["place_id"]), None)
            since = row["place_since"] if row is not None and current is not None else None

            if row is None:  # first position from this phone: just note where it is
                current = self._place_at(places, lat, lon, acc, hint)
                since = None
                changes.append(Change("started", person, current, ts))
            else:
                if current is not None and self._has_left(current, lat, lon, acc, hint):
                    changes.append(Change("left", person, current, ts))
                    current, since = None, None
                if current is None:
                    arrived = self._place_at(places, lat, lon, acc, hint)
                    if arrived is not None:
                        changes.append(Change("arrived", person, arrived, ts))
                        current, since = arrived, ts
            conn.execute("INSERT OR REPLACE INTO fixes (person_id, ts, lat, lon, acc, batt, vel, place_id, place_since)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (person.id, ts, lat, lon, acc, None if batt is None else int(batt), vel,
                          current.id if current else None, since))
            for c in changes:
                if c.kind != "started":
                    conn.execute("INSERT INTO visits (person_id, place_id, place_name, kind, ts) VALUES (?, ?, ?, ?, ?)",
                                 (person.id, c.place.id, c.place.name, c.kind, c.ts))
        if now - self._pruned > 86400:
            self.prune(now)
        return changes

    @staticmethod
    def _place_at(places: List[Place], lat: float, lon: float, acc: float, hint) -> Optional[Place]:
        if hint and hint[0] == "enter":
            named = next((p for p in places if p.name.strip().lower() == hint[1]), None)
            if named is not None:
                return named
        left = hint[1] if hint and hint[0] == "leave" else None  # the phone just said it left there
        inside = [(distance_m(lat, lon, p.lat, p.lon) / p.radius, p) for p in places
                  if p.name.strip().lower() != left
                  and acc <= max(ARRIVE_MAX_ACC_M, 2 * p.radius) and distance_m(lat, lon, p.lat, p.lon) <= p.radius]
        return min(inside, key=lambda x: x[0])[1] if inside else None

    @staticmethod
    def _has_left(place: Place, lat: float, lon: float, acc: float, hint) -> bool:
        if hint and hint[1] == place.name.strip().lower():
            return hint[0] == "leave"
        margin = max(LEAVE_MARGIN_M[0], min(acc, LEAVE_MARGIN_M[1]))
        return acc <= LEAVE_MAX_ACC_M and distance_m(lat, lon, place.lat, place.lon) > place.radius + margin

    def prune(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._pruned = now
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM visits WHERE ts < ?", (now - HISTORY_DAYS * 86400,))


# --------------------------------------------------------------- phone setup
def initials(name: str) -> str:
    """OwnTracks shows a two-letter tag on its map."""
    letters = [w[0] for w in re.findall(r"[A-Za-z0-9]+", name)]
    return ("".join(letters[:2]) if len(letters) > 1 else (name.strip()[:2] or "SH")).upper()


def owntracks_config(person: Person, endpoint: str, places: Iterable[Place], now: Optional[float] = None) -> Dict[str, Any]:
    """The settings the OwnTracks app imports: HTTP mode to Sentinel, this person's login, and
    the named places (so the phone itself reports arriving and leaving promptly)."""
    now = time.time() if now is None else now
    return {
        "_type": "configuration",
        "mode": 3,  # HTTP
        "url": endpoint,
        "auth": True,
        "username": person.username,
        "password": person.token,
        "deviceId": "phone",
        "tid": initials(person.name),
        "monitoring": 1,  # "significant changes": easy on the battery
        "waypoints": [{"_type": "waypoint", "desc": p.name, "lat": round(p.lat, 6), "lon": round(p.lon, 6),
                       "rad": int(p.radius), "tst": int(now) - i} for i, p in enumerate(places)],
    }


def owntracks_link(config: Dict[str, Any]) -> str:
    """owntracks:///config?inline=... — scanned or tapped on the phone, it sets the app up."""
    raw = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()
    return "owntracks:///config?inline=" + urllib.parse.quote(raw, safe="")


# --------------------------------------------------------------- questions
@dataclass
class LocationQuestion:
    kind: str  # "where", "at", "arrived", "left", "list"
    person_ids: List[int] = field(default_factory=list)
    place_id: Optional[int] = None  # the place asked about; None with at/arrived/left = home


_COMMANDS = r"^(videos?|recordings?|footage|status|help|clip|pause|resume|enable|disable)\b"
_CAR_WORDS = r"\b(cars?|trucks?|vehicles?|suvs?|jeeps?|vans?|sedans?|pickups?|minivans?)\b"
_LIST = (
    r"\bwho'?s\s+(home|here|out|away)\b", r"\bwho\s+is\s+(home|here|out|away)\b",
    r"\b(is\s+)?any(one|body)\s+home\b", r"\bwhere'?s?\s+(is\s+)?(every(one|body)|the family|the kids|all of us)\b",
    r"\bwhere\s+(is|are)\s+(every(one|body)|the family|the kids|we)\b",
)
_WHEN = r"\bwhen\b|\bwhat time\b|\bhow long ago\b|\bdid\b"
_LEFT = r"\b(leave|left|leaves|head(?:ed)? out|take off|took off|gone)\b"
_ARRIVED = (r"\b(get|got|gets|come|came|arrive|arrived|make it|made it)\b.*\b(home|back|here)\b|\barriv\w*\b"
            r"|\b(get|got|gets|make it|made it|go|went)\s+(to|at)\b")


def car_name_words(vehicles: Iterable) -> List[str]:
    """Words naming a family car itself ('fusion', 'edge'), so 'Is Abigail's Fusion home?' stays a car question."""
    out = []
    for v in vehicles:
        name = v.name.lower()
        if v.owner:
            name = re.sub(rf"\b{re.escape(v.owner.lower())}'?s?\b", " ", name)
        out += [w for w in re.findall(r"[a-z][a-z0-9-]+", name) if w not in ("the", "and", "s")]
    return list(dict.fromkeys(out))


def location_question(text: str, people: List[Person], places: List[Place], vehicles: Iterable = ()) -> Optional[LocationQuestion]:
    """A question about where family members are, or None (cars, visitors and the rest go elsewhere)."""
    t = text.strip().lower()
    if not people or not t or re.match(_COMMANDS, t):
        return None
    t = " " + re.sub(r"[^a-z0-9' ]+", " ", t) + " "
    if re.search(_CAR_WORDS, t) or _words_in(t, car_name_words(vehicles)):
        return None
    ids = [p.id for p in people if _words_in(t, p.words())]
    if not ids:
        return LocationQuestion("list") if any(re.search(p, t) for p in _LIST) else None
    place = next((p for p in sorted(places, key=lambda p: -len(p.name)) if not p.is_home and _words_in(t, p.words())), None)
    place_id = place.id if place else None
    if re.search(_WHEN, t):
        if re.search(_LEFT, t):
            return LocationQuestion("left", ids, place_id)
        if re.search(_ARRIVED, t):
            return LocationQuestion("arrived", ids, place_id)
    if re.search(r"\bwhere\b", t):
        return LocationQuestion("where", ids)
    if place is not None and re.search(r"\b(is|are|still|been)\b", t):
        return LocationQuestion("at", ids, place_id)
    if re.search(r"\b(is|are|still|been)\b", t) and re.search(r"\b(home|here|back|around|out|away|gone)\b", t):
        return LocationQuestion("at", ids)
    return None


# --------------------------------------------------------------- answers
@dataclass
class CarLine:
    name: str  # "Abigail's Fusion"
    here: bool
    last_arrival: Optional[float] = None
    last_departure: Optional[float] = None


@dataclass
class PersonView:
    person: Person
    fix: Optional[Dict[str, Any]]  # LocationStore.fix()
    place: Optional[Place]  # where the phone is now (None = not at a named place)
    visits: List[Dict[str, Any]] = field(default_factory=list)  # newest first
    cars: List[CarLine] = field(default_factory=list)


def _clock(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}{'am' if t.tm_hour < 12 else 'pm'}"


def _when(ts: float, now: float) -> str:
    """'at 2:15pm', 'yesterday at 9:05pm', 'on Mon 26 Oct at 8:00am'."""
    day = lambda x: time.localtime(x)[:3]  # noqa: E731
    if day(ts) == day(now):
        return f"at {_clock(ts)}"
    if day(ts) == day(now - 86400):
        return f"yesterday at {_clock(ts)}"
    return "on " + time.strftime("%a %d %b", time.localtime(ts)) + f" at {_clock(ts)}"


def _since(ts: float, now: float) -> str:
    return "since " + _when(ts, now)[3:] if _when(ts, now).startswith("at ") else "since " + _when(ts, now)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def at_phrase(place: Place) -> str:
    """'home' / 'at the salon'."""
    return "home" if place.is_home else f"at {place.name}"


def to_phrase(place: Place) -> str:
    return "home" if place.is_home else f"to {place.name}"


def place_phrase(place: Place) -> str:
    return "home" if place.is_home else place.name


def _miles(m: float) -> str:
    mi = m / 1609.344
    return f"{mi:.1f} miles" if mi < 10 else f"{round(mi)} miles"


def whereabouts(view: PersonView, places: List[Place], now: float) -> str:
    """'at the salon (since 2:15pm)' / 'about 3.2 miles northeast of home, driving (about 40 mph)'."""
    fix = view.fix or {}
    if view.place is not None:
        text = at_phrase(view.place)
        if fix.get("place_since"):
            text += f" ({_since(fix['place_since'], now)})"
        return text
    lat, lon = fix["lat"], fix["lon"]
    nearest = min(places, key=lambda p: distance_m(lat, lon, p.lat, p.lon)) if places else None
    if nearest is None:
        text = "somewhere I don't have a name for (no places are set up yet)"
    else:
        d = distance_m(lat, lon, nearest.lat, nearest.lon)
        if d < 500:
            text = f"near {place_phrase(nearest)}"
        else:
            text = f"about {_miles(d)} {bearing_word(nearest.lat, nearest.lon, lat, lon)} of {place_phrase(nearest)}"
    vel = fix.get("vel")
    if vel is not None and vel >= MOVING_KMH and now - fix["ts"] < 900:
        text += f", on the move (about {round(vel * 0.621371)} mph)"
    return text


def _freshness(view: PersonView, now: float) -> str:
    fix = view.fix or {}
    if fix and now - fix["ts"] > FRESH_SECONDS:
        return f" (last update from the phone {_when(fix['ts'], now)})"
    return ""


def _battery(view: PersonView, now: float) -> str:
    fix = view.fix or {}
    if fix.get("batt") is not None and fix["batt"] <= 15 and now - fix["ts"] < 2 * 3600:
        return f" {view.person.name}'s phone battery is at {fix['batt']}%."
    return ""


def car_sentence(car: CarLine, now: float) -> str:
    if car.here:
        return f"{_cap(car.name)} is at home."
    return f"{_cap(car.name)} isn't home" + (f" (it left {_when(car.last_departure, now)})." if car.last_departure else ".")


def _cars(view: PersonView, now: float) -> str:
    return "".join(" " + car_sentence(c, now) for c in view.cars)


def _no_phone(view: PersonView, now: float) -> str:
    text = f"{view.person.name}'s phone isn't sharing its location with Sentinel yet"
    if view.cars:
        return text + ", so I can only tell you about the car:" + _cars(view, now)
    return text + "."


def _last_visit(view: PersonView, kind: str, place_id: Optional[int]) -> Optional[Dict[str, Any]]:
    return next((v for v in view.visits if v["kind"] == kind and v["place_id"] == place_id), None)


def answer_location(q: LocationQuestion, views: Dict[int, PersonView], places: List[Place],
                    now: Optional[float] = None) -> Tuple[str, List[Tuple[str, float, float]]]:
    """Reply text, plus map pins (name, lat, lon) for people who aren't at a named place."""
    now = time.time() if now is None else now
    home = next((p for p in places if p.is_home), None)
    pins: List[Tuple[str, float, float]] = []

    if q.kind == "list":
        at_home, away, silent = [], [], []
        for v in views.values():
            if not v.fix:
                silent.append(v.person.name)
            elif v.place is not None and v.place.is_home:
                at_home.append(v.person.name)
            else:
                away.append(f"{v.person.name} ({whereabouts(v, places, now)})")
        lines = []
        if at_home:
            lines.append("Home (by phone): " + ", ".join(at_home) + ".")
        if away:
            lines.append("Out: " + "; ".join(away) + ".")
        if silent:
            lines.append("Not sharing location yet: " + ", ".join(silent) + ".")
        return "\n".join(lines), pins

    asked = next((p for p in places if p.id == q.place_id), None) if q.place_id is not None else home
    lines = []
    for pid in q.person_ids:
        v = views.get(pid)
        if v is None:
            continue
        name = v.person.name
        if not v.fix:
            lines.append(_no_phone(v, now))
            continue
        if q.kind == "at" and asked is None:
            lines.append(f"I don't have a place called home yet — add it on the People page. {name} is "
                         f"{whereabouts(v, places, now)}.")
            continue
        if q.kind == "where":
            text = f"{name} is {whereabouts(v, places, now)}.{_freshness(v, now)}"
        elif q.kind == "at":
            if v.place is not None and v.place.id == asked.id:
                text = f"Yes — {name} is {whereabouts(v, places, now)}.{_freshness(v, now)}"
            else:
                text = f"No — {name} is {whereabouts(v, places, now)}.{_freshness(v, now)}"
        else:
            if asked is None:
                lines.append("I don't have a place called home yet — add it on the People page.")
                continue
            arr = _last_visit(v, "arrived", asked.id)
            dep = _last_visit(v, "left", asked.id)
            if q.kind == "arrived":
                if arr:
                    text = f"{name} got {to_phrase(asked)} {_when(arr['ts'], now)}"
                    text += f", then left {_when(dep['ts'], now)}." if dep and dep["ts"] > arr["ts"] else (
                        " and is still there." if v.place is not None and v.place.id == asked.id else ".")
                elif v.place is not None and v.place.id == asked.id:
                    text = f"{name} is {at_phrase(asked)}, but was already there when the phone started sharing, so I don't know since when."
                else:
                    text = f"I haven't seen {name} get {to_phrase(asked)} in the last {HISTORY_DAYS} days."
                    text += f" {name} is {whereabouts(v, places, now)}."
            else:
                if dep:
                    text = f"{name} left {place_phrase(asked)} {_when(dep['ts'], now)}"
                    text += f" and got back {_when(arr['ts'], now)}." if arr and arr["ts"] > dep["ts"] else "."
                elif v.place is not None and v.place.id == asked.id:
                    text = f"{name} hasn't left — {name} is {whereabouts(v, places, now)}."
                else:
                    text = f"I haven't seen {name} leave {place_phrase(asked)} in the last {HISTORY_DAYS} days."
            if asked.is_home:
                for car in v.cars:
                    t = car.last_arrival if q.kind == "arrived" else car.last_departure
                    if t:
                        text += f" {_cap(car.name)} {'got home' if q.kind == 'arrived' else 'left'} {_when(t, now)}."
                lines.append(text + _battery(v, now))
                continue
        lines.append(text + _cars(v, now) + _battery(v, now))
        if v.place is None and q.kind in ("where", "at"):
            pins.append((name, v.fix["lat"], v.fix["lon"]))
    return "\n".join(lines) or "I don't know that person.", pins


def change_message(change: Change, cars: List[CarLine]) -> Optional[str]:
    """Telegram text for an arrival/departure ('📍 Abigail got to the salon.')."""
    name, place = change.person.name, change.place
    if change.kind == "started":
        where = f" ({at_phrase(place)})" if place else ""
        return f"📍 {name}'s phone is now sharing its location with Sentinel{where}."
    if place is None:
        return None
    if change.kind == "arrived":
        return f"🏠 {name} got home." if place.is_home else f"📍 {name} got {to_phrase(place)}."
    text = f"👋 {name} left {place_phrase(place)}."
    if place.is_home:
        parked = [c.name for c in cars if c.here]
        if parked:
            text += f" ({_cap(' and '.join(parked))} {'is' if len(parked) == 1 else 'are'} still here.)"
    return text
