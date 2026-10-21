"""Tests for 'did anyone come by?' style questions: understanding, answering, storage."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import time
import unittest

from sentinel.engine.queries import parse_video_query
from sentinel.engine.questions import (answer_question, clip_hint, group_visits, is_question,
                                       parse_question)
from sentinel.recordings.store import RecordingStore

NOW = time.mktime((2026, 10, 21, 15, 0, 0, 0, 0, -1))  # 3pm local
TODAY = time.mktime((2026, 10, 21, 0, 0, 0, 0, 0, -1))
CAMS = ["Front Door", "Garage", "Rear Door"]


def at(hour, minute=0, day=0):
    return TODAY + day * 86400 + hour * 3600 + minute * 60


def ev(ts, grp, desc, cam="Front Door", snapshot=None):
    return {"ts": ts, "grp": grp, "description": desc, "camera_name": cam, "snapshot": snapshot}


class TestUnderstanding(unittest.TestCase):
    def test_recognises_questions(self):
        for text in ("Did anyone come by?", "anyone come by today", "Has anybody been here?", "Who came by?",
                     "any deliveries today?", "Did the mail come?", "Has my Amazon package arrived?",
                     "What happened last night?", "anything going on?", "Any cars in the last 2 hours?",
                     "was someone at the front door this morning", "did a dog come into the yard?",
                     "what did I miss"):
            self.assertTrue(is_question(text), text)

    def test_leaves_commands_alone(self):
        for text in ("status", "videos today people", "Videos last 2 hours", "enable messages", "access",
                     "help", "clip", "pause 30", "resume", "thanks", "ok"):
            self.assertFalse(is_question(text), text)

    def test_subjects(self):
        self.assertEqual(parse_question("did anyone come by?", CAMS, NOW).groups, {"person", "delivery"})
        self.assertEqual(parse_question("any deliveries?", CAMS, NOW).groups, {"delivery"})
        self.assertEqual(parse_question("did my amazon van come", CAMS, NOW).groups, {"delivery"})
        self.assertEqual(parse_question("any cars today?", CAMS, NOW).groups, {"vehicle"})
        self.assertEqual(parse_question("did a deer come by?", CAMS, NOW).groups, {"animal"})
        self.assertEqual(parse_question("what happened today?", CAMS, NOW).groups, set())
        q = parse_question("was anyone at the front door?", CAMS, NOW)
        self.assertEqual(q.cameras, ["Front Door"])

    def test_periods(self):
        q = parse_question("did anyone come by?", CAMS, NOW)
        self.assertEqual((q.start, q.end, q.period), (TODAY, NOW, "today"))
        q = parse_question("anyone come by yesterday?", CAMS, NOW)
        self.assertEqual((q.start, q.end, q.day_offset), (TODAY - 86400, TODAY, 1))
        q = parse_question("what happened last night", CAMS, NOW)
        self.assertEqual((q.start, q.end), (at(18, day=-1), at(7)))
        q = parse_question("anyone this morning?", CAMS, NOW)
        self.assertEqual((q.start, q.end), (at(5), at(12)))
        q = parse_question("any deliveries in the last 2 hours?", CAMS, NOW)
        self.assertEqual((q.start, q.period), (NOW - 7200, "in the last 2 hours"))
        q = parse_question("anyone in the past hour?", CAMS, NOW)
        self.assertEqual((q.start, q.period), (NOW - 3600, "in the last hour"))
        q = parse_question("anyone since 9am?", CAMS, NOW)
        self.assertEqual((q.start, q.period), (at(9), "since 9:00am"))
        early = TODAY + 3 * 3600
        q = parse_question("did anyone come by?", CAMS, early)
        self.assertEqual((q.start, q.period), (early - 12 * 3600, "in the last 12 hours"))


class TestAnswers(unittest.TestCase):
    def test_delivery_and_driver_are_one_visit(self):
        events = [ev(at(10, 42), "delivery", "📦 An Amazon van pulled in at the Front Door.", snapshot="a.jpg"),
                  ev(at(10, 43), "person", "🚶 Someone is at the Front Door."),
                  ev(at(14, 5), "person", "🚶 Someone is walking past the Garage.", "Garage", snapshot="b.jpg")]
        self.assertEqual([len(v) for v in group_visits(events)], [2, 1])
        q = parse_question("did anyone come by?", CAMS, NOW)
        text, photos = answer_question(q, events, None, NOW)
        self.assertIn("Yes — 2 visits today:", text)
        self.assertIn("• 10:42am — 📦 An Amazon van pulled in at the Front Door. Also: Someone is at the Front Door.", text)
        self.assertIn("• 2:05pm — 🚶 Someone is walking past the Garage.", text)
        self.assertEqual([p["snapshot"] for p in photos], ["b.jpg", "a.jpg"])  # newest first

    def test_nobody_mentions_the_last_visitor(self):
        q = parse_question("did anyone come by?", CAMS, NOW)
        last = ev(at(16, 23, day=-1), "person", "🚶 Someone is at the Front Door.")
        text, photos = answer_question(q, [], last, NOW)
        self.assertEqual(text, "No — nobody came by today.\nThe last one was yesterday at 4:23pm: Someone is at the Front Door.")
        self.assertEqual(photos, [])
        q = parse_question("any deliveries at the garage?", CAMS, NOW)
        self.assertEqual(answer_question(q, [], None, NOW)[0], "No deliveries today at the Garage.")

    def test_other_kinds_get_a_footnote(self):
        q = parse_question("did anyone come by?", CAMS, NOW)
        text, _ = answer_question(q, [ev(at(9), "person", "🚶 Someone is at the Front Door.")], None, NOW,
                                  others={"vehicle": 3, "animal": 1})
        self.assertIn("(Also 1 animal sighting and 3 vehicles", text)

    def test_long_days_are_trimmed(self):
        events = [ev(at(8) + i * 600, "person", f"🚶 Visit {i}.") for i in range(14)]
        q = parse_question("what happened today", CAMS, NOW)
        text, _ = answer_question(q, events, None, NOW)
        self.assertIn("…4 earlier, then:", text)
        self.assertIn("Visit 13", text)
        self.assertNotIn("Visit 3.", text)

    def test_clip_hint_is_a_working_videos_command(self):
        visit = [ev(at(10, 42), "delivery", "x"), ev(at(10, 44), "person", "y")]
        hint = clip_hint(visit, NOW)
        self.assertEqual(hint, "videos 10:41am-10:46am")
        vq, err = parse_video_query(hint, CAMS, NOW)
        self.assertIsNone(err)
        self.assertEqual((vq.start, vq.end), (at(10, 41), at(10, 46)))
        night = [ev(at(2, 10, day=-1), "person", "z")]
        self.assertEqual(clip_hint(night, NOW), "videos yesterday 2:09am-2:12am")
        vq, err = parse_video_query(clip_hint(night, NOW), CAMS, NOW)
        self.assertEqual(vq.start, at(2, 9, day=-1))  # am stays am
        self.assertIsNone(clip_hint([ev(at(9, day=-3), "person", "w")], NOW))  # too old for "videos"
        self.assertIsNone(clip_hint([ev(at(23, 59, day=-1), "person", "v")], NOW))  # crosses midnight


class TestEventStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-events-")
        self.store = RecordingStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_add_query_and_retention(self):
        now = time.time()
        old = self.store.add_event(now - 40 * 86400, "192.168.0.10", "Front Door", "person", "🚶 old", snapshot_jpeg=b"jpeg")
        new = self.store.add_event(now - 60, "192.168.0.9", "Garage", "delivery", "📦 new", snapshot_jpeg=b"jpeg2")
        self.assertEqual([e["id"] for e in self.store.events(now - 41 * 86400, now)], [old, new])
        self.assertEqual([e["id"] for e in self.store.events(now - 3600, now, ["delivery"])], [new])
        self.assertEqual(self.store.events(now - 3600, now, None, ["192.168.0.10"]), [])
        self.assertEqual(self.store.last_event(now - 3600)["id"], old)
        old_snap = self.store.event_snapshot_path(self.store.events(now - 41 * 86400, now - 39 * 86400)[0])
        self.assertTrue(os.path.isfile(old_snap))
        self.store.apply_retention(30)
        self.assertEqual([e["id"] for e in self.store.events(0, now)], [new])
        self.assertFalse(os.path.exists(old_snap))


class _FakeTelegram:
    def __init__(self):
        self.texts, self.photos = [], []

    def reply_to_chat(self, chat_id, text):
        self.texts.append(text)

    def send_photo(self, chat_id, jpeg, caption):
        self.photos.append(caption)


class TestEngineAnswer(unittest.TestCase):
    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-q-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        sm = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        url = "rtsp://u:p@192.168.0.10:554/h264/ch1/sub/av_stream"
        sm.settings["camera_profiles"] = {url: {"source": url, "display": "Front Door"}}
        self.engine = Engine(sm)
        self.engine._build_cameras()
        self.engine.telegram = _FakeTelegram()
        self.engine.detection_worker = object()

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_answers_with_photos(self):
        now = time.time()
        store = self.engine.recording_store
        store.add_event(now - 120, "192.168.0.10", "Front Door", "delivery", "📦 A UPS truck is at the Front Door.",
                        snapshot_jpeg=b"\xff\xd8jpeg")
        store.add_event(now - 100, "192.168.0.10", "Front Door", "vehicle", "🚗 A car is driving past the Front Door.")
        self.engine.answer_question("1", "any deliveries in the last hour?")
        reply = self.engine.telegram.texts[0]
        self.assertIn("Yes — 1 delivery in the last hour:", reply)
        self.assertIn("A UPS truck", reply)
        self.assertIn("1 vehicle", reply)  # footnote
        self.assertEqual(len(self.engine.telegram.photos), 1)
        self.assertIn("A UPS truck", self.engine.telegram.photos[0])

    def test_backfill_from_log(self):
        import json
        with open("sentinel_events.log", "w") as handle:
            for ts, label, vt, desc in (("2026-10-20T10:42:00", "truck", "ups", "📦 A UPS truck is at the Front Door."),
                                        ("2026-10-20T10:43:00", "person", None, "🚶 Someone is at the Front Door."),
                                        ("2026-10-20T11:00:00", "car", "suv", "🚙 An SUV is at the Front Door.")):
                handle.write(json.dumps({"timestamp": ts, "event": "object_detected", "camera_display": "Front Door",
                                         "label": label, "vehicle_type": vt, "description": desc}) + "\n")
        self.assertEqual(self.engine.backfill_events_from_log(), 3)
        self.assertEqual(self.engine.backfill_events_from_log(), 0)  # only once
        groups = [e["grp"] for e in self.engine.recording_store.events(0, time.time())]
        self.assertEqual(groups, ["delivery", "person", "vehicle"])
        self.assertEqual(self.engine.recording_store.events(0, time.time())[0]["camera_key"], "192.168.0.10")


if __name__ == "__main__":
    unittest.main()
