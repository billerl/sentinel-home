"""
Camera setup: the Zones page, adding/testing/editing/removing cameras, clean frames to draw zones on.
"""

from __future__ import annotations

import time

import cv2
from flask import Flask, Response, jsonify, render_template, request

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result
    _write_request_ok = ctx.write_request_ok

    # ------------------------------------------------ camera settings / zones

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


    @app.route("/api/config/cameras", methods=["POST"])
    def api_add_camera():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        return _result(*engine.add_camera(body))

    @app.route("/api/config/cameras/test", methods=["POST"])
    def api_test_camera():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        result = dict(engine.test_camera(body))
        jpeg = result.pop("jpeg", None)
        if jpeg:
            import base64

            result["snapshot"] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        return jsonify(result)

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
