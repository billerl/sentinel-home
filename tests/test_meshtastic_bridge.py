"""The LoRa base station's relay: a collar tracker's Meshtastic position as a family phone's OwnTracks message."""

import _test_env  # noqa: F401  (isolates the event log)

import importlib.util
import os
import unittest
from types import SimpleNamespace

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
        self.assertEqual(B.commands([{"_type": "cmd", "action": "reportLocation"}, {"_type": "cmd", "action": "beep"}]),
                         {"reportLocation", "beep"})
        self.assertEqual(B.commands([]), set())

    def test_chat_text(self):
        nodes = {"!b2079b56": {"user": {"longName": "Ben's radio"}}}
        packet = {"id": 5, "fromId": "!b2079b56", "toId": "!3248f6ed", "rxTime": 1_800_000_000,
                  "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
        self.assertEqual(B.mesh_text(packet, nodes), {"packet": 5, "from": "!b2079b56", "to": "!3248f6ed", "channel": 0,
                                                       "name": "Ben's radio", "text": "hi", "ts": 1_800_000_000})
        self.assertEqual(B.mesh_text(dict(packet, decoded={"portnum": "TEXT_MESSAGE_APP", "text": B.BELL}), nodes), {})
        self.assertEqual(B.mesh_text(dict(packet, decoded={"portnum": "POSITION_APP"}), nodes), {})

    def test_channels_and_where_a_direct_message_goes(self):
        def ch(index, role, name, psk):
            return SimpleNamespace(index=index, role=role, settings=SimpleNamespace(name=name, psk=psk))
        channels = B.channel_list([ch(0, 1, "Sentinel", b"k" * 32), ch(1, 2, "", b"\x01"), ch(2, 2, "Family", b"f" * 32),
                                   ch(3, 0, "", b"")])
        self.assertEqual([(c["index"], c["name"], c["public"]) for c in channels],
                         [(0, "Sentinel", False), (1, "LongFast", True), (2, "Family", False)])
        self.assertEqual(B.dm_channel({}, channels), 1)  # the public channel, never the collars' own
        self.assertEqual(B.dm_channel({"channel": 2}, channels), 2)

    def test_send_status(self):
        ack = {"fromId": "!b2079b56", "decoded": {"routing": {"errorReason": "NONE"}}}
        self.assertEqual(B.send_status(ack, "!b2079b56"), {"status": "delivered"})
        self.assertEqual(B.send_status(dict(ack, fromId="!3248f6ed"), "^all"), {"status": "relayed"})
        nak = {"fromId": "!3248f6ed", "decoded": {"routing": {"errorReason": "MAX_RETRANSMIT"}}}
        self.assertEqual(B.send_status(nak, "!b2079b56"), {"status": "failed", "error": "no answer from their radio"})

    def test_node_list_leaves_out_the_base(self):
        nodes = {"!3248f6ed": {"user": {"id": "!3248f6ed"}, "lastHeard": 9},
                 "!b2079b56": {"user": {"id": "!b2079b56", "longName": "Ben", "publicKey": "abc"}, "lastHeard": 5,
                               "hopsAway": 2}}
        self.assertEqual([(n["id"], n["pki"], n["hops"]) for n in B.node_list(nodes, "!3248f6ed")],
                         [("!b2079b56", True, 2)])


if __name__ == "__main__":
    unittest.main()
