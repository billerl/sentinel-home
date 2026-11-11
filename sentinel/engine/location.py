"""
Family locations ("Where is Elizabeth?") from the free OwnTracks phone app.

Each person who agrees installs OwnTracks and points it at Sentinel's /owntracks address
with their own username and password (the People page shows a QR code that sets it all
up). The phone posts its position now and then; Sentinel keeps each person's latest
position, their arrivals/departures at named places (home, school, the salon), and every
position for the family Timeline page (kept until the person deletes it; since 2026-10-31,
when the family asked for a Google-Timeline-style history).

A person's phone says where they are; their car is a separate thing. Family members swap
cars, so answers give both: "Elizabeth is at the salon. Elizabeth's Fusion is at home."

A family member can also be a pet with a GPS tracker on its collar (a Meshtastic tracker heard by a LoRa
base station, which posts its positions here like a phone: tools/meshtastic_bridge.py). Places are too loose
for a dog (leaving needs 100+ m), so trackers also get the yard: a shape drawn on the Family page, with
"Xena left the yard" / "Xena is back in the yard" within a few metres. Trackers don't count as someone home.
"""

import base64
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
LOGIN_LINK_DAYS = 7  # a personal "choose your Sentinel password" link
MIN_WEB_PASSWORD = 8
PERSON_ROLES = ("guest", "user", "admin")  # a person's own web login; the shared household login is the owner
# The yard (trackers only): outside means beyond the edge by more than the fix's own uncertainty (at least
# YARD_MARGIN_M: a collar GPS wanders a few metres); fixes vaguer than YARD_MAX_ACC_M never move the dog.
# In or out takes two readings in a row: a collar left indoors strays 10-25 m while claiming ~6 m, and single
# readings 8-26 m past the edge sent "Xena left the yard!" 3 times in a day (2026-11-07). A dog that really got
# out is reported one reading (~30 s) later.
YARD_MARGIN_M = 8
YARD_MAX_ACC_M = 50


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
    """'41.11, -88.17' or a Google/Apple/OpenStreetMap link -> (lat, lon)."""
    t = urllib.parse.unquote_plus(str(text or ""))
    n = r"(-?\d+(?:\.\d+)?)"
    patterns = (
        rf"!3d{n}!4d{n}",  # Google place links (the pin, not the view)
        rf"@{n},{n}",  # Google map view
        rf"/maps/(?:search|place|dir)/{n},\s*{n}",  # Google dropped-pin share links, once followed
        rf"[?&](?:q|ll|sll|query|destination|center|daddr|coordinate)={n},\s*{n}",
        rf"[?&]mlat={n}&mlon={n}",  # openstreetmap.org
        r"^\s*\(?(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\)?\s*$",  # plain numbers
    )
    for p in patterns:
        m = re.search(p, t)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
                return lat, lon
    return _parse_dms(t)


def _parse_dms(text: str) -> Optional[Tuple[float, float]]:
    """41°06'31.8"N 88°06'45.4"W (what Google Maps shows for a dropped pin)."""
    part = r"(\d{1,3})\s*°\s*(?:(\d{1,2}(?:\.\d+)?)\s*['′]\s*)?(?:(\d{1,2}(?:\.\d+)?)\s*[\"″]\s*)?([NSEW])"
    found = re.findall(part, text, re.IGNORECASE)
    if len(found) != 2:
        return None
    vals = {}
    for deg, mins, secs, hemi in found:
        v = float(deg) + float(mins or 0) / 60 + float(secs or 0) / 3600
        hemi = hemi.upper()
        vals["lat" if hemi in "NS" else "lon"] = -v if hemi in "SW" else v
    if set(vals) != {"lat", "lon"} or abs(vals["lat"]) > 90 or abs(vals["lon"]) > 180:
        return None
    return round(vals["lat"], 6), round(vals["lon"], 6)


# Share buttons give short links with no numbers in them; following Google's redirect reveals them.
_SHORT_LINK_HOSTS = ("maps.app.goo.gl", "goo.gl", "g.co", "maps.google.com", "maps.apple.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # we read each Location header ourselves
        return None


def resolve_map_link(text: str, timeout: float = 6.0) -> Optional[Tuple[float, float]]:
    """Follow a short map share link's redirects (headers only) until coordinates appear in the address.

    Never reads the page itself: Google centres an unknown place on the requester, i.e. on home."""
    m = re.search(r"https?://\S+", str(text or ""))
    if not m:
        return None
    url = m.group(0).rstrip(").,")
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(5):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in _SHORT_LINK_HOSTS):
            return None
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with opener.open(req, timeout=timeout):
                return None  # no redirect: nothing more to learn
        except urllib.error.HTTPError as e:
            nxt = e.headers.get("Location") if 300 <= e.code < 400 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not nxt:
            return None
        url = urllib.parse.urljoin(url, nxt)
        coords = parse_coordinates(url)
        if coords:
            return coords
    return None


def _words_in(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![a-z']){re.escape(w)}(?![a-z])", text) for w in words if w)


@dataclass
class Person:
    id: int
    name: str  # "Elizabeth"
    aliases: List[str] = field(default_factory=list)  # "Liz", "Mom"
    username: str = ""  # what the phone app logs in with
    token: str = ""  # the phone app's password (a random device key, shown in the setup QR code)
    tracker: str = ""  # a collar GPS tracker instead of a phone (its Meshtastic id, "!a1cacb2a"): a pet
    role: str = "user"  # what their own web login may do: "guest", "user" or "admin" (sentinel/web/roles.py)
    role_since: float = 0.0  # when that role was given (a guest sees recordings from that day on)
    pet: bool = False  # marked as a pet on the Family page (one without a collar tracker)

    @property
    def is_pet(self) -> bool:
        """A pet has no phone, login or role (a collar tracker makes it one too)."""
        return self.pet or bool(self.tracker)

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
    kind: str  # "arrived", "left", "started" (first position from a phone), "left_yard", "back_in_yard"
    person: Person
    place: Optional[Place]
    ts: float


PHOTO_DIR = "people_photos"  # next to the database: <id>.jpg
PHOTO_SIZE = 256  # pixels, square


class LocationStore:
    """People, places, each person's latest position and their arrivals/departures (SQLite)."""

    def __init__(self, root: str) -> None:
        import os

        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._pruned = 0.0
        self._yard_pending: Dict[int, bool] = {}  # person id -> in/out that one reading said; the next must agree
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
            # Every position a phone sent (the family's timeline), kept until the person deletes it.
            conn.execute("CREATE TABLE IF NOT EXISTS points (person_id INTEGER NOT NULL, ts REAL NOT NULL,"
                         " lat REAL NOT NULL, lon REAL NOT NULL, acc REAL, vel REAL, PRIMARY KEY (person_id, ts))")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
            if "invite" not in columns:  # setup link sent to the person's own phone (no Sentinel login needed)
                conn.execute("ALTER TABLE people ADD COLUMN invite TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE people ADD COLUMN invite_expires REAL NOT NULL DEFAULT 0")
            if "tracker" not in columns:
                conn.execute("ALTER TABLE people ADD COLUMN tracker TEXT NOT NULL DEFAULT ''")
            if "in_yard" not in {row[1] for row in conn.execute("PRAGMA table_info(fixes)")}:
                conn.execute("ALTER TABLE fixes ADD COLUMN in_yard INTEGER")
            # The yard's outline for pets' trackers: [[lat, lon], ...] (one row).
            conn.execute("CREATE TABLE IF NOT EXISTS yard (id INTEGER PRIMARY KEY CHECK (id = 1), points TEXT NOT NULL)")
            if "web_hash" not in columns:  # the person's own Sentinel web login (bcrypt) and its setup link
                conn.execute("ALTER TABLE people ADD COLUMN web_hash TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE people ADD COLUMN login_code TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE people ADD COLUMN login_expires REAL NOT NULL DEFAULT 0")
            if "role" not in columns:  # what the person's own web login may do (sentinel/web/roles.py)
                conn.execute("ALTER TABLE people ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
                conn.execute("ALTER TABLE people ADD COLUMN role_since REAL NOT NULL DEFAULT 0")
            if "pet" not in columns:  # a pet without a collar tracker (no phone, login or role)
                conn.execute("ALTER TABLE people ADD COLUMN pet INTEGER NOT NULL DEFAULT 0")
            # The places last sent to each phone (places_signature) and {place id: [name, tst]}, so a
            # renamed or deleted one can be replaced or removed. No row: never sent since setup.
            # Spots offered as new places on Telegram (frequent_spots); answer "", "save" or "no".
            conn.execute("CREATE TABLE IF NOT EXISTS spot_asks (id INTEGER PRIMARY KEY AUTOINCREMENT, lat REAL NOT NULL,"
                         " lon REAL NOT NULL, ts REAL NOT NULL, answer TEXT NOT NULL DEFAULT '')")
            conn.execute("CREATE TABLE IF NOT EXISTS phone_places (person_id INTEGER PRIMARY KEY,"
                         " signature TEXT NOT NULL, sent TEXT NOT NULL)")
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
        keys = r.keys()
        return Person(r["id"], r["name"], [a for a in r["aliases"].split(",") if a], r["username"], r["token"],
                      r["tracker"] if "tracker" in keys else "",
                      (r["role"] if r["role"] in PERSON_ROLES else "user") if "role" in keys else "user",
                      r["role_since"] if "role_since" in keys else 0.0,
                      bool(r["pet"]) if "pet" in keys else False)

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

    def set_pet(self, person_id: int, pet: bool) -> None:
        """Mark as a pet (or a person again); a pet's web login, if it somehow had one, goes."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET pet = ? WHERE id = ?", (int(bool(pet)), person_id))
            if pet:
                conn.execute("UPDATE people SET web_hash = '', login_code = '', login_expires = 0, role = 'user'"
                             " WHERE id = ?", (person_id,))

    def set_tracker(self, person_id: int, tracker: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET tracker = ? WHERE id = ?", (tracker, person_id))

    def yard(self) -> List[Tuple[float, float]]:
        with self._connect() as conn:
            row = conn.execute("SELECT points FROM yard WHERE id = 1").fetchone()
        try:
            return [(float(a), float(b)) for a, b in json.loads(row["points"])] if row else []
        except (ValueError, TypeError):
            return []

    def set_yard(self, points: List[Tuple[float, float]]) -> None:
        """A new outline (3+ corners), or [] to remove it. Pets' in/out state starts over."""
        with self._lock, self._connect() as conn:
            if points:
                conn.execute("INSERT OR REPLACE INTO yard (id, points) VALUES (1, ?)", (json.dumps(points),))
            else:
                conn.execute("DELETE FROM yard")
            conn.execute("UPDATE fixes SET in_yard = NULL")
            self._yard_pending.clear()

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
            conn.execute("DELETE FROM points WHERE person_id = ?", (person_id,))

    # ------------------------------------------------------------ timeline
    def points(self, person_id: int, start: float, end: float) -> List[Tuple[float, float, float, Optional[float], Optional[float]]]:
        """(ts, lat, lon, acc, vel) the phone sent between start and end, oldest first."""
        with self._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT ts, lat, lon, acc, vel FROM points WHERE person_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (person_id, start, end))]

    def point_days(self, person_id: int) -> List[str]:
        """Local dates ('2026-10-31') that have any positions."""
        with self._connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM points WHERE person_id = ? ORDER BY 1",
                (person_id,))]

    def delete_points(self, person_id: int, before: Optional[float] = None) -> int:
        """Delete this person's timeline (all of it, or what's older than ``before``)."""
        with self._lock, self._connect() as conn:
            if before is None:
                return conn.execute("DELETE FROM points WHERE person_id = ?", (person_id,)).rowcount
            return conn.execute("DELETE FROM points WHERE person_id = ? AND ts < ?", (person_id, before)).rowcount

    # ------------------------------------------------------------ personal web logins
    def has_web_login(self, person_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT web_hash FROM people WHERE id = ?", (person_id,)).fetchone()
        return bool(row and row[0])

    def new_login_code(self, person_id: int, now: Optional[float] = None) -> Tuple[str, float]:
        """A link code that lets this person choose their own Sentinel password (one use)."""
        now = time.time() if now is None else now
        code, expires = secrets.token_urlsafe(16), now + LOGIN_LINK_DAYS * 86400
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET login_code = ?, login_expires = ? WHERE id = ?", (code, expires, person_id))
        return code, expires

    def by_login_code(self, code: str, now: Optional[float] = None) -> Optional[Person]:
        now = time.time() if now is None else now
        code = str(code or "")
        if len(code) < 16:
            return None
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM people WHERE login_code != '' AND login_expires > ?", (now,)).fetchall()
        found = None
        for r in rows:
            if secrets.compare_digest(r["login_code"].encode(), code.encode()):
                found = self._person(r)
        return found

    def set_web_password(self, person_id: int, password: str) -> None:
        """Save the person's own Sentinel password (bcrypt); their setup link stops working."""
        import bcrypt

        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET web_hash = ?, login_code = '', login_expires = 0 WHERE id = ?",
                         (hashed, person_id))

    def remove_web_login(self, person_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET web_hash = '', login_code = '', login_expires = 0 WHERE id = ?",
                         (person_id,))

    def web_account(self, username: str) -> Optional[Person]:
        """The person whose own web login this username is (None once the login is removed or the person deleted)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM people WHERE username = ? AND web_hash != ''", (str(username or ""),)).fetchone()
        person = self._person(row) if row is not None else None
        return person if person is not None and not person.is_pet else None

    def set_role(self, person_id: int, role: str, now: Optional[float] = None) -> None:
        if role not in PERSON_ROLES:
            raise ValueError("unknown role")
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE people SET role = ?, role_since = ? WHERE id = ? AND role != ?",
                         (role, time.time() if now is None else now, person_id, role))

    def web_login(self, username: str, password: str) -> Optional[Person]:
        """The person with this username and Sentinel password, or None."""
        import bcrypt

        wanted = str(username or "").strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM people WHERE username = ? AND web_hash != ''", (wanted,)).fetchone()
        if row is None:
            return None
        try:
            return self._person(row) if bcrypt.checkpw(str(password or "").encode("utf-8"),
                                                       row["web_hash"].encode("ascii")) else None
        except (ValueError, TypeError):
            return None

    def delete_person(self, person_id: int) -> None:
        self.forget(person_id)
        self.set_photo(person_id, None)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM people WHERE id = ?", (person_id,))

    # ---- profile pictures (People page), shown as their pin on the TV's map
    def photo_path(self, person_id: int) -> str:
        import os

        return os.path.join(os.path.dirname(self.db_path), PHOTO_DIR, f"{int(person_id)}.jpg")

    def photo_version(self, person_id: int) -> Optional[int]:
        """When the picture was last changed (for the browser's cache), None without one."""
        import os

        try:
            return int(os.path.getmtime(self.photo_path(person_id)))
        except OSError:
            return None

    def set_photo(self, person_id: int, jpeg: Optional[bytes]) -> None:
        """Keeps (or, with None, removes) the person's picture: a small square JPEG already checked by the caller."""
        import os

        path = self.photo_path(person_id)
        if jpeg is None:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(jpeg)
        os.replace(path + ".tmp", path)

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

    def phone_places(self, person_id: int) -> Tuple[str, Dict[str, Any]]:
        """(signature, {place id: [name, tst]}) of the places last sent to this person's phone; ("", {}) if never."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM phone_places WHERE person_id = ?", (person_id,)).fetchone()
        return (row["signature"], json.loads(row["sent"])) if row else ("", {})

    def phone_places_sent(self, person_id: int, places: Iterable[Place], sent: Optional[Dict[str, Any]] = None) -> None:
        places = list(places)
        if sent is None:
            sent = {str(p.id): [p.name, WAYPOINT_TST_BASE + p.id] for p in places}
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO phone_places (person_id, signature, sent) VALUES (?, ?, ?)",
                         (person_id, places_signature(places), json.dumps(sent)))

    def spot_asks(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM spot_asks ORDER BY id")]

    def add_spot_ask(self, lat: float, lon: float, now: Optional[float] = None) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("INSERT INTO spot_asks (lat, lon, ts) VALUES (?, ?, ?)",
                                    (lat, lon, time.time() if now is None else now)).lastrowid)

    def answer_spot(self, ask_id: int, answer: str) -> Optional[Dict[str, Any]]:
        """Notes the answer; returns the ask, or None if it doesn't exist."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM spot_asks WHERE id = ?", (ask_id,)).fetchone()
            if row is not None:
                conn.execute("UPDATE spot_asks SET answer = ? WHERE id = ?", (answer, ask_id))
        return dict(row) if row is not None else None

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
            if payload.get("_type") == "location":  # the timeline keeps them all, even ones that arrive late
                conn.execute("INSERT OR IGNORE INTO points (person_id, ts, lat, lon, acc, vel) VALUES (?, ?, ?, ?, ?, ?)",
                             (person.id, ts, lat, lon, acc, vel))
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
            in_yard = row["in_yard"] if row is not None and "in_yard" in row.keys() else None
            yard = self.yard() if person.tracker else []
            if yard and acc <= YARD_MAX_ACC_M:
                inside, edge = yard_position(yard, lat, lon)
                said = True if inside else (False if edge > max(YARD_MARGIN_M, acc) else None)  # None: at the fence
                if in_yard is None:
                    in_yard = said
                elif said is not None and said == bool(in_yard):
                    self._yard_pending.pop(person.id, None)
                elif said is not None:
                    if self._yard_pending.pop(person.id, None) is said:
                        changes.append(Change("back_in_yard" if said else "left_yard", person, None, ts))
                        in_yard = said
                    else:
                        self._yard_pending[person.id] = said
            conn.execute("INSERT OR REPLACE INTO fixes (person_id, ts, lat, lon, acc, batt, vel, place_id, place_since,"
                         " in_yard) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (person.id, ts, lat, lon, acc, None if batt is None else int(batt), vel,
                          current.id if current else None, since, None if in_yard is None else int(bool(in_yard))))
            for c in changes:
                if c.kind in ("arrived", "left"):
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
def yard_position(outline: List[Tuple[float, float]], lat: float, lon: float) -> Tuple[bool, float]:
    """(inside the outline?, metres to its nearest edge), on a flat local map (a yard is small)."""
    lat0 = outline[0][0]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0

    def xy(a: float, b: float) -> Tuple[float, float]:
        return (b - outline[0][1]) * kx, (a - lat0) * ky

    x, y = xy(lat, lon)
    pts = [xy(a, b) for a, b in outline]
    inside, edge = False, float("inf")
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        edge = min(edge, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return inside, edge


OUTING_SECONDS = 3600  # "the dogs are going out": their trackers are asked for a position often, for up to an hour
OUTING_POLL_SECONDS = 30
_PETS = r"(?:the\s+)?(?:dogs?|pets?|pups?|puppies|doggies?)"


def outing_command(text: str, people: List[Person]) -> Optional[Tuple[str, List[Person]]]:
    """("out" | "in", the pets) for "the dogs are going out" / "let Xena out" / "Xena's back inside"; None otherwise.
    Questions ("is Xena outside?") are left to location_question."""
    low = re.sub(r"[.!]+$", "", text.strip().lower()).replace("’", "'")
    pets = [p for p in people if p.tracker]
    if not pets or low.endswith("?") or re.match(r"(is|are|where|did|has|have|when|was|were)\b", low):
        return None
    m = re.match(r"(?:please\s+)?(?:keep\s+(?:an\s+eye|watch|tabs)\s+on|watch|track|follow)\s+(?P<s>.+?)(?:\s+(?:now|please|for me))?$",
                 low)  # "keep an eye on Xena", "watch the dogs": the same as letting them out (2026-11-09)
    if m:
        named = _pets_named(m.group("s"), pets)
        if named:
            return "out", named
    for kind, word in (("out", r"out(?:side)?"), ("in", r"(?:back\s+)?in(?:side)?")):
        m = (re.match(rf"(?:i'?m\s+|we'?re\s+)?(?:let(?:ting)?|put(?:ting)?|tak(?:e|ing)|took|bring(?:ing)?|brought)"
                      rf"\s+(?P<s>.+?)\s+{word}$", low)
             or re.match(rf"(?P<s>.+?)\s*(?:\s(?:is|are)|'s|'re)?\s+(?:going\s+|gone\s+|head(?:ed|ing)\s+|back\s+)?{word}"
                         rf"(?:\s+now)?$", low))  # (also short: "dogs outside", "Xena going out", 2026-11-07)
        if not m:
            continue
        subject = m.group("s")
        if re.fullmatch(r"everyone|everybody", subject):
            return kind, pets
        named = _pets_named(subject, pets)
        if named:
            return kind, named
    return None


def _pets_named(subject: str, pets: List[Person]) -> List[Person]:
    subject = re.sub(r"'s\s+collars?$|\s+collars?$", "", subject.strip())
    if re.fullmatch(rf"(?:all\s+)?{_PETS}|both\s+(?:of\s+)?{_PETS}|both", subject):
        return pets
    named = [p for p in pets if any(re.search(rf"\b{re.escape(w)}\b", subject) for w in p.words())]
    return named if named and re.fullmatch(r"[\w' ,&]+", subject) else []


def beep_command(text: str, people: List[Person]) -> Optional[List[Person]]:
    """The pets whose collars should beep ("recall Xena", "beep the dogs", "beep Xena's collar"); None otherwise."""
    low = re.sub(r"[.!]+$", "", text.strip().lower()).replace("\u2019", "'")
    pets = [p for p in people if p.tracker]
    m = re.match(r"(?:please\s+)?(?:recall|beep|buzz)\s+(?P<s>.+?)(?:\s+(?:now|please))?$", low)
    return (_pets_named(m.group("s"), pets) or None) if pets and m else None


def lost_command(text: str, people: List[Person]) -> Optional[Tuple[str, List[Person]]]:
    """("lost" | "found", the pets) for "Xena is lost", "the dogs got out", "I can't find Xena", "we found Xena",
    "Xena is safe"; None otherwise. Questions are left alone."""
    low = re.sub(r"[.!]+$", "", text.strip().lower()).replace("\u2019", "'")
    pets = [p for p in people if p.tracker]
    if not pets or low.endswith("?") or re.match(r"(is|are|where|did|has|have|when|was|were)\b", low):
        return None
    lost =r"(?:lost|missing|loose|gone|run\s+off|ran\s+off|run\s+away|ran\s+away|got\s+out|got\s+loose|escaped)"
    for kind, pattern in (
            ("lost", rf"(?:oh\s+no,?\s+)?(?P<s>.+?)\s*(?:\s(?:is|are|has|have|got)|'s|'re)?\s+(?:been\s+|gone\s+)?{lost}"
                     rf"(?:\s+again)?$"),
            ("lost", r"(?:i|we)\s+can'?t\s+find\s+(?P<s>.+)$"),
            ("lost", rf"(?:{_PETS}\s+)?(?:(?P<s>.+?)\s+)?lost\s+mode(?:\s+on)?$|lost\s+(?P<s2>{_PETS})$"),
            ("found", r"(?:(?:i|we)\s+)?found\s+(?P<s>.+)$"),
            ("found", r"(?P<s>.+?)\s*(?:\s(?:is|are|was|were)|'s|'re)\s+(?:found|safe)(?:\s+now)?$")):
        m = re.match(pattern, low)
        if not m:
            continue
        subject = m.groupdict().get("s") or m.groupdict().get("s2") or "dogs"
        named = _pets_named(subject, pets)
        if named:
            return kind, named
    return None


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
        "cmd": True,  # lets Sentinel's reply ask for a fresh position when the one it has is old
        "waypoints": [_waypoint(p) for p in places],
    }


WAYPOINT_TST_BASE = 1_000_000_000  # a place's id on the phones: this + its id (Android keys places by "tst")


def _waypoint(place: Place, tst: Optional[int] = None) -> Dict[str, Any]:
    return {"_type": "waypoint", "desc": place.name, "lat": round(place.lat, 6), "lon": round(place.lon, 6),
            "rad": int(place.radius), "tst": WAYPOINT_TST_BASE + place.id if tst is None else tst}


def places_signature(places: Iterable[Place]) -> str:
    """Changes when a place is added, removed, renamed, moved or resized."""
    raw = json.dumps(sorted((p.id, p.name, round(p.lat, 6), round(p.lon, 6), int(p.radius)) for p in places))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def set_waypoints(on_phone: Any, places: Iterable[Place],
                  sent_before: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The command that brings a phone's places up to date, given the list the phone sent
    ({"_type": "waypoints"}) and what was sent to it before ({place id: [name, tst]}); returns it
    with the new "sent before". A place keeps the "tst" it has on the phone, by name or, renamed,
    by the one sent for it last time: Android keys places by "tst" (a new one would be a second
    copy) and iPhones by name. A name Sentinel sent before that is gone now goes with impossible
    coordinates, which removes it on an iPhone (Android ignores it; there the rename replaced it).
    Places the person added on the phone themselves are left alone."""
    have: Dict[str, Tuple[str, int]] = {}  # lower-case name -> (name as on the phone, tst)
    listed = on_phone.get("waypoints") if isinstance(on_phone, dict) else None
    for w in listed if isinstance(listed, list) else []:
        try:
            have[str(w["desc"]).strip().lower()] = (str(w["desc"]), int(w["tst"]))
        except (KeyError, TypeError, ValueError):
            continue
    places = list(places)
    taken = {tst for _name, tst in have.values()}
    out, sent = [], {}
    for p in places:
        key = p.name.strip().lower()
        before = sent_before.get(str(p.id))
        if key in have:
            tst = have[key][1]
        elif before and int(before[1]) in taken:
            tst = int(before[1])  # renamed: replaces the old one on Android
        else:
            tst = WAYPOINT_TST_BASE + p.id
            while tst in taken:
                tst += 1
            taken.add(tst)
        out.append(_waypoint(p, tst))
        sent[str(p.id)] = [p.name, tst]
    current = {p.name.strip().lower() for p in places}
    for name, _tst in sent_before.values():
        key = str(name).strip().lower()
        if key not in current and key in have:
            out.append({"_type": "waypoint", "desc": have[key][0], "lat": 360.0, "lon": 360.0, "rad": 0,
                        "tst": have[key][1]})
    return {"_type": "cmd", "action": "setWaypoints", "waypoints": {"_type": "waypoints", "waypoints": out}}, sent


def owntracks_link(config: Dict[str, Any]) -> str:
    """owntracks:///config?inline=... — scanned or tapped on the phone, it sets the app up."""
    raw = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()
    return "owntracks:///config?inline=" + urllib.parse.quote(raw, safe="")


# --------------------------------------------------------------- spots worth naming
# A place someone keeps going back to that isn't saved yet (a new job, a friend's house): after stops
# there on SPOT_MIN_DAYS different days in the last SPOT_DAYS, Sentinel offers to save it.
SPOT_DAYS = 21
SPOT_MIN_DAYS = 3
SPOT_MIN_STAY_S = 20 * 60
SPOT_STAY_M = 150  # positions this close to where a stop began are the same stop
SPOT_SAME_M = 200  # stops this close on different days are the same spot
SPOT_MAX_ACC_M = 100
SPOT_LONGEST_S = 10 * 3600  # a phone silent longer than this might be off, not still there


@dataclass
class Stop:
    person_id: int
    start: float
    end: float
    lat: float
    lon: float


@dataclass
class Spot:
    lat: float
    lon: float
    stops: List[Stop]

    def days(self) -> List[str]:
        return sorted({time.strftime("%Y-%m-%d", time.localtime(s.start)) for s in self.stops})


def stops_in(person_id: int, points: Iterable[Tuple[float, float, float, Optional[float], Optional[float]]]) -> List[Stop]:
    """Where a phone stayed put for SPOT_MIN_STAY_S or more. A phone that goes quiet counts as still
    there until its next report (an iPhone sends nothing while it sits still), up to SPOT_LONGEST_S."""
    pts = sorted(points)

    def moving(k: int) -> bool:
        return pts[k][4] is not None and pts[k][4] >= MOVING_KMH

    def vague(k: int) -> bool:
        return pts[k][3] is not None and pts[k][3] > SPOT_MAX_ACC_M

    out: List[Stop] = []
    i = 0
    while i < len(pts):
        if moving(i) or vague(i):
            i += 1
            continue
        ts0, lat0, lon0 = pts[i][:3]
        j, lats, lons = i + 1, [lat0], [lon0]
        while j < len(pts) and pts[j][0] - pts[j - 1][0] <= SPOT_LONGEST_S and not moving(j) and (
                vague(j) or distance_m(lat0, lon0, pts[j][1], pts[j][2]) <= SPOT_STAY_M):  # (vague: no news)
            if not vague(j):
                lats.append(pts[j][1])
                lons.append(pts[j][2])
            j += 1
        end = pts[j - 1][0]
        if j < len(pts) and pts[j][0] - end <= SPOT_LONGEST_S:
            end = pts[j][0]  # quiet until it moved on
        if end - ts0 >= SPOT_MIN_STAY_S:
            lats.sort()
            lons.sort()
            out.append(Stop(person_id, ts0, end, lats[len(lats) // 2], lons[len(lons) // 2]))
        i = j
    return out


def frequent_spots(stops: Iterable[Stop], places: Iterable[Place], skip: Iterable[Tuple[float, float]] = ()) -> List[Spot]:
    """Unsaved spots with stops on SPOT_MIN_DAYS or more different days, most visited first. Spots at
    or next to a saved place, or near one in skip (already asked about), are left out."""
    places, skip = list(places), list(skip)
    spots: List[Spot] = []
    for stop in sorted(stops, key=lambda s: s.start):
        if any(distance_m(stop.lat, stop.lon, p.lat, p.lon) <= p.radius + SPOT_STAY_M for p in places):
            continue
        spot = next((sp for sp in spots if distance_m(stop.lat, stop.lon, sp.lat, sp.lon) <= SPOT_SAME_M), None)
        if spot is None:
            spots.append(Spot(stop.lat, stop.lon, [stop]))
        else:
            spot.stops.append(stop)
            lats = sorted(s.lat for s in spot.stops)
            lons = sorted(s.lon for s in spot.stops)
            spot.lat, spot.lon = lats[len(lats) // 2], lons[len(lons) // 2]
    found = [sp for sp in spots if len(sp.days()) >= SPOT_MIN_DAYS
             and not any(distance_m(sp.lat, sp.lon, lat, lon) <= SPOT_SAME_M for lat, lon in skip)]
    return sorted(found, key=lambda sp: -len(sp.days()))


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
    """Words naming a family car itself ('fusion', 'edge'), so 'Is Elizabeth's Fusion home?' stays a car question."""
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
    name: str  # "Elizabeth's Fusion"
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
    yard: List[Tuple[float, float]] = field(default_factory=list)  # for trackers: the yard's outline, if drawn


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


def _feet(m: float) -> str:
    ft = m * 3.28084
    return f"{max(10, round(ft, -1)):.0f} ft" if ft < 1000 else _miles(m)


def whereabouts(view: PersonView, places: List[Place], now: float) -> str:
    """'at the salon (since 2:15pm)' / 'about 3.2 miles northeast of home, driving (about 40 mph)'."""
    fix = view.fix or {}
    if view.person.tracker and view.yard and fix.get("in_yard") is not None:
        if fix["in_yard"]:
            return "in the yard"
        edge = yard_position(view.yard, fix["lat"], fix["lon"])[1]
        if edge < 500:
            lat0 = sum(a for a, _ in view.yard) / len(view.yard)
            lon0 = sum(b for _, b in view.yard) / len(view.yard)
            return f"outside the yard, about {_feet(edge)} {bearing_word(lat0, lon0, fix['lat'], fix['lon'])} of it"
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
        return f" (last update from the {'tracker' if view.person.tracker else 'phone'} {_when(fix['ts'], now)})"
    return ""


def _battery(view: PersonView, now: float) -> str:
    fix = view.fix or {}
    if fix.get("batt") is not None and fix["batt"] <= 15 and now - fix["ts"] < 2 * 3600:
        return f" {view.person.name}'s {'tracker' if view.person.tracker else 'phone'} battery is at {fix['batt']}%."
    return ""


def car_sentence(car: CarLine, now: float) -> str:
    if car.here:
        return f"{_cap(car.name)} is at home."
    return f"{_cap(car.name)} isn't home" + (f" (it left {_when(car.last_departure, now)})." if car.last_departure else ".")


def _cars(view: PersonView, now: float) -> str:
    return "".join(" " + car_sentence(c, now) for c in view.cars)


def _no_phone(view: PersonView, now: float) -> str:
    text = (f"{view.person.name}'s tracker hasn't sent a position yet" if view.person.tracker
            else f"{view.person.name}'s phone isn't sharing its location with Sentinel yet")
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
            lines.append(f"I don't have a place called home yet — add it on the Family page. {name} is "
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
                lines.append("I don't have a place called home yet — add it on the Family page.")
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
    """Telegram text for an arrival/departure ('📍 Elizabeth got to the salon.')."""
    name, place = change.person.name, change.place
    if change.kind == "left_yard":
        return f"🐕 {name} left the yard!"
    if change.kind == "back_in_yard":
        return f"🐕 {name} is back in the yard."
    if change.kind == "started":
        where = f" ({at_phrase(place)})" if place else ""
        device = "tracker is now sending" if change.person.tracker else "phone is now sharing"
        return f"📍 {name}'s {device} its location with Sentinel{where}."
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
