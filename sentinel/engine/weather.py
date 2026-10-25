"""
Weather for the live grid's spare tile (3 cameras in a 2x2 grid leave one empty).

From Open-Meteo: free, no account or key, and it is only sent the town/ZIP's rough
coordinates. The server fetches it (at most every 10 minutes, shared by every
browser) so phones on mobile data and the Fire TV don't each call out.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from sentinel.logging_utils import app_logger

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_SECONDS = 10 * 60
RETRY_SECONDS = 2 * 60
WINDY_MPH = 20  # gusts this strong set trees and bushes moving: expect motion clips

# WMO weather codes -> (words, day icon, night icon)
_CODES: Dict[int, Tuple[str, str, str]] = {
    0: ("Clear", "☀️", "🌙"),
    1: ("Mostly clear", "🌤️", "🌙"),
    2: ("Partly cloudy", "⛅", "☁️"),
    3: ("Cloudy", "☁️", "☁️"),
    45: ("Fog", "🌫️", "🌫️"),
    48: ("Freezing fog", "🌫️", "🌫️"),
    51: ("Light drizzle", "🌦️", "🌧️"),
    53: ("Drizzle", "🌦️", "🌧️"),
    55: ("Heavy drizzle", "🌧️", "🌧️"),
    56: ("Freezing drizzle", "🌧️", "🌧️"),
    57: ("Freezing drizzle", "🌧️", "🌧️"),
    61: ("Light rain", "🌦️", "🌧️"),
    63: ("Rain", "🌧️", "🌧️"),
    65: ("Heavy rain", "🌧️", "🌧️"),
    66: ("Freezing rain", "🌧️", "🌧️"),
    67: ("Freezing rain", "🌧️", "🌧️"),
    71: ("Light snow", "🌨️", "🌨️"),
    73: ("Snow", "🌨️", "🌨️"),
    75: ("Heavy snow", "❄️", "❄️"),
    77: ("Snow grains", "🌨️", "🌨️"),
    80: ("Showers", "🌦️", "🌧️"),
    81: ("Showers", "🌧️", "🌧️"),
    82: ("Heavy showers", "⛈️", "⛈️"),
    85: ("Snow showers", "🌨️", "🌨️"),
    86: ("Heavy snow showers", "❄️", "❄️"),
    95: ("Thunderstorm", "⛈️", "⛈️"),
    96: ("Thunderstorm, hail", "⛈️", "⛈️"),
    99: ("Thunderstorm, hail", "⛈️", "⛈️"),
}


def describe_code(code: Any, is_day: bool = True) -> Tuple[str, str]:
    words, day, night = _CODES.get(int(code) if code is not None else -1, ("", "🌡️", "🌡️"))
    return words, day if is_day else night


def _get_json(url: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "SentinelHome"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WeatherService:
    """``current()`` returns the card's data (cached); fetching happens on a background thread."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._place: Optional[Dict[str, Any]] = None  # {query, name, lat, lon}
        self._data: Optional[Dict[str, Any]] = None
        self._fetched_at = 0.0
        self._failed: Tuple[Any, float] = (None, 0.0)  # (query+units, when) of the last failure
        self._busy = False
        self._error = ""

    def _query(self) -> str:
        return str(self.settings.get("weather_location") or "").strip()

    def _units(self) -> str:
        return "celsius" if self.settings.get("weather_units") == "celsius" else "fahrenheit"

    def current(self) -> Dict[str, Any]:
        query = self._query()
        if not query:
            return {"configured": False}
        now, key = time.time(), (query, self._units())
        with self._lock:
            stale_key = self._data is None or self._data.get("key") != key
            due = stale_key or now - self._fetched_at > REFRESH_SECONDS
            failed_key, failed_at = self._failed
            if due and not self._busy and (failed_key != key or now - failed_at > RETRY_SECONDS):
                self._busy = True
                threading.Thread(target=self._refresh, args=key, daemon=True, name="weather").start()
            data = self._data if not stale_key else None
            error = self._error if failed_key == key else ""
        if data is None:
            return {"configured": True, "loading": not error, "error": error}
        return {"configured": True, **{k: v for k, v in data.items() if k != "key"}}

    def _refresh(self, query: str, units: str) -> None:
        try:
            place = self._place if self._place and self._place["query"] == query else self._geocode(query)
            raw = _get_json(FORECAST_URL, {
                "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                           "wind_speed_10m,wind_gusts_10m,is_day,precipitation",
                "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                "temperature_unit": units, "wind_speed_unit": "mph", "forecast_days": 2, "forecast_hours": 12,
            })
            data = self._card(raw, place, units)
            data["key"] = (query, units)
            with self._lock:
                self._place, self._data, self._fetched_at = place, data, time.time()
                self._error, self._failed = "", (None, 0.0)
        except Exception as e:  # network down, bad place name: keep the last card, try again later
            message = str(e) if isinstance(e, LookupError) else "weather service unreachable"
            with self._lock:
                self._error, self._failed = message, ((query, units), time.time())
            app_logger.log("warning", "weather_fetch_failed", error_message=str(e)[:200], component="weather",
                           action="refresh")
        finally:
            with self._lock:
                self._busy = False

    @staticmethod
    def _geocode(query: str) -> Dict[str, Any]:
        params = {"name": query, "count": 1, "language": "en"}
        if query.isdigit() and len(query) == 5:
            params["countryCode"] = "US"  # a US ZIP code
        results = _get_json(GEOCODE_URL, params).get("results") or []
        if not results:
            raise LookupError(f"couldn't find “{query}” — try a town name or ZIP code")
        r = results[0]
        name = ", ".join(x for x in (r.get("name"), r.get("admin1") if r.get("country_code") == "US" else r.get("country")) if x)
        return {"query": query, "name": name, "lat": r["latitude"], "lon": r["longitude"]}

    @staticmethod
    def _card(raw: Dict[str, Any], place: Dict[str, Any], units: str) -> Dict[str, Any]:
        cur, hourly, daily = raw.get("current") or {}, raw.get("hourly") or {}, raw.get("daily") or {}
        is_day = bool(cur.get("is_day", 1))
        words, icon = describe_code(cur.get("weather_code"), is_day)
        gusts = cur.get("wind_gusts_10m") or 0
        hours = []
        for i, t in enumerate((hourly.get("time") or [])[1:7], start=1):  # the next 6 hours
            h_words, h_icon = describe_code(hourly["weather_code"][i], bool((hourly.get("is_day") or [1] * 99)[i]))
            hour = int(t[11:13])
            hours.append({"label": f"{hour % 12 or 12}{'a' if hour < 12 else 'p'}",
                          "temp": round(hourly["temperature_2m"][i]), "icon": h_icon,
                          "rain": (hourly.get("precipitation_probability") or [0] * 99)[i] or 0})
        hhmm = lambda s: (f"{int(s[11:13]) % 12 or 12}:{s[14:16]}" if s else "")  # noqa: E731
        return {
            "place": place["name"],
            "unit": "°C" if units == "celsius" else "°F",
            "temp": round(cur.get("temperature_2m", 0)),
            "feels": round(cur.get("apparent_temperature", 0)),
            "humidity": cur.get("relative_humidity_2m"),
            "wind": round(cur.get("wind_speed_10m") or 0),
            "gusts": round(gusts),
            "windy": gusts >= WINDY_MPH,
            "words": words,
            "icon": icon,
            "high": round((daily.get("temperature_2m_max") or [0])[0]),
            "low": round((daily.get("temperature_2m_min") or [0])[0]),
            "rain_today": (daily.get("precipitation_probability_max") or [0])[0] or 0,
            "sunrise": hhmm((daily.get("sunrise") or [""])[0]),
            "sunset": hhmm((daily.get("sunset") or [""])[0]),
            "hours": hours,
            "updated": hhmm(cur.get("time") or ""),
        }
