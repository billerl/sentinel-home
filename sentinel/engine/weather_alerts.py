"""
Severe weather warnings from the US National Weather Service (api.weather.gov: free, no key), checked
every two minutes for the house's spot. A warning that's dangerous now (tornado, severe thunderstorm,
flash flood, blizzard...) is said out loud, sent to every Sentinel chat and shown as a red banner on the
camera screens, even at 3 a.m. when phones are on silent. Watches only go to Telegram; advisories and
statements are left out.

Only rough coordinates leave the house (2 decimals, about a kilometre): enough for the county and the
storm-based warning areas.
"""

import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

ALERTS_URL = "https://api.weather.gov/alerts/active?point={lat:.2f},{lon:.2f}"
CHECK_SECONDS = 120
STATE_FILE = "weather_alerts.json"  # ids already announced, so a restart doesn't repeat them
# Said out loud: warnings of something dangerous happening now or very soon.
LOUD_SEVERITIES = {"Extreme", "Severe"}
# Never out loud even if marked severe (slow-moving, nobody needs waking for them).
QUIET_EVENTS = {"Flood Warning", "Coastal Flood Warning", "Lakeshore Flood Warning", "Red Flag Warning",
                "Hydrologic Outlook"}


def level(props: Dict[str, Any]) -> str:
    """'loud' (speak + Telegram + banner), 'notice' (Telegram + banner) or '' (ignored)."""
    event = str(props.get("event") or "")
    if event.endswith("Warning") or event.endswith("Emergency"):
        if props.get("severity") in LOUD_SEVERITIES and event not in QUIET_EVENTS:
            return "loud"
        return "notice"
    if event.endswith("Watch"):
        return "notice"
    return ""


def _clock(iso: str) -> str:
    """'4:45 PM' from '2026-11-06T16:45:00-05:00' (the NWS gives local times)."""
    m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", iso or "")
    if not m:
        return ""
    hour = int(m.group(1))
    return f"{hour % 12 or 12}:{m.group(2)} {'AM' if hour < 12 else 'PM'}"


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!])(\s|$)", text)
    text = m.group(1) if m else text
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def spoken(alert: Dict[str, Any]) -> str:
    """'Weather alert. A tornado warning is in effect here until 4:45 PM. Take shelter now...'"""
    until = f" until {alert['until']}" if alert["until"] else ""
    words = f"Weather alert. A {alert['event'].lower()} is in effect here{until}."
    if alert["instruction"]:
        words += " " + _first_sentence(alert["instruction"])
    return words


def message(alert: Dict[str, Any]) -> str:
    until = f" until {alert['until']}" if alert["until"] else ""
    icon = "🌪️" if "Tornado" in alert["event"] else "⚠️"
    text = f"{icon} {alert['event']}{until}"
    if alert["headline"]:
        text += f"\n{alert['headline']}"
    if alert["instruction"]:
        text += f"\n\n{_first_sentence(alert['instruction'], 400)}"
    return text


def _fetch(lat: float, lon: float) -> List[Dict[str, Any]]:
    req = urllib.request.Request(ALERTS_URL.format(lat=lat, lon=lon), headers={
        "User-Agent": "SentinelHome (home security; https://github.com/billerl/sentinel-home)",
        "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return [f.get("properties") or {} for f in json.loads(resp.read().decode("utf-8")).get("features") or []]


class WeatherAlerts:
    """``tick()`` from the engine's loop; ``active()`` for the web banner and voice questions."""

    def __init__(self, settings: Dict[str, Any], coords: Callable[[], Optional[Tuple[float, float]]],
                 notify: Callable[[str], None], speak: Callable[[str], Any], root: str,
                 fetch: Callable[[float, float], List[Dict[str, Any]]] = _fetch,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.coords = coords
        self.notify = notify  # (text): every Sentinel chat
        self.speak = speak  # (text): out loud
        self.fetch = fetch
        self.clock = clock
        self.path = os.path.join(root, STATE_FILE)
        self._lock = threading.Lock()
        self._active: List[Dict[str, Any]] = []
        self._checked = 0.0
        self._busy = False
        self._failing = False
        self._announced: Dict[str, Tuple[float, str]] = {}  # alert id -> (when, its level); kept 2 days
        try:
            with open(self.path) as f:
                self._announced = {str(k): (float(v[0]), str(v[1])) for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError, TypeError, IndexError):
            pass

    def enabled(self) -> bool:
        return bool(self.settings.get("weather_alerts_enabled", True))

    def active(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._active]

    def tick(self) -> None:
        """Start a check on a thread if one is due (the NWS can take seconds to answer)."""
        now = self.clock()
        with self._lock:
            if self._busy or now - self._checked < CHECK_SECONDS:
                return
            self._checked, self._busy = now, True
        threading.Thread(target=self.check, name="weather-alerts", daemon=True).start()

    def check(self) -> None:
        try:
            spot = self.coords() if self.enabled() else None
            if spot is None:
                with self._lock:
                    self._active = []
                return
            try:
                raw = self.fetch(*spot)
            except Exception as e:  # offline, NWS busy: keep what we had, say so once
                if not self._failing:
                    app_logger.log("warning", "weather_alerts_failed", error_message=str(e)[:200],
                                   component="weather", action="alerts")
                self._failing = True
                return
            self._failing = False
            self._update(raw)
        finally:
            with self._lock:
                self._busy = False

    def _update(self, raw: List[Dict[str, Any]]) -> None:
        alerts = []
        for p in raw:
            kind = level(p)
            if not kind or p.get("messageType") == "Cancel" or p.get("status", "Actual") != "Actual":
                continue
            alerts.append({"id": str(p.get("id") or ""), "event": str(p.get("event") or "Weather alert"),
                           "level": kind, "until": _clock(p.get("ends") or p.get("expires") or ""),
                           "headline": str(p.get("headline") or ""),
                           "instruction": str(p.get("instruction") or ""),
                           "references": [str(r.get("identifier") or r.get("@id") or "")
                                          for r in p.get("references") or [] if isinstance(r, dict)]})
        alerts.sort(key=lambda a: a["level"] != "loud")
        with self._lock:
            self._active = [{k: a[k] for k in ("event", "level", "until", "headline")} for a in alerts]
        now = self.clock()
        for a in alerts:
            if not a["id"] or a["id"] in self._announced:
                continue
            # An update of a warning we already announced (new end time, new wording): no new alarm,
            # unless it got worse (a watch became a warning, a warning became an emergency).
            earlier = [self._announced[r][1] for r in a["references"] if r in self._announced]
            self._announced[a["id"]] = (now, a["level"])
            if earlier and not (a["level"] == "loud" and "loud" not in earlier):
                continue
            app_logger.log("info", "weather_alert", alert_event=a["event"], alert_level=a["level"], until=a["until"],
                           component="weather", action="alerts")
            self.notify(message(a))
            if a["level"] == "loud" and self.settings.get("weather_alerts_speak", True):
                self.speak(spoken(a))
        self._announced = {k: v for k, v in self._announced.items() if now - v[0] < 2 * 86400}
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump(self._announced, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass

    def summary(self) -> str:
        """For "any weather alerts?": one sentence."""
        active = self.active()
        if not active:
            return "There are no weather warnings or watches for here right now."
        parts = [f"a {a['event'].lower()}" + (f" until {a['until']}" if a["until"] else "") for a in active[:3]]
        return "Right now there's " + ", and ".join(parts) + "."
