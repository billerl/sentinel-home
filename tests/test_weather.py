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
        card = WeatherService._card(RAW, {"name": "Tiffin, Ohio"}, "fahrenheit")
        self.assertEqual((card["temp"], card["feels"], card["high"], card["low"]), (69, 67, 70, 40))
        self.assertEqual((card["words"], card["icon"], card["unit"]), ("Clear", "☀️", "°F"))
        self.assertTrue(card["windy"])  # 24 mph gusts: trees will set off motion
        self.assertEqual([h["label"] for h in card["hours"]], ["5p", "6p", "7p", "8p", "9p", "10p"])
        self.assertEqual(card["hours"][4]["rain"], 60)
        self.assertEqual(card["hours"][2]["icon"], "🌙")  # 7 pm, after dark
        self.assertEqual((card["sunrise"], card["sunset"], card["updated"]), ("7:55", "6:37", "4:45"))

    def test_not_configured_makes_no_requests(self):
        with mock.patch.object(weather, "_get_json") as get:
            self.assertEqual(WeatherService({}).current(), {"configured": False})
            get.assert_not_called()

    def test_settings(self):
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_location"]("  Tiffin,   Ohio "), "Tiffin, Ohio")
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_location"]("44883"), "44883")
        with self.assertRaises(ValueError):
            admin.SETTINGS_SCHEMA["weather_location"]("<script>")
        self.assertEqual(admin.SETTINGS_SCHEMA["weather_units"](""), "fahrenheit")


if __name__ == "__main__":
    unittest.main()
