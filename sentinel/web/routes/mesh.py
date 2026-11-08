"""
Mesh chat: the Mesh page, and the LoRa base station's check-in (sentinel/engine/mesh.py).
"""

from __future__ import annotations

from functools import lru_cache

import segno
from flask import Flask, Response, jsonify, render_template, request

from sentinel.web.auth import _client_ip
from sentinel.web.context import WebContext


@lru_cache(maxsize=8)
def _qr(url: str) -> str:
    """(The page asks every few seconds; the invites hardly ever change.)"""
    return segno.make(url, error="m").svg_inline(scale=4, dark="#000", light="#fff", border=2)


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    @app.route("/mesh/bridge", methods=["POST"])
    def mesh_bridge():
        # The base station, not a browser: HTTP Basic with a pet tracker's login (the one it already has).
        ip = _client_ip(request.remote_addr or "", request.headers)
        if engine is None:
            return jsonify({}), 503
        if ctx.device_login_blocked(ip):
            return jsonify({}), 429
        creds = request.authorization
        reply = engine.mesh.bridge_checkin(creds.username if creds else "", creds.password if creds else "",
                                           request.get_json(silent=True, force=True))
        if reply is None:
            ctx.device_login_failed(ip, "mesh base station")
            return Response("{}", status=401, mimetype="application/json",
                            headers={"WWW-Authenticate": 'Basic realm="Sentinel"'})
        return jsonify(reply)

    @app.route("/mesh")
    def mesh_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("mesh.html")

    @app.route("/api/mesh")
    def api_mesh():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        view = engine.mesh.view()
        view["qr"] = {k: _qr(url) for k, url in view["invites"].items() if url}
        resp = jsonify(view)
        resp.headers["Cache-Control"] = "no-store"  # (holds the Family channel's key)
        return resp

    @app.route("/api/mesh/thread")
    def api_mesh_thread():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return jsonify({"messages": engine.mesh.thread(request.args.get("peer", ""))})

    @app.route("/api/mesh/send", methods=["POST"])
    def api_mesh_send():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.mesh.send(body.get("peer"), body.get("text")))

    @app.route("/api/mesh/family-channel", methods=["POST"])
    def api_mesh_family_channel():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.mesh.make_family_channel())

    @app.route("/api/mesh/base-name", methods=["POST"])
    def api_mesh_base_name():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.mesh.rename_base(body.get("name"), body.get("short")))

    @app.route("/api/mesh/nickname", methods=["POST"])
    def api_mesh_nickname():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.mesh.set_nickname(body.get("node"), body.get("name")))

    @app.route("/api/mesh/unread")
    def api_mesh_unread():
        # the Live page's 📡 (a TV: asked every 20 s)
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(engine.mesh.unread() if engine is not None else {"unread": 0, "from": []})

    @app.route("/api/mesh/speak", methods=["POST"])
    def api_mesh_speak():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.mesh.set_speak((request.get_json(silent=True) or {}).get("on")))

    @app.route("/api/mesh/alerts", methods=["POST"])
    def api_mesh_alerts():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.mesh.set_alerts((request.get_json(silent=True) or {}).get("on")))
