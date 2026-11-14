"""The event log must never raise: it's called from the alert path, the Telegram listener and the voice thread,
so a full disk or an unwritable log file must not stop them (bug hunt 2026-11-13)."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest

from sentinel.logging_utils import StructuredLogger


class TestStructuredLogger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-log-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_redacted_json_lines(self):
        log = StructuredLogger(os.path.join(self.dir, "events.log"))
        log.log("info", "camera_connected", source="rtsp://user:secret@10.0.0.2/live")
        with open(log.log_path) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["event"], "camera_connected")
        self.assertNotIn("secret", rec["source"])

    def test_an_unwritable_log_does_not_raise(self):
        blocker = os.path.join(self.dir, "not-a-folder")
        with open(blocker, "w") as f:
            f.write("x")
        log = StructuredLogger(os.path.join(blocker, "events.log"))  # its folder is a file: can't be written
        log.log("info", "object_detected", label="person")  # (no exception)
        log.log("error", "telegram_failed")
        self.assertEqual(log.dropped, 2)

    def test_an_odd_value_is_written_as_text(self):
        log = StructuredLogger(os.path.join(self.dir, "events.log"))
        log.log("info", "x", when=object())
        with open(log.log_path) as f:
            self.assertIn("object object", json.loads(f.readline())["when"])


if __name__ == "__main__":
    unittest.main()
