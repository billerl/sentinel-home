"""Tests for sentinel.web.auth.WebAuthManager."""

import unittest
from unittest.mock import patch

import bcrypt

from sentinel.web.auth import WebAuthManager, _username_equal


def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("ascii")


class TestWebAuth(unittest.TestCase):
    def test_verify_success(self) -> None:
        h = _hash("correct horse")
        auth = WebAuthManager(
            {
                "username": "alice",
                "password_hash": h,
                "max_attempts": 5,
                "lockout_minutes": 15,
                "lockout_backoff_factor": 2,
                "lockout_max_minutes": 240,
                "attempt_window_minutes": 5,
                "session_idle_minutes": 60,
                "session_max_days": 7,
            }
        )
        self.assertTrue(auth.verify_password("alice", "correct horse"))
        self.assertFalse(auth.verify_password("alice", "wrong"))
        self.assertFalse(auth.verify_password("bob", "correct horse"))

    def test_lockout_and_backoff(self) -> None:
        t0 = 1000.0
        auth = WebAuthManager(
            {
                "username": "u",
                "password_hash": _hash("p"),
                "max_attempts": 3,
                "lockout_minutes": 1 / 60.0,
                "lockout_backoff_factor": 2,
                "lockout_max_minutes": 10,
                "attempt_window_minutes": 5,
                "session_idle_minutes": 60,
                "session_max_days": 7,
            }
        )
        with patch("sentinel.web.auth.time.monotonic", return_value=t0):
            self.assertFalse(auth.is_locked("1.1.1.1")[0])
            auth.record_failure("1.1.1.1", t0)
            auth.record_failure("1.1.1.1", t0 + 0.01)
            locked, ra = auth.record_failure("1.1.1.1", t0 + 0.02)
            self.assertTrue(locked)
            self.assertGreater(ra, 0)

        with patch("sentinel.web.auth.time.monotonic", return_value=t0 + 9999):
            self.assertFalse(auth.is_locked("1.1.1.1")[0])

    def test_session_idle(self) -> None:
        auth = WebAuthManager(
            {
                "username": "u",
                "password_hash": _hash("p"),
                "session_idle_minutes": 1 / 60.0,
                "session_max_days": 7,
                "max_attempts": 99,
                "attempt_window_minutes": 5,
                "lockout_minutes": 15,
                "lockout_backoff_factor": 2,
                "lockout_max_minutes": 240,
            }
        )
        t0 = 500.0
        with patch("sentinel.web.auth.time.monotonic", return_value=t0):
            tok = auth.create_session("u", "10.0.0.1")
        with patch("sentinel.web.auth.time.monotonic", return_value=t0 + 2.0):
            self.assertIsNone(auth.validate_session(tok, "10.0.0.1"))

    def test_session_ip_binding(self) -> None:
        auth = WebAuthManager(
            {
                "username": "u",
                "password_hash": _hash("p"),
                "bind_session_to_ip": True,
                "session_idle_minutes": 60,
                "session_max_days": 7,
                "max_attempts": 99,
                "attempt_window_minutes": 5,
                "lockout_minutes": 15,
                "lockout_backoff_factor": 2,
                "lockout_max_minutes": 240,
            }
        )
        t0 = 100.0
        with patch("sentinel.web.auth.time.monotonic", return_value=t0):
            tok = auth.create_session("u", "10.0.0.1")
        with patch("sentinel.web.auth.time.monotonic", return_value=t0 + 1.0):
            self.assertIsNotNone(auth.validate_session(tok, "10.0.0.1"))
            self.assertIsNone(auth.validate_session(tok, "10.0.0.2"))

    def test_revoke_returns_record(self) -> None:
        auth = WebAuthManager(
            {
                "username": "u",
                "password_hash": _hash("p"),
                "session_idle_minutes": 60,
                "session_max_days": 7,
                "max_attempts": 99,
                "attempt_window_minutes": 5,
                "lockout_minutes": 15,
                "lockout_backoff_factor": 2,
                "lockout_max_minutes": 240,
            }
        )
        with patch("sentinel.web.auth.time.monotonic", return_value=0.0):
            tok = auth.create_session("u", "1.1.1.1")
        rec = auth.revoke_session(tok, "logout")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.username, "u")

    def test_username_equal(self) -> None:
        self.assertTrue(_username_equal("ab", "ab"))
        self.assertFalse(_username_equal("a", "ab"))


if __name__ == "__main__":
    unittest.main()
