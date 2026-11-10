"""
The Talk button (sentinel/engine/phone_talk.py): a phone's recording in, Jarvis's answer out.
"""

from __future__ import annotations

import base64
import binascii

from flask import Flask, jsonify, request

from sentinel.web.context import WebContext

MAX_UPLOAD = 4 * 1024 * 1024  # 30 s of phone audio, base64 in JSON (other requests keep the 256 KB cap)


def register(app: Flask, ctx: WebContext) -> None:
    engine, _config_guard = ctx.engine, ctx.config_guard

    @app.route("/api/talk", methods=["POST"])
    def api_talk():
        request.max_content_length = MAX_UPLOAD
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("audio"), str):
            return jsonify({"error": "expected {\"audio\": base64}"}), 400
        try:
            audio = base64.b64decode(body["audio"], validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "bad audio"}), 400
        out = engine.phone_talk.talk(audio, str(body.get("pending") or ""))
        return (jsonify(out), 400) if "error" in out else jsonify(out)

    @app.route("/api/talk/answer", methods=["POST"])
    def api_talk_answer():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        out = engine.phone_talk.answer(str(body.get("id") or ""), bool(body.get("yes")))
        return (jsonify(out), 400) if "error" in out else jsonify(out)
