"""Weather tile data: Open-Meteo's answer turned into the card, and the settings it takes."""

import _test_env  # noqa: F401  (isolates the event log)

import unittest
from unittest import mock

from sentinel.engine import admin, weather
from sentinel.engine.weather import WeatherService

RAW = {
    "current": {"time": "2026-10-25T16:45", "temperature_2m": 69.2, "apparent_temperature": 66.8,
                "relative_humidity_2m": 40, "weather_code": 0, "wind_speed_10m": 12.1, "wind_gusts_10m": 24.0,
                "is_day": 1},
    "hourly": {"time": [f"2026-10-25T{h:02d}:00" for h in range(16, 24)],
               "temperature_2m": [69, 69, 67, 62, 58, 56, 54, 52], "weather_code": [0, 0, 0, 1, 3, 61, 0, 0],
               "precipitation_probability": [0, 0, 0, 0, 10, 60, 0, 0], "is_day": [1, 1, 1, 0, 0, 0, 0, 0]},
    "daily": {"temperature_2m_max": [69.5], "temperature_2m_min": [40.2], "precipitation_probability_max": [60],
              "sunrise": ["2026-10-25T07:55"], "sunset": ["2026-10-25T18:37"]},
}


class TestWeather(unittest.TestCase):
    def test_card(self):
        card = WeatherService._card(RAW, {"name": "Springfield, Ohio"}, "fahrenheit")
        self.assertEqual((card["temp"], card["feels"], card["high"], card["low"]), (69, 67, 70, 40))
        self.assertEqual((card["words"], card["icon"], card["unit"]), ("Clear", "☀️", "°F"))
        self.assertTrue(card["windy"])  # 24 mph gusts: trees will set off motion
        self.assertEqual([h["label"] for h in card["hours"]], ["5p", "6p", "7p", "8p", "9p", "10p"])
        self.assertEqual(card["hours"][4]["rain"], 60)
        self.assertEqual(card["hours"][2]["icon"], "🌙")  # 7 pm, after dark
        self.assertEqual((card["sunrise"], card["sunset"], card["updated"]), ("7:55", "6:37", "4:45"))
        self.assertEqual((card["sky"], card["sun_min"]), ("clear", [475, 1117]))  # the tile's background

    def test_sky_kind(self):
        kinds = {c: weather.sky_kind(c) for c in (0, 1, 2, 3, 45, 48, 51, 63, 66, 71, 77, 80, 82, 85, 95, 99, None)}
        self.assertEqual(kinds, {0: "clear", 1: "clear", 2: "partly", 3: "cloudy", 45: "fog", 48: "fog", 51: "rain",
                                 63: "rain", 66: "rain", 71: "snow", 77: "snow", 80: "rain", 82: "rain", 85: "snow",
                                 95: "storm", 99: "storm", None: "clear"})

    def test_full_screen(self):
        raw = {**RAW, "current": {**RAW["current"], "wind_direction_10m": 200, "pressure_msl": 1016.3, "visibility": 24140,
                                  "uv_index": 2.6, "dew_point_2m": 41.2, "cloud_cover": 30},
               "daily": {**RAW["daily"], "time": ["2026-10-25", "2026-10-26"], "temperature_2m_max": [69.5, 61],
                         "temperature_2m_min": [40.2, 44], "weather_code": [0, 63], "precipitation_sum": [0, 0.42],
                         "snowfall_sum": [0, 0], "uv_index_max": [4.1, 1.2], "sunrise": ["2026-10-25T07:55", "2026-10-26T07:56"],
                         "sunset": ["2026-10-25T18:37", "2026-10-26T18:35"], "precipitation_probability_max": [60, 90]}}
        full = WeatherService._card(raw, {"name": "Springfield, Ohio"}, "fahrenheit",
                                    {"current": {"us_aqi": 42.4, "pm2_5": 6.21}, "hourly": {"us_aqi": [30, 55, None]}})["full"]
        now = full["now"]
        self.assertEqual((now["dir"], now["pressure"], now["pressure_unit"], now["visibility"], now["uv_words"]),
                         ("S", 30.01, "inHg", 15.0, "Moderate"))  # 200° is from the south; 24 km is 15 miles
        self.assertEqual([(d["name"], d["low"], d["high"], d["rain"], d["words"]) for d in full["days"]],
                         [("Today", 40, 70, 60, "Clear"), ("Mon", 44, 61, 90, "Rain")])
        self.assertEqual((full["days"][1]["precip"], full["days"][0]["uv_words"], full["days"][0]["sunset"]), (0.42, "Moderate", "6:37 pm"))
        self.assertEqual(len(full["hours"]), 8)
        self.assertEqual((full["hours"][5]["label"], full["hours"][5]["rain"], full["hours"][5]["words"]), ("9p", 60, "Light rain"))
        self.assertEqual(full["air"], {"aqi": 42, "words": "Good", "pm25": 6.2, "max_today": 55})
        self.assertIsNone(WeatherService._card(raw, {"name": "x"}, "fahrenheit")["full"]["air"])  # air service down: still a card

    def test_not_configured_makes_no_requests(self):
        with mock.patch.object(weather, "_get_json") as get:
            self.assertEqual(WeatherService({}).current(), {"configured": False})
            get.assert_not_called()

    def test_settings(self):
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_location"]("  Springfield,   Ohio "), "Springfield, Ohio")
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_location"]("45501"), "45501")
        with self.assertRaises(ValueError):
            admin.SETTINGS_SCHEMA["weather_location"]("<script>")
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_units"](""), "fahrenheit")


if __name__ == "__main__":
    unittest.main()
