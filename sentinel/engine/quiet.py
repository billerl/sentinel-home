"""
Quiet cameras: one camera's alerts paused for a while ("pause the garage camera for two hours" while someone
works in the driveway). The camera keeps watching and recording, its history and routines carry on; only the
alerts stop: the house speaker, Telegram photos and pages with "Hear alerts" on. It comes back by itself.

Kept in recorded_videos/quiet_cameras.json so a restart doesn't bring the alerts back early.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

STATE_FILE = "quiet_cameras.json"
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60

_PAUSE = r"\b(pause|mute|silence|snooze|quiet|hush|stop|turn off|shut off|no more)\b"
_RESUME = r"\b(resume|unpause|un-pause|unmute|un-mute|unsilence|turn (?:back )?on|back on|start)\b"
_ALERT = r"\b(alerts?|notifications?|notifying|announcements?|announcing|messages?|telling me)\b"
_CAMERA = r"\bcam(?:era)?s?\b"
_ALL = r"\b(all|every|each)\b[^.]*\bcam(?:era)?s?\b|\bthe cameras\b|\bcameras\b"
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10,
            "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45, "forty-five": 45, "sixty": 60,
            "ninety": 90}
_AMOUNT = r"(\d{1,4}(?:\.5)?|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")"
QuietCommand = Tuple[str, List[str], int]  # ("pause" | "resume" | "which", camera names, minutes)


def _minutes(t: str) -> int:
    if re.search(r"\bhalf (?:an|a) hour\b", t):
        return 30
    m = re.search(_AMOUNT + r"\s*(?:and a half\s*)?(hours?|hrs?|minutes?|mins?)\b", t)
    if m is None:
        return DEFAULT_MINUTES
    n = float(m.group(1)) if m.group(1)[0].isdigit() else float(_NUMBERS[m.group(1)])
    hours = m.group(2).startswith("h")
    if "and a half" in m.group(0) or (hours and t[m.end():].startswith(" and a half")):
        n += 0.5
    if hours:
        n *= 60
    return int(max(1, min(MAX_MINUTES, round(n))))


def quiet_command(text: str, cameras: Sequence[Tuple[str, Iterable[str]]]) -> Optional[QuietCommand]:
    """What a sentence asks of the cameras' alerts, or None when it isn't about that.

    ``cameras``: (name, its zone names) for each: "the driveway" finds the camera with that zone. A camera has to
    be named (or "all cameras"), so "pause" for the music and "Pause 30" for a Telegram chat stay what they were."""
    t = re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip(" .!?")
    if re.search(r"\b(which|what|any|are)\b.*\bcam(?:era)?s?\b.*\b(paused|muted|quiet|silenced)\b", t):
        return "which", [], 0
    named = [name for name, zones in cameras
             if any(w and re.search(r"\b" + re.escape(w.lower()) + r"\b", t) for w in [name, *zones])]
    everything = not named and re.search(_ALL, t) is not None
    if not named and not everything:
        return None
    names = named or [name for name, _zones in cameras]
    if not names:
        return None
    about_cameras = everything or re.search(_CAMERA, t) is not None or re.search(_ALERT, t) is not None
    if re.search(_RESUME, t) and about_cameras and not re.search(r"\b(tv|television)\b", t):
        return "resume", names, 0
    if re.search(_PAUSE, t) and about_cameras and not re.search(r"\b(record|recording|tv|television|show)\b", t):
        return "pause", names, _minutes(t)
    return None


def say_minutes(minutes: int) -> str:
    hours, rest = divmod(max(1, int(minutes)), 60)
    parts = ([f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []) + \
            ([f"{rest} minute{'s' if rest != 1 else ''}"] if rest else [])
    return " and ".join(parts)


def _list(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class QuietCameras:
    def __init__(self, root: str, clock: Callable[[], float] = time.time) -> None:
        self.path = os.path.join(os.path.abspath(root), STATE_FILE)
        self._clock = clock
        self._lock = threading.Lock()
        self._until: Dict[str, float] = {}  # camera name -> when its alerts come back
        try:
            with open(self.path, encoding="utf-8") as f:
                kept = json.load(f)
            self._until = {str(k): float(v) for k, v in kept.items() if float(v) > clock()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._until, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # (still paused until Sentinel restarts)

    def is_quiet(self, camera: str) -> bool:
        with self._lock:
            return self._until.get(camera, 0.0) > self._clock()

    def view(self) -> Dict[str, int]:
        """{camera name: minutes left} for the paused ones."""
        now = self._clock()
        with self._lock:
            return {name: max(1, int((until - now + 59) // 60)) for name, until in self._until.items() if until > now}

    def run(self, command: QuietCommand) -> str:
        """Do what quiet_command understood; the answer for Telegram and Jarvis."""
        kind, names, minutes = command
        now = self._clock()
        if kind == "which":
            left = self.view()
            if not left:
                return "No camera's alerts are paused."
            return "Alerts are paused for " + _list([f"{n} ({say_minutes(m)} left)" for n, m in left.items()]) + "."
        with self._lock:
            self._until = {n: u for n, u in self._until.items() if u > now}
            if kind == "resume":
                back = [n for n in names if n in self._until]
                for n in back:
                    del self._until[n]
                self._save()
                if not back:
                    return "Those alerts weren't paused."
                return f"Okay, alerts from {_list(back)} are back on."
            for n in names:
                self._until[n] = now + minutes * 60
            self._save()
        cams = f"the {names[0]} camera" if len(names) == 1 else _list(list(names))
        return (f"Okay, no alerts from {cams} for {say_minutes(minutes)}. "
                f"{'It keeps' if len(names) == 1 else 'They keep'} recording.")
