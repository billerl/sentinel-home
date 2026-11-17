"""
Print: upload a PDF or photo and the household printer prints it (sentinel/engine/printing.py).
"""

from __future__ import annotations

from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request

from sentinel.engine.printing import MAX_BYTES
from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    def _who() -> str:
        viewer = ctx.viewer()
        return viewer.person.name if viewer is not None and viewer.person is not None else "Household"

    @app.route("/print")
    def print_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("print.html", max_mb=MAX_BYTES // (1024 * 1024))

    @app.route("/api/print")
    def api_print():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        resp = jsonify(engine.printer.status())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/print", methods=["POST"])
    def api_print_send():
        """The file itself is the request body (not JSON, so not the usual write check): the custom header still
        can't be sent from another site without a CORS preflight, which Sentinel never grants."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        if request.headers.get("X-Sentinel-Request") != "1":
            return jsonify({"error": "bad request"}), 400
        request.max_content_length = MAX_BYTES + 1024  # (the rest of Sentinel takes forms only: 256 KB)
        if (request.content_length or 0) > MAX_BYTES:
            return _result(False, f"That file is too big to print from here (over {MAX_BYTES // (1024 * 1024)} MB).")
        data = request.get_data(cache=False)
        args = request.args
        return _result(*engine.printer.send(
            data, unquote(request.headers.get("X-File-Name", ""))[:200], copies=args.get("copies", "1"),
            color=args.get("color", "1") != "0", pages=args.get("pages", "")[:60], who=_who()))

    @app.route("/api/print/cancel", methods=["POST"])
    def api_print_cancel():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        return _result(*engine.printer.cancel(str(body.get("job") or "")[:130]))
