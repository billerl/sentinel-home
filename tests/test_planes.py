import _test_env  # noqa: F401  (keeps tests off the production log)

import unittest

from sentinel.engine.planes import Planes, plane_map_words, plane_question, route_fits

HOME = (41.1145, -88.1780)  # the docs' made-up example spot


def airport(city, code, lat, lon):
    return {"municipality": city, "iata_code": code, "latitude": lat, "longitude": lon}


ROUTES = {  # adsbdb answers, by flight number
    "DAL1776": {"airline": {"name": "Delta Air Lines"}, "origin": airport("Tampa", "TPA", 27.98, -82.53),
                "destination": airport("Chicago", "ORD", 41.98, -87.90)},
    # a Southwest number looked up for another leg: the plane is over Illinois, not between these two
    "SWA3232": {"airline": {"name": "Southwest Airlines"}, "origin": airport("Los Angeles", "LAX", 33.94, -118.41),
                "destination": airport("San Francisco", "SFO", 37.62, -122.38)},
}


class FakeNet:
    def __init__(self, aircraft):
        self.aircraft, self.asked = aircraft, []

    def __call__(self, url):
        self.asked.append(url)
        if "adsb.lol" in url:
            return {"ac": self.aircraft}
        callsign = url.rsplit("/", 1)[1]
        if callsign in ROUTES:
            return {"response": {"flightroute": ROUTES[callsign]}}
        return {"response": "unknown callsign"}


SKY = [
    {"hex": "a1", "flight": "DAL1776 ", "r": "N101DQ", "t": "A321", "alt_baro": 11000, "track": 0.0, "lat": 41.1745, "lon": -88.2780},
    {"hex": "a2", "flight": "SWA3232 ", "r": "N8656B", "t": "B738", "alt_baro": 36000, "track": 274.0, "lat": 41.4345, "lon": -88.5080},
    {"hex": "a3", "flight": "N2628U  ", "r": "N2628U", "t": "C172", "alt_baro": 3800, "track": 305.0, "lat": 41.3245, "lon": -88.5680},
    {"hex": "a4", "flight": "", "t": "", "category": "A7", "alt_baro": 900, "lat": 41.1245, "lon": -88.1880},  # a helicopter
    {"hex": "a5", "flight": "RCH123", "t": "C17", "dbFlags": 1, "alt_baro": 25000, "lat": 41.3045, "lon": -87.9680},
    {"hex": "a6", "flight": "XYZ", "alt_baro": 30000},  # no position: left out
]


class TestPlaneQuestions(unittest.TestCase):
    def test_what_people_say(self):
        for text in ("What plane is that?", "what's that plane", "Hey Jarvis, what kind of plane is that?",
                     "what's flying overhead", "what is flying over the house", "what's that helicopter",
                     "which flight is that", "whose plane is that"):
            self.assertEqual(plane_question(text), "that", text)
        for text in ("Any planes around?", "are there any planes nearby", "how many planes are overhead",
                     "what planes are flying over", "what planes are in the area", "any helicopters around"):
            self.assertEqual(plane_question(text), "list", text)
        for text in ("show me the planes on the TV", "where is Kate", "what's on the TV", "turn on the fan",
                     "play Jet Airliner", "book a plane ticket", "what time does her plane land"):
            self.assertIsNone(plane_question(text), text)

    def test_tv_map_words(self):
        for words in ("the planes", "planes", "airplanes overhead", "the flight map", "air traffic", "the plane map"):
            self.assertTrue(plane_map_words(words), words)
        for words in ("front door", "kate's location", "the weather"):
            self.assertFalse(plane_map_words(words), words)


class TestPlanes(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.net = FakeNet([dict(a) for a in SKY])
        self.planes = Planes(home=lambda: HOME, get_json=self.net, clock=lambda: self.now)

    def test_only_routes_that_fit_are_told(self):
        found = {p["callsign"] or p["id"]: p for p in self.planes.nearby()}
        self.assertEqual(len(found), 5)
        self.assertEqual(found["DAL1776"]["route"]["to"]["city"], "Chicago")
        self.assertIsNone(found["SWA3232"]["route"])  # Los Angeles -> San Francisco can't be over Illinois
        self.assertEqual(found["SWA3232"]["airline"], "Southwest Airlines")  # (the airline is still right)
        self.assertIsNone(found["N2628U"]["route"])  # private: no lookup at all
        self.assertFalse(any("N2628U" in u for u in self.net.asked))

    def test_route_heading_the_wrong_way_is_dropped(self):
        plane = {"lat": 41.1745, "lon": -88.2780, "track": 180.0}  # on the line, but flying back toward Tampa
        route = {"from": {"lat": 27.98, "lon": -82.53}, "to": {"lat": 41.98, "lon": -87.90}}
        self.assertFalse(route_fits(plane, route))
        plane["track"] = 5.0
        self.assertTrue(route_fits(plane, route))

    def test_what_plane_is_that(self):
        said = self.planes.answer("that")
        # the low helicopter right by the house is nearest overhead
        self.assertTrue(said.startswith("That's probably a helicopter, 900 feet up"), said)
        self.net.aircraft = [a for a in self.net.aircraft if a["hex"] != "a4"]
        self.now += 60
        said = self.planes.answer("that")
        self.assertIn("Delta Air Lines flight 1776, an Airbus A321 from Tampa to Chicago, 11,000 feet up", said)
        self.assertIn("7 miles northwest", said)

    def test_whats_around(self):
        said = self.planes.answer("list")
        self.assertTrue(said.startswith("5 planes within 29 miles."), said)
        self.assertEqual(said.count("• "), 3)
        self.net.aircraft = [dict(SKY[1])]
        self.now += 60
        said = self.planes.answer("list")
        self.assertIn("Southwest Airlines flight 3232, a Boeing 737, 36,000 feet up", said)
        self.assertNotIn("San Francisco", said)
        self.assertIn("A military C-17", Planes(home=lambda: HOME, get_json=FakeNet([SKY[4]])).answer("list"))

    def test_fetched_at_most_every_few_seconds(self):
        self.planes.nearby()
        self.planes.nearby()
        self.assertEqual(sum("adsb.lol" in u for u in self.net.asked), 1)
        self.assertEqual(sum("adsbdb" in u for u in self.net.asked), 3)  # DAL1776, SWA3232, RCH123 (not N2628U)
        self.now += 30
        self.planes.nearby()
        self.assertEqual(sum("adsb.lol" in u for u in self.net.asked), 2)
        self.assertEqual(sum("adsbdb" in u for u in self.net.asked), 3)  # routes kept for hours

    def test_trouble_is_said_plainly(self):
        def down(url):
            raise OSError("no internet")
        self.assertIn("couldn't reach the flight tracker", Planes(home=lambda: HOME, get_json=down).answer("that"))
        self.assertIn("save Home", Planes(home=lambda: None, get_json=self.net).answer("list"))
        self.assertIn("don't see any planes", Planes(home=lambda: HOME, get_json=FakeNet([])).answer("list"))
        view = Planes(home=lambda: HOME, get_json=down).map_view()
        self.assertEqual((view["planes"], view["error"]), ([], "The flight tracker isn't answering."))

    def test_tv_map_view(self):
        view = self.planes.map_view()
        self.assertEqual(view["home"], list(HOME))
        delta = next(p for p in view["planes"] if p["tag"] == "DAL1776")
        self.assertEqual((delta["route"], delta["feet"], delta["low"]), ("TPA → ORD", "11,000 feet up", False))
        self.assertTrue(next(p for p in view["planes"] if p["tag"] == "N2628U")["low"])


if __name__ == "__main__":
    unittest.main()
