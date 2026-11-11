import _test_env  # noqa: F401  (keeps tests off the production log)

import unittest

from sentinel.engine.planes import Planes, pick_leg, plane_map_words, plane_question, route_fits

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


def stop(city, code, lat, lon):
    return {"location": city, "iata": code, "lat": lat, "lon": lon}


STOPS = {  # adsb.lol's route list, by flight number (stops in order); others are a 404
    # adsbdb has Southwest 3232 as another leg; adsb.lol knows its stops, the middle leg passing over the plane
    "SWA3232": {"_airports": [stop("Detroit", "DTW", 42.21, -83.35), stop("Chicago", "MDW", 41.79, -87.75),
                              stop("Denver", "DEN", 39.86, -104.67)]},
}


class FakeNet:
    def __init__(self, aircraft):
        self.aircraft, self.asked = aircraft, []

    def __call__(self, url):
        self.asked.append(url)
        if "/v2/point/" in url:
            return {"ac": self.aircraft}
        if "vrs-standing-data" in url:
            callsign = url.rsplit("/", 1)[1].replace(".json", "")
            if callsign in STOPS:
                return STOPS[callsign]
            raise OSError("HTTP Error 404: Not Found")
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
                     "which flight is that", "whose plane is that",
                     "What was that playing? It just flew over.",  # (as heard, 2026-11-10: "plane" -> "playing")
                     "what was that that just flew over the house"):
            self.assertEqual(plane_question(text), "that", text)
        for text in ("Any planes around?", "are there any planes nearby", "how many planes are overhead",
                     "what planes are flying over", "what planes are in the area", "any helicopters around"):
            self.assertEqual(plane_question(text), "list", text)
        for text in ("show me the planes on the TV", "where is Kate", "what's on the TV", "turn on the fan",
                     "play Jet Airliner", "book a plane ticket", "what time does her plane land",
                     "what was that noise", "what is that playing on the TV"):
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
        # adsbdb's Los Angeles -> San Francisco can't be over Illinois; adsb.lol's stops have the leg it's on
        self.assertEqual((found["SWA3232"]["route"]["from"]["city"], found["SWA3232"]["route"]["to"]["city"]), ("Chicago", "Denver"))
        self.assertEqual(found["SWA3232"]["airline"], "Southwest Airlines")  # (the name from adsbdb)
        self.assertIsNone(found["N2628U"]["route"])  # private: no lookup at all
        self.assertFalse(any("N2628U" in u for u in self.net.asked))

    def test_route_heading_the_wrong_way_is_dropped(self):
        plane = {"lat": 41.1745, "lon": -88.2780, "track": 180.0}  # on the line, but flying back toward Tampa
        route = {"from": {"lat": 27.98, "lon": -82.53}, "to": {"lat": 41.98, "lon": -87.90}}
        self.assertFalse(route_fits(plane, route))
        plane["track"] = 5.0
        self.assertTrue(route_fits(plane, route))

    def test_leg_flown_back_the_other_way(self):
        # looked up as Tampa -> Chicago (adsbdb's pair), but on that line heading south: the same number flown back
        tpa, ord_ = {"city": "Tampa", "lat": 27.98, "lon": -82.53}, {"city": "Chicago", "lat": 41.98, "lon": -87.90}
        plane = {"lat": 41.1745, "lon": -88.2780, "track": 175.0}
        self.assertEqual(pick_leg(plane, [[tpa, ord_]])["to"]["city"], "Tampa")
        plane["track"] = 90.0  # heading for neither: nothing said
        self.assertIsNone(pick_leg(plane, [[tpa, ord_]]))
        self.assertIsNone(pick_leg(plane, []))

    def test_what_plane_is_that(self):
        said = self.planes.answer("that")
        # the low helicopter right by the house is nearest overhead
        self.assertTrue(said.startswith("That's probably a helicopter, 900 feet up"), said)
        self.net.aircraft = [a for a in self.net.aircraft if a["hex"] != "a4"]
        self.now += 60
        said = self.planes.answer("that")
        self.assertIn("Delta Air Lines flight 1776, an Airbus A321 from Tampa to Chicago, 11,000 feet up", said)
        self.assertIn("7 miles northwest", said)

    def test_planes_at_the_airport_arent_what_anyone_means(self):
        parked = {"hex": "g1", "flight": "AAL716", "t": "A319", "alt_baro": "ground", "lat": HOME[0], "lon": HOME[1]}
        self.net.aircraft = [parked] + [dict(a) for a in SKY]
        said = self.planes.answer("that")
        self.assertNotIn("American", said)
        self.assertNotIn("on the ground", self.planes.answer("list"))
        lonely = Planes(home=lambda: HOME, get_json=FakeNet([parked]), clock=lambda: 1000.0).answer("that")
        self.assertIn("on the ground, close by", lonely)  # (nothing flying: then it's the one)

    def test_the_one_that_just_went_over(self):
        # heard, then asked: by then a low plane is miles off. It flew east right over the house 90 s ago.
        low = {"hex": "c1", "flight": "N12345", "r": "N12345", "t": "C172", "alt_baro": 2000, "gs": 240.0,
               "track": 90.0, "lat": HOME[0], "lon": HOME[1] + 6 / (60 * 0.7534)}
        high = {"hex": "c2", "flight": "N55555", "r": "N55555", "t": "GLEX", "alt_baro": 34000, "gs": 430.0,
                "track": 0.0, "lat": HOME[0] - 0.05, "lon": HOME[1] + 0.03}
        planes = Planes(home=lambda: HOME, get_json=FakeNet([low, high]), clock=lambda: 1000.0)
        said = planes.answer("that")
        self.assertTrue(said.startswith("That's probably a Cessna 172 (N12345), 2,000 feet up, 7 miles east, "
                                        "and it passed right over the house a minute ago."), said)
        self.assertNotIn("trackers can't see", said)
        alone = Planes(home=lambda: HOME, get_json=FakeNet([high]), clock=lambda: 1000.0).answer("that")
        self.assertIn("34,000 feet up", alone)
        self.assertIn("may be one the trackers can't see", alone)  # a loud low one heard isn't a jet that high

    def test_military_farther_out(self):
        herc = {"hex": "ae1", "flight": "ANVIL30", "t": "C30J", "dbFlags": 1, "alt_baro": 2400, "lat": HOME[0] + 1.5,
                "lon": HOME[1]}
        net = FakeNet([herc, dict(SKY[0])])
        said = Planes(home=lambda: HOME, get_json=net, clock=lambda: 1000.0).answer("military")
        self.assertIn("/150", net.asked[0])  # (looks ~170 miles out, not 40)
        self.assertTrue(said.startswith("1 military aircraft within 173 miles."), said)
        self.assertIn("A military C-130J Hercules (ANVIL30), 2,400 feet up, 104 miles north", said)
        self.assertFalse(any("adsbdb" in u for u in net.asked))  # (no route lookups for the airliners out there)
        none = Planes(home=lambda: HOME, get_json=FakeNet([dict(SKY[0])]), clock=lambda: 1000.0).answer("military")
        self.assertIn("don't see any military aircraft", none)
        for text in ("any military planes around?", "are there any C-130s nearby", "is there a C 130 around"):
            self.assertEqual(plane_question(text), "military", text)

    def test_whats_around(self):
        said = self.planes.answer("list")
        self.assertTrue(said.startswith("5 planes within 29 miles."), said)
        self.assertEqual(said.count("• "), 3)
        self.net.aircraft = [dict(SKY[1])]
        self.now += 60
        said = self.planes.answer("list")
        self.assertIn("Southwest Airlines flight 3232, a Boeing 737 from Chicago to Denver, 36,000 feet up", said)
        self.assertNotIn("San Francisco", said)
        self.assertIn("A military C-17", Planes(home=lambda: HOME, get_json=FakeNet([SKY[4]])).answer("list"))

    def test_fetched_at_most_every_few_seconds(self):
        self.planes.nearby()
        self.planes.nearby()
        self.assertEqual(sum("/v2/point/" in u for u in self.net.asked), 1)
        self.assertEqual(sum("adsbdb" in u for u in self.net.asked), 3)  # DAL1776, SWA3232, RCH123 (not N2628U)
        self.now += 30
        self.planes.nearby()
        self.assertEqual(sum("/v2/point/" in u for u in self.net.asked), 2)
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


class TestPlanesNearTheAsker(unittest.TestCase):
    """Asked from a phone away from home: the planes over that person, not over the house."""

    def setUp(self):
        self.net = FakeNet([dict(a) for a in SKY])
        self.planes = Planes(home=lambda: HOME, get_json=self.net, clock=lambda: 1000.0)

    def test_away_from_home_looks_over_them(self):
        from sentinel.engine.planes import ASKER

        away = (41.8, -87.6)  # ~45 miles off
        token = ASKER.set(away)
        try:
            said = self.planes.answer("list")
        finally:
            ASKER.reset(token)
        self.assertIn("/41.8000/-87.6000/", self.net.asked[-1] if "/v2/point/" in self.net.asked[-1]
                      else next(u for u in self.net.asked if "/v2/point/" in u))
        self.assertIn("miles of you", said)
        view = self.planes.map_view(at=away)
        self.assertEqual((view["center"], view["away"], view["home"]), ([41.8, -87.6], True, list(HOME)))

    def test_near_home_is_still_the_house(self):
        from sentinel.engine.planes import ASKER

        token = ASKER.set((HOME[0] + 0.02, HOME[1]))  # in the yard, a mile off at most
        try:
            said = self.planes.answer("list")
        finally:
            ASKER.reset(token)
        self.assertNotIn("of you", said)
        self.assertTrue(all(f"/{HOME[0]:.4f}/{HOME[1]:.4f}/" in u for u in self.net.asked if "/v2/point/" in u))
        self.assertFalse(self.planes.map_view()["away"])

    def test_no_home_but_a_phone(self):
        planes = Planes(home=lambda: None, get_json=self.net, clock=lambda: 1000.0)
        self.assertTrue(planes.map_view(at=(41.8, -87.6))["away"])
        self.assertEqual(planes.map_view()["error"], "Save Home on the Family page first.")

class TestWhoIsAsking(unittest.TestCase):
    def test_telegram_name_or_login_to_their_phone(self):
        import time
        import types

        from sentinel.engine.engine import Engine
        from sentinel.engine.location import Person

        people = [Person(1, "Alex", aliases=["Dad"]), Person(2, "Grace", tracker="!a1b2c3d4")]
        fixes = {1: {"ts": time.time() - 60, "lat": 41.8, "lon": -87.6}, 2: {"ts": time.time(), "lat": 1.0, "lon": 1.0}}
        fake = types.SimpleNamespace(locations=types.SimpleNamespace(people=lambda: people, fix=fixes.get))
        self.assertEqual(Engine.asker_spot(fake, name="Alex"), (41.8, -87.6))
        self.assertEqual(Engine.asker_spot(fake, person_id=1), (41.8, -87.6))
        self.assertIsNone(Engine.asker_spot(fake, name="Grace"))  # (a dog's collar isn't someone asking)
        self.assertIsNone(Engine.asker_spot(fake, name="Ben"))
        self.assertIsNone(Engine.asker_spot(fake))
        fixes[1]["ts"] = time.time() - 7200  # a phone silent for two hours: not where they are now
        self.assertIsNone(Engine.asker_spot(fake, name="Alex"))

if __name__ == "__main__":
    unittest.main()
