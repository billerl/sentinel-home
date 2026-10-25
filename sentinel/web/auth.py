"""
Web login: bcrypt passwords, server-side sessions, IP lockout with backoff.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import secrets
import threading
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


@dataclass
class Pairing:
    """A TV/other screen waiting for a signed-in phone to approve it (QR sign-in)."""
    client_ip: str
    user_agent: str
    created: float
    short: str  # shown on both screens so the phone user can check they match
    approved_by: Optional[str] = None


PAIRING_TTL_SECONDS = 180.0
PAIRING_MAX_PER_IP = 3
PAIRING_MAX = 50


def _username_equal(a: str, b: str) -> bool:
    """Constant-time comparison for usernames (fixed-width padding)."""
    ea = a.encode("utf-8")[:256].ljust(256, b"\0")
    eb = b.encode("utf-8")[:256].ljust(256, b"\0")
    return secrets.compare_digest(ea, eb)


class WebAuthManager:
    """Sessions + failed-login lockout."""

    def __init__(self, auth_cfg: Dict[str, Any], store_path: Optional[str] = None) -> None:
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

        # Keyed by a hash of the token. Saved to ``store_path`` (hashes only) so a restart (every
        # update) doesn't sign everyone out, e.g. a Fire TV left showing the cameras.
        self._sessions: Dict[str, SessionRecord] = {}
        self._store_path = store_path
        self._store_lock = threading.Lock()
        self._saved_at = 0.0
        self._load_sessions()
        self._pairings: Dict[str, Pairing] = {}
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

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load_sessions(self) -> None:
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            wall, mono = time.time(), time.monotonic()
            for key, r in saved.items():
                self._sessions[key] = SessionRecord(r["username"], r["client_ip"],
                                                    mono - (wall - r["created"]), mono - (wall - r["last_seen"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._sessions.clear()  # unreadable: everyone signs in again

    def _save_sessions(self) -> None:
        if not self._store_path:
            return
        wall, mono = time.time(), time.monotonic()
        data = {key: {"username": r.username, "client_ip": r.client_ip,
                      "created": wall - (mono - r.created), "last_seen": wall - (mono - r.last_seen)}
                for key, r in list(self._sessions.items())}
        with self._store_lock:
            tmp = self._store_path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, self._store_path)
                self._saved_at = mono
            except OSError:
                pass

    def create_session(self, username: str, client_ip: str) -> str:
        token = secrets.token_urlsafe(32)
        t = time.monotonic()
        self._sessions[self._key(token)] = SessionRecord(
            username=username,
            client_ip=client_ip,
            created=t,
            last_seen=t,
        )
        self._save_sessions()
        return token

    def revoke_session(self, token: str, reason: str = "") -> Optional[SessionRecord]:
        """Remove session; return the record if one existed (for logging)."""
        rec = self._sessions.pop(self._key(token), None)
        if rec:
            self._save_sessions()
        return rec

    def validate_session(self, token: Optional[str], client_ip: str) -> Optional[SessionRecord]:
        if not token:
            return None
        key = self._key(token)
        rec = self._sessions.get(key)
        if not rec:
            return None
        now = time.monotonic()
        if (now - rec.created > self._max_life_sec or now - rec.last_seen > self._idle_sec
                or (self._bind_session_to_ip and rec.client_ip != client_ip)):
            self._sessions.pop(key, None)
            self._save_sessions()
            return None
        rec.last_seen = now
        if now - self._saved_at > 300:
            self._save_sessions()  # keep "last seen" roughly current on disk
        return rec

    def touch_session(self, token: str) -> None:
        rec = self._sessions.get(self._key(token))
        if rec:
            rec.last_seen = time.monotonic()

    # ------------------------------------------------------------ QR sign-in
    def _prune_pairings(self, now: float) -> None:
        for code in [c for c, p in self._pairings.items() if now - p.created > PAIRING_TTL_SECONDS]:
            self._pairings.pop(code, None)

    def create_pairing(self, client_ip: str, user_agent: str) -> Optional[Tuple[str, Pairing]]:
        """New one-time code for a screen that wants signing in; None if this IP has too many waiting."""
        now = time.monotonic()
        self._prune_pairings(now)
        if (sum(1 for p in self._pairings.values() if p.client_ip == client_ip) >= PAIRING_MAX_PER_IP
                or len(self._pairings) >= PAIRING_MAX):
            return None
        code = secrets.token_urlsafe(24)
        short = "".join(secrets.choice("ACDEFHJKMNPRTUVWXY3479") for _ in range(4))
        pairing = Pairing(client_ip, (user_agent or "")[:200], now, short)
        self._pairings[code] = pairing
        return code, pairing

    def pairing(self, code: str) -> Optional[Pairing]:
        self._prune_pairings(time.monotonic())
        return self._pairings.get(code) if code else None

    def approve_pairing(self, code: str, username: str) -> bool:
        pairing = self.pairing(code)
        if pairing is None or pairing.approved_by:
            return False
        pairing.approved_by = username
        return True

    def claim_pairing(self, code: str, client_ip: str) -> Tuple[str, Optional[str]]:
        """The waiting screen polls: ("waiting"|"expired", None) or ("approved", session token)."""
        pairing = self.pairing(code)
        if pairing is None or pairing.client_ip != client_ip:
            return "expired", None
        if not pairing.approved_by:
            return "waiting", None
        self._pairings.pop(code, None)  # one use
        return "approved", self.create_session(pairing.approved_by, client_ip)
