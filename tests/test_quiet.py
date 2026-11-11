"""Quiet cameras (sentinel/engine/quiet.py): one camera's alerts paused for a while, by voice or Telegram."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest

from sentinel.engine.quiet import QuietCameras, quiet_command, say_minutes

CAMS = [("Garage", ["Driveway", "Street"]), ("Front Door", ["Porch"]), ("Rear Door", [])]


class TestQuietCommand(unittest.TestCase):
    def test_pause_one_camera(self):
        self.assertEqual(quiet_command("Pause the garage camera notifications.", CAMS), ("pause", ["Garage"], 60))
        self.assertEqual(quiet_command("mute the garage camera for two hours", CAMS), ("pause", ["Garage"], 120))
        self.assertEqual(quiet_command("Pause garage alerts for 45 minutes", CAMS), ("pause", ["Garage"], 45))
        self.assertEqual(quiet_command("stop the alerts from the driveway for half an hour", CAMS), ("pause", ["Garage"], 30))
        self.assertEqual(quiet_command("silence the front door camera for an hour and a half", CAMS),
                         ("pause", ["Front Door"], 90))

    def test_several_and_all(self):
        self.assertEqual(quiet_command("pause the garage and front door cameras for 3 hours", CAMS),
                         ("pause", ["Garage", "Front Door"], 180))
        self.assertEqual(quiet_command("pause all the cameras for 20 minutes", CAMS),
                         ("pause", ["Garage", "Front Door", "Rear Door"], 20))

    def test_resume_and_which(self):
        self.assertEqual(quiet_command("resume the garage camera", CAMS), ("resume", ["Garage"], 0))
        self.assertEqual(quiet_command("turn the garage alerts back on", CAMS), ("resume", ["Garage"], 0))
        self.assertEqual(quiet_command("unmute all cameras", CAMS)[0], "resume")
        self.assertEqual(quiet_command("which cameras are paused?", CAMS), ("which", [], 0))

    def test_other_requests_are_left_alone(self):
        for text in ("pause", "Pause 30", "pause the music", "resume", "pause the TV", "show me the garage camera",
                     "did anyone come to the front door?", "turn off the garage light", "stop recording the garage camera",
                     "show the garage camera on the TV", "turn on the porch light", "clip garage"):
            self.assertIsNone(quiet_command(text, CAMS), text)


class TestQuietCameras(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-quiet-")
        self.now = 1000.0
        self.quiet = QuietCameras(self.dir, clock=lambda: self.now)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_paused_then_back_by_itself(self):
        said = self.quiet.run(("pause", ["Garage"], 120))
        self.assertEqual(said, "Okay, no alerts from the Garage camera for 2 hours. It keeps recording.")
        self.assertTrue(self.quiet.is_quiet("Garage"))
        self.assertFalse(self.quiet.is_quiet("Front Door"))
        self.now += 119 * 60
        self.assertEqual(self.quiet.view(), {"Garage": 1})
        self.now += 61
        self.assertFalse(self.quiet.is_quiet("Garage"))
        self.assertEqual(self.quiet.run(("which", [], 0)), "No camera's alerts are paused.")

    def test_resume_and_a_restart_keeps_it(self):
        self.quiet.run(("pause", ["Garage", "Front Door"], 60))
        again = QuietCameras(self.dir, clock=lambda: self.now)
        self.assertTrue(again.is_quiet("Front Door"))
        self.assertEqual(again.run(("resume", ["Garage", "Rear Door"], 0)), "Okay, alerts from Garage are back on.")
        self.assertFalse(again.is_quiet("Garage"))
        self.assertIn("Front Door (1 hour left)", again.run(("which", [], 0)))
        self.assertEqual(again.run(("resume", ["Rear Door"], 0)), "Those alerts weren't paused.")

    def test_said(self):
        self.assertEqual(say_minutes(90), "1 hour and 30 minutes")
        self.assertEqual(say_minutes(45), "45 minutes")


class TestThroughTheEngine(unittest.TestCase):
    """Said to Jarvis or typed on Telegram, with a real engine and stand-in cameras."""

    @classmethod
    def setUpClass(cls):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        cls.dir = tempfile.mkdtemp(prefix="sentinel-quiet-engine-")
        cls.old_cwd = os.getcwd()
        os.chdir(cls.dir)
        sm = SettingsManager(settings_file=os.path.join(cls.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": "123456789:" + "A" * 35, "telegram_chat_id": "111", "volume": 0,
                            "telegram_enabled": False})
        cls.engine = Engine(sm)
        cls.engine.cameras = {name: types.SimpleNamespace(display_name=name, profile={"zone_names": zones})
                              for name, zones in CAMS}

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.engine.quiet.run(("resume", [name for name, _ in CAMS], 0))

    def test_said_to_jarvis(self):
        reply = self.engine.answer_voice("Pause the garage camera notifications.", lambda _text: None)
        self.assertEqual(reply.text, "Okay, no alerts from the Garage camera for 1 hour. It keeps recording.")
        self.assertTrue(self.engine.quiet.is_quiet("Garage"))
        self.assertFalse(self.engine.quiet.is_quiet("Front Door"))
        self.engine.answer_voice("Turn the garage alerts back on", lambda _text: None)
        self.assertFalse(self.engine.quiet.is_quiet("Garage"))

    def test_typed_on_telegram(self):
        telegram = self.engine.telegram
        telegram.question_filter = self.engine.commands.claims  # (as Engine.start sets them)
        telegram.switch_filter = self.engine.switches.understands
        for text in ("Pause the garage camera for 2 hours", "mute the driveway alerts", "Resume the garage camera",
                     "which cameras are paused?"):
            telegram._pending_questions.clear()
            telegram.route_text("111", text)
            self.assertEqual(telegram._pending_questions, [("111", text)], text)  # (goes to the shared commands)
        telegram._pending_questions.clear()
        self.assertTrue(self.engine.commands.claims("Pause the garage camera for 2 hours"))
        self.assertFalse(self.engine.commands.claims("pause"))


if __name__ == "__main__":
    unittest.main()
