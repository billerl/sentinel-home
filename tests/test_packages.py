"""Package alerts: a parcel left after someone came to the door, a reminder at sunset, and what's not a parcel."""

import _test_env  # noqa: F401  (isolates the event log)

import tempfile
import types
import unittest

import numpy as np

from sentinel.engine import packages as P

BOX = (500, 400, 80, 60)  # where the parcel is left


def porch(parcel=False, clock_digits=0, seed=7):
    """A textured porch picture; optionally with a parcel, and the camera's clock showing other digits."""
    rng = np.random.default_rng(seed)
    frame = (rng.integers(60, 120, (720, 1280, 1)).repeat(3, axis=2)).astype(np.uint8)
    if parcel:
        x, y, w, h = BOX
        frame[y:y + h, x:x + w] = (60, 140, 200)  # brown cardboard
        frame[y + 20:y + 40, x + 20:x + 60] = 240  # its label
    if clock_digits:
        frame[20:50, 20:300] = 255 if clock_digits % 2 else 0
    return frame


class FakeChecker:
    available = True

    def probability(self, frame, box):  # a parcel where one was left, nothing elsewhere
        return 0.95 if P._iou(box, BOX) > 0.3 else 0.1


class TestPackages(unittest.TestCase):
    def setUp(self):
        self.people = False
        detector = types.SimpleNamespace(detect=lambda frame, min_score: [
            types.SimpleNamespace(group="person", box=(100, 100, 50, 120))] if self.people else [])
        self.sent = []
        self.sunset = None
        self.away = False
        self.waiting, self.gone = [], []
        self.watch = P.PackageWatch({}, lambda: [], detector, FakeChecker(),
                                    lambda text, jpeg, ask_done=False: self.sent.append(text), lambda now: self.sunset,
                                    lambda: self.away, on_waiting=self.waiting.append, on_gone=self.gone.append)
        self.t = 1000.0

    def look(self, frame, step=10.0):
        self.t += step
        self.watch.look("Front Door", frame, self.t)

    def deliver(self):
        self.look(porch())
        self.people = True
        self.look(porch())
        self.watch.visit("Front Door", self.t)
        self.people = False
        self.look(porch(parcel=True), 25)  # they've gone: something new by the door
        self.look(porch(parcel=True))  # ... still there next time: a parcel

    def test_a_delivery(self):
        self.deliver()
        self.assertEqual(self.sent, ["📦 A package was left at the Front Door."])
        for _ in range(5):
            self.look(porch(parcel=True))
        self.assertEqual(len(self.sent), 1)  # once
        self.assertEqual(self.watch.packages(), {"Front Door": 1})

    def test_got_it(self):
        self.deliver()
        self.look(porch(parcel=True))
        self.assertEqual(self.waiting[-1], "Front Door")  # repeating routines get their chance
        self.assertEqual(self.watch.clear(), 1)
        self.assertEqual(self.gone, ["Front Door"])
        n = len(self.waiting)
        self.sunset = self.t + 5
        self.look(porch(parcel=True))
        self.look(porch(parcel=True))
        self.assertEqual((len(self.waiting), len(self.sent)), (n, 1))  # no reminders, no repeats
        for text in ("I got the package", "the package is in", "got it"):
            self.assertTrue(P.TAKEN_IN.match(text), text)
        self.assertFalse(P.TAKEN_IN.match("did I get a package?"))

    def test_a_delivery_during_a_restart(self):
        """The quiet picture from before the restart is the reference: a parcel left meanwhile is new."""
        with tempfile.TemporaryDirectory() as d:
            self.watch.state_dir = d
            self.watch.clock = lambda: self.t
            self.look(porch())
            self.watch.stop()
            later = P.PackageWatch({}, lambda: [], self.watch.detector, FakeChecker(),
                                   lambda text, jpeg, ask_done=False: self.sent.append(text), lambda now: None,
                                   lambda: False, clock=lambda: self.t, state_dir=d)
            self.watch = later
            self.watch.visit("Front Door", self.t + 15)  # the camera saw the driver just after the restart
            self.look(porch(parcel=True), 40)  # first quiet snapshot after the restart
            self.look(porch(parcel=True))
            self.assertEqual(self.sent, ["📦 A package was left at the Front Door."])

    def test_a_restart_alone_is_not_a_visit(self):
        """Sun shadows move while Sentinel restarts: with nobody seen, a change isn't a parcel (2026-11-09)."""
        with tempfile.TemporaryDirectory() as d:
            self.watch.state_dir = d
            self.watch.clock = lambda: self.t
            self.look(porch())
            self.watch.stop()
            self.watch = P.PackageWatch({}, lambda: [], self.watch.detector, FakeChecker(),
                                        lambda text, jpeg, ask_done=False: self.sent.append(text), lambda now: None,
                                        lambda: False, clock=lambda: self.t, state_dir=d)
            for _ in range(4):
                self.look(porch(parcel=True), 40)
            self.assertEqual(self.sent, [])

    def test_an_old_saved_picture_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            self.watch.state_dir = d
            self.watch.clock = lambda: self.t
            self.look(porch())
            self.watch.stop()
            self.watch = P.PackageWatch({}, lambda: [], self.watch.detector, FakeChecker(),
                                        lambda text, jpeg, ask_done=False: self.sent.append(text), lambda now: None,
                                        lambda: False, state_dir=d)
            self.look(porch(parcel=True), P.REFERENCE_KEEP + 60)
            self.look(porch(parcel=True))
            self.assertEqual(self.sent, [])

    def test_nothing_without_someone_at_the_door(self):
        """A cat across the mat at night, sun shadows, a bug on the lens: no person, no parcel."""
        self.look(porch())
        self.look(porch(parcel=True))
        self.look(porch(parcel=True))
        self.assertEqual(self.sent, [])

    def test_not_while_they_are_still_there(self):
        self.look(porch())
        self.watch.visit("Front Door", self.t + 5)
        self.look(porch(parcel=True))  # 5 s after the camera last saw them: maybe still standing there
        self.look(porch(parcel=True), 3)
        self.assertEqual(self.sent, [])

    def test_a_reminder_at_sunset_and_taken_in(self):
        self.deliver()
        self.sunset = self.t + 5
        self.look(porch(parcel=True))
        self.look(porch(parcel=True))
        self.assertEqual(self.sent[1:], ["📦 Reminder: the package is still at the Front Door."])
        for _ in range(P.GONE_SNAPSHOTS):
            self.look(porch())  # the family took it in: nothing said
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.watch.packages(), {})

    def test_gone_while_nobody_is_home(self):
        self.deliver()
        self.away = True
        for _ in range(P.GONE_SNAPSHOTS):
            self.look(porch())
        self.assertEqual(self.sent[-1], "📦 The package at the Front Door is gone, and nobody is home.")

    def test_the_clock_and_the_edges_are_not_changes(self):
        self.assertEqual(P.changes(porch(clock_digits=1), porch(clock_digits=2)), [])
        edge = porch()
        edge[600:719, 1200:1280] = 250
        self.assertEqual(P.changes(porch(), edge), [])
        found = P.changes(porch(), porch(parcel=True))
        self.assertTrue(found and P._iou(found[0], BOX) > 0.5, found)

    def test_door_cameras_by_name(self):
        cams = [types.SimpleNamespace(display_name=n) for n in ("Front Door", "Garage", "Back Porch")]
        self.watch.cameras = lambda: cams
        self.assertEqual([c.display_name for c in self.watch.door_cameras()], ["Front Door", "Back Porch"])
        self.watch.settings["package_cameras"] = "garage"
        self.assertEqual([c.display_name for c in self.watch.door_cameras()], ["Garage"])


if __name__ == "__main__":
    unittest.main()
