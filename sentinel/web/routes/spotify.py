"""
Spotify: now-playing strip, controls, sign-in (sentinel/engine/spotify.py), and this PC as a speaker
(sentinel/audio/spotify_player.py).
"""

from __future__ import annotations


from flask import Flask, jsonify, request

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    # ---- Spotify: now-playing strip, controls, sign-in (sentinel/engine/spotify.py) ----
    @app.route("/api/spotify")
    def api_spotify():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"connected": False})
        return jsonify(engine.spotify.now_playing())

    @app.route("/api/spotify/control", methods=["POST"])
    def api_spotify_control():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        action, value = str(body.get("action") or ""), str(body.get("value") or "")
        if action not in ("pause", "resume", "next", "previous", "volume", "move", "play", "now"):
            return jsonify({"error": "unknown action"}), 400
        if action == "volume" and value not in ("+", "-") and not value.isdigit():
            return jsonify({"error": "bad volume"}), 400
        message = engine.spotify.command(action, value[:200])
        return jsonify({"ok": True, "message": message, "now": engine.spotify.now_playing(fresh=True)})

    @app.route("/api/spotify/devices")
    def api_spotify_devices():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None or not engine.spotify.connected():
            return jsonify({"devices": []})
        try:
            devices = engine.spotify.devices()
        except Exception as e:
            return jsonify({"devices": [], "error": str(e)})
        return jsonify({"devices": [{"name": d.get("name"), "type": d.get("type"), "active": d.get("is_active")}
                                    for d in devices]})

    @app.route("/api/spotify/login", methods=["POST"])
    def api_spotify_login():
        err = _config_guard()
        if err:
            return err
        try:
            return jsonify({"ok": True, "url": engine.spotify.begin_login()})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/spotify/finish", methods=["POST"])
    def api_spotify_finish():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.spotify.finish_login(str(body.get("url") or "")[:2000]))

    @app.route("/api/spotify/disconnect", methods=["POST"])
    def api_spotify_disconnect():
        err = _config_guard()
        if err:
            return err
        engine.spotify.disconnect()
        return _result(True, "disconnected")

    # ---- this PC as a Spotify speaker (sentinel/audio/spotify_player.py) ----
    @app.route("/api/spotify/speaker")
    def api_spotify_speaker():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"installed": False})
        return jsonify(engine.spotify_player.status())

    @app.route("/api/spotify/speaker/login", methods=["POST"])
    def api_spotify_speaker_login():
        err = _config_guard()
        if err:
            return err
        try:
            return jsonify({"ok": True, "url": engine.spotify_player.begin_login()})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/spotify/speaker/finish", methods=["POST"])
    def api_spotify_speaker_finish():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.spotify_player.finish_login(str(body.get("url") or "")[:3000]))

    @app.route("/api/spotify/speaker/signout", methods=["POST"])
    def api_spotify_speaker_signout():
        err = _config_guard()
        if err:
            return err
        engine.spotify_player.sign_out()
        return _result(True, "signed out")
