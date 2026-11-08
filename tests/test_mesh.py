"""Mesh chat: what the LoRa base station brings and takes at each check-in, and the Mesh page's view."""

import _test_env  # noqa: F401  (isolates the event log)

import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from sentinel.engine import mesh as M

BASE = {"id": "!3248f6ed", "name": "Grace's base", "short": "GB",
        "channels": [{"index": 0, "name": "Sentinel", "public": False, "primary": True},
                     {"index": 1, "name": "LongFast", "public": True, "primary": False},
                     {"index": 2, "name": "Family", "public": False, "primary": False}],
        "invites": {"contact": "https://meshtastic.org/v/#abc", "2": "https://meshtastic.org/e/?add=true#xyz"}}
PET = SimpleNamespace(tracker="!a1cacb2a")


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def unpaused_chats(self):
        return ["1"]

    def reply_to_chat(self, chat, text):
        self.sent.append(text)


class FakeSettings:
    def save_settings(self, force=False):
        pass


class TestMeshChat(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-mesh-")
        self.telegram = FakeTelegram()
        self.settings = {}
        self.chat = M.MeshChat(self.dir, self.settings, FakeSettings(),
                               lambda u, p: PET if (u, p) == ("grace", "secret") else None, self.telegram)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def checkin(self, **body):
        return self.chat.bridge_checkin("grace", "secret", dict({"base": BASE}, **body))

    def wait_for_telegram(self, n):
        deadline = time.time() + 2
        while len(self.telegram.sent) < n and time.time() < deadline:
            time.sleep(0.01)

    def test_wrong_login_and_phones_are_refused(self):
        self.assertIsNone(self.chat.bridge_checkin("grace", "nope", {}))
        chat = M.MeshChat(self.dir, {}, FakeSettings(), lambda u, p: SimpleNamespace(tracker=""), self.telegram)
        self.assertIsNone(chat.bridge_checkin("ben", "phone", {}))  # a family phone isn't the base station

    def test_direct_message_comes_to_telegram_once(self):
        heard = {"packet": 77, "from": "!b2079b56", "to": "!3248f6ed", "channel": 1, "name": "Ben's radio",
                 "text": "Dinner at 6?", "ts": time.time()}
        reply = self.checkin(received=[heard], nodes=[{"id": "!b2079b56", "name": "Ben's radio", "pki": True}])
        self.checkin(received=[heard])  # the base didn't hear Sentinel's answer and sent it again
        self.chat._trackers = lambda: ["!b2079b56"]
        self.assertEqual(self.chat.view()["nodes"], [])  # a pet's collar isn't someone to chat with
        self.chat._trackers = lambda: []
        self.wait_for_telegram(1)
        time.sleep(0.05)
        self.assertEqual(self.telegram.sent, ["📡 Ben's radio (mesh): Dinner at 6?"])
        self.assertEqual(reply["send"], [])
        view = self.chat.view()
        dm = next(c for c in view["chats"] if c["peer"] == "!b2079b56")
        self.assertEqual((dm["unread"], dm["private"], dm["last"]), (1, True, "Dinner at 6?"))
        self.assertEqual(len(self.chat.thread("!b2079b56")), 1)
        self.assertEqual(next(c for c in self.chat.view()["chats"] if c["peer"] == "!b2079b56")["unread"], 0)

    def test_public_chatter_is_kept_but_not_sent_to_telegram(self):
        self.checkin(received=[
            {"packet": 1, "from": "!11112222", "to": "^all", "channel": 1, "name": "Stranger", "text": "test", "ts": 1},
            {"packet": 2, "from": "!b2079b56", "to": "^all", "channel": 2, "name": "Ben", "text": "home soon", "ts": 2},
            {"packet": 3, "from": "!b2079b56", "to": "!99998888", "channel": 1, "name": "Ben", "text": "x", "ts": 3}])
        self.wait_for_telegram(1)
        time.sleep(0.05)
        self.assertEqual(self.telegram.sent, ["📡 Ben in Family (mesh): home soon"])
        chats = self.chat.view()["chats"]
        self.assertEqual([c["peer"] for c in chats], ["#2", "#1"])  # Family, then public; the collars' never
        self.assertEqual(chats[0]["last"], "Ben: home soon")

    def test_alerts_can_be_turned_off(self):
        self.assertTrue(self.chat.set_alerts(False)[0])
        self.checkin(received=[{"packet": 9, "from": "!b2079b56", "to": "!3248f6ed", "text": "hi", "ts": 1}])
        time.sleep(0.05)
        self.assertEqual(self.telegram.sent, [])

    def test_sending_waits_for_the_base_and_reports_back(self):
        self.assertFalse(self.chat.send("!b2079b56", "hi")[0])  # never checked in: nowhere to send it
        self.checkin()
        self.assertFalse(self.chat.send("!b2079b56", "x" * 201)[0])
        self.assertFalse(self.chat.send("nobody", "hi")[0])
        self.assertFalse(self.chat.send("#2", "  ")[0])
        self.assertTrue(self.chat.send("!b2079b56", "On my way")[0])
        self.assertTrue(self.chat.send("#2", "Hi all")[0])
        out = self.checkin()["send"]
        self.assertEqual([(m["to"], m.get("channel"), m["text"]) for m in out],
                         [("!b2079b56", None, "On my way"), ("^all", 2, "Hi all")])
        self.assertEqual(len(self.checkin()["send"]), 2)  # handed out until the base says how it went
        self.checkin(results=[{"id": out[0]["id"], "status": "sent"}, {"id": out[1]["id"], "status": "sent"},
                              {"id": out[0]["id"], "status": "delivered"}, {"id": out[1]["id"], "status": "failed",
                                                                            "error": "no radio heard it"}])
        self.checkin(results=[{"id": out[0]["id"], "status": "relayed"}])  # late: doesn't undo "delivered"
        self.assertEqual(self.checkin()["send"], [])
        self.assertEqual(self.chat.thread("!b2079b56")[0]["status"], "delivered")
        self.assertEqual(self.chat.thread("#2")[0]["status"], "failed: no radio heard it")

    def test_a_message_the_base_never_takes_gives_up(self):
        self.checkin()
        self.chat.send("#2", "anyone?")
        self.chat._checked_in = 1  # the base went quiet
        with self.chat._connect() as conn:
            conn.execute("UPDATE messages SET ts = ?", (time.time() - M.SEND_EXPIRE_S - 1,))
        self.assertEqual(self.checkin()["send"], [])
        self.assertTrue(self.chat.thread("#2")[0]["status"].startswith("failed"))

    def test_invites_and_requests_reach_the_base_once(self):
        self.checkin(nodes=[])
        view = self.chat.view()
        self.assertEqual(view["invites"], {"contact": BASE["invites"]["contact"], "family": BASE["invites"]["2"]})
        self.assertTrue(self.chat.make_family_channel()[0])  # already there: nothing to do
        base = dict(BASE, channels=BASE["channels"][:2])
        self.chat.bridge_checkin("grace", "secret", {"base": base})
        self.assertIsNone(self.chat.view()["invites"]["family"])
        self.chat.make_family_channel()
        self.assertTrue(self.chat.rename_base("Kate's house", "KATEHOUSE")[0])
        reply = self.chat.bridge_checkin("grace", "secret", {"base": base})
        self.assertEqual((reply["make_channel"], reply["rename"]), ("Family", {"long": "Kate's house", "short": "KATE"}))
        reply = self.chat.bridge_checkin("grace", "secret", {"base": base})
        self.assertNotIn("make_channel", reply)
        self.assertNotIn("rename", reply)


if __name__ == "__main__":
    unittest.main()
