"""The weather tile's moon: tonight's real phase, and whether it's up (sentinel/engine/moon.py)."""

import _test_env  # noqa: F401  (isolates the event log)

import calendar
import time
import unittest

from sentinel.engine.moon import altitude, moon, phase
from sentinel.engine.weather import WeatherService

SPRINGFIELD = (39.92, -83.81)  # (Springfield, Ohio: a made-up household's town)


def utc(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M"))


class TestMoon(unittest.TestCase):
    def test_phase_on_known_nights(self):
        self.assertLess(phase(utc("2024-04-08 18:18"))[0], 0.01)  # the total solar eclipse: new moon
        self.assertGreater(phase(utc("2025-03-14 06:58"))[0], 0.99)  # the total lunar eclipse: full
        lit, waxing = phase(utc("2026-11-18 17:00"))  # 2026-11-18: past first quarter, growing
        self.assertTrue(0.55 < lit < 0.7 and waxing)
        lit, waxing = phase(utc("2026-12-01 12:00"))  # a week after the full moon: shrinking
        self.assertTrue(0.4 < lit < 0.9 and not waxing)

    def test_up_only_between_moonrise_and_moonset(self):
        # 2026-11-18 (the tile showed a moon all night): that evening's moon sets after midnight and rises
        # after lunch the next day
        lat, lon = SPRINGFIELD
        self.assertGreater(altitude(lat, lon, utc("2026-11-19 01:00")), 10)  # 8 pm there: high in the south
        self.assertLess(altitude(lat, lon, utc("2026-11-19 11:00")), -10)  # 6 am there: long set
        self.assertFalse(moon(lat, lon, utc("2026-11-19 11:00"))["up"])
        self.assertTrue(moon(lat, lon, utc("2026-11-19 01:00"))["up"])
        self.assertIsNone(moon(None, None)["up"])  # (no town yet: the phase only)

    def test_the_weather_card_carries_it(self):
        service = WeatherService({"weather_location": "Springfield"})
        service._place = {"query": "Springfield", "name": "Springfield, Ohio", "lat": SPRINGFIELD[0], "lon": SPRINGFIELD[1]}
        service._data = {"key": ("Springfield", "fahrenheit"), "temp": 50}
        service._fetched_at = time.time()
        card = service.current()
        self.assertEqual(set(card["moon"]), {"lit", "waxing", "up"})
        self.assertIsInstance(card["moon"]["up"], bool)


if __name__ == "__main__":
    unittest.main()
