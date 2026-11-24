"""
The Things page: where the family's things are kept (sentinel/engine/things.py).
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    @app.route("/things")
    def things_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("things.html")

    @app.route("/api/things")
    def api_things():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return jsonify({"things": engine.things.all()})

    @app.route("/api/things", methods=["POST"])
    def api_things_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        viewer = ctx.viewer()
        by = viewer.person.name if viewer is not None and viewer.person is not None else "Household"
        thing_id = body.get("id") if isinstance(body.get("id"), int) else None
        return _result(*engine.things.put(body.get("name"), body.get("place"), by=by, thing_id=thing_id))

    @app.route("/api/things/<int:thing_id>/delete", methods=["POST"])
    def api_things_delete(thing_id: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.things.forget(thing_id=thing_id))
