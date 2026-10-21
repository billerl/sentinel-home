"""
Answer plain-language Telegram questions about what the cameras saw:
"did anyone come by?", "any deliveries today?", "what happened last night?",
"was anyone at the garage this morning?".

Rule-based on purpose: free, instant, private, and it can only report what is in
the event history. ``parse_question`` turns text into a structured ``Question``;
``answer_question`` turns a question plus recorded events into a reply. A smarter
front end (e.g. a small local language model) could later produce the same
``Question`` without touching the answering side.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sentinel.engine.queries import _TIME, _clock, _day_start

VISIT_GAP_SECONDS = 240  # sightings closer than this are one visit (van pulls up, driver walks over)
MAX_LINES = 10
GROUP_PRIORITY = ("delivery", "person", "vehicle", "animal")  # what headlines a visit

_SUBJECTS = {
    "delivery": r"deliver\w*|packages?|parcels?|amazon|ups|fedex|usps|dhl|mail|mailman|postman|post",
    "person": r"anyone|anybody|someone|somebody|people|persons?|visitors?|guests?|strangers?|who|knock\w*",
    "vehicle": r"cars?|vehicles?|trucks?|vans?",
    "animal": r"animals?|dogs?|cats?|deer|critters?|wildlife",
}
_EVERYTHING = r"what happened|what'?s happened|what went on|anything|activity|going on|any motion|what did i miss|news"

_QUESTION_PATTERNS = [
    rf"\b(did|has|have|was|were|is|are|any|been)\b.*\b({'|'.join(_SUBJECTS.values())})\b",
    r"\bwho\b.*\b(came|come|coming|was|were|stopped|visited|been|is)\b",
    r"\b(come|came|stop|stopped|swing|swung|drop|dropped)\s+(by|over)\b",
    rf"\b({_EVERYTHING})\b",
    r"\bany\s+(visitors|deliveries|packages|cars|vehicles|animals)\b",
]

QUESTION_HELP = (
    "I can answer questions about what the cameras saw, e.g.:\n"
    "- Did anyone come by?\n"
    "- Any deliveries today?\n"
    "- What happened last night?\n"
    "- Was anyone at the front door this morning?\n"
    "- Any cars in the last 2 hours?"
)


def is_question(text: str) -> bool:
    t = text.strip().lower()
    if not t or re.match(r"^(videos?|recordings?|footage|status|help|clip|pause|resume|enable|disable)\b", t):
        return False
    return any(re.search(p, t) for p in _QUESTION_PATTERNS)


@dataclass
class Question:
    start: float
    end: float
    groups: Set[str] = field(default_factory=set)  # empty = everything
    cameras: List[str] = field(default_factory=list)
    period: str = "today"  # human wording: "today", "last night", "in the last 2 hours"
    day_offset: Optional[int] = 0  # 0 = today, 1 = yesterday, None = spans days (for clip hints)


def _period(t: str, now: float) -> Tuple[float, float, str, Optional[int]]:
    today = _day_start(now)
    yesterday = _day_start(today - 43200)

    m = re.search(r"\b(?:in the |over the )?(?:last|past)\s+(\d+|an?|one|few|couple(?: of)?)?\s*(minute|min|hour|hr|day)s?\b", t)
    if m:
        word = (m.group(1) or "1").strip()
        amount = {"a": 1, "an": 1, "one": 1, "few": 3, "couple": 2, "couple of": 2}.get(word)
        amount = amount if amount is not None else int(word)
        unit = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[m.group(2)]
        noun = {"min": "minute", "hr": "hour"}.get(m.group(2), m.group(2))
        label = f"in the last {noun}" if amount == 1 else f"in the last {amount} {noun}s"
        return now - amount * unit, now, label, None
    if re.search(r"\blast night\b|\bovernight\b", t):
        return yesterday + 18 * 3600, min(now, today + 7 * 3600), "last night", None
    if re.search(r"\bthis morning\b", t):
        return today + 5 * 3600, min(now, today + 12 * 3600), "this morning", 0
    if re.search(r"\bthis afternoon\b", t):
        return today + 12 * 3600, min(now, today + 17 * 3600), "this afternoon", 0
    if re.search(r"\b(this evening|tonight)\b", t):
        return today + 17 * 3600, now, "this evening", 0
    if re.search(r"\byesterday\b", t):
        return yesterday, today, "yesterday", 1
    if re.search(r"\bthis week\b|\blast (?:few|couple of) days\b", t):
        return today - 6 * 86400, now, "this week", None
    m = re.search(rf"\b(?:since|after)\s+{_TIME}", t)
    if m:
        a = _clock(m.group(1), m.group(2), m.group(3), default_pm=False)
        if a:
            start = today + a[0] * 3600 + a[1] * 60
            if start > now:
                start -= 86400
            return start, now, f"since {_fmt_clock(start)}", 0 if start >= today else None
    if re.search(r"\btoday\b", t) or now - today >= 6 * 3600:
        return today, now, "today", 0
    # Early morning with no time given: "today" would be only a few hours, so look back 12.
    return now - 12 * 3600, now, "in the last 12 hours", None


def parse_question(text: str, camera_names: Iterable[str] = (), now: Optional[float] = None) -> Question:
    now = time.time() if now is None else now
    t = text.strip().lower()
    cameras = [name for name in camera_names if name and name.lower() in t]
    for name in cameras:
        t = t.replace(name.lower(), " ")
    # Common nicknames for cameras ("back door" for "Rear Door", "driveway" for "Garage") are
    # left to zone names in the descriptions; only exact camera names filter.
    start, end, label, day_offset = _period(t, now)

    if re.search(rf"\b({_EVERYTHING})\b", t) and not re.search(rf"\b({_SUBJECTS['delivery']}|{_SUBJECTS['animal']})\b", t):
        groups: Set[str] = set()
    else:
        groups = {g for g, words in _SUBJECTS.items() if re.search(rf"\b({words})\b", t)}
        if "delivery" in groups:
            groups.discard("vehicle")  # "amazon van" is a delivery question
        if groups == {"person"} or not groups:
            groups = {"person", "delivery"}  # a delivery driver counts as someone coming by
    return Question(start, end, groups, cameras, label, day_offset)


# ------------------------------------------------------------------ answering
def _fmt_clock(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}{'am' if t.tm_hour < 12 else 'pm'}"


def _fmt_when(ts: float, now: float) -> str:
    today = _day_start(now)
    if ts >= today:
        return f"today at {_fmt_clock(ts)}"
    if ts >= _day_start(today - 43200):
        return f"yesterday at {_fmt_clock(ts)}"
    return time.strftime("%a %d %b", time.localtime(ts)) + f" at {_fmt_clock(ts)}"


def _plain(description: str) -> str:
    """Drop the leading emoji: '🚶 Someone is at the Front Door.' -> 'Someone is at the Front Door.'"""
    return description.split(" ", 1)[1] if description and not description[0].isalnum() and " " in description else description


def group_visits(events: List[Dict]) -> List[List[Dict]]:
    """Cluster time-sorted events into visits (gaps under VISIT_GAP_SECONDS)."""
    visits: List[List[Dict]] = []
    for event in sorted(events, key=lambda e: e["ts"]):
        if visits and event["ts"] - visits[-1][-1]["ts"] < VISIT_GAP_SECONDS:
            visits[-1].append(event)
        else:
            visits.append([event])
    return visits


def headline(visit: List[Dict]) -> Dict:
    rank = {g: i for i, g in enumerate(GROUP_PRIORITY)}
    return min(visit, key=lambda e: (rank.get(e["grp"], 9), e["ts"]))


def _noun(groups: Set[str], n: int) -> str:
    if groups == {"delivery"}:
        return "delivery" if n == 1 else "deliveries"
    if groups == {"vehicle"}:
        return "vehicle" if n == 1 else "vehicles"
    if groups == {"animal"}:
        return "animal sighting" if n == 1 else "animal sightings"
    if groups and groups <= {"person", "delivery"}:
        return "visit" if n == 1 else "visits"
    return "event" if n == 1 else "events"


def _none_text(groups: Set[str], period: str) -> str:
    if groups == {"delivery"}:
        return f"No deliveries {period}."
    if groups == {"vehicle"}:
        return f"No vehicles {period}."
    if groups == {"animal"}:
        return f"No animals {period}."
    if groups and groups <= {"person", "delivery"}:
        return f"No — nobody came by {period}."
    return f"All quiet {period} — nothing recognised."


def clip_hint(visit: List[Dict], now: float) -> Optional[str]:
    """A 'videos ...' command that fetches this visit's clips (today's or yesterday's only)."""
    start, end = visit[0]["ts"] - 60, visit[-1]["ts"] + 120
    today = _day_start(now)
    if _day_start(start) != _day_start(end) or start < _day_start(today - 43200):
        return None
    day = "" if start >= today else "yesterday "
    return f"videos {day}{_fmt_clock(start)}-{_fmt_clock(end)}"


def answer_question(q: Question, events: List[Dict], last_before: Optional[Dict], now: Optional[float] = None,
                     others: Optional[Dict[str, int]] = None) -> Tuple[str, List[Dict]]:
    """Reply text and up to 3 events whose snapshots are worth sending (newest visits first).

    ``events`` are the matching events in the period; ``last_before`` the most recent
    matching event before it (for "the last one was..."); ``others`` counts other kinds
    of events in the same period, mentioned in a footnote.
    """
    now = time.time() if now is None else now
    where = f" at the {', '.join(q.cameras)}" if q.cameras else ""
    visits = group_visits(events)
    if not visits:
        text = _none_text(q.groups, q.period)
        if where:
            text = text[:-1] + where + "."
        if last_before is not None:
            text += f"\nThe last one was {_fmt_when(last_before['ts'], now)}: {_plain(last_before['description'])}"
        return text, []

    n = len(visits)
    if q.groups:
        lead = f"Yes — {n} {_noun(q.groups, n)}{where} {q.period}:"
    else:
        lead = f"Here's what happened{where} {q.period} ({n} {_noun(q.groups, n)}):"
    lines = [lead]
    shown = visits[-MAX_LINES:]
    if len(visits) > len(shown):
        lines.append(f"…{len(visits) - len(shown)} earlier, then:")
    multi_day = q.day_offset is None and _day_start(shown[0][0]["ts"]) != _day_start(now)
    for visit in shown:
        top = headline(visit)
        when = _fmt_when(top["ts"], now) if multi_day else _fmt_clock(top["ts"])
        line = f"• {when} — {top['description']}"
        extra = next((e for e in visit if e["grp"] != top["grp"]), None)
        if extra is not None:
            line += f" Also: {_plain(extra['description'])}"
        lines.append(line)
    if others:
        bits = [f"{count} {_noun({g}, count)}" for g, count in sorted(others.items()) if count]
        if bits:
            lines.append(f"(Also {' and '.join(bits)} — ask \"what happened {q.period}\" for everything.)")
    hint = clip_hint(shown[-1], now)
    if hint:
        lines.append(f'For the clips, reply "{hint}".')
    photos = [headline(v) for v in reversed(shown) if headline(v).get("snapshot")][:3]
    return "\n".join(lines), photos
