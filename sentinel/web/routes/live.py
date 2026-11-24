"""
The home page, the Live page and what it shows: camera list, snapshots, MJPEG streams, weather.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from typing import Dict

from flask import Flask, Response, jsonify, render_template, request

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import TV_BROWSER, WebContext


DARTS_KEY_FILE = ".darts_tv_key"  # (owner-only, not in git; see darts_key below)

# Changes every time Sentinel starts (an update means a restart).
BOOT_ID = f"{int(time.time())}"


def register(app: Flask, ctx: WebContext) -> None:
    server, settings, broker, engine = ctx.server, ctx.settings, ctx.broker, ctx.engine

    def games_url(key: str = "games_url") -> str:
        """The family game site's address (settings games_url; darts_url for its darts scorer), only if it's a
        plain web address."""
        url = str(server._settings.settings.get(key) or "").strip().rstrip("/")
        return url if re.match(r"^https?://[^\s\"'<>]+$", url) else ""
    auth, limiter, _session_token, _require_session = ctx.auth, ctx.limiter, ctx.session_token, ctx.require_session

    def darts_key() -> str:
        """What lets the darts scorer (a separate little site on this computer, with no logins of its own) put its
        scoreboard on a TV: a key made here once and kept in DARTS_KEY_FILE. The Home page's Darts tile carries it,
        so only phones that came from a signed-in Sentinel page get the "show on the TV" button."""
        try:
            with open(DARTS_KEY_FILE) as f:
                key = f.read().strip()
        except OSError:
            key = ""
        if len(key) < 16:
            key = secrets.token_urlsafe(18)
            fd = os.open(DARTS_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
        return key

    @app.route("/api/local/darts_tv", methods=["GET", "POST"])
    def darts_tv():
        """For the darts scorer on this computer only (never through the tunnel), with the key: the TVs that can
        show its scoreboard, and (POST) put it on one."""
        local = request.remote_addr in ("127.0.0.1", "::1") and not (
            request.headers.get("Cf-Connecting-Ip") or request.headers.get("Cf-Ray"))
        if not local or engine is None or not games_url("darts_url") or \
                not hmac.compare_digest(request.headers.get("X-Darts-Key", ""), darts_key()):
            return jsonify({"error": "not allowed"}), 403
        if request.method == "GET":
            return jsonify({"tvs": [{"id": k, "name": v} for k, v in engine.switches.camera_tvs().items()]})
        body = request.get_json(silent=True) or {}
        ok, said = engine.switches.tv_show_page(str(body.get("tv") or "")[:40], "the darts scoreboard", "#map=darts",
                                                "darts")
        return jsonify({"ok": ok, "message": said})

    @app.route("/")
    def index():
        """The home page; TVs (the kiosk live view) get the cameras straight away."""
        redir = _require_session()
        if redir:
            return redir
        if TV_BROWSER.search(request.headers.get("User-Agent", "")):
            return live_page()
        order = engine.home_layouts.get(ctx.viewer_person_id()) if engine is not None else []
        # Photos never go through Cloudflare: from the tunnel the tile opens them over Tailscale instead (the
        # family's phones run it; tested 2026-11-17: a tap from the tunnel page to the Tailscale address opens)
        photos_url = None if ctx.from_home_network(tailscale=True) or not ctx.tailscale_base else \
            ctx.tailscale_base + "/photos"
        return render_template("home.html", boot_id=BOOT_ID, tile_order=order, photos_url=photos_url,
                               games_url=games_url(),
                               darts_url=games_url("darts_url") and games_url("darts_url") + "?k=" + darts_key())

    @app.route("/api/home_layout", methods=["POST"])
    def api_home_layout():
        """This login's tile order on the home page ({"order": [tile keys]}; [] = back to the usual order)."""
        err = ctx.config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        return ctx.result(*engine.home_layouts.save(ctx.viewer_person_id(), body.get("order") if isinstance(body, dict) else None))

    @app.route("/live")
    def live_page():
        redir = _require_session()
        if redir:
            return redir
        cams = [{"id": cid, "display": disp} for cid, disp in broker.list_cameras()]
        wcfg = server._web_cfg()
        max_fps = max(1, int(wcfg.get("max_fps", 10)))
        snapshot_interval_ms = max(40, min(500, int(1000 / max_fps)))
        return render_template(
            "grid.html",
            boot_id=BOOT_ID,
            cameras=cams,
            snapshot_interval_ms=snapshot_interval_ms,
            games_url=games_url(),
            darts_url=games_url("darts_url"),
        )

    @app.route("/api/cameras")
    def api_cameras():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        cams = [{"id": cid, "display": disp} for cid, disp in broker.list_cameras()]
        resp = jsonify(cams)
        resp.headers["X-Sentinel-Boot"] = BOOT_ID  # open pages reload themselves after an update
        return resp

    slow_reports: Dict[str, float] = {}

    @app.route("/api/slow", methods=["POST"])
    def api_slow():
        """A page's report of requests that took long on the device (static/slowlog.js): logged as web_slow, with
        how long Sentinel itself took for each, so the log shows whether the time went in the connection or here."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        now, who = time.time(), request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
        if now - slow_reports.get(who, 0) < 5:
            return jsonify({"ok": False}), 429
        slow_reports[who] = now
        body = request.get_json(silent=True) or {}
        items = []
        for it in (body.get("items") if isinstance(body.get("items"), list) else [])[:20]:
            if isinstance(it, dict):
                items.append({k: (str(it.get(k))[:80] if k in ("path", "type") else it.get(k) if isinstance(it.get(k), (int, float)) else None)
                              for k in ("path", "type", "ms", "wait", "took")})
        if items:
            app_logger.log("info", "web_slow", page=str(body.get("page") or "")[:60], conn=str(body.get("conn") or "")[:12],
                           hidden=body.get("hidden") is True, items=items,
                           via="tunnel" if request.remote_addr == "127.0.0.1" else request.remote_addr,
                           component="web", action="report")
        return jsonify({"ok": True})

    @app.route("/api/weather")
    def api_weather():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"configured": False})
        data = dict(engine.weather.current())
        heater = getattr(engine, "heater", None)
        data["inside"] = heater.inside() if heater is not None else None  # the BroadLink's thermometer (RM4 Pro)
        return jsonify(data)

    @app.route("/api/weather/alerts")
    def api_weather_alerts():
        """Severe weather warnings now (the camera screens' red banner): polled every minute."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"alerts": engine.weather_alerts.active() if engine is not None else []})

    # ---- Family locations: OwnTracks posts, setup links, People page (sentinel/engine/location.py) ----
    @app.route("/snapshot.jpg")
    def snapshot():
        redir = _require_session()
        if redir:
            return redir
        camera_id = request.args.get("id", "")
        if not camera_id:
            return "Missing id", 400
        try:
            max_width = max(0, min(4000, int(request.args.get("w", "0"))))
        except ValueError:
            max_width = 0
        jpeg, _ = broker.get_latest_jpeg(camera_id, max_width or None)
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
        released = []

        def release_once():
            # The slot goes back when the response is closed, whichever way: a HEAD request (or one dropped before its
            # first frame) never starts generate(), so its finally never ran and the slot was kept for good; after
            # 8 of those nobody could watch live video (bug hunt 2026-11-13)
            if not released:
                released.append(True)
                limiter.release(ip)

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
                release_once()

        resp = Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )
        resp.call_on_close(release_once)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp
