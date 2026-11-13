"""
Weather for the live grid's spare tile (3 cameras in a 2x2 grid leave one empty), and its full-screen
view (tap the tile): now in detail, 48 hours, 7 days and the air quality.

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
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"  # also free, no key
REFRESH_SECONDS = 10 * 60
RETRY_SECONDS = 2 * 60
WINDY_MPH = 20  # gusts this strong set trees and bushes moving: expect motion clips
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# US AQI bands (EPA): upper bound -> words
AQI_BANDS = ((50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"), (200, "Unhealthy"),
             (300, "Very unhealthy"), (10_000, "Hazardous"))
UV_BANDS = ((2, "Low"), (5, "Moderate"), (7, "High"), (10, "Very high"), (99, "Extreme"))

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


def sky_kind(code: Any) -> str:
    """The weather tile's background picture for a WMO code: clear, partly, cloudy, fog, rain, snow or storm."""
    c = int(code) if code is not None else 0
    if c >= 95:
        return "storm"
    if 71 <= c <= 77 or c in (85, 86):
        return "snow"
    if c >= 51:
        return "rain"
    return {2: "partly", 3: "cloudy", 45: "fog", 48: "fog"}.get(c, "clear")


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
                           "wind_speed_10m,wind_gusts_10m,wind_direction_10m,is_day,precipitation,pressure_msl,"
                           "cloud_cover,dew_point_2m,visibility,uv_index",
                "hourly": "temperature_2m,apparent_temperature,weather_code,precipitation_probability,precipitation,"
                          "wind_speed_10m,wind_gusts_10m,wind_direction_10m,relative_humidity_2m,uv_index,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                         "precipitation_sum,snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max,"
                         "wind_direction_10m_dominant,uv_index_max,sunrise,sunset",
                "temperature_unit": units, "wind_speed_unit": "mph", "forecast_days": 7, "forecast_hours": 48,
                "precipitation_unit": "mm" if units == "celsius" else "inch",
            })
            try:  # the air quality is a separate service: the card works without it
                air = _get_json(AIR_URL, {"latitude": place["lat"], "longitude": place["lon"], "timezone": "auto",
                                          "current": "us_aqi,pm2_5", "hourly": "us_aqi", "forecast_days": 1})
            except Exception:
                air = {}
            data = self._card(raw, place, units, air)
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
    def _card(raw: Dict[str, Any], place: Dict[str, Any], units: str, air: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        minutes = lambda s: (int(s[11:13]) * 60 + int(s[14:16]) if s else None)  # noqa: E731
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
            # The tile's sky: the kind of weather, and sunrise/sunset in minutes after midnight (the page's own clock
            # decides day, dawn, dusk or night, so it changes on time even between weather updates)
            "sky": sky_kind(cur.get("weather_code")),
            "sun_min": [minutes((daily.get("sunrise") or [""])[0]), minutes((daily.get("sunset") or [""])[0])],
            "hours": hours,
            "updated": hhmm(cur.get("time") or ""),
            "full": _full(raw, air or {}, units),
        }


def _band(value: Optional[float], bands) -> str:
    return next((words for top, words in bands if value is not None and value <= top), "")


def _compass(degrees: Optional[float]) -> str:
    return COMPASS[int((degrees % 360) / 45 + 0.5) % 8] if degrees is not None else ""


def _full(raw: Dict[str, Any], air: Dict[str, Any], units: str) -> Dict[str, Any]:
    """The full-screen view: now in detail, the next 48 hours, 7 days, the air quality."""
    cur, hourly, daily = raw.get("current") or {}, raw.get("hourly") or {}, raw.get("daily") or {}
    metric = units == "celsius"

    def col(src: Dict[str, Any], key: str, i: int, default: Any = None) -> Any:
        values = src.get(key) or []
        return values[i] if i < len(values) and values[i] is not None else default

    def num(value: Any, digits: int = 0) -> Optional[float]:
        return None if value is None else (round(value) if digits == 0 else round(value, digits))

    hours = []
    for i, t in enumerate(hourly.get("time") or []):
        words, icon = describe_code(col(hourly, "weather_code", i), bool(col(hourly, "is_day", i, 1)))
        hour = int(t[11:13])
        hours.append({"t": t, "label": f"{hour % 12 or 12}{'a' if hour < 12 else 'p'}", "day": t[:10],
                      "temp": num(col(hourly, "temperature_2m", i)), "feels": num(col(hourly, "apparent_temperature", i)),
                      "rain": col(hourly, "precipitation_probability", i, 0),
                      "precip": num(col(hourly, "precipitation", i, 0), 2), "wind": num(col(hourly, "wind_speed_10m", i)),
                      "gusts": num(col(hourly, "wind_gusts_10m", i)), "dir": _compass(col(hourly, "wind_direction_10m", i)),
                      "humidity": col(hourly, "relative_humidity_2m", i), "uv": num(col(hourly, "uv_index", i), 1),
                      "words": words, "icon": icon})
    hhmm = lambda s: f"{int(s[11:13]) % 12 or 12}:{s[14:16]} {'am' if int(s[11:13]) < 12 else 'pm'}" if s else ""  # noqa: E731
    days = []
    for i, d in enumerate(daily.get("time") or []):
        words, icon = describe_code(col(daily, "weather_code", i), True)
        name = time.strftime("%a", time.strptime(d, "%Y-%m-%d"))
        days.append({"date": d, "name": "Today" if i == 0 else name, "words": words, "icon": icon,
                     "high": num(col(daily, "temperature_2m_max", i)), "low": num(col(daily, "temperature_2m_min", i)),
                     "rain": col(daily, "precipitation_probability_max", i, 0),
                     "precip": num(col(daily, "precipitation_sum", i, 0), 2),
                     "snow": num(col(daily, "snowfall_sum", i, 0), 1),  # cm, or inches with precipitation_unit=inch
                     "wind": num(col(daily, "wind_speed_10m_max", i)), "gusts": num(col(daily, "wind_gusts_10m_max", i)),
                     "dir": _compass(col(daily, "wind_direction_10m_dominant", i)),
                     "uv": num(col(daily, "uv_index_max", i), 1), "uv_words": _band(col(daily, "uv_index_max", i), UV_BANDS),
                     "sunrise": hhmm(col(daily, "sunrise", i, "")), "sunset": hhmm(col(daily, "sunset", i, ""))})
    visibility = cur.get("visibility")
    pressure = cur.get("pressure_msl")
    now = {"wind": num(cur.get("wind_speed_10m")), "gusts": num(cur.get("wind_gusts_10m")),
           "dir": _compass(cur.get("wind_direction_10m")), "dir_deg": cur.get("wind_direction_10m"),
           "humidity": cur.get("relative_humidity_2m"), "dew": num(cur.get("dew_point_2m")),
           "pressure": None if pressure is None else (round(pressure) if metric else round(pressure * 0.02953, 2)),
           "pressure_unit": "hPa" if metric else "inHg", "clouds": cur.get("cloud_cover"),
           "visibility": None if visibility is None else (round(visibility / 1000, 1) if metric else round(visibility / 1609.34, 1)),
           "visibility_unit": "km" if metric else "mi", "uv": num(cur.get("uv_index"), 1),
           "uv_words": _band(cur.get("uv_index"), UV_BANDS), "precip_unit": "mm" if metric else "in",
           "snow_unit": "cm" if metric else "in"}
    aqi = (air.get("current") or {}).get("us_aqi")
    today_aqi = [v for v in ((air.get("hourly") or {}).get("us_aqi") or []) if v is not None]
    return {"now": now, "hours": hours, "days": days,
            "air": None if aqi is None else {"aqi": round(aqi), "words": _band(aqi, AQI_BANDS),
                                             "pm25": num((air.get("current") or {}).get("pm2_5"), 1),
                                             "max_today": round(max(today_aqi)) if today_aqi else None}}
