"""
Routines: "when <something happens>, only <if ...>, then <do these things>", e.g. when a person is seen
on the Back Yard camera after dark, turn on the patio lights until there's been no motion there for
2 minutes.

Made on the Routines page (drop-downs), or in plain words to Jarvis / Telegram: Claude turns the words
into ``Routine {json}`` and Sentinel reads it back in its own words (describe()); it's saved only after
a spoken yes or a ✅ tap. Stored in SQLite next to the other stores.

A routine refers to cameras, switches, people and cars by id, so renaming one doesn't break it;
names are accepted on the way in (normalize()) because that's what Claude and people use.

Lights a routine turned on are turned off again when its timer runs out, but only if the routine
was what turned them on: a light someone switched on by hand is left alone. If Sentinel stops while
holding a light on, it's turned off at the next start (HOLDS_FILE).
"""

import bisect
import itertools
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

DB_NAME = "routines.db"
HOLDS_FILE = "routine_holds.json"
SEEN = ("person", "vehicle", "animal", "delivery", "package")  # package: left at a door (packages.py)
TRIGGERS = ("seen", "motion", "sensor", "time", "temp", "arrive", "leave", "car_arrive", "car_leave")
WATCHERS = ("seen", "motion", "sensor")  # triggers that can hold a light on "until quiet"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_MINUTES = 12 * 60
MAX_TEXT = 200
MAX_ACTIONS = 5
MESSAGE_COOLDOWN = 5 * 60  # a routine sends Telegram messages at most this often...
SAY_COOLDOWN = 2 * 60  # ...and speaks at most this often (each routine on its own: one per room says its room in turn)
TV_CAMERA_MAX_MINUTES = 30  # "show the front door on the TV": it goes back to the show after this at most
MOTION_REPEAT = 30.0  # a motion routine fires at most this often per camera
HOUSE_WIDE_WAIT = 2.5  # a Wi-Fi sensor routine waits this long to see whether the whole house moved at once
SENSOR_REARM = 30.0  # a Wi-Fi sensor routine fires when movement starts after this long quiet, not all through it
# Wi-Fi sensor rules ("this one and that one, but not that one"): how far apart the sensors' movement may be.
# They never react at the same instant: walking through, the next room's line fires a few seconds later.
WITHIN_DEFAULT, WITHIN_MIN, WITHIN_MAX = 5, 2, 60
REPLAY_HOURS = 24  # the editor shows when a sensor rule would have gone off over this long
TEMP_CHECK_SECONDS = 60.0  # the outdoor temperature (the weather card's) is looked at this often
TEMP_REARM = 2.0  # "when it drops below 32": goes again only after it's been back above 34 (no flapping at the line)
TEMP_RANGE = (-60.0, 140.0)
HOLD_GIVE_UP = 6 * 3600.0  # a timed light whose plug never answers again is let go this long after its end
SETTLE_SECONDS = 20.0  # after a routine switches a light, the picture changes: that isn't motion
COMMAND = re.compile(r"^routine\s+(\{.*\})\s*$", re.IGNORECASE | re.DOTALL)
LIST = re.compile(r"^(?:what are |list |show )?(?:my |the )?routines\??$", re.IGNORECASE)
TOGGLE = re.compile(r"^(?:(delete|remove)\s+routine\s+#?(\d{1,4})|(?:turn\s+(on|off)\s+)?routine\s+#?(\d{1,4})(?:\s+(on|off))?)$",
                    re.IGNORECASE)
# Plain-words requests for a new routine: these go straight to Claude (never mistaken for "did a car pull in?").
REQUEST = re.compile(r"\b(?:new|create|make|add|set ?up)\b.{0,20}\broutines?\b|\broutine\b.*\b(?:when|whenever|every time)\b"
                     r"|^(?:when|whenever|every ?time|each time)\b.*\b(?:turn|switch)\b.*\b(?:on|off)\b", re.IGNORECASE)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def is_routine_command(text: str) -> bool:
    t = (text or "").strip()
    if COMMAND.match(t) or LIST.match(t):
        return True
    m = TOGGLE.match(t)
    return bool(m and (m.group(1) or m.group(3) or m.group(5)))  # "routine 3" alone isn't a command


# ------------------------------------------------------------------ sunrise / sunset
def sun_times(lat: float, lon: float, ts: float) -> Optional[Tuple[float, float]]:
    """(sunrise, sunset) as Unix times for the local day containing ts; None in polar day/night.
    The standard "sunrise equation" (good to a minute or two, plenty for "after dark")."""
    t = time.localtime(ts)
    noon = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
    jdate = noon / 86400.0 + 2440587.5
    n = round(jdate - 2451545.0 + lon / 360.0)  # this local day's number (local noon is ~lon/360 off 12:00 UTC)
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if not -1 <= cos_w <= 1:
        return None
    w = math.degrees(math.acos(cos_w)) / 360.0
    return (transit - w - 2440587.5) * 86400, (transit + w - 2440587.5) * 86400


def _clock_words(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}{':%02d' % m if m else ''} {'am' if h < 12 else 'pm'}"


def _minutes_words(n: int) -> str:
    if n % 60 == 0 and n >= 60:
        return f"{n // 60} hour" + ("s" if n > 60 else "")
    return f"{n} minute" + ("s" if n != 1 else "")


def _join(items: List[str], word: str = "or") -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


# ------------------------------------------------------------------ checking and describing a routine
@dataclass
class Catalog:
    """What a routine can refer to, id -> name (from the engine)."""
    cameras: Dict[str, str] = field(default_factory=dict)
    switches: Dict[str, str] = field(default_factory=dict)
    people: Dict[int, str] = field(default_factory=dict)
    cars: Dict[int, str] = field(default_factory=dict)
    sensors: Dict[str, str] = field(default_factory=dict)  # Wi-Fi motion lines (path id -> name)
    tvs: Dict[str, str] = field(default_factory=dict)  # TVs that can show a camera (switch id -> name)


def _pick(values: Any, table: Dict[Any, str], what: str) -> List[Any]:
    """Ids from ids or names ("Back Yard", "back yard camera"); raises ValueError naming what's unknown."""
    if values in (None, "", "any", "anyone", "anything"):
        return []
    if not isinstance(values, list):
        values = [values]
    out = []
    for v in values[:10]:
        if v in table:
            out.append(v)
            continue
        if isinstance(v, str) and v.isdigit() and int(v) in table:
            out.append(int(v))
            continue
        name = re.sub(r"\s+", " ", str(v)).strip().lower()
        name = re.sub(rf"\s+{what}$", "", name)
        hit = [k for k, n in table.items() if n.lower() == name] or \
              [k for k, n in table.items() if name and (name in n.lower() or n.lower() in name)]
        if len(hit) != 1:
            raise ValueError(f"I don't know a {what} called “{v}”" if not hit else f"“{v}” matches more than one {what}")
        out.append(hit[0])
    return list(dict.fromkeys(out))


def _minutes(value: Any, what: str) -> Optional[int]:
    if value in (None, "", 0):
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{what} should be a number of minutes")
    if not 1 <= n <= MAX_MINUTES:
        raise ValueError(f"{what} should be between 1 minute and 12 hours")
    return n


def _seconds(value: Any) -> int:
    if value in (None, ""):
        return WITHIN_DEFAULT
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError("“within” should be a number of seconds")
    if not WITHIN_MIN <= n <= WITHIN_MAX:
        raise ValueError(f"“within” should be between {WITHIN_MIN} and {WITHIN_MAX} seconds")
    return n


def _degrees(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("the temperature should be a number of degrees")
    if isinstance(value, bool) or not TEMP_RANGE[0] <= n <= TEMP_RANGE[1]:
        raise ValueError("the temperature should be a number of degrees")
    return int(n) if n == int(n) else round(n, 1)


def starts_it(w: Dict[str, Any], path: str) -> bool:
    """Does movement on this Wi-Fi sensor start the rule? The first group ("any of these"), else the
    "all of these" group, else any sensor but the "but not" ones."""
    first = w.get("sensors") or w.get("all") or []
    return path in first if first else path not in w.get("none", [])


def is_rule(w: Dict[str, Any]) -> bool:
    """A sensor trigger with "and" / "but not" groups: decided a few seconds after the first movement."""
    return w.get("type") == "sensor" and bool(w.get("all") or w.get("none"))


def rule_met(w: Dict[str, Any], first: float, last_moved: Callable[[str], float]) -> bool:
    """Decided ``within`` seconds after ``first`` (the movement that started it): every "and" sensor has
    moved since ``within`` before it, and no "but not" sensor has."""
    since = first - w.get("within", WITHIN_DEFAULT)
    return all(last_moved(p) >= since for p in w.get("all", [])) and \
        not any(last_moved(p) >= since for p in w.get("none", []))


def _text(value: Any, what: str) -> str:
    t = re.sub(r"\s+", " ", str(value or "")).strip()
    if not t:
        raise ValueError(f"say what the {what} should be")
    return t[:MAX_TEXT]


def normalize(spec: Any, cat: Catalog) -> Dict[str, Any]:
    """A checked routine with ids, from Claude's / the page's version; ValueError says what's wrong."""
    if not isinstance(spec, dict):
        raise ValueError("that isn't a routine")
    name = re.sub(r"\s+", " ", str(spec.get("name") or "")).strip()[:60]
    when = spec.get("when") if isinstance(spec.get("when"), dict) else {}
    kind = str(when.get("type") or "")
    if kind not in TRIGGERS:
        raise ValueError("say what starts it (a camera sees something, motion, a Wi-Fi sensor, a time, "
                         "the outdoor temperature, someone arriving or leaving)")
    w: Dict[str, Any] = {"type": kind}
    if kind == "seen":
        what = when.get("what") or list(SEEN)
        what = [what] if isinstance(what, str) else what
        what = [x.lower().rstrip("s") if isinstance(x, str) else x for x in what]
        what = list(SEEN) if any(x in ("anything", "something", "object") for x in what) else what
        if not what or any(x not in SEEN for x in what):
            raise ValueError("a camera can see: a person, a vehicle, an animal, a delivery van or a package left at the door")
        w["what"] = [x for x in SEEN if x in what]
    if kind == "seen" and when.get("repeat_hours"):
        if "package" not in w["what"]:
            raise ValueError("only a package left at the door can repeat (until it's taken in)")
        try:
            hours = float(when["repeat_hours"])
        except (TypeError, ValueError):
            raise ValueError("repeat every how many hours? (1 to 24)")
        if not 0.5 <= hours <= 24:
            raise ValueError("it can repeat every 1 to 24 hours")
        w["repeat_hours"] = int(hours) if hours == int(hours) else round(hours, 1)
    if kind in ("seen", "motion"):
        w["cameras"] = _pick(when.get("cameras"), cat.cameras, "camera")
    elif kind == "sensor":
        w["sensors"] = _pick(when.get("sensors"), cat.sensors, "sensor")
        also = _pick(when.get("all"), cat.sensors, "sensor")
        unless = _pick(when.get("none"), cat.sensors, "sensor")
        if len(set(w["sensors"]) | set(also) | set(unless)) < len(w["sensors"]) + len(also) + len(unless):
            raise ValueError("a sensor can only be in one group (any of these / all of these / but not these)")
        if unless and not w["sensors"] and not also and cat.sensors and set(unless) >= set(cat.sensors):
            raise ValueError("every sensor is under “but not”, so nothing could start it")
        if also:
            w["all"] = also
        if unless:
            w["none"] = unless
        if also or unless:
            w["within"] = _seconds(when.get("within"))
    elif kind == "time":
        at = str(when.get("at") or "").strip().lower()
        if at not in ("sunset", "sunrise") and not _CLOCK.match(at):
            raise ValueError("the time should be sunset, sunrise or a time like 21:30")
        w["at"] = at if at in ("sunset", "sunrise") else "%02d:%s" % (int(at.split(":")[0]), at.split(":")[1])
        try:
            offset = int(round(float(when.get("offset") or 0)))
        except (TypeError, ValueError):
            raise ValueError("the offset should be a number of minutes")
        if abs(offset) > 180:
            raise ValueError("the offset should be within 3 hours")
        if offset and at in ("sunset", "sunrise"):
            w["offset"] = offset
    elif kind == "temp":
        below, above = when.get("below"), when.get("above")
        if (below in (None, "")) == (above in (None, "")):
            raise ValueError("say the temperature: when it drops below, or rises above, how many degrees")
        if below not in (None, ""):
            w["below"] = _degrees(below)
        else:
            w["above"] = _degrees(above)
    elif kind in ("arrive", "leave"):
        w["people"] = _pick(when.get("people"), cat.people, "person")
    else:
        w["cars"] = _pick(when.get("cars"), cat.cars, "car")

    only = spec.get("only") if isinstance(spec.get("only"), dict) else {}
    o: Dict[str, Any] = {}
    if only.get("dark") and only.get("light"):
        raise ValueError("it can't be only after dark and only in daylight")
    if only.get("dark"):
        o["dark"] = True
    elif only.get("light"):
        o["light"] = True
    if only.get("from") or only.get("to"):
        start, end = str(only.get("from") or "").strip(), str(only.get("to") or "").strip()
        if not (_CLOCK.match(start) and _CLOCK.match(end)) or start == end:
            raise ValueError("the hours should be like 22:00 to 06:00")
        o["from"], o["to"] = ("%02d:%s" % (int(x.split(":")[0]), x.split(":")[1]) for x in (start, end))
    days = only.get("days")
    if days:
        days = [str(d).lower()[:3] for d in (days if isinstance(days, list) else [days])]
        if any(d not in DAYS for d in days):
            raise ValueError("days should be mon, tue, wed, thu, fri, sat, sun")
        if len(set(days)) < 7:
            o["days"] = [d for d in DAYS if d in days]
    if only.get("nobody_home"):
        o["nobody_home"] = True
    for key in ("temp_below", "temp_above"):
        if only.get(key) not in (None, ""):
            o[key] = _degrees(only[key])
    if "temp_below" in o and "temp_above" in o and o["temp_above"] >= o["temp_below"]:
        raise ValueError("it can't be both above and below those temperatures")

    actions = spec.get("then")
    actions = [actions] if isinstance(actions, dict) else actions
    if not isinstance(actions, list) or not actions:
        raise ValueError("say what it should do")
    then = []
    for a in actions[:MAX_ACTIONS]:
        if not isinstance(a, dict):
            raise ValueError("that isn't something it can do")
        do = str(a.get("do") or "")
        if do == "switch":
            sw = _pick(a.get("switch"), cat.switches, "switch")
            if len(sw) != 1:
                raise ValueError("say which switch to turn on or off")
            on = a.get("on", True)
            if not isinstance(on, bool):
                on = str(on).lower() in ("on", "true", "1")
            step: Dict[str, Any] = {"do": "switch", "switch": sw[0], "on": on}
            if on:
                minutes = _minutes(a.get("for"), "how long it stays on")
                quiet = _minutes(a.get("until_quiet"), "the quiet time")
                if minutes and quiet:
                    raise ValueError("pick one: on for a set time, or until things are quiet")
                if quiet and kind not in WATCHERS:
                    raise ValueError("“until there's no motion” needs a routine started by a camera or a Wi-Fi sensor")
                if minutes:
                    step["for"] = minutes
                if quiet:
                    step["until_quiet"] = quiet
            then.append(step)
        elif do == "telegram":
            then.append({"do": "telegram", "text": _text(a.get("text"), "message"),
                         **({"photo": True} if a.get("photo") and kind == "seen" else {})})
        elif do == "say":
            then.append({"do": "say", "text": _text(a.get("text"), "announcement")})
        elif do == "tv_camera":
            tv = _pick(a.get("tv"), cat.tvs, "TV") or (list(cat.tvs)[:1] if len(cat.tvs) == 1 else [])
            if len(tv) != 1:
                raise ValueError("say which TV shows the camera" if cat.tvs else "no TV here can show a camera yet")
            cam = _pick(a.get("camera"), cat.cameras, "camera")
            if len(cam) > 1:
                raise ValueError("pick one camera to show")
            if not cam and kind not in ("seen", "motion"):
                raise ValueError("say which camera to show")
            minutes = _minutes(a.get("for"), "how long it shows the camera") or 1
            if minutes > TV_CAMERA_MAX_MINUTES:
                raise ValueError(f"a camera can stay on the TV for up to {TV_CAMERA_MAX_MINUTES} minutes")
            then.append({"do": "tv_camera", "tv": tv[0], "camera": cam[0] if cam else "", "for": minutes})
        else:
            raise ValueError("it can turn a switch on or off, send a Telegram message, say something out loud, "
                             "or show a camera on a TV")
    out = {"when": w, "only": o, "then": then}
    out["name"] = name or default_name(out, cat)
    return out


def default_name(spec: Dict[str, Any], cat: Catalog) -> str:
    sw = next((a for a in spec["then"] if a["do"] == "switch"), None)
    w = spec["when"]
    first = spec["then"][0]["do"]
    head = (cat.switches.get(sw["switch"], "Switch") if sw else
            {"telegram": "Message", "tv_camera": "Camera on TV"}.get(first, "Announcement"))
    where = {"seen": lambda: ", ".join(cat.cameras.get(c, "?") for c in w["cameras"]) or "any camera",
             "motion": lambda: ", ".join(cat.cameras.get(c, "?") for c in w["cameras"]) or "any camera",
             "sensor": lambda: ", ".join(cat.sensors.get(c, "?") for c in w["sensors"] or w.get("all", []))
             or "any Wi-Fi sensor",
             "time": lambda: w["at"],
             "temp": lambda: f"below {w['below']}°" if "below" in w else f"above {w['above']}°", "arrive": lambda: "arrivals", "leave": lambda: "departures",
             "car_arrive": lambda: "car home", "car_leave": lambda: "car leaves"}[w["type"]]()
    return f"{head[:1].upper()}{head[1:]} — {where}"[:60]


def describe(spec: Dict[str, Any], cat: Catalog) -> str:
    """The routine in plain words: read back before it's saved, and shown on the page."""
    w, o = spec["when"], spec.get("only", {})
    cams = _join([cat.cameras.get(c, "a removed camera") for c in w.get("cameras", [])]) if w.get("cameras") else ""
    if w["type"] == "seen":
        what = [{"person": "a person", "vehicle": "a vehicle", "animal": "an animal", "delivery": "a delivery",
                 "package": "a package left at the door"}[x]
                for x in w["what"]]
        what_words = "anything" if len(what) == len(SEEN) else _join(what)
        when = f"When {'any camera' if not cams else 'the ' + cams + ' camera'} sees {what_words}"
        if w.get("repeat_hours"):
            n = w["repeat_hours"]
            when += f" (and again every {n} {'hour' if n == 1 else 'hours'} while the package is still there)"
    elif w["type"] == "motion":
        when = f"When there's motion on {'any camera' if not cams else 'the ' + cams + ' camera'}"
    elif w["type"] == "sensor":
        def names(ids: List[str], word: str = "or") -> str:
            return _join([cat.sensors.get(c, "a removed sensor") for c in ids], word)
        also, unless = w.get("all", []), w.get("none", [])
        if w["sensors"]:
            when = f"When the {names(w['sensors'])} Wi-Fi sensor notices movement"
            if also:
                when += f", and so {'does' if len(also) == 1 else 'do'} the {names(also, 'and')}"
        elif also:
            when = (f"When the {names(also)} Wi-Fi sensor notices movement" if len(also) == 1
                    else f"When the {names(also, 'and')} Wi-Fi sensors all notice movement")
        else:
            when = "When any Wi-Fi sensor notices movement"
        if unless:
            when += f", but not if the {names(unless)} does too"
        if also or unless:
            when += f" (within {w['within']} seconds)"
    elif w["type"] == "time":
        if w["at"] in ("sunset", "sunrise"):
            off = w.get("offset", 0)
            when = (f"Every day {_minutes_words(abs(off))} {'before' if off < 0 else 'after'} {w['at']}" if off
                    else f"Every day at {w['at']}")
        else:
            when = f"Every day at {_clock_words(w['at'])}"
    elif w["type"] == "temp":
        when = (f"When it drops below {w['below']}° outside" if "below" in w
                else f"When it rises above {w['above']}° outside")
    elif w["type"] in ("arrive", "leave"):
        who = _join([cat.people.get(p, "someone removed") for p in w["people"]]) if w["people"] else "anyone"
        when = f"When {who} {'gets home' if w['type'] == 'arrive' else 'leaves home'}"
    else:
        cars = _join([cat.cars.get(c, "a removed car") for c in w["cars"]]) if w["cars"] else "a family car"
        when = f"When {cars} {'pulls in' if w['type'] == 'car_arrive' else 'drives off'}"
    limits = []
    if o.get("dark"):
        limits.append("after dark")
    if o.get("light"):
        limits.append("in daylight")
    if o.get("from"):
        limits.append(f"between {_clock_words(o['from'])} and {_clock_words(o['to'])}")
    if o.get("days"):
        names = {"mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays", "fri": "Fridays",
                 "sat": "Saturdays", "sun": "Sundays"}
        days = o["days"]
        limits.append("on weekdays" if days == list(DAYS[:5]) else "on weekends" if days == ["sat", "sun"]
                      else "on " + _join([names[d] for d in days], "and"))
    if o.get("nobody_home"):
        limits.append("when nobody's home")
    if "temp_below" in o and "temp_above" in o:
        limits.append(f"when it's between {o['temp_above']}° and {o['temp_below']}° outside")
    elif "temp_below" in o:
        limits.append(f"when it's below {o['temp_below']}° outside")
    elif "temp_above" in o:
        limits.append(f"when it's above {o['temp_above']}° outside")
    if limits:
        when += ", only " + _join(limits, "and")
    steps = []
    for a in spec["then"]:
        if a["do"] == "switch":
            name = cat.switches.get(a["switch"], "a removed switch")
            text = f"turn {'on' if a['on'] else 'off'} the {name}"
            if a.get("for"):
                text += f" for {_minutes_words(a['for'])}"
            if a.get("until_quiet"):
                text += f" until there's been no motion for {_minutes_words(a['until_quiet'])}"
            steps.append(text)
        elif a["do"] == "telegram":
            steps.append(f"send “{a['text']}” on Telegram" + (" with a photo" if a.get("photo") else ""))
        elif a["do"] == "tv_camera":
            cam = f"the {cat.cameras.get(a['camera'], 'removed')} camera" if a["camera"] else "that camera"
            steps.append(f"show {cam} on the {cat.tvs.get(a['tv'], 'removed TV')} (if it's on) for "
                         f"{_minutes_words(a['for'])}, then go back to what was on")
        else:
            steps.append(f"say “{a['text']}” out loud")
    return f"{when}: {_join(steps, 'and')}."


# ------------------------------------------------------------------ storage
@dataclass
class Routine:
    id: int
    name: str
    enabled: bool
    spec: Dict[str, Any]
    last_run: Optional[float]
    owner: Optional[int] = None  # the person who made it with their own login (None = a household routine)


class RoutineStore:
    def __init__(self, root: str) -> None:
        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                         " enabled INTEGER NOT NULL DEFAULT 1, spec TEXT NOT NULL, created REAL NOT NULL, last_run REAL)")
            if "owner" not in {row[1] for row in conn.execute("PRAGMA table_info(routines)")}:
                conn.execute("ALTER TABLE routines ADD COLUMN owner INTEGER")  # "made by" (people.id in location.db)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def all(self) -> List[Routine]:
        with self._connect() as conn:
            return [Routine(r["id"], r["name"], bool(r["enabled"]), json.loads(r["spec"]), r["last_run"], r["owner"])
                    for r in conn.execute("SELECT * FROM routines ORDER BY id")]

    def get(self, routine_id: int) -> Optional[Routine]:
        return next((r for r in self.all() if r.id == routine_id), None)

    def save(self, spec: Dict[str, Any], routine_id: Optional[int] = None, owner: Optional[int] = None) -> int:
        """(``owner`` is set when a routine is made and stays with it through later edits.)"""
        with self._lock, self._connect() as conn:
            if routine_id is None:
                cur = conn.execute("INSERT INTO routines (name, spec, created, owner) VALUES (?, ?, ?, ?)",
                                   (spec["name"], json.dumps(spec), time.time(), owner))
                return int(cur.lastrowid)
            conn.execute("UPDATE routines SET name = ?, spec = ? WHERE id = ?", (spec["name"], json.dumps(spec), routine_id))
            return routine_id

    def set_enabled(self, routine_id: int, on: bool) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("UPDATE routines SET enabled = ? WHERE id = ?", (int(on), routine_id)).rowcount > 0

    def delete(self, routine_id: int) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM routines WHERE id = ?", (routine_id,)).rowcount > 0

    def ran(self, routine_id: int, ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE routines SET last_run = ? WHERE id = ?", (ts, routine_id))


# ------------------------------------------------------------------ running them
def _later(delay: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


@dataclass
class _Hold:
    """A switch a routine turned on and will turn off again."""
    routine_id: int
    switch: str
    until: float  # for "for N minutes": the end; for "until quiet": recomputed from the last motion
    quiet: Optional[float]  # seconds without motion, or None
    cameras: List[str]  # whose motion keeps it on ([] = any camera / any Wi-Fi sensor)
    wifi: bool = False  # cameras are Wi-Fi motion sensors (path ids)


class RoutineRunner:
    """Matches what happens (engine hooks) against the saved routines and does their steps.

    ``hooks`` come from the engine: set_switch(id, on, source) -> (ok, msg), switch_on(id) -> bool|None,
    telegram(text, jpeg|None), say(text), coords() -> (lat, lon)|None, nobody_home() -> bool,
    outdoor_temp() -> degrees|None (the weather card's, in its unit),
    last_motion(camera key|None) -> ts, last_sensor_motion(Wi-Fi sensor path id|None) -> ts."""

    def __init__(self, store: RoutineStore, hooks: Dict[str, Callable], holds_path: str = HOLDS_FILE,
                 clock: Callable[[], float] = time.time, spawn: Optional[Callable] = None,
                 later: Optional[Callable[[float, Callable[[], None]], None]] = None) -> None:
        self.store = store
        # Steps run off the caller's thread (camera/detection threads must never wait on a slow plug).
        self._spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, name="routine", daemon=True).start())
        self._later = later or _later
        self.hooks = hooks
        self.holds_path = holds_path
        self._clock = clock
        self._lock = threading.Lock()
        self._holds: Dict[Tuple[int, str], _Hold] = {}
        self._said: Dict[Tuple[int, str], float] = {}  # (routine, "telegram" / "say") -> when it last did it
        self._motion_fired: Dict[Tuple[int, str], float] = {}
        self._package_fired: Dict[Tuple[int, str], float] = {}  # (routine, camera) -> last time, while a parcel waits
        self._sensor_moved: Dict[str, float] = {}  # Wi-Fi sensor path -> its latest moving reading
        self._deciding: Dict[int, float] = {}  # sensor rule -> when the movement that started it was
        # When set: did most of the house's sensor paths start moving around this moment (a Wi-Fi blip)? Then
        # sensor routines stay quiet. Asked HOUSE_WIDE_WAIT after the movement, once the other paths have reported.
        self.house_wide: Optional[Callable[[float], int]] = None
        self._settle_until = 0.0
        self._temp_checked = 0.0
        self._temp_met: Dict[int, bool] = {}  # temperature routine -> was it past its line at the last look
        self._time_checked = clock()
        self._routines: List[Routine] = []
        self.reload()

    def reload(self) -> None:
        self._routines = self.store.all()
        specs = {r.id: json.dumps(r.spec["when"], sort_keys=True) for r in self._routines}
        if getattr(self, "_temp_specs", None) is not None:  # an edited temperature line starts over
            for rid in [k for k in self._temp_met if specs.get(k) != self._temp_specs.get(k)]:
                del self._temp_met[rid]
        self._temp_specs = specs

    # ------------------------------------------------------------ conditions
    def is_dark(self, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else now
        coords = self.hooks["coords"]()
        times = sun_times(coords[0], coords[1], now) if coords else None
        if times is None:  # no location known: a plain evening/night guess
            return not 7 <= time.localtime(now).tm_hour < 18
        return not times[0] <= now < times[1]

    def _allowed(self, r: Routine, now: float, past: bool = False) -> bool:
        """``past``: replaying history, where "nobody's home" can't be known any more (it's left out)."""
        o = r.spec.get("only", {})
        if o.get("dark") and not self.is_dark(now):
            return False
        if o.get("light") and self.is_dark(now):
            return False
        t = time.localtime(now)
        if o.get("days") and DAYS[t.tm_wday] not in o["days"]:
            return False
        if o.get("from"):
            here = "%02d:%02d" % (t.tm_hour, t.tm_min)
            start, end = o["from"], o["to"]
            inside = start <= here < end if start < end else (here >= start or here < end)  # 22:00-06:00 crosses midnight
            if not inside:
                return False
        if o.get("nobody_home") and not past and not self.hooks["nobody_home"]():
            return False
        if ("temp_below" in o or "temp_above" in o) and not past:
            temp = self.outdoor_temp()
            if temp is None:  # no weather town, or the forecast can't be reached: it waits rather than guesses
                app_logger.log("info", "routine_skipped_no_temperature", routine_id=r.id, component="routines", action="run")
                return False
            if ("temp_below" in o and not temp < o["temp_below"]) or ("temp_above" in o and not temp > o["temp_above"]):
                return False
        return True

    def outdoor_temp(self) -> Optional[float]:
        get = self.hooks.get("outdoor_temp")
        return get() if get else None

    def _temperature(self, now: float) -> None:
        """Temperature routines go when it crosses their line, then not again until it's been TEMP_REARM back
        the other side. The first look only notes which side it's on (a restart on a cold night isn't a drop)."""
        if now - self._temp_checked < TEMP_CHECK_SECONDS:
            return
        self._temp_checked = now
        mine = [r for r in self._routines if r.enabled and r.spec["when"]["type"] == "temp"]
        temp = self.outdoor_temp() if mine else None
        if temp is None:
            return
        for r in mine:
            w = r.spec["when"]
            past = temp < w["below"] if "below" in w else temp > w["above"]
            back = temp >= w["below"] + TEMP_REARM if "below" in w else temp <= w["above"] - TEMP_REARM
            was = self._temp_met.get(r.id)
            if was is None:
                self._temp_met[r.id] = past
            elif past and not was:
                self._temp_met[r.id] = True
                self._fire(lambda _w: False, None, None, only={r.id})
            elif back and was:
                self._temp_met[r.id] = False

    # ------------------------------------------------------------ events from the engine
    def seen(self, camera: str, group: str, jpeg: Optional[bytes] = None, sure: bool = True) -> None:
        """``sure=False``: a moving person too dim to alert about. Enough to switch a light, but routines
        that send a message or speak wait for a real sighting."""
        groups = {group, "vehicle"} if group == "delivery" else {group}  # a delivery van is a vehicle too
        only = None
        if not sure:
            only = {r.id for r in self._routines if all(a["do"] == "switch" for a in r.spec["then"])}
        self._fire(lambda w: w["type"] == "seen" and groups & set(w["what"]) and (not w["cameras"] or camera in w["cameras"]),
                   camera, jpeg, keep=only)

    def _repeaters(self, camera: str) -> List[Routine]:
        return [r for r in self._routines if r.enabled and r.spec["when"]["type"] == "seen"
                and r.spec["when"].get("repeat_hours") and "package" in r.spec["when"]["what"]
                and (not r.spec["when"]["cameras"] or camera in r.spec["when"]["cameras"])]

    def package_waiting(self, camera: str) -> None:
        """A parcel is still at the door (every package snapshot): routines set to repeat go again every N hours."""
        now = self._clock()
        due = set()
        for r in self._repeaters(camera):
            last = self._package_fired.setdefault((r.id, camera), now)  # the first time was its "seen"
            if now - last >= r.spec["when"]["repeat_hours"] * 3600:
                self._package_fired[(r.id, camera)] = now
                due.add(r.id)
        if due:
            self._fire(lambda w: False, camera, None, only=due)

    def package_gone(self, camera: str) -> None:
        """Taken in (or "✅ Got it"): no more repeats until the next parcel."""
        for key in [k for k in self._package_fired if k[1] == camera]:
            del self._package_fired[key]

    def motion(self, camera: str) -> None:
        """Called on every frame with motion: cheap unless a motion routine is due."""
        now = self._clock()
        if now < self._settle_until:
            return
        due = [r for r in self._routines if r.enabled and r.spec["when"]["type"] == "motion"
               and (not r.spec["when"]["cameras"] or camera in r.spec["when"]["cameras"])
               and now - self._motion_fired.get((r.id, camera), 0) >= MOTION_REPEAT]
        if not due:
            return
        for r in due:
            self._motion_fired[(r.id, camera)] = now
        ids = {r.id for r in due}
        self._fire(lambda w, _ids=ids: False, camera, None, only=ids)

    def sensor(self, path: str) -> None:
        """A Wi-Fi motion sensor sees movement (called on each moving reading). No settling time: a light
        switching on changes the camera picture, not the radio signal. A rule with "and" / "but not" groups
        is decided ``within`` seconds later, once the other sensors have had their chance to react."""
        now = self._clock()
        fresh = now - self._sensor_moved.get(path, -math.inf) >= SENSOR_REARM  # (a jittery line once said
        self._sensor_moved[path] = now  # "someone opened the front door" every 2 minutes for an hour)
        due, rules = [], []
        for r in self._routines:
            w = r.spec["when"]
            if not r.enabled or w["type"] != "sensor" or not starts_it(w, path):
                continue
            if is_rule(w):
                if now - self._motion_fired.get((r.id, "rule"), 0) >= MOTION_REPEAT:
                    rules.append(r)
            elif fresh and now - self._motion_fired.get((r.id, "wifi:" + path), 0) >= MOTION_REPEAT:
                due.append(r)
        for r in due:
            self._motion_fired[(r.id, "wifi:" + path)] = now
        if due and self.house_wide is None:
            self._fire(lambda w: False, None, None, only={r.id for r in due})
        elif due:
            ids = {r.id for r in due}
            self._later(HOUSE_WIDE_WAIT, lambda: self._fire_unless_house_wide(ids, now, path))
        for r in rules:
            with self._lock:
                if r.id in self._deciding:
                    continue
                self._deciding[r.id] = now
            self._later(r.spec["when"]["within"], lambda rid=r.id: self._decide(rid))

    def _blip(self, at: float, path: str, ids) -> bool:
        """Most of the house moved at once around ``at``: the Wi-Fi blipped (logged, and nothing fires)."""
        if self.house_wide is None:
            return False
        from sentinel.engine.wifi_motion import HOUSE_WIDE_PATHS

        together = self.house_wide(at)
        if together < HOUSE_WIDE_PATHS:
            return False
        app_logger.log("info", "routine_skipped_house_wide", routines=sorted(ids), path=path, paths=together,
                       component="routines", action="sensor")
        return True

    def _fire_unless_house_wide(self, ids, at: float, path: str) -> None:
        if not self._blip(at, path, ids):
            self._fire(lambda w: False, None, None, only=set(ids))

    def _decide(self, rid: int) -> None:
        with self._lock:
            first = self._deciding.pop(rid, None)
        r = next((x for x in self._routines if x.id == rid), None)
        if first is None or r is None or not r.enabled:
            return
        if self._blip(first, "rule", [rid]):
            return
        if rule_met(r.spec["when"], first, lambda p: self._sensor_moved.get(p, -math.inf)):
            self._motion_fired[(rid, "rule")] = self._clock()
            self._fire(lambda w: False, None, None, only={rid})

    def replay(self, spec: Dict[str, Any], moments: List[Tuple[str, float, float, float]],
               line_of: Callable[[str], float], start: float, end: float) -> List[Tuple[float, float]]:
        """When a sensor routine would have gone off between start and end: (first, last) of each time,
        from the sensors' recorded bursts of movement. A burst counts only if it reached the path's
        movement line as it is now (``line_of``), so raising a line is replayed right. Times closer than
        SAY_COOLDOWN are one time (it would speak once)."""
        w = spec["when"]
        r = Routine(0, "", True, spec, None)
        bursts: Dict[str, List[Tuple[float, float]]] = {}
        for path, b0, b1, peak in moments:
            if peak >= line_of(path):
                bursts.setdefault(path, []).append((b0, b1))
        within = w.get("within", 0) if is_rule(w) else 0
        step = within or MOTION_REPEAT  # while movement goes on, it's checked again this often

        starts: Dict[str, List[float]] = {}
        reach: Dict[str, List[float]] = {}  # per path, the latest end of any burst so far (sorted by start)
        for path, spans in bursts.items():
            spans.sort()
            starts[path] = [b0 for b0, _ in spans]
            reach[path] = list(itertools.accumulate((b1 for _, b1 in spans), max))

        def moved(path: str, a: float, b: float) -> bool:
            i = bisect.bisect_right(starts.get(path, []), b)  # bursts that began by b...
            return i > 0 and reach[path][i - 1] >= a  # ...one of them still going at a

        def met(t: float) -> bool:
            return all(moved(p, t - within, t + within) for p in w.get("all", [])) and \
                not any(moved(p, t - within, t + within) for p in w.get("none", []))

        checks = sorted({b0 + i * step for path, spans in bursts.items() if starts_it(w, path)
                         for b0, b1 in spans for i in range(int((b1 - b0) // step) + 1)})
        out: List[List[float]] = []
        for t in checks:
            if not start <= t < end or (within and not met(t)) or not self._allowed(r, t, past=True):
                continue
            if out and t - out[-1][1] < SAY_COOLDOWN:
                out[-1][1] = t
            else:
                out.append([t, t])
        return [(a, b) for a, b in out]

    def person(self, kind: str, person_id: int) -> None:
        self._fire(lambda w: w["type"] == kind and (not w["people"] or person_id in w["people"]), None, None)

    def car(self, kind: str, car_id: int) -> None:
        self._fire(lambda w: w["type"] == "car_" + kind and (not w["cars"] or car_id in w["cars"]), None, None)

    def tick(self) -> None:
        """Every few seconds: time-of-day routines, and turning lights back off."""
        now = self._clock()
        if not self._holds and os.path.exists(self.holds_path):
            self.restore()  # left on when Sentinel last stopped
        last, self._time_checked = self._time_checked, now
        for r in self._routines:
            w = r.spec["when"]
            if not r.enabled or w["type"] != "time":
                continue
            for day in (last, now) if time.localtime(last).tm_yday != time.localtime(now).tm_yday else (now,):
                at = self._time_of(w, day)
                if at is not None and last < at <= now:
                    self._fire(lambda _w: False, None, None, only={r.id})
        self._temperature(now)
        self._release(now)

    def _time_of(self, w: Dict[str, Any], day: float) -> Optional[float]:
        if w["at"] in ("sunset", "sunrise"):
            coords = self.hooks["coords"]()
            times = sun_times(coords[0], coords[1], day) if coords else None
            if times is None:
                return None
            return times[1 if w["at"] == "sunset" else 0] + 60 * w.get("offset", 0)
        t = time.localtime(day)
        h, m = (int(x) for x in w["at"].split(":"))
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))

    # ------------------------------------------------------------ doing it
    def _fire(self, match: Callable[[Dict[str, Any]], Any], camera: Optional[str], jpeg: Optional[bytes],
              only: Optional[set] = None, keep: Optional[set] = None) -> None:
        now = self._clock()
        todo = [r for r in self._routines if r.enabled and (r.id in only if only is not None else match(r.spec["when"]))
                and (keep is None or r.id in keep) and self._allowed(r, now)]
        for r in todo:
            self._spawn(self.run, r, camera, jpeg)

    def run(self, r: Routine, camera: Optional[str] = None, jpeg: Optional[bytes] = None) -> None:
        now = self._clock()
        due = {kind for kind, wait in (("telegram", MESSAGE_COOLDOWN), ("say", SAY_COOLDOWN))
               if now - self._said.get((r.id, kind), 0) >= wait}
        did = []
        for a in r.spec["then"]:
            if a["do"] == "switch":
                did.append(self._switch(r, a, camera, now))
            elif a["do"] == "telegram" and "telegram" in due:
                self.hooks["telegram"](a["text"], jpeg if a.get("photo") else None)
                did.append("telegram")
            elif a["do"] == "say" and "say" in due:
                self.hooks["say"](a["text"])
                did.append("say")
            elif a["do"] == "tv_camera" and (a["camera"] or camera) and "tv_camera" in self.hooks:
                if self.hooks["tv_camera"](a["tv"], a["camera"] or camera, a["for"] * 60):
                    did.append("tv_camera")
        for kind in due & set(did):
            self._said[(r.id, kind)] = now
        if any(did):
            self.store.ran(r.id, now)
            r.last_run = now
            app_logger.log("info", "routine_ran", routine=r.name, routine_id=r.id, steps=[d for d in did if d],
                           component="routines", action="run")

    def _switch(self, r: Routine, a: Dict[str, Any], camera: Optional[str], now: float) -> str:
        key = (r.id, a["switch"])
        timed = a.get("for") or a.get("until_quiet")
        with self._lock:
            hold = self._holds.get(key)
            if hold is not None:  # already holding it on: just keep it on longer
                hold.until = now + (a.get("for") or a.get("until_quiet")) * 60
                return ""
        state = self.hooks["switch_on"](a["switch"])
        if state == a["on"]:
            return ""  # already that way (a light someone turned on by hand stays theirs)
        ok, _msg = self.hooks["set_switch"](a["switch"], a["on"], "routine")
        if not ok:
            return ""
        self._settle_until = now + SETTLE_SECONDS
        if a["on"] and timed:
            w = r.spec["when"]
            wifi = w["type"] == "sensor"
            cams = (w.get("sensors") or w.get("all", [])) if wifi else w.get("cameras", [])
            with self._lock:
                self._holds[key] = _Hold(r.id, a["switch"], now + timed * 60,
                                         a["until_quiet"] * 60 if a.get("until_quiet") else None, cams, wifi)
            self._save_holds()
        return "switch"

    def _release(self, now: float) -> None:
        with self._lock:
            holds = list(self._holds.items())
        for key, hold in holds:
            end = hold.until
            if hold.quiet is not None:
                cams = hold.cameras or [None]
                last_of = self.hooks.get("last_sensor_motion", lambda p: 0.0) if hold.wifi else self.hooks["last_motion"]
                last = max(last_of(c) for c in cams)
                end = max(end, last + hold.quiet)
            if now < end:
                continue
            with self._lock:
                others = any(h.switch == hold.switch for k, h in self._holds.items() if k != key)
            # The hold is let go only once the switch is off (or another routine still holds it on): a plug that
            # wasn't answering when the time ran out used to keep the light on for good (bug hunt 2026-11-13)
            done = True
            if not others:
                state = self.hooks["switch_on"](hold.switch)
                if state:
                    result = self.hooks["set_switch"](hold.switch, False, "routine")
                    done = bool(result[0]) if isinstance(result, tuple) else bool(result)
                    self._settle_until = now + SETTLE_SECONDS
                elif state is None:  # not answering: try again next time
                    done = False
                if not done and now - end > HOLD_GIVE_UP:
                    done = True
                    app_logger.log("error", "routine_switch_off_gave_up", switch=hold.switch, routine=hold.routine_id,
                                   component="routines", action="release")
            if done:
                with self._lock:
                    self._holds.pop(key, None)
                self._save_holds()

    def _save_holds(self) -> None:
        with self._lock:
            switches = sorted({h.switch for h in self._holds.values()})
        try:
            if switches:
                tmp = self.holds_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(switches, f)
                os.replace(tmp, self.holds_path)
            elif os.path.exists(self.holds_path):
                os.remove(self.holds_path)
        except OSError:
            pass

    def restore(self) -> None:
        """At start: lights a routine was holding on when Sentinel stopped go off (once the switches are found)."""
        try:
            with open(self.holds_path) as f:
                switches = json.load(f)
        except (OSError, ValueError):
            return
        left = []
        for sw in switches if isinstance(switches, list) else []:
            state = self.hooks["switch_on"](sw)
            if state is None:
                left.append(sw)  # not found yet; try again next time
            elif state:
                self.hooks["set_switch"](sw, False, "routine")
        try:
            if left:
                with open(self.holds_path, "w") as f:
                    json.dump(left, f)
            else:
                os.remove(self.holds_path)
        except OSError:
            pass

    def holding(self) -> Dict[int, float]:
        """Routine id -> when its light should go off (for the page)."""
        with self._lock:
            return {h.routine_id: h.until for h in self._holds.values()}
