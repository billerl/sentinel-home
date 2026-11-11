"""
The House page: a 3D drawing of the home with the devices on it (sentinel/engine/house.py).
"""

from __future__ import annotations


from flask import Flask, jsonify, render_template, request

from sentinel.web.context import TV_BROWSER, WebContext


def register(app: Flask, ctx: WebContext) -> None:
    broker, engine, _require_session, _config_guard = ctx.broker, ctx.engine, ctx.require_session, ctx.config_guard
    _result = ctx.result

    # ---- the House page: a 3D drawing of the home with the devices on it (sentinel/engine/house.py) ----
    @app.route("/house")
    def house_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("house.html", tv=bool(TV_BROWSER.search(request.headers.get("User-Agent", ""))))

    @app.route("/api/house")
    def api_house():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"drawing": None, "devices": []})
        return jsonify(engine.house.view())

    @app.route("/api/house/live")
    def api_house_live():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"paths": [], "boards": [], "switches": [], "cameras": [], "remotes": [], "heater": {}})
        live = engine.house_live()
        # cameras are placed by address (the web id changes at every start); the page snapshots by web id
        live["cameras"] = [{"id": host or cid, "web": cid, "name": disp} for cid, host, disp in broker.list_camera_addresses()]
        return jsonify(live)

    @app.route("/api/house/device", methods=["POST"])
    def api_house_device():
        """Place or move a device ({"kind", "ref", "at", "level", "turn"}) or take it off ({"id", "remove": true})."""
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        ok, message = (engine.house.remove(body.get("id")) if body.get("remove") is True
                       else engine.house.place(body))
        return (jsonify({"ok": True, "message": message, **engine.house.view()}) if ok
                else (jsonify({"error": message}), 400))

    @app.route("/api/house/drawing", methods=["POST"])
    def api_house_drawing():
        """Replace the drawing (rooms, features, marks); devices stay where they are."""
        err = _config_guard()
        if err:
            return err
        return _result(*engine.house.set_drawing(request.get_json(silent=True)))
