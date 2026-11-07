"""
Routines: "when this happens, do that" (sentinel/engine/routines.py).
"""

from __future__ import annotations


from flask import Flask, jsonify, render_template, request

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    # ---- routines: "when this happens, do that" (sentinel/engine/routines.py) ----
    @app.route("/routines")
    def routines_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("routines.html")

    @app.route("/api/routines")
    def api_routines():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"routines": [], "catalog": {}})
        return jsonify(engine.routines_view())

    @app.route("/api/routines", methods=["POST"])
    def api_routines_save():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.routine_save(request.get_json(silent=True) or {}))

    @app.route("/api/routines/check", methods=["POST"])
    def api_routines_check():
        err = _config_guard()
        if err:
            return err
        return jsonify(engine.routine_check(request.get_json(silent=True) or {}))

    @app.route("/api/routines/<int:routine_id>/<action>", methods=["POST"])
    def api_routines_action(routine_id, action):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.routine_action(routine_id, action, (request.get_json(silent=True) or {}).get("on")))
