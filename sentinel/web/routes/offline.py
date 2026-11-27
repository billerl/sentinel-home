"""
The Offline helper page (sentinel/engine/offline.py) and the offline library itself, shown under /library behind
Sentinel's own sign-in: the library server listens on 127.0.0.1 only, so phones reach it through Sentinel and no
new port is opened on the home network.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, stream_with_context

from sentinel.web.context import WebContext

PHOTO_MAX_BYTES = 4 * 1024 * 1024
PASS_HEADERS = ("Content-Type", "Content-Length", "Cache-Control", "ETag", "Last-Modified", "Content-Security-Policy",
                "Location")


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session = ctx.engine, ctx.require_session

    @app.route("/offline")
    def offline_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("offline.html")

    @app.route("/api/offline/status")
    def api_offline_status():
        if _require_session():
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return jsonify(engine.offline.status())

    @app.route("/api/offline/ask", methods=["POST"])
    def api_offline_ask():
        """A question (JSON), or a photo (the JPEG is the body; the question comes in a header)."""
        if _require_session():
            return jsonify({"error": "unauthorized"}), 401
        if engine is None or request.headers.get("X-Sentinel-Request") != "1":
            return jsonify({"error": "configuration unavailable"}), 503
        jpeg, history = None, []
        if request.mimetype == "image/jpeg":
            request.max_content_length = PHOTO_MAX_BYTES + 1024
            jpeg = request.get_data(cache=False)
            if not jpeg.startswith(b"\xff\xd8"):
                return jsonify({"error": "That doesn't look like a photo."}), 400
            from urllib.parse import unquote

            question = unquote(request.headers.get("X-Question", ""))[:1000]
        else:
            body = request.get_json(silent=True)
            body = body if isinstance(body, dict) else {}
            question = body.get("question") if isinstance(body.get("question"), str) else ""
            raw = body.get("history") if isinstance(body.get("history"), list) else []
            history = [(str(h[0])[:500], str(h[1])[:800]) for h in raw[-3:]
                       if isinstance(h, list) and len(h) == 2 and all(isinstance(x, str) for x in h)]
        ok, said = engine.offline.start(question, history=history, jpeg=jpeg)
        return jsonify({"ok": True, "id": said}) if ok else (jsonify({"error": said}), 400)

    @app.route("/api/offline/job/<job_id>")
    def api_offline_job(job_id: str):
        if _require_session():
            return jsonify({"error": "unauthorized"}), 401
        job = engine.offline.job(job_id) if engine is not None else None
        return jsonify(job) if job is not None else (jsonify({"error": "That answer has timed out. Ask again."}), 404)

    @app.route("/guides")
    def guides_page():
        """The local PDF guides by folder, or the pages that match a search."""
        redir = _require_session()
        if redir:
            return redir
        guides = getattr(getattr(engine, "offline", None), "guides", None)
        if guides is None:
            return "Sentinel isn't ready", 503
        q = (request.args.get("q") or "").strip()[:200]
        return render_template("guides.html", q=q, found=guides.search(q, 25) if q else [],
                               books=[] if q else guides.listing(), folder=bool(guides.folder()),
                               indexing=guides.indexing)

    @app.route("/guides/page/<int:rowid>")
    def guides_text(rowid: int):
        """One PDF page's text (readable on any phone), with a link to the original page and its pictures."""
        redir = _require_session()
        if redir:
            return redir
        guides = getattr(getattr(engine, "offline", None), "guides", None)
        page = guides.page(rowid) if guides is not None else None
        if page is None:
            abort(404)
        return render_template("guides.html", page=page, q="", found=[], books=[], folder=True, indexing="")

    @app.route("/guides/file/<path:rel>")
    def guides_file(rel: str):
        redir = _require_session()
        if redir:
            return redir
        guides = getattr(getattr(engine, "offline", None), "guides", None)
        path = guides.pdf_path(rel) if guides is not None else None
        if path is None:
            abort(404)
        return send_file(path, mimetype="application/pdf", conditional=True)

    @app.route("/library/", defaults={"sub": ""})
    @app.route("/library/<path:sub>")
    def library_proxy(sub: str):
        """The library server's pages, for signed-in family only (GET only: it has nothing to change)."""
        redir = _require_session()
        if redir:
            return redir
        if engine is None:
            return "Sentinel isn't ready", 503
        base = engine.offline.library_base()
        url = f"{base}/{sub}" + (f"?{request.query_string.decode('latin-1')}" if request.query_string else "")
        try:
            upstream = urllib.request.urlopen(urllib.request.Request(url, headers={
                k: v for k, v in (("Accept", request.headers.get("Accept")),
                                  ("Accept-Language", request.headers.get("Accept-Language")),
                                  ("If-None-Match", request.headers.get("If-None-Match")),
                                  ("Range", request.headers.get("Range"))) if v}), timeout=20)
            status = upstream.status
        except urllib.error.HTTPError as e:
            upstream, status = e, e.code
        except OSError:
            return render_template("offline.html", library_down=True), 503
        headers = {h: upstream.headers[h] for h in PASS_HEADERS if upstream.headers.get(h)}

        def body():
            try:
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        return Response(stream_with_context(body()), status=status, headers=headers)

