"""
Web login: bcrypt passwords, server-side sessions, IP lockout with backoff.
"""

from __future__ import annotations

import bisect
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import bcrypt

# Precomputed bcrypt (password "secret") — wrong-username path burns same verify cost.
_DUMMY_HASH = b"$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31Dm"


def _client_ip(remote_addr: str, headers: Any) -> str:
    """Best-effort client IP (Cloudflare / reverse proxy aware)."""
    if remote_addr and remote_addr not in ("127.0.0.1", "::1"):
        return remote_addr
    cf = headers.get("Cf-Connecting-Ip") if headers else None
    if cf:
        return cf.strip()
    xff = (headers.get("X-Forwarded-For") or "").split(",")[0].strip() if headers else ""
    if xff:
        return xff
    return remote_addr or "unknown"


@dataclass
class SessionRecord:
    username: str
    client_ip: str
    created: float
    last_seen: float


def _username_equal(a: str, b: str) -> bool:
    """Constant-time comparison for usernames (fixed-width padding)."""
    ea = a.encode("utf-8")[:256].ljust(256, b"\0")
    eb = b.encode("utf-8")[:256].ljust(256, b"\0")
    return secrets.compare_digest(ea, eb)


class WebAuthManager:
    """Sessions + failed-login lockout."""

    def __init__(self, auth_cfg: Dict[str, Any]) -> None:
        self._username = auth_cfg.get("username") or ""
        self._password_hash = auth_cfg.get("password_hash") or ""
        self._idle_sec = float(auth_cfg.get("session_idle_minutes", 60)) * 60.0
        self._max_life_sec = float(auth_cfg.get("session_max_days", 7)) * 86400.0
        self._max_attempts = int(auth_cfg.get("max_attempts", 5))
        self._window_sec = float(auth_cfg.get("attempt_window_minutes", 5)) * 60.0
        self._lockout_base_sec = float(auth_cfg.get("lockout_minutes", 15)) * 60.0
        self._backoff_factor = float(auth_cfg.get("lockout_backoff_factor", 2))
        self._lockout_cap_sec = float(auth_cfg.get("lockout_max_minutes", 240)) * 60.0
        self._bind_session_to_ip = bool(auth_cfg.get("bind_session_to_ip", True))

        self._sessions: Dict[str, SessionRecord] = {}
        self._fail_times: Dict[str, List[float]] = {}
        self._lockout_until: Dict[str, float] = {}
        self._lockout_generation: Dict[str, int] = {}

    def reload_config(self, auth_cfg: Dict[str, Any]) -> None:
        """Refresh auth parameters from settings (sessions kept)."""
        self._username = auth_cfg.get("username") or ""
        self._password_hash = auth_cfg.get("password_hash") or ""
        self._idle_sec = float(auth_cfg.get("session_idle_minutes", 60)) * 60.0
        self._max_life_sec = float(auth_cfg.get("session_max_days", 7)) * 86400.0
        self._max_attempts = int(auth_cfg.get("max_attempts", 5))
        self._window_sec = float(auth_cfg.get("attempt_window_minutes", 5)) * 60.0
        self._lockout_base_sec = float(auth_cfg.get("lockout_minutes", 15)) * 60.0
        self._backoff_factor = float(auth_cfg.get("lockout_backoff_factor", 2))
        self._lockout_cap_sec = float(auth_cfg.get("lockout_max_minutes", 240)) * 60.0
        self._bind_session_to_ip = bool(auth_cfg.get("bind_session_to_ip", True))

    def is_locked(self, client_ip: str, now: Optional[float] = None) -> Tuple[bool, float]:
        """Return (locked, seconds_until_unlock)."""
        t = now if now is not None else time.monotonic()
        until = self._lockout_until.get(client_ip, 0.0)
        if until <= t:
            if client_ip in self._lockout_until:
                del self._lockout_until[client_ip]
            return False, 0.0
        return True, until - t

    def _prune_failures(self, client_ip: str, now: float) -> None:
        times = self._fail_times.get(client_ip)
        if not times:
            return
        cutoff = now - self._window_sec
        idx = bisect.bisect_left(times, cutoff)
        if idx > 0:
            times = times[idx:]
        if times:
            self._fail_times[client_ip] = times
        else:
            del self._fail_times[client_ip]

    def record_failure(self, client_ip: str, now: Optional[float] = None) -> Tuple[bool, float]:
        """
        Record a failed login for client_ip.
        Returns (now_locked, retry_after_seconds).
        """
        t = now if now is not None else time.monotonic()
        locked, remaining = self.is_locked(client_ip, t)
        if locked:
            return True, remaining

        self._prune_failures(client_ip, t)
        times = self._fail_times.setdefault(client_ip, [])
        times.append(t)
        self._fail_times[client_ip] = times

        if len(times) < self._max_attempts:
            return False, 0.0

        gen = self._lockout_generation.get(client_ip, 0)
        duration = min(
            self._lockout_cap_sec,
            self._lockout_base_sec * (self._backoff_factor**gen),
        )
        self._lockout_until[client_ip] = t + duration
        self._lockout_generation[client_ip] = gen + 1
        self._fail_times.pop(client_ip, None)
        return True, duration

    def record_success(self, client_ip: str) -> None:
        self._fail_times.pop(client_ip, None)
        self._lockout_generation.pop(client_ip, None)
        self._lockout_until.pop(client_ip, None)

    def verify_password(self, username: str, password: str) -> bool:
        if not self._password_hash:
            return False
        try:
            if not _username_equal(username, self._username):
                self.verify_password_dummy_timing(password)
                return False
            pw = password.encode("utf-8")
            return bcrypt.checkpw(pw, self._password_hash.encode("ascii"))
        except (ValueError, TypeError):
            self.verify_password_dummy_timing(password)
            return False

    def verify_password_dummy_timing(self, password: str) -> None:
        """Burn bcrypt CPU when username is wrong (mitigate user enumeration)."""
        try:
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
        except (ValueError, TypeError):
            pass

    def create_session(self, username: str, client_ip: str) -> str:
        token = secrets.token_urlsafe(32)
        t = time.monotonic()
        self._sessions[token] = SessionRecord(
            username=username,
            client_ip=client_ip,
            created=t,
            last_seen=t,
        )
        return token

    def revoke_session(self, token: str, reason: str = "") -> Optional[SessionRecord]:
        """Remove session; return the record if one existed (for logging)."""
        return self._sessions.pop(token, None)

    def validate_session(self, token: Optional[str], client_ip: str) -> Optional[SessionRecord]:
        if not token:
            return None
        rec = self._sessions.get(token)
        if not rec:
            return None
        now = time.monotonic()
        if now - rec.created > self._max_life_sec:
            self._sessions.pop(token, None)
            return None
        if now - rec.last_seen > self._idle_sec:
            self._sessions.pop(token, None)
            return None
        if self._bind_session_to_ip and rec.client_ip != client_ip:
            self._sessions.pop(token, None)
            return None
        rec.last_seen = now
        return rec

    def touch_session(self, token: str) -> None:
        rec = self._sessions.get(token)
        if rec:
            rec.last_seen = time.monotonic()
