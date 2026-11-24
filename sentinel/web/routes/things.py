"""
The Things page: where the family's things are kept (sentinel/engine/things.py).
"""

from __future__ import annotations

from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request, send_file

from sentinel.engine.things import PHOTO_MAX_BYTES

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
        return jsonify({"things": engine.things.all(), "claude": engine.assistant.enabled()})

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
        # (the page has a box of its own for the spot; left out = said in one breath with the place)
        spot = body.get("spot") if isinstance(body.get("spot"), str) and (body.get("spot").strip() or thing_id) else None
        if thing_id is None:  # (a new entry may be several: "keys, key fobs")
            return _result(*engine.things.put_all(body.get("name"), body.get("place"), by=by, spot=spot))
        return _result(*engine.things.put(body.get("name"), body.get("place"), by=by, thing_id=thing_id, spot=spot))

    @app.route("/api/things/<int:thing_id>/delete", methods=["POST"])
    def api_things_delete(thing_id: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.things.forget(thing_id=thing_id))

    # ---- from a photo: Claude lists what's in a drawer, the person says what's right (PhotoScans) ----
    def _by() -> str:
        viewer = ctx.viewer()
        return viewer.person.name if viewer is not None and viewer.person is not None else "Household"

    @app.route("/api/things/photo", methods=["POST"])
    def api_things_photo():
        """The photo itself is the body (a JPEG the page already shrank); where it is comes in a header."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None or request.headers.get("X-Sentinel-Request") != "1":
            return jsonify({"error": "configuration unavailable"}), 503
        if not engine.assistant.enabled():
            return jsonify({"error": "Reading a photo needs “ask Claude” turned on (Settings → Assistant)."}), 400
        request.max_content_length = PHOTO_MAX_BYTES + 1024  # (the rest of Sentinel takes forms only: 256 KB)
        ok, said = engine.thing_photos.start(request.get_data(cache=False),
                                             unquote(request.headers.get("X-Place", ""))[:200], _by(),
                                             spot=unquote(request.headers.get("X-Spot", ""))[:60] or None)
        return jsonify({"ok": True, "id": said}) if ok else (jsonify({"error": said}), 400)

    @app.route("/api/things/photo/<sid>")
    def api_things_photo_state(sid: str):
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        scan = engine.thing_photos.view(sid) if engine is not None else None
        return jsonify(scan) if scan is not None else (jsonify({"error": "That photo has timed out. Send it again."}), 404)

    @app.route("/api/things/photo/<sid>/save", methods=["POST"])
    def api_things_photo_save(sid: str):
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if isinstance(body, dict) and body.get("cancel"):
            engine.thing_photos.cancel(sid)
            return _result(True, "Okay, nothing kept.")
        return _result(*engine.thing_photos.save(sid, body.get("names") if isinstance(body, dict) else None))

    @app.route("/things/photo/<name>")
    def things_photo_file(name: str):
        redir = _require_session()
        if redir:
            return redir
        path = engine.thing_photos.photo_path(name) if engine is not None else None
        if path is None:
            return "", 404
        return send_file(path, mimetype="image/jpeg", max_age=30 * 86400)
