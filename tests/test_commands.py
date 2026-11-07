"""The command registry: one place for what both Telegram and voice understand."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import unittest

from sentinel.engine.commands import Command, Commands, parse_feature_toggle

HOME = (41.1145, -88.1780)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.c = Commands(lambda chat, text: self.sent.append((chat, text)))

    def test_first_match_wins_on_telegram(self):
        self.c.add(Command("a", lambda t: t if t.startswith("hi") else None, lambda m, _t: "A " + m, prefix="🐕 "))
        self.c.add(Command("b", lambda t: t, lambda _m, _t: "B"))
        self.assertTrue(self.c.telegram_reply("1", "hi there"))
        self.assertTrue(self.c.telegram_reply("1", "other"))
        self.assertEqual(self.sent, [("1", "🐕 A hi there"), ("1", "B")])

    def test_empty_but_not_none_still_matches(self):  # "any movement inside?" understands [] = every sensor
        self.c.add(Command("indoor", lambda t: [] if "inside" in t else None, lambda _m, _t: "quiet",
                           speak=lambda s: s, voice_stage="late"))
        self.assertTrue(self.c.claims("anything inside"))
        self.assertEqual(self.c.voice_reply("anything inside", "late"), "quiet")
        self.assertFalse(self.c.telegram_reply("1", "outside"))

    def test_claim_if_only_affects_claiming(self):
        waiting = []
        self.c.add(Command("pkg", lambda t: t == "got it" or None, lambda _m, _t: "ok", claim_if=lambda: bool(waiting)))
        self.assertFalse(self.c.claims("got it"))
        waiting.append(1)
        self.assertTrue(self.c.claims("got it"))
        self.assertTrue(self.c.telegram_reply("1", "got it"))

    def test_voice_stages_and_guards(self):
        self.c.add(Command("tg only", lambda t: t, lambda _m, _t: "x"))
        self.c.add(Command("early", lambda t: t == "a" or None, lambda _m, _t: "E", speak=str.lower,
                           voice_if=lambda t: t != "b"))
        self.c.add(Command("late", lambda t: t in ("a", "b") or None, lambda _m, _t: "L", speak=str.lower,
                           voice_stage="late"))
        self.assertEqual(self.c.voice_reply("a", "early"), "e")
        self.assertEqual(self.c.voice_reply("a", "late"), "l")
        self.assertIsNone(self.c.voice_reply("b", "early"))
        self.assertIsNone(self.c.voice_reply("zzz", "early"))  # the Telegram-only command never speaks


class TestFeatureToggle(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_feature_toggle("Disable clips, recording"), ({"clips": False, "recording": False}, None))
        self.assertEqual(parse_feature_toggle("enable night bug filter"), ({"night_bug": True}, None))
        self.assertEqual(parse_feature_toggle("hello"), (None, None))
        self.assertIn("lasers", parse_feature_toggle("enable lasers")[1])


class _FakeTelegram:
    chat_ids = ["111"]

    def __init__(self):
        self.texts = []
        self.pins = []

    def reply_to_chat(self, _chat, text):
        self.texts.append(text)
        return True

    def send_location(self, _chat, lat, lon):
        self.pins.append((lat, lon))
        return True


class _FakePackages:
    def __init__(self):
        self.waiting = {"Front Door": 1}

    def packages(self):
        return dict(self.waiting)

    def clear(self):
        n, self.waiting = sum(self.waiting.values()), {}
        return n


class TestOnTheEngine(unittest.TestCase):
    """Both channels give the same answers, each in its own style."""

    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-commands-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.engine = Engine(SettingsManager(settings_file=os.path.join(self.dir, "settings.json")))
        self.engine.telegram = _FakeTelegram()
        loc = self.engine.locations
        loc.save_place("Home", *HOME)
        liz = loc.person(loc.save_person("Elizabeth"))
        self.xena = loc.save_person("Xena")
        loc.set_tracker(self.xena, "!a1cacb2a")
        loc.record(liz, {"_type": "location", "lat": HOME[0], "lon": HOME[1], "acc": 10, "tst": int(time.time())})

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def voice(self, text):
        reply = self.engine.answer_voice(text, lambda _s: None)
        return None if reply is None else reply.text

    def test_pets(self):
        self.assertTrue(self.engine.commands.claims("The dogs are going out"))
        self.engine.answer_question("111", "The dogs are going out")
        self.assertTrue(self.engine.telegram.texts[-1].startswith("🐕 OK, I'll keep a close eye on Xena"))
        self.assertIn(self.xena, self.engine.family._outings)
        self.assertTrue(self.voice("the dogs are in").startswith("OK, Xena is in."))
        self.assertNotIn(self.xena, self.engine.family._outings)
        self.assertEqual(self.voice("Recall Xena"), "Beeping Xena's collar.")
        self.assertIn(self.xena, self.engine.family._beeps)

    def test_package_taken_in(self):
        self.assertFalse(self.engine.commands.claims("I got the package"))  # nothing waiting: not ours to take
        self.engine.packages = _FakePackages()
        self.assertTrue(self.engine.commands.claims("I got the package"))
        self.assertNotEqual(self.voice("got it"), "Okay, the package is marked as taken in: no more reminders.")
        self.assertEqual(self.voice("I got the package"), "Okay, the package is marked as taken in: no more reminders.")
        self.engine.packages = _FakePackages()
        self.engine.answer_question("111", "the package is in")
        self.assertEqual(self.engine.telegram.texts[-1], "✅ Okay, the package is marked as taken in: no more reminders.")

    def test_location(self):
        self.engine.answer_question("111", "where is Elizabeth?")
        self.assertEqual(self.engine.telegram.texts[-1], "Elizabeth is home.")
        self.assertEqual(self.voice("where is Elizabeth"), "Elizabeth is home.")
        # a routine about someone is not a question about them
        self.assertNotIn("Elizabeth is", self.voice("new routine when Elizabeth gets home turn on the patio lights"))

    def test_recap_is_telegram_only(self):
        ran = []
        self.engine.recap.run = lambda chat=None: ran.append(chat)
        self.engine.answer_question("111", "Recap")
        self.assertEqual(ran, ["111"])
        self.assertIsNone(self.engine.commands.voice_reply("recap", "early"))


if __name__ == "__main__":
    unittest.main()
