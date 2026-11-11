"""
Notes: private notes and shared lists (sentinel/engine/notes.py).
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from sentinel.web.context import WebContext
from sentinel.web.roles import ADMIN


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    def _who():
        """(person id or None for the household login, admin?, their name)"""
        viewer = ctx.viewer()
        return viewer.person_id, viewer.at_least(ADMIN), viewer.person.name if viewer.person is not None else "Household"

    @app.route("/notes")
    def notes_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("notes.html")

    @app.route("/api/notes")
    def api_notes():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        who, admin, _ = _who()
        names = {p.id: p.name for p in engine.locations.people()}
        resp = jsonify({"me": who, "notes": engine.notes.visible(who, admin, names)})
        resp.headers["Cache-Control"] = "no-store"  # (private notes: never kept by a browser or anything between)
        return resp

    @app.route("/api/notes", methods=["POST"])
    def api_notes_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        who, admin, name = _who()
        return _result(*engine.notes.save(who, admin, body if isinstance(body, dict) else {}, name))

    @app.route("/api/notes/<int:note_id>/items", methods=["POST"])
    def api_notes_items(note_id: int):
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        who, _, name = _who()
        return _result(*engine.notes.items(who, note_id, body if isinstance(body, dict) else {}, name))

    @app.route("/api/notes/<int:note_id>/delete", methods=["POST"])
    def api_notes_delete(note_id: int):
        err = _config_guard()
        if err:
            return err
        who, admin, _ = _who()
        return _result(*engine.notes.delete(who, admin, note_id))
