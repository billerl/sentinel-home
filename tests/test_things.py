"""Where things are: what's understood, and the store (sentinel/engine/things.py)."""

import _test_env  # noqa: F401  (isolates the event log)

import shutil
import tempfile
import unittest

from sentinel.engine.things import Things, is_list, key, parse_find, parse_forget, parse_put


class TestWords(unittest.TestCase):
    def test_telling_where_a_thing_is(self):
        self.assertEqual(parse_put("My darts are in the garage cabinet."), ("darts", "in the garage cabinet", "are"))
        self.assertEqual(parse_put("I put the 10 millimeter socket in the red toolbox"),
                         ("10 millimeter socket", "in the red toolbox", ""))
        self.assertEqual(parse_put("remember that the stepper motors are in bin 4 on the shelf"),
                         ("stepper motors", "in bin 4 on the shelf", "are"))
        self.assertEqual(parse_put("we keep the batteries in the junk drawer"), ("batteries", "in the junk drawer", ""))
        self.assertEqual(parse_put("I left my keys on the kitchen counter"), ("keys", "on the kitchen counter", ""))
        self.assertEqual(parse_put("remember my darts"), ("darts", None, ""))  # asks where
        self.assertEqual(parse_put("keep track of the stepper motors"), ("stepper motors", None, ""))

    def test_other_peoples_sentences(self):
        for said in ("remember to call Kate", "my car is in the driveway", "the tv is on", "my wife is at work",
                     "remember the porch light is on", "I put it in the garage", "turn on the porch light",
                     "the heater is on high", "remind me to buy darts", "play some music"):
            self.assertIsNone(parse_put(said), said)
        for said in ("where is my car", "what time is it", "who's home", "where is everyone"):
            self.assertIsNone(parse_find(said), said)
        for said in ("forget it", "never mind"):
            self.assertIsNone(parse_forget(said), said)

    def test_asking(self):
        self.assertEqual(parse_find("Where are my darts?"), ("darts", ""))
        self.assertEqual(parse_find("where did I put the 10 mm socket"), ("10 mm socket", "explicit"))
        self.assertEqual(parse_find("do you know where my darts are"), ("darts", "explicit"))
        self.assertEqual(parse_find("where do I keep the batteries"), ("batteries", "explicit"))
        self.assertEqual(parse_find("find my darts"), ("darts", "explicit"))
        self.assertEqual(parse_find("where is Kate"), ("kate", ""))  # (only answered if a thing is called that)
        self.assertEqual(parse_forget("forget where my darts are"), "darts")
        self.assertTrue(is_list("what things do you know"))
        self.assertTrue(is_list("list my things"))
        self.assertFalse(is_list("list my reminders"))

    def test_names_are_compared_loosely(self):
        self.assertEqual(key("My 10 Millimeter sockets"), "10 mm socket")
        self.assertEqual(key("ten mm socket"), "10 mm socket")
        self.assertEqual(key("10mm socket"), "10 mm socket")
        self.assertEqual(key("the batteries"), "battery")
        self.assertEqual(key("glass"), "glass")


class TestThings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-things-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.now = [1000.0]
        self.things = Things(self.dir, clock=lambda: self.now[0])

    def test_kept_found_moved_and_forgotten(self):
        self.assertEqual(self.things.put("darts", "in my garage cabinet", "are", "Alex"),
                         (True, "Okay, the darts are in your garage cabinet."))
        self.assertEqual(self.things.put("10 millimeter socket", "in the red toolbox")[1],
                         "Okay, the 10 millimeter socket is in the red toolbox.")
        self.assertEqual(self.things.answer("darts"), "The darts are in your garage cabinet.")
        self.assertEqual(self.things.answer("ten mm sockets"), "The 10 millimeter socket is in the red toolbox.")
        self.assertEqual(self.things.answer("socket"), "The 10 millimeter socket is in the red toolbox.")
        self.assertEqual(self.things.answer("dart"), "The darts are in your garage cabinet.")
        self.things.put("my darts", "on the shelf by the dartboard", "are")  # moved: still one of them
        self.assertEqual(len(self.things.all()), 2)
        self.assertEqual(self.things.answer("darts"), "Your darts are on the shelf by the dartboard.")
        self.assertIsNone(self.things.answer("kate", explicit=False))  # not a thing: somebody else's question
        self.assertIn("don't know where the hammer is yet", self.things.answer("hammer"))
        self.assertEqual(self.things.forget("darts")[0], True)
        self.assertEqual(self.things.forget("darts")[0], False)
        self.assertIn("1 thing is", self.things.summary())

    def test_several_that_fit_and_near_spellings(self):
        self.things.put("socket wrench", "in the truck")
        self.things.put("10 mm socket", "in the red toolbox")
        self.things.put("stepper motors", "in bin 4", "are")
        said = self.things.answer("socket")
        self.assertTrue(said.startswith("I know of 2: "), said)
        self.assertEqual(self.things.answer("stopper motors"), "The stepper motors are in bin 4.")

    def test_the_pages_edits(self):
        self.assertEqual(self.things.put("", "in the box"), (False, "What is the thing called?"))
        self.assertEqual(self.things.put("tape", ""), (False, "Where is it?"))
        self.things.put("tape", "junk drawer", by="Kate")  # typed without "in"
        thing = self.things.all()[0]
        self.assertEqual((thing["place"], thing["by"], thing["verb"]), ("in junk drawer", "Kate", "is"))
        self.assertTrue(self.things.put("duct tape", "on the workbench", thing_id=thing["id"])[0])
        self.assertEqual([t["name"] for t in self.things.all()], ["duct tape"])
        self.assertFalse(self.things.put("x", "in y", thing_id=999)[0])
        self.assertTrue(self.things.forget(thing_id=thing["id"])[0])
        self.assertEqual(self.things.all(), [])


class TestFromAPhoto(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np

        self.dir = tempfile.mkdtemp(prefix="sentinel-things-photo-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.things = Things(self.dir)
        self.jpeg = cv2.imencode(".jpg", np.full((2000, 3000, 3), 120, np.uint8))[1].tobytes()
        self.reply = '{"items": ["10 mm socket", "needle-nose pliers", "10 millimeter sockets"], ' \
                     '"unsure": [{"looks_like": "small black cylinder, front left", "guess": "capacitor"}, {"guess": "x"}]}'
        self.asked = []

    def scans(self):
        from sentinel.engine.things import PhotoScans

        def look(system, prompt, jpeg, what):
            self.asked.append((prompt, len(jpeg), what))
            return self.reply
        return PhotoScans(self.things, self.dir, look, lambda: "today's limit of Claude questions is used up")

    def test_claude_lists_and_the_person_decides(self):
        from sentinel.engine.things import parse_seen

        self.assertEqual(parse_seen("Sure! " + self.reply)["items"], ["10 mm socket", "needle-nose pliers"])  # (same thing once)
        self.assertIsNone(parse_seen("I can't see the photo."))
        scans = self.scans()
        self.things.put("needle-nose pliers", "in the truck")  # another pair, somewhere else: must stay there
        ok, sid = scans.start(self.jpeg, "top drawer of the red toolbox", "Alex", wait=True)
        self.assertTrue(ok)
        self.assertEqual(self.asked[0][0], "This is in top drawer of the red toolbox. What is kept here?")
        self.assertLess(self.asked[0][1], len(self.jpeg))  # shrunk before it leaves
        view = scans.view(sid)
        self.assertEqual((view["state"], view["items"], view["unsure"]),
                         ("ready", ["10 mm socket", "needle-nose pliers"],
                          [{"looks_like": "small black cylinder, front left", "guess": "capacitor"}]))
        self.assertIsNotNone(scans.photo_path(view["photo"]))
        self.assertEqual(len(self.things.all()), 1)  # nothing kept until the person says
        self.assertEqual(scans.save(sid, ["needle-nose pliers", "capacitors", "", 7]),
                         (True, "Kept 2 things in top drawer of the red toolbox."))
        kept = {(t["name"], t["place"]) for t in self.things.all()}
        self.assertEqual(kept, {("needle-nose pliers", "in the truck"), ("capacitors", "in top drawer of the red toolbox"),
                                ("needle-nose pliers", "in top drawer of the red toolbox")})
        self.assertTrue(all(t["photo"] == view["photo"] for t in self.things.all() if "drawer" in t["place"]))
        self.assertTrue(self.things.answer("pliers").startswith("I know of 2: "))
        self.assertIsNone(scans.view(sid))
        self.assertFalse(scans.save(sid, ["x"])[0])
        # said again by voice: it moved, and the old drawer's photo goes with it
        self.things.put("capacitors", "in bin 9")
        self.assertEqual([t["photo"] for t in self.things.all() if t["name"] == "capacitors"], [""])

    def test_what_is_refused_and_what_fails(self):
        scans = self.scans()
        self.assertEqual(scans.start(self.jpeg, "")[0], False)
        self.assertEqual(scans.start(b"not a picture", "the shelf"), (False, "That doesn't look like a photo."))
        self.assertIsNone(scans.photo_path("../settings.json"))
        self.reply = None  # Claude is off, or today's questions are used up
        ok, sid = scans.start(self.jpeg, "the shelf", wait=True)
        self.assertEqual((scans.view(sid)["state"], scans.view(sid)["error"]),
                         ("failed", "today's limit of Claude questions is used up"))
        photo = scans.view(sid)["photo"]
        scans.cancel(sid)
        self.assertIsNone(scans.view(sid))
        self.assertIsNotNone(photo)


class TestEngineSide(unittest.TestCase):
    """What the command registry takes (Engine._things_understood), without a whole engine."""

    def setUp(self):
        import types

        from sentinel.engine.engine import Engine

        self.dir = tempfile.mkdtemp(prefix="sentinel-things-engine-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.engine = types.SimpleNamespace(things=Things(self.dir))
        self.understood = lambda text: Engine._things_understood(self.engine, text)
        self.answer = lambda text: Engine._things_answer(self.engine, self.understood(text), "Telegram")
        self.voice = lambda text: Engine._things_voice(self.engine, text)

    def test_only_its_own_sentences(self):
        self.assertIsNone(self.understood("where is kate"))  # nobody keeps a thing called Kate: the family's question
        self.assertIsNone(self.understood("forget the routine"))
        self.assertEqual(self.answer("my darts are in the garage cabinet"), "Okay, the darts are in the garage cabinet.")
        self.assertEqual(self.answer("where are my darts"), "The darts are in the garage cabinet.")
        self.assertEqual(self.answer("where is the dart"), "The darts are in the garage cabinet.")
        self.assertIn("don't know where the hammer is yet", self.answer("where did i put the hammer"))
        self.assertIn("Where are they?", self.answer("remember my stepper motors"))  # (Telegram: told how to say it)
        self.assertIn("1 thing is", self.answer("list my things"))
        self.assertIn("forgotten", self.answer("forget where my darts are"))
        self.assertIsNone(self.understood("where are my darts"))  # gone: back to being anybody's question

    def test_by_voice_it_asks_and_takes_the_next_words(self):
        self.assertIsNone(self.voice("my darts are in the garage cabinet"))  # complete: the registry's
        reply = self.voice("remember my darts")
        self.assertEqual(reply.text, "Where are they?")
        self.assertEqual(reply.dictate("They're in the garage cabinet."), "Okay, the darts are in the garage cabinet.")
        self.assertEqual(self.voice("keep track of the tape measure").dictate("I put it on the workbench"),
                         "Okay, the tape measure is on the workbench.")
        self.assertIn("didn't catch", self.voice("remember my darts").dictate(""))


if __name__ == "__main__":
    unittest.main()
