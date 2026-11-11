"""
What every area's routes (sentinel/web/routes/) share: Sentinel's parts and the checks each request needs.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from flask import g, jsonify, redirect, request, url_for

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.changelog import ChangeLog
from sentinel.web.roles import OWNER, Viewer

if TYPE_CHECKING:
    from sentinel.web.server import WebServer

PERSON_PREFIX = "person:"  # session username of a family member signed in with their own password

# TV browsers show the live cameras full-time (Fire TV "AFT..." models in Fully Kiosk, Samsung/LG TV browsers).
TV_BROWSER = re.compile(r"\bAFT[A-Z0-9]+\b|SMART-TV|Tizen|Web0S|webOS|BRAVIA")


class WebContext:
    """Made once per start(); the route modules close over it the way the routes once closed over start()."""

    def __init__(self, server: "WebServer", auth_cfg: Dict[str, Any]) -> None:
        self.server = server  # its _web_cfg()/_auth_cfg() are read live (settings change while running)
        self.settings = server._settings
        self.broker = server._broker
        self.recordings = server._recordings
        self.engine = server._engine
        self.auth = server._auth
        self.limiter = server._limiter
        self.auth_cfg = auth_cfg  # as read at start (session length)
        self._device_failures: Dict[str, List[float]] = {}  # ip -> recent wrong logins (phones, base station, links)
        self.changes: Optional[ChangeLog] = None  # who changed what (kept beside the other data)
        data_db = getattr(getattr(self.engine, "locations", None), "db_path", None)
        if isinstance(data_db, str):
            import os

            self.changes = ChangeLog(os.path.dirname(data_db))

    def device_login_blocked(self, ip: str) -> bool:
        """Too many wrong phone / base station logins or setup links from one address: refuse for a while."""
        now = time.time()
        recent = [t for t in self._device_failures.get(ip, []) if now - t < 600]
        self._device_failures[ip] = recent
        return len(recent) >= 20

    def device_login_failed(self, ip: str, what: str) -> None:
        self._device_failures.setdefault(ip, []).append(time.time())
        app_logger.log("warning", "location_auth_failed", what=what, ip=ip, component="web_server",
                       action="location")

    def session_token(self) -> Optional[str]:
        return request.cookies.get("sentinel_session")

    def viewer(self) -> Optional[Viewer]:
        """Who this request comes from and their role (sentinel/web/roles.py); None = not signed in. The shared
        household login is the owner. A person's own login stops working the moment it is removed or they are deleted."""
        if "viewer" in g:
            return g.viewer
        g.viewer = None
        auth = self.auth
        rec = auth.validate_session(self.session_token(), _client_ip(request.remote_addr or "", request.headers)) \
            if auth is not None else None
        if rec is None:
            return None
        if not rec.username.startswith(PERSON_PREFIX):
            g.viewer = Viewer(OWNER, None, rec.username)
        elif self.engine is not None:
            person = self.engine.locations.web_account(rec.username[len(PERSON_PREFIX):])
            if person is not None:
                g.viewer = Viewer(person.role, person, rec.username)
        return g.viewer

    def require_session(self):
        if self.auth is None or self.viewer() is None:
            return redirect(url_for("login"))
        return None

    def viewer_person_id(self) -> Optional[int]:
        """The family member signed in with their own password (None = the shared household login)."""
        viewer = self.viewer()
        return viewer.person_id if viewer is not None else None

    def denied(self, message: str):
        """A route's own "not yours to change" answer (the role itself was already checked before the route)."""
        return jsonify({"error": message}), 403

    def set_session_cookie(self, resp, token: str, remember: bool) -> None:
        secure = request.headers.get("X-Forwarded-Proto", "").lower() == "https" or request.is_secure
        # "Keep me signed in": a year; otherwise the configured session length.
        max_age = 365 * 86400 if remember else int(self.auth_cfg.get("session_max_days", 7)) * 86400
        resp.set_cookie("sentinel_session", token, httponly=True, samesite="Lax", secure=secure,
                        max_age=max_age, path="/")

    @staticmethod
    def write_request_ok() -> bool:
        # JSON + custom header: browsers won't send either cross-site without a
        # CORS preflight (which we never grant), so this blocks CSRF.
        return request.is_json and request.headers.get("X-Sentinel-Request") == "1"

    def config_guard(self):
        """Shared checks for config writes; returns an error response or None."""
        redir = self.require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if self.engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        if not self.write_request_ok():
            return jsonify({"error": "bad request"}), 400
        return None

    @staticmethod
    def result(ok: bool, message: str):
        return (jsonify({"ok": True, "message": message}) if ok
                else (jsonify({"error": message}), 400))

    @staticmethod
    def base_url() -> str:
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
        return f"{proto}://{request.host}"
