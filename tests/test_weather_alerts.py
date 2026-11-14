"""Severe weather warnings: what's said out loud, what only goes to Telegram, and no repeats."""

import _test_env  # noqa: F401  (isolates the event log)

import shutil
import tempfile
import unittest

from sentinel.engine.weather_alerts import WeatherAlerts, level, spoken


def nws(id_, event, severity="Severe", refs=(), **extra):
    """One alert's properties, the way api.weather.gov sends them."""
    return {"id": id_, "event": event, "severity": severity, "messageType": "Update" if refs else "Alert",
            "status": "Actual", "ends": "2026-11-06T16:45:00-05:00",
            "headline": f"{event} issued November 6 at 3:52PM EST until 4:45PM EST by NWS Cleveland OH",
            "instruction": "TAKE COVER NOW! Move to a basement or an interior room on the lowest floor. Avoid windows.",
            "references": [{"identifier": r} for r in refs], **extra}


class TestWeatherAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-wx-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = 1000.0
        self.raw = []
        self.sent, self.said = [], []
        self.settings = {}

    def alerts(self):
        return WeatherAlerts(self.settings, lambda: (41.1145, -88.1780), self.sent.append, self.said.append, self.dir,
                             fetch=lambda lat, lon: list(self.raw), clock=lambda: self.now)

    def test_levels(self):
        self.assertEqual(level(nws("a", "Tornado Warning", "Extreme")), "loud")
        self.assertEqual(level(nws("a", "Severe Thunderstorm Warning")), "loud")
        self.assertEqual(level(nws("a", "Tornado Watch")), "notice")
        self.assertEqual(level(nws("a", "Flood Warning")), "notice")  # slow: no need to wake anyone
        self.assertEqual(level(nws("a", "Wind Chill Warning", "Moderate")), "notice")
        self.assertEqual(level(nws("a", "Wind Advisory", "Moderate")), "")
        self.assertEqual(level(nws("a", "Special Weather Statement", "Moderate")), "")

    def test_a_tornado_warning_is_said_sent_and_shown_once(self):
        wx = self.alerts()
        self.raw = [nws("w1", "Tornado Warning", "Extreme"), nws("w2", "Wind Advisory", "Moderate")]
        wx.check()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("🌪️ Tornado Warning until 4:45 PM"))
        self.assertEqual(self.said, ["Weather alert. A tornado warning is in effect here until 4:45 PM. TAKE COVER NOW!"])
        self.assertEqual([a["event"] for a in wx.active()], ["Tornado Warning"])  # the banner (no advisory)
        wx.check()
        self.assertEqual((len(self.sent), len(self.said)), (1, 1))  # still in effect: not again
        self.assertIn("tornado warning until 4:45 PM", wx.summary())

    def test_a_warning_telegram_didnt_take_is_sent_again(self):
        # (bug hunt 2026-11-13: marked announced before sending, so a failed send was never retried)
        tries = []
        up = [False]
        wx = WeatherAlerts(self.settings, lambda: (41.1145, -88.1780), lambda text: tries.append(text) or up[0],
                           self.said.append, self.dir, fetch=lambda lat, lon: list(self.raw), clock=lambda: self.now)
        self.raw = [nws("w1", "Tornado Warning", "Extreme")]
        wx.check()  # Telegram isn't answering
        self.assertEqual(len(tries), 1)
        self.assertEqual(len(self.said), 1)  # said out loud at once
        self.now += 120
        up[0] = True
        wx.check()  # next check: sent again, and this time it goes
        self.assertEqual(len(tries), 2)
        self.now += 120
        wx.check()
        self.assertEqual(len(tries), 2)  # delivered: not again
        self.assertEqual(len(self.said), 1)  # (and never said twice)

    def test_an_unsent_warning_that_has_ended_is_dropped(self):
        tries = []
        wx = WeatherAlerts(self.settings, lambda: (41.1145, -88.1780), lambda text: tries.append(text) and False,
                           self.said.append, self.dir, fetch=lambda lat, lon: list(self.raw), clock=lambda: self.now)
        self.raw = [nws("w1", "Tornado Warning", "Extreme")]
        wx.check()
        self.raw = []  # it's over
        self.now += 120
        wx.check()
        self.assertEqual(len(tries), 1)

    def test_a_watch_only_goes_to_telegram(self):
        wx = self.alerts()
        self.raw = [nws("t1", "Tornado Watch")]
        wx.check()
        self.assertEqual((len(self.sent), self.said), (1, []))

    def test_updates_dont_alarm_again_but_a_watch_becoming_a_warning_does(self):
        wx = self.alerts()
        self.raw = [nws("s1", "Severe Thunderstorm Warning")]
        wx.check()
        self.raw = [nws("s2", "Severe Thunderstorm Warning", refs=["s1"])]  # extended: new id, same storm
        wx.check()
        self.assertEqual((len(self.sent), len(self.said)), (1, 1))
        self.raw = [nws("t1", "Tornado Watch")]
        wx.check()
        self.raw = [nws("t2", "Tornado Warning", "Extreme", refs=["t1"])]  # upgraded
        wx.check()
        self.assertEqual(len(self.said), 2)

    def test_a_restart_doesnt_repeat_and_switching_off_is_quiet(self):
        self.raw = [nws("w1", "Tornado Warning", "Extreme")]
        self.alerts().check()
        self.alerts().check()  # a new Sentinel process, same warning
        self.assertEqual(len(self.said), 1)
        self.settings["weather_alerts_speak"] = False
        self.raw = [nws("w3", "Blizzard Warning")]
        self.alerts().check()
        self.assertEqual((len(self.sent), len(self.said)), (2, 1))  # sent, not said
        self.settings["weather_alerts_enabled"] = False
        wx = self.alerts()
        wx.check()
        self.assertEqual(wx.active(), [])

    def test_offline_keeps_the_banner(self):
        wx = self.alerts()
        self.raw = [nws("w1", "Tornado Warning", "Extreme")]
        wx.check()
        wx.fetch = lambda lat, lon: (_ for _ in ()).throw(OSError("offline"))
        wx.check()
        self.assertEqual(len(wx.active()), 1)

    def test_spoken_without_an_end_time_or_advice(self):
        a = {"event": "Snow Squall Warning", "until": "", "instruction": ""}
        self.assertEqual(spoken(a), "Weather alert. A snow squall warning is in effect here.")


if __name__ == "__main__":
    unittest.main()
