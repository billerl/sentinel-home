"""
Flask HTTP server: login, MJPEG streams, camera JSON API.
"""

from __future__ import annotations

import re
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import cv2
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from sentinel.logging_utils import app_logger
from sentinel.web.auth import WebAuthManager, _client_ip
from sentinel.web.bind_util import bind_host_allowed
from sentinel.web.frame_broker import FrameBroker

if TYPE_CHECKING:
    from sentinel.recordings import RecordingStore
    from sentinel.settings import SettingsManager


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


class WebServer:
    """Runs Flask in a daemon thread when web.enabled and password_hash are set."""

    def __init__(
        self,
        settings_manager: "SettingsManager",
        frame_broker: FrameBroker,
        recording_store: Optional["RecordingStore"] = None,
    ) -> None:
        self._settings = settings_manager
        self._broker = frame_broker
        self._recordings = recording_store
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

        bind_host = str(cfg.get("bind_host", "127.0.0.1"))
        camera_block = str(cfg.get("camera_subnet_block", "192.168.0.0/24"))
        ok, msg = bind_host_allowed(bind_host, camera_block)
        if not ok:
            print(f"Web viewer: {msg}")
            return False

        if not (auth_cfg.get("session_secret") or "").strip():
            import secrets as sec

            auth_cfg["session_secret"] = sec.token_hex(32)
            merged = dict(self._web_cfg())
            merged["auth"] = {**auth_cfg}
            self._settings.settings["web"] = merged
            self._settings.save_settings(force=True)

        self._auth = WebAuthManager(auth_cfg)
        self._limiter = _StreamLimiter(
            int(cfg.get("max_concurrent_streams", 4)),
            int(cfg.get("max_concurrent_streams_per_session", 2)),
        )

        app = Flask(__name__, template_folder=_template_dir())
        app.secret_key = auth_cfg["session_secret"]
        self._app = app
        broker = self._broker
        settings = self._settings
        auth = self._auth
        limiter = self._limiter
        recordings = self._recordings

        def _loggable_username(typed: str) -> str:
            # People sometimes type their password into the username box; only
            # log the attempted name when it is the real one.
            configured = str(self._auth_cfg().get("username", ""))
            return typed if typed and typed == configured else "(other)"

        def _session_token() -> Optional[str]:
            return request.cookies.get("sentinel_session")

        def _require_session():
            if auth is None:
                return redirect(url_for("login"))
            ip = _client_ip(request.remote_addr or "", request.headers)
            tok = _session_token()
            rec = auth.validate_session(tok, ip)
            if not rec:
                return redirect(url_for("login"))
            return None

        @app.route("/login", methods=["GET", "POST"])
        def login():
            if auth is None:
                return "Web auth not configured", 503
            ip = _client_ip(request.remote_addr or "", request.headers)
            auth.reload_config(self._auth_cfg())

            if request.method == "GET":
                locked, retry_after = auth.is_locked(ip)
                return render_template(
                    "login.html",
                    locked=locked,
                    retry_after=int(retry_after) + 1 if locked else 0,
                )

            locked, retry_after = auth.is_locked(ip)
            if locked:
                resp = Response("Too many attempts", 429)
                resp.headers["Retry-After"] = str(int(retry_after) + 1)
                app_logger.log(
                    "warning",
                    "web_login_locked",
                    ip=ip,
                    username_attempted=_loggable_username(request.form.get("username", "")),
                    retry_after_seconds=int(retry_after) + 1,
                    component="web_server",
                    action="login",
                )
                return resp

            username = request.form.get("username", "")
            password = request.form.get("password", "")

            if auth.verify_password(username, password):
                auth.record_success(ip)
                token = auth.create_session(username, ip)
                app_logger.log(
                    "info",
                    "web_login_success",
                    username=username,
                    ip=ip,
                    user_agent=request.headers.get("User-Agent", ""),
                    component="web_server",
                    action="login",
                )
                nxt = request.args.get("next") or url_for("index")
                resp = redirect(nxt)
                secure = (
                    request.headers.get("X-Forwarded-Proto", "").lower() == "https"
                    or request.is_secure
                )
                
                # Check for "Remember me" checkbox
                remember = request.form.get("remember") == "1"
                if remember:
                    # One year in seconds
                    max_age = 365 * 86400
                else:
                    # Default session length (e.g., 7 days or as configured)
                    max_age = int(auth_cfg.get("session_max_days", 7)) * 86400

                resp.set_cookie(
                    "sentinel_session",
                    token,
                    httponly=True,
                    samesite="Lax",
                    secure=secure,
                    max_age=max_age,
                    path="/",
                )
                return resp

            now_locked, ra = auth.record_failure(ip)
            app_logger.log(
                "warning",
                "web_login_failed",
                username_attempted=_loggable_username(username),
                ip=ip,
                component="web_server",
                action="login",
            )
            if now_locked:
                app_logger.log(
                    "warning",
                    "web_login_locked",
                    ip=ip,
                    username_attempted=_loggable_username(username),
                    retry_after_seconds=int(ra) + 1,
                    component="web_server",
                    action="login",
                )
                resp = Response("Too many attempts", 429)
                resp.headers["Retry-After"] = str(int(ra) + 1)
                return resp
            return render_template("login.html", locked=False, error="Invalid username or password"), 401

        @app.route("/logout", methods=["POST"])
        def logout():
            if auth is None:
                return redirect(url_for("login"))
            tok = _session_token()
            if tok:
                rec = auth.revoke_session(tok, "logout")
                if rec:
                    app_logger.log(
                        "info",
                        "web_session_revoked",
                        username=rec.username,
                        ip=_client_ip(request.remote_addr or "", request.headers),
                        reason="logout",
                        component="web_server",
                        action="logout",
                    )
            resp = redirect(url_for("login"))
            resp.delete_cookie("sentinel_session", path="/")
            return resp

        @app.route("/")
        def index():
            redir = _require_session()
            if redir:
                return redir
            cams = [{"id": cid, "display": disp} for cid, disp in broker.list_cameras()]
            wcfg = self._web_cfg()
            max_fps = max(1, int(wcfg.get("max_fps", 10)))
            snapshot_interval_ms = max(40, min(500, int(1000 / max_fps)))
            return render_template(
                "grid.html",
                cameras=cams,
                snapshot_interval_ms=snapshot_interval_ms,
            )

        @app.route("/api/cameras")
        def api_cameras():
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            cams = [{"id": cid, "display": disp} for cid, disp in broker.list_cameras()]
            return jsonify(cams)

        @app.route("/snapshot.jpg")
        def snapshot():
            redir = _require_session()
            if redir:
                return redir
            camera_id = request.args.get("id", "")
            if not camera_id:
                return "Missing id", 400
            frame, _ = broker.get_latest(camera_id)
            if frame is None:
                return "Not found", 404
            wcfg = settings.settings.get("web", {})
            q = int(wcfg.get("jpeg_quality", 80))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
            if not ok:
                return "Encode error", 500
            resp = Response(buf.tobytes(), mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            return resp

        @app.route("/stream")
        def stream():
            """MJPEG by camera id in query string with high-precision polling."""
            redir = _require_session()
            if redir:
                return redir
            camera_id = request.args.get("id", "")
            if not camera_id:
                return Response("Missing id", status=400)
            ip = _client_ip(request.remote_addr or "", request.headers)
            if limiter is None or not limiter.try_acquire(ip):
                return "Too many concurrent streams", 503

            wcfg = settings.settings.get("web", {})
            max_fps = max(1, int(wcfg.get("max_fps", 10)))
            # Polling slightly faster than target FPS to ensure we don't miss a pump cycle
            poll_interval = 1.0 / (max_fps * 1.5)

            def generate():
                last_ts = 0.0
                try:
                    while True:
                        jpeg, ts = broker.get_latest_jpeg(camera_id)
                        if jpeg is None:
                            time.sleep(0.1)
                            continue
                        
                        # Only send if it's a new frame
                        if ts > last_ts:
                            header = (
                                f"--frame\r\n"
                                f"Content-Type: image/jpeg\r\n"
                                f"Content-Length: {len(jpeg)}\r\n\r\n"
                            ).encode()
                            yield header + jpeg + b"\r\n"
                            last_ts = ts
                        
                        time.sleep(poll_interval)
                finally:
                    limiter.release(ip)

            resp = Response(
                generate(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["X-Accel-Buffering"] = "no"
            return resp

        @app.route("/api/recordings")
        def api_recordings():
            """Timeline data for one local day: camera rows + clip offsets in seconds."""
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if recordings is None:
                return jsonify({"error": "recordings unavailable"}), 503
            from sentinel.recordings.store import local_day_bounds

            today = time.strftime("%Y-%m-%d")
            date_str = request.args.get("date") or today
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                return jsonify({"error": "bad date"}), 400
            try:
                day_start, day_end = local_day_bounds(date_str)
            except ValueError:
                return jsonify({"error": "bad date"}), 400

            clips = recordings.query(day_start, day_end)

            # Rows: live cameras first (keyed by host, as the detector names clips),
            # then any other cameras seen that day, with "unknown" last.
            rows: List[Dict[str, str]] = []
            seen = set()
            for key, display in broker.list_camera_hosts():
                if key not in seen:
                    seen.add(key)
                    rows.append({"key": key, "name": display})
            for clip in sorted(clips, key=lambda c: c["camera_key"] == "unknown"):
                if clip["camera_key"] not in seen:
                    seen.add(clip["camera_key"])
                    rows.append({"key": clip["camera_key"], "name": clip["camera_name"]})

            def _day(offset_days: int) -> str:
                return time.strftime(
                    "%Y-%m-%d", time.localtime(day_start + 43200 + offset_days * 86400)
                )

            now = time.time()
            return jsonify(
                {
                    "date": date_str,
                    "prev_date": _day(-1),
                    "next_date": _day(1) if date_str < today else None,
                    "day_seconds": day_end - day_start,
                    "now_offset": now - day_start if day_start <= now < day_end else None,
                    "cameras": rows,
                    "clips": [
                        {
                            "id": c["id"],
                            "cam": c["camera_key"],
                            "s": round(c["start_ts"] - day_start, 1),
                            "e": round(c["end_ts"] - day_start, 1),
                            "reason": c["reason"],
                        }
                        for c in clips
                    ],
                }
            )

        @app.route("/recordings/<int:recording_id>/video")
        def recording_video(recording_id: int):
            """Browser-playable MP4 of one clip (converted on first request, then cached)."""
            redir = _require_session()
            if redir:
                return redir
            if recordings is None:
                return "Recordings unavailable", 503
            path = recordings.web_playable_path(recording_id)
            if not path:
                return "Clip not found or could not be converted", 404
            return send_file(path, mimetype="video/mp4", conditional=True, max_age=3600)

        port = int(cfg.get("port", 8088))
        app_logger.log(
            "info",
            "web_server_start",
            bind_host=bind_host,
            port=port,
            component="web_server",
            action="start",
        )

        def _run() -> None:
            try:
                app.run(
                    host=bind_host,
                    port=port,
                    threaded=True,
                    use_reloader=False,
                    debug=False,
                )
            except OSError as e:
                print(f"Web viewer: failed to bind {bind_host}:{port}: {e}")
                app_logger.log(
                    "error",
                    "web_server_bind_failed",
                    bind_host=bind_host,
                    port=port,
                    error_message=str(e),
                    component="web_server",
                    action="start",
                )

        self._thread = threading.Thread(target=_run, name="sentinel-web", daemon=True)
        self._thread.start()
        print(f"Web viewer: http://{bind_host}:{port}/")
        return True
