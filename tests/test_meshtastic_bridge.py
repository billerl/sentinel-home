"""The LoRa base station's relay: a collar tracker's Meshtastic position as a family phone's OwnTracks message."""

import _test_env  # noqa: F401  (isolates the event log)

import importlib.util
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "meshtastic_bridge", os.path.join(os.path.dirname(__file__), "..", "tools", "meshtastic_bridge.py"))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


class TestBridge(unittest.TestCase):
    def test_exact_position(self):
        packet = {"fromId": "!a1cacb2a", "rxTime": 1_800_000_100,
                  "decoded": {"position": {"latitudeI": 411145000, "longitudeI": -881780000, "time": 1_800_000_000,
                                           "precisionBits": 32, "HDOP": 120, "groundSpeed": 2}}}
        msg = B.owntracks(packet, {"!a1cacb2a": 87}, 1_800_000_200)
        self.assertEqual((msg["lat"], msg["lon"], msg["tst"], msg["batt"], msg["vel"]),
                         (41.1145, -88.178, 1_800_000_000, 87, 7))
        self.assertEqual(msg["acc"], 6.0)

    def test_public_channel_blur_is_vague(self):
        """Shared on a public channel (precision 13: ~3 km), it must never move the dog in or out of the yard."""
        self.assertGreater(B.accuracy_m({"precisionBits": 13}), 1000)
        self.assertEqual(B.accuracy_m({}), 10.0)

    def test_sentinel_asks_for_a_position(self):
        self.assertTrue(B.wants_position([{"_type": "cmd", "action": "reportLocation"}]))
        self.assertFalse(B.wants_position([]))


if __name__ == "__main__":
    unittest.main()
