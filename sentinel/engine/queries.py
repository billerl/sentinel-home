"""
Understand Telegram requests like "videos last 2 hours", "videos 2pm-4pm people",
"videos yesterday front door" and turn them into a time range plus filters.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

_GROUP_WORDS = {
    "person": ("people", "person", "persons", "someone", "humans"),
    "vehicle": ("vehicle", "vehicles", "car", "cars", "truck", "trucks", "van", "vans"),
    "animal": ("animal", "animals", "dog", "dogs", "cat", "cats", "deer"),
}
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"

VIDEO_QUERY_HELP = (
    "Ask for recorded videos by time, e.g.:\n"
    "- Videos last 2 hours\n"
    "- Videos today / Videos yesterday\n"
    "- Videos 2pm-4pm (or Videos from 14:00 to 16:00)\n"
    "- Videos since 8am\n"
    "Add people, vehicles or animals, or a camera name, to narrow it down:\n"
    "- Videos today people front door"
)


def is_video_query(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.match(r"^(videos?|recordings?|footage|clips (from|since|today|yesterday|last))\b", t))


@dataclass
class VideoQuery:
    start: float
    end: float
    groups: Set[str] = field(default_factory=set)
    cameras: List[str] = field(default_factory=list)
    label: str = ""


def _day_start(ts: float) -> float:
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _clock(hour: str, minute: Optional[str], meridiem: Optional[str], default_pm: bool) -> Optional[Tuple[int, int]]:
    h, m = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if meridiem == "pm" else 0)
    elif default_pm and 1 <= h <= 6:
        h += 12  # "videos 2-4" most likely means the afternoon
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def parse_video_query(text: str, camera_names: Iterable[str] = (), now: Optional[float] = None) -> Tuple[Optional[VideoQuery], Optional[str]]:
    """Parse a request; returns (query, None) or (None, error message)."""
    now = time.time() if now is None else now
    t = text.strip().lower()
    t = re.sub(r"^(videos?|recordings?|footage|clips)\s*", "", t)

    groups = {g for g, words in _GROUP_WORDS.items() if any(re.search(rf"\b{w}\b", t) for w in words)}
    cameras = [name for name in camera_names if name and name.lower() in t]
    for name in cameras:
        t = t.replace(name.lower(), " ")
    for words in _GROUP_WORDS.values():
        for w in words:
            t = re.sub(rf"\b{w}\b", " ", t)
    t = re.sub(r"\b(of|with|from the|on the|at the|for|show me|please)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    today = _day_start(now)
    base = today
    if re.search(r"\byesterday\b", t):
        base = _day_start(today - 43200)
        t = re.sub(r"\byesterday\b", " ", t).strip()

    m = re.search(r"\blast\s+(\d+)?\s*(minute|min|hour|hr|day)s?\b", t) or re.search(r"\bpast\s+(\d+)?\s*(minute|min|hour|hr|day)s?\b", t)
    if m:
        amount = int(m.group(1) or 1)
        unit = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[m.group(2)]
        return VideoQuery(now - amount * unit, now, groups, cameras, f"last {amount} {m.group(2)}{'s' if amount > 1 else ''}"), None

    m = re.search(rf"(?:from\s+|between\s+)?{_TIME}\s*(?:-|to|until|and)\s*{_TIME}", t)
    if m:
        a = _clock(m.group(1), m.group(2), m.group(3) or m.group(6), default_pm=not (m.group(3) or m.group(6)))
        b = _clock(m.group(4), m.group(5), m.group(6), default_pm=not m.group(6))
        if not a or not b:
            return None, "I couldn't read that time range. " + VIDEO_QUERY_HELP
        start = base + a[0] * 3600 + a[1] * 60
        end = base + b[0] * 3600 + b[1] * 60
        if end <= start:
            end += 86400
        return VideoQuery(start, min(end, now), groups, cameras, f"{m.group(0).strip()}"), None

    m = re.search(rf"\b(?:since|after)\s+{_TIME}", t)
    if m:
        a = _clock(m.group(1), m.group(2), m.group(3), default_pm=False)
        if not a:
            return None, "I couldn't read that time. " + VIDEO_QUERY_HELP
        start = base + a[0] * 3600 + a[1] * 60
        if start > now:
            start -= 86400
        return VideoQuery(start, now, groups, cameras, f"since {m.group(0).split(None, 1)[1]}"), None

    if t in ("", "today") or re.fullmatch(r"(today|now|recent|latest)", t):
        if base != today:
            return VideoQuery(base, today, groups, cameras, "yesterday"), None
        if t in ("", "recent", "latest"):
            return VideoQuery(now - 3600, now, groups, cameras, "last hour"), None
        return VideoQuery(today, now, groups, cameras, "today"), None
    return None, VIDEO_QUERY_HELP
