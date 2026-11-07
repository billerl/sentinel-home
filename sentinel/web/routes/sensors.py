"""
Wi-Fi motion sensors: the Sensors and History pages (sentinel/engine/wifi_motion.py, firmware/wifi_motion).
"""

from __future__ import annotations

import time

from flask import Flask, jsonify, render_template, request

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    # ---- Wi-Fi motion sensors (sentinel/engine/wifi_motion.py, firmware/wifi_motion) ----
    @app.route("/sensors")
    def sensors_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("sensors.html")

    @app.route("/sensors/history")
    def sensors_history_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("sensors_history.html")

    @app.route("/api/wifi_motion/events")
    def api_wifi_motion_events():
        """One day's stretches of movement (?day=YYYY-MM-DD, default today) for the History page."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"events": [], "paths": []})
        try:
            day = time.mktime(time.strptime(request.args.get("day") or time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        except (ValueError, OverflowError):
            return jsonify({"error": "bad day"}), 400
        end = time.mktime(time.localtime(day + 26 * 3600)[:3] + (0, 0, 0, 0, 0, -1))  # next midnight (DST-safe)
        wm = engine.wifi_motion
        return jsonify({"day": time.strftime("%Y-%m-%d", time.localtime(day)), "start": day, "end": end,
                        "now": time.time(), "events": wm.events(day, end),
                        "paths": [{"id": k, "name": v} for k, v in wm.paths().items()]})

    @app.route("/api/wifi_motion")
    def api_wifi_motion():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"sensors": []})
        return jsonify(engine.wifi_motion.view())

    @app.route("/api/wifi_motion/rename", methods=["POST"])
    def api_wifi_motion_rename():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.wifi_motion.rename(str(body.get("id") or "")[:24], str(body.get("name") or "")[:60]))

    @app.route("/api/wifi_motion/board", methods=["POST"])
    def api_wifi_motion_board():
        """Rename a board ({"id", "name"}) or make its light flash ({"id", "identify": true})."""
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        bid = str(body.get("id") or "")[:12]
        if body.get("identify") is True:
            return _result(*engine.wifi_motion.identify(bid))
        return _result(*engine.wifi_motion.rename_board(bid, str(body.get("name") or "")[:60]))

    @app.route("/api/wifi_motion/adjust", methods=["POST"])
    def api_wifi_motion_adjust():
        """Move a path's movement line ({"id", "ratio"}) or relearn its quiet level ({"id", "relearn": true})."""
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        pid = str(body.get("id") or "")[:24]
        if body.get("relearn") is True:
            return _result(*engine.wifi_motion.relearn(pid))
        return _result(*engine.wifi_motion.set_ratio(pid, body.get("ratio")))
