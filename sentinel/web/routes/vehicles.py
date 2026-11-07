"""
The Vehicles page: which household car is which (recorded_videos/household.db).
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request, send_file

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

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
