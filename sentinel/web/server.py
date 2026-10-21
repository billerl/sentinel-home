"""
Flask HTTP server: login, MJPEG streams, camera JSON API.
"""

from __future__ import annotations

import os
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
    send_from_directory,
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

        _register_static_types()
        app = Flask(__name__, template_folder=_template_dir(), static_folder=_static_dir())
        app.secret_key = auth_cfg["session_secret"]
        app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # forms and config JSON only

        @app.after_request
        def _security_headers(resp):
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "same-origin")
            return resp
        @app.route("/favicon.ico")
        def favicon():
            # Browsers ask for this before (and without) logging in.
            return send_from_directory(_static_dir(), "favicon.ico", mimetype="image/vnd.microsoft.icon",
                                       max_age=7 * 86400)

        self._app = app
        broker = self._broker
        settings = self._settings
        auth = self._auth
        limiter = self._limiter
        recordings = self._recordings
        engine = self._engine

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
            jpeg, _ = broker.get_latest_jpeg(camera_id)
            if jpeg is None:
                return "Not found", 404
            resp = Response(jpeg, mimetype="image/jpeg")
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
            if broker.get_latest(camera_id)[0] is None:
                return Response("Camera not found or offline", status=404)
            ip = _client_ip(request.remote_addr or "", request.headers)
            token = _session_token()
            if limiter is None or not limiter.try_acquire(ip):
                return "Too many concurrent streams", 503

            wcfg = settings.settings.get("web", {})
            max_fps = max(1, int(wcfg.get("max_fps", 10)))
            # Polling slightly faster than target FPS to ensure we don't miss a pump cycle
            poll_interval = 1.0 / (max_fps * 1.5)

            def generate():
                last_ts = 0.0
                last_frame_at = time.monotonic()
                next_auth_check = time.monotonic() + 5.0
                try:
                    while True:
                        now = time.monotonic()
                        if now >= next_auth_check:
                            # End streams whose session was logged out or expired.
                            if auth is None or not auth.validate_session(token, ip):
                                return
                            next_auth_check = now + 5.0
                        if now - last_frame_at > 20.0:
                            return  # camera went away; the page reconnects
                        jpeg, ts = broker.get_latest_jpeg(camera_id)
                        if jpeg is None:
                            time.sleep(0.1)
                            continue
                        # Only send if it's a new frame
                        if ts > last_ts:
                            last_frame_at = now
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

        # ------------------------------------------------ camera settings / zones
        def _write_request_ok() -> bool:
            # JSON + custom header: browsers won't send either cross-site without a
            # CORS preflight (which we never grant), so this blocks CSRF.
            return request.is_json and request.headers.get("X-Sentinel-Request") == "1"

        @app.route("/zones")
        def zones_page():
            redir = _require_session()
            if redir:
                return redir
            return render_template("zones.html")

        @app.route("/api/config/cameras")
        def api_camera_configs():
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            return jsonify(engine.camera_configs())

        @app.route("/api/config/cameras/<key>", methods=["POST"])
        def api_update_camera_config(key: str):
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            if not _write_request_ok():
                return jsonify({"error": "bad request"}), 400
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "expected a JSON object"}), 400
            ok, message = engine.update_camera_config(key, body)
            if not ok:
                return jsonify({"error": message}), 400
            app_logger.log(
                "info", "web_camera_config_saved", camera_ip=key, fields=sorted(body),
                ip=_client_ip(request.remote_addr or "", request.headers),
                component="web_server", action="update_camera_config",
            )
            return jsonify({"ok": True})

        def _config_guard():
            """Shared checks for config writes; returns an error response or None."""
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            if not _write_request_ok():
                return jsonify({"error": "bad request"}), 400
            return None

        def _result(ok: bool, message: str):
            return (jsonify({"ok": True, "message": message}) if ok
                    else (jsonify({"error": message}), 400))

        @app.route("/api/config/cameras", methods=["POST"])
        def api_add_camera():
            err = _config_guard()
            if err:
                return err
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "expected a JSON object"}), 400
            return _result(*engine.add_camera(body))

        @app.route("/api/config/cameras/<key>/delete", methods=["POST"])
        def api_remove_camera(key: str):
            err = _config_guard()
            if err:
                return err
            return _result(*engine.remove_camera(key))

        @app.route("/api/config/cameras/<key>/reconnect", methods=["POST"])
        def api_reconnect_camera(key: str):
            err = _config_guard()
            if err:
                return err
            return _result(*engine.reconnect_camera(key))

        @app.route("/vehicles")
        def vehicles_page():
            redir = _require_session()
            if redir:
                return redir
            return render_template("vehicles.html")

        @app.route("/api/household")
        def api_household():
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            return jsonify(engine.household_view())

        @app.route("/api/household/vehicles", methods=["POST"])
        def api_household_save():
            err = _config_guard()
            if err:
                return err
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "expected a JSON object"}), 400
            return _result(*engine.household_save(body))

        @app.route("/api/household/vehicles/<int:vehicle_id>/delete", methods=["POST"])
        def api_household_delete(vehicle_id: int):
            err = _config_guard()
            if err:
                return err
            return _result(*engine.household_delete(vehicle_id))

        @app.route("/api/household/assign", methods=["POST"])
        def api_household_assign():
            err = _config_guard()
            if err:
                return err
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "expected a JSON object"}), 400
            return _result(*engine.household_assign(body))

        @app.route("/household/photos/<int:sighting_id>.jpg")
        def household_photo(sighting_id: int):
            redir = _require_session()
            if redir:
                return "Unauthorized", 401
            if engine is None:
                return "Not found", 404
            path = engine.household.crop_path(sighting_id)
            if not os.path.isfile(path):
                return "Not found", 404
            return send_file(path, mimetype="image/jpeg", max_age=86400)

        @app.route("/settings")
        def settings_page():
            redir = _require_session()
            if redir:
                return redir
            return render_template("settings.html")

        @app.route("/help")
        def help_page():
            return redirect(url_for("settings_page") + "#help")

        @app.route("/api/settings", methods=["GET", "POST"])
        def api_settings():
            if request.method == "GET":
                redir = _require_session()
                if redir:
                    return jsonify({"error": "unauthorized"}), 401
                if engine is None:
                    return jsonify({"error": "configuration unavailable"}), 503
                view = engine.settings_view()
                view["web_username"] = str(self._auth_cfg().get("username", ""))
                return jsonify(view)
            err = _config_guard()
            if err:
                return err
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "expected a JSON object"}), 400
            return _result(*engine.update_settings(body))

        @app.route("/api/settings/telegram-test", methods=["POST"])
        def api_telegram_test():
            err = _config_guard()
            if err:
                return err
            return _result(*engine.send_telegram_test())

        @app.route("/api/settings/password", methods=["POST"])
        def api_change_password():
            err = _config_guard()
            if err:
                return err
            ip = _client_ip(request.remote_addr or "", request.headers)
            locked, retry_after = auth.is_locked(ip)
            if locked:
                return jsonify({"error": f"too many attempts; try again in {int(retry_after) + 1}s"}), 429
            body = request.get_json(silent=True) or {}
            current = str(body.get("current", ""))
            new = str(body.get("new", ""))
            username = str(self._auth_cfg().get("username", ""))
            if not auth.verify_password(username, current):
                auth.record_failure(ip)
                app_logger.log("warning", "web_password_change_failed", ip=ip,
                               component="web_server", action="change_password")
                return jsonify({"error": "current password is wrong"}), 400
            if len(new) < 10:
                return jsonify({"error": "new password must be at least 10 characters"}), 400
            import bcrypt

            new_hash = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
            web = dict(self._web_cfg())
            web["auth"] = {**self._auth_cfg(), "password_hash": new_hash}
            self._settings.settings["web"] = web
            self._settings.save_settings(force=True)
            auth.reload_config(web["auth"])
            app_logger.log("info", "web_password_changed", ip=ip,
                           component="web_server", action="change_password")
            return jsonify({"ok": True, "message": "password changed"})

        @app.route("/api/status")
        def api_status():
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "status unavailable"}), 503
            return jsonify(engine.system_status())

        @app.route("/api/config/cameras/<key>/snapshot.jpg")
        def api_camera_snapshot(key: str):
            """Clean frame (no overlays) for drawing zones on."""
            redir = _require_session()
            if redir:
                return redir
            frame = engine.camera_snapshot(key) if engine is not None else None
            if frame is None:
                return "Camera offline", 404
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                return "Encode error", 500
            resp = Response(buf.tobytes(), mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "no-store"
            if request.args.get("download"):
                name = f"{key}_{time.strftime('%Y%m%d_%H%M%S')}.jpg".replace(":", "-")
                resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
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
                            "hd": bool(c.get("hd_path")),
                            "labels": (c.get("labels") or "").split(",") if c.get("labels") else [],
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
