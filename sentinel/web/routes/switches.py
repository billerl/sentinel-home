"""
Smart plugs and switches (sentinel/engine/switches.py) and the TV remote (sentinel/engine/samsung_tv.py).
"""

from __future__ import annotations

import time

from flask import Flask, Response, jsonify, request

from sentinel.web.auth import _client_ip
from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, auth, _session_token, _require_session = ctx.engine, ctx.auth, ctx.session_token, ctx.require_session
    _config_guard, _result = ctx.config_guard, ctx.result

    # ---- smart plugs and switches: the Live page's Switches panel (sentinel/engine/switches.py) ----
    @app.route("/api/switches")
    def api_switches():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"switches": [], "scanning": False})
        return jsonify(engine.switches.view())

    @app.route("/api/switches/set", methods=["POST"])
    def api_switches_set():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get("on"), bool):
            return jsonify({"error": "say on or off"}), 400
        ok, message = engine.switches.set(str(body.get("id") or "")[:40], body["on"], source="web")
        return (jsonify({"ok": True, "message": message, **engine.switches.view()}) if ok
                else (jsonify({"error": message, **engine.switches.view()}), 400))

    @app.route("/api/switches/rename", methods=["POST"])
    def api_switches_rename():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.switches.rename(str(body.get("id") or "")[:40], str(body.get("name") or "")[:60]))

    @app.route("/api/switches/scan", methods=["POST"])
    def api_switches_scan():
        err = _config_guard()
        if err:
            return err
        engine.switches.request_scan()
        return _result(True, "looking")

    # ---- the TV remote (a Samsung TV among the switches; sentinel/engine/samsung_tv.py) ----
    @app.route("/api/tv/<tv_id>")
    def api_tv(tv_id):
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        view = engine.switches.tv_view(tv_id[:40]) if engine is not None else None
        return jsonify(view) if view else (jsonify({"error": "no such TV"}), 404)

    @app.route("/api/tv/<tv_id>/icon/<app_id>")
    def api_tv_icon(tv_id, app_id):
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        data = engine.switches.tv_icon(tv_id[:40], app_id[:40]) if engine is not None else None
        if not data:
            return "", 404
        resp = app.response_class(data, mimetype="image/png")
        resp.headers["Cache-Control"] = "private, max-age=604800"
        return resp

    @app.route("/api/tv/<tv_id>/screen")
    def api_tv_screen(tv_id):
        """The TV's screen as MJPEG, while the remote is open (sentinel/engine/tv_mirror.py)."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        mirror = engine.switches.tv_screen(tv_id[:40]) if engine is not None else None
        if mirror is None:
            return jsonify({"error": "this TV can't show its screen"}), 404
        ip = _client_ip(request.remote_addr or "", request.headers)
        token = _session_token()
        # (Not counted against the camera streams' limit: one picture of one TV, only while its remote is open.)

        def generate():
            last, sent_at, started = -1, 0.0, time.monotonic()
            next_auth_check = time.monotonic() + 5.0
            while True:
                if time.monotonic() >= next_auth_check:
                    if auth is None or not auth.validate_session(token, ip):
                        return
                    next_auth_check = time.monotonic() + 5.0
                jpeg, count = mirror.latest()  # also keeps the TV recording
                if jpeg is None and time.monotonic() - started > 30.0:
                    return  # the TV isn't sending a picture (off, or not answering): the page says so
                # A still screen is sent again every 2 s: a closed page is only noticed when sending fails,
                # and until then the TV would keep recording.
                if jpeg is not None and (count != last or time.monotonic() - sent_at > 2.0):
                    last, sent_at = count, time.monotonic()
                    yield (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
                           ).encode() + jpeg + b"\r\n"
                time.sleep(0.1)

        resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.route("/api/tv/<tv_id>/<action>", methods=["POST"])
    def api_tv_action(tv_id, action):
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        tv_id = tv_id[:40]
        if action == "key":
            return _result(*engine.switches.tv_key(tv_id, str(body.get("key") or "")[:12]))
        if action == "open":
            return _result(*engine.switches.tv_open(tv_id, str(body.get("app") or "")[:40]))
        if action == "type":
            return _result(*engine.switches.tv_type(tv_id, str(body.get("text") or "")[:100]))
        if action == "live":
            return _result(*engine.switches.tv_live(tv_id, body.get("reload") is True))
        if action == "tap":
            return _result(*engine.switches.tv_tap(tv_id, body.get("x"), body.get("y")))
        return jsonify({"error": "unknown action"}), 404
