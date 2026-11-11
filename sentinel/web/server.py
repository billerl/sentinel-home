"""
Flask HTTP server: the app, its settings and listeners; the pages and API are in sentinel/web/routes/.
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from flask import Flask, g, request, send_from_directory
from werkzeug.serving import make_server

from sentinel.logging_utils import app_logger
from sentinel.web.auth import WebAuthManager
from sentinel.web.bind_util import bind_host_allowed
from sentinel.web import roles
from sentinel.web.context import TV_BROWSER, WebContext  # noqa: F401  (TV_BROWSER: tests import it from here)
from sentinel.web.frame_broker import FrameBroker
from sentinel.web.routes import (cameras, family, house, live, mesh, notes, recipes, recordings, routines, sensors,
                                 settings, signin, spotify, switches, talk, vehicles)
from sentinel.web.routes.signin import _from_this_home, _home_nets  # noqa: F401  (tests use them here)

if TYPE_CHECKING:
    from sentinel.recordings import RecordingStore
    from sentinel.settings import SettingsManager


SLOW_SECONDS = 1.0  # a request Sentinel took longer than this over is logged (web_slow_server)
LONG_PATHS = ("/stream", "/api/talk")  # (meant to take long: live video, speech recognition)


def _home_lan_address(host: str) -> bool:
    """A bind address other screens in the house can use: private IPv4, not loopback or Tailscale's range."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (addr.version == 4 and addr.is_private and not addr.is_loopback
            and addr not in ipaddress.ip_network("100.64.0.0/10"))


class _StreamLimiter:
    def __init__(self, global_max: int, per_ip_max: int) -> None:
        self._lock = threading.Lock()
        self._global = 0
        self._per_ip: Dict[str, int] = {}
        self._gmax = max(1, global_max)
        self._imax = max(1, per_ip_max)

    def try_acquire(self, ip: str) -> bool:
        with self._lock:
            if self._global >= self._gmax:
                return False
            if self._per_ip.get(ip, 0) >= self._imax:
                return False
            self._global += 1
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            return True

    def release(self, ip: str) -> None:
        with self._lock:
            self._global = max(0, self._global - 1)
            cur = self._per_ip.get(ip, 0)
            if cur <= 1:
                self._per_ip.pop(ip, None)
            else:
                self._per_ip[ip] = cur - 1


def _template_dir() -> str:
    import os

    return os.path.join(os.path.dirname(__file__), "templates")


def _register_static_types() -> None:
    import mimetypes

    mimetypes.add_type("application/manifest+json", ".webmanifest")  # not in every system table


def _static_dir() -> str:
    import os

    return os.path.join(os.path.dirname(__file__), "static")  # icons + web app manifest (public)


class WebServer:
    """Runs Flask in a daemon thread when web.enabled and password_hash are set."""

    def __init__(
        self,
        settings_manager: "SettingsManager",
        frame_broker: FrameBroker,
        recording_store: Optional["RecordingStore"] = None,
        engine: Optional[Any] = None,
    ) -> None:
        self._settings = settings_manager
        self._broker = frame_broker
        self._recordings = recording_store
        self._engine = engine
        self._thread: Optional[threading.Thread] = None
        self._auth: Optional[WebAuthManager] = None
        self._limiter: Optional[_StreamLimiter] = None
        self._app: Optional[Flask] = None

    def _web_cfg(self) -> Dict[str, Any]:
        cfg = self._settings.settings.get("web")
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def _auth_cfg(self) -> Dict[str, Any]:
        cfg = self._web_cfg().get("auth")
        return cfg if isinstance(cfg, dict) else {}

    def start(self) -> bool:
        cfg = self._web_cfg()
        if not cfg.get("enabled"):
            return False
        auth_cfg = self._auth_cfg()
        if not (auth_cfg.get("password_hash") or "").strip():
            print(
                "Web viewer: web.enabled is true but no password_hash. "
                "Run: python -m sentinel.web.set_password"
            )
            return False

        # One address or several, comma-separated: loopback for the tunnel plus this PC's home-network address
        # lets screens in the house (the camera TV) reach Sentinel without a round trip through the internet.
        bind_hosts = [h.strip() for h in str(cfg.get("bind_host", "127.0.0.1")).split(",") if h.strip()] or ["127.0.0.1"]
        camera_block = str(cfg.get("camera_subnet_block", "192.168.0.0/24"))
        for bind_host in bind_hosts:
            ok, msg = bind_host_allowed(bind_host, camera_block)
            if not ok:
                print(f"Web viewer: {msg}")
                return False
        port = int(cfg.get("port", 8088))

        if not (auth_cfg.get("session_secret") or "").strip():
            import secrets as sec

            auth_cfg["session_secret"] = sec.token_hex(32)
            merged = dict(self._web_cfg())
            merged["auth"] = {**auth_cfg}
            self._settings.settings["web"] = merged
            self._settings.save_settings(force=True)

        self._auth = WebAuthManager(auth_cfg, store_path=os.path.join(
            os.path.dirname(os.path.abspath(self._settings.settings_file)), ".web_sessions.json"))
        self._limiter = _StreamLimiter(
            int(cfg.get("max_concurrent_streams", 4)),
            int(cfg.get("max_concurrent_streams_per_session", 2)),
        )

        _register_static_types()
        app = Flask(__name__, template_folder=_template_dir(), static_folder=_static_dir())
        app.secret_key = auth_cfg["session_secret"]
        app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # forms and config JSON only

        @app.before_request
        def _clock_in():
            g.started = time.perf_counter()

        @app.after_request
        def _security_headers(resp):
            # How long Sentinel itself took: a header the page's slow-request report reads (static/slowlog.js),
            # and a log line when it's over a second, so "it's slow" can be told apart from the phone's connection
            took = time.perf_counter() - g.get("started", time.perf_counter())
            resp.headers["Server-Timing"] = f"app;dur={took * 1000:.0f}"
            if took > SLOW_SECONDS and not request.path.startswith(LONG_PATHS) and "/screen" not in request.path:
                app_logger.log("info", "web_slow_server", path=request.path, ms=round(took * 1000),
                               via="tunnel" if request.remote_addr == "127.0.0.1" else request.remote_addr,
                               component="web", action="request")
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            # SAMEORIGIN, not DENY: the TV's camera page shows the location map (/where) in a frame of its own
            # (DENY left the TV blank, 2026-11-10); other sites still can't frame Sentinel's pages
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            resp.headers.setdefault("Referrer-Policy", "same-origin")
            return resp

        @app.route("/favicon.ico")
        def favicon():
            # Browsers ask for this before (and without) logging in.
            return send_from_directory(_static_dir(), "favicon.ico", mimetype="image/vnd.microsoft.icon",
                                       max_age=7 * 86400)

        self._app = app
        ctx = WebContext(self, auth_cfg)

        # screens in the house that came in through the tunnel move to the home-network address
        home_base = next((f"http://{h}:{port}" for h in bind_hosts if _home_lan_address(h)), None)
        signin.register(app, ctx, home_base)
        roles.install(app, ctx)  # every route's minimum role, checked before the route runs
        for area in (live, family, mesh, notes, recipes, spotify, switches, sensors, house, routines, cameras, vehicles,
                     settings, recordings, talk):
            area.register(app, ctx)

        app_logger.log(
            "info",
            "web_server_start",
            bind_host=",".join(bind_hosts),
            port=port,
            component="web_server",
            action="start",
        )

        def _run(host: str) -> None:
            try:
                server = make_server(host, port, app, threaded=True)
            except OSError as e:
                print(f"Web viewer: failed to bind {host}:{port}: {e}")
                app_logger.log(
                    "error",
                    "web_server_bind_failed",
                    bind_host=host,
                    port=port,
                    error_message=str(e),
                    component="web_server",
                    action="start",
                )
                return
            server.serve_forever()

        threads = [threading.Thread(target=_run, args=(h,), name=f"sentinel-web-{i}", daemon=True)
                   for i, h in enumerate(bind_hosts)]
        for t in threads:
            t.start()
        self._thread = threads[0]
        print("Web viewer: " + ", ".join(f"http://{h}:{port}/" for h in bind_hosts))
        return True
