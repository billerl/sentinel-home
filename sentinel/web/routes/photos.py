"""
Photos: the family's photos on the backup drive, and everyone's own albums (sentinel/engine/photos.py).

Home network or Tailscale only (the family's choice, 2026-11-17: "I don't want to expose it to Cloudflare yet"):
through the tunnel every photo route is refused, and the page gives the home and Tailscale addresses. A
before_request check covers every route here, so a new one can't be forgotten.
"""

from __future__ import annotations

import os
from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request, send_file

from sentinel.engine.photos import PLAYS, UPLOAD_MAX
from sentinel.web.context import WebContext
from sentinel.web.roles import ADMIN


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    def _who():
        """(person id or None, the household login?, a name for their album folder)"""
        viewer = ctx.viewer()
        person = viewer.person if viewer is not None else None
        return (viewer.person_id if viewer is not None else None, person is None,
                person.name if person is not None else "Household")

    def _photos():
        return getattr(engine, "photos", None)

    PHOTO_ENDPOINTS = {"photos_page", "api_photos", "api_photos_folder", "photo_thumb", "photo_view", "photo_original",
                       "api_photo_album_new", "api_photo_album_share", "api_photo_album_delete", "api_photo_upload",
                       "api_photo_delete", "api_photo_rescan"}

    @app.before_request
    def _photos_at_home_only():
        if request.endpoint not in PHOTO_ENDPOINTS or ctx.from_home_network(tailscale=True):
            return None
        if request.endpoint == "photos_page":
            return render_template("photos_away.html", home=ctx.home_base and ctx.home_base + "/photos",
                                   tailscale=ctx.tailscale_base and ctx.tailscale_base + "/photos"), 403
        return jsonify({"error": "Photos open only on the home network."}), 403

    def _unauthorized():
        return jsonify({"error": "unauthorized"}), 401

    def _private(resp):
        resp.headers["Cache-Control"] = "private, max-age=86400"  # (a private album's photo: never a shared cache)
        return resp

    @app.route("/photos")
    def photos_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("photos.html")

    @app.route("/api/photos")
    def api_photos():
        if _require_session():
            return _unauthorized()
        photos = _photos()
        if photos is None:
            return jsonify({"error": "unavailable"}), 503
        photos.rescan_if_old()
        person, household, _ = _who()
        viewer = ctx.viewer()
        out = photos.overview(person, household)
        out["admin"] = viewer is not None and viewer.at_least(ADMIN)
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/photos/folder")
    def api_photos_folder():
        if _require_session():
            return _unauthorized()
        photos = _photos()
        person, household, _ = _who()
        try:
            offset = int(request.args.get("offset") or 0)
        except ValueError:
            offset = 0
        out = photos.listing(request.args.get("path", "")[:1000], person, household, offset) if photos else None
        if out is None:
            return jsonify({"error": "That folder isn't there, or isn't shared with you."}), 404
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _file():
        photos = _photos()
        person, household, _ = _who()
        return photos.file_for(request.args.get("p", "")[:1000], person, household) if photos else None

    @app.route("/photos/thumb")
    def photo_thumb():
        if _require_session():
            return _unauthorized()
        found = _file()
        out = _photos().thumbnail(*found) if found else None
        if out is None:
            return "", 404
        return _private(send_file(out, mimetype="image/jpeg", conditional=True))

    @app.route("/photos/view")
    def photo_view():
        if _require_session():
            return _unauthorized()
        found = _file()
        if found is None:
            return "", 404
        path, row = found
        if row["kind"] == "file":  # (a document in someone's album: downloaded, never shown inline)
            return _private(send_file(path, as_attachment=True, download_name=row["name"], conditional=True))
        if row["kind"] == "video":
            if os.path.splitext(path)[1].lower() not in PLAYS:
                return "", 415  # (an old AVI: the page offers it as a download)
            return _private(send_file(path, conditional=True))
        out = _photos().display(path, row)
        if out is None:  # (a picture Pillow can't open: the original as it is)
            return _private(send_file(path, conditional=True))
        return _private(send_file(out, mimetype="image/jpeg", conditional=True))

    @app.route("/photos/original")
    def photo_original():
        if _require_session():
            return _unauthorized()
        found = _file()
        if found is None:
            return "", 404
        return _private(send_file(found[0], as_attachment=True, download_name=found[1]["name"], conditional=True))

    # ---- albums: one's own
    @app.route("/api/photos/albums", methods=["POST"])
    def api_photo_album_new():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        person, _, name = _who()
        return _result(*_photos().create_album(str(body.get("name") or "")[:80], body.get("shared") is True,
                                               person, name))

    @app.route("/api/photos/albums/<int:album_id>/share", methods=["POST"])
    def api_photo_album_share(album_id: int):
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        person, household, _ = _who()
        return _result(*_photos().set_shared(album_id, body.get("shared") is True, person, household))

    @app.route("/api/photos/albums/<int:album_id>/delete", methods=["POST"])
    def api_photo_album_delete(album_id: int):
        err = _config_guard()
        if err:
            return err
        person, household, _ = _who()
        return _result(*_photos().delete_album(album_id, person, household))

    @app.route("/api/photos/albums/<int:album_id>/upload", methods=["POST"])
    def api_photo_upload(album_id: int):
        """The file is the request body (not JSON): the custom header still can't come from another site without
        a CORS preflight, which Sentinel never grants."""
        if _require_session():
            return _unauthorized()
        if _photos() is None:
            return jsonify({"error": "unavailable"}), 503
        if request.headers.get("X-Sentinel-Request") != "1":
            return jsonify({"error": "bad request"}), 400
        request.max_content_length = UPLOAD_MAX + 1024  # (the rest of Sentinel takes forms only)
        if (request.content_length or 0) > UPLOAD_MAX:
            return _result(False, f"That file is too big (over {UPLOAD_MAX // (1024 * 1024)} MB).")
        person, household, _ = _who()
        return _result(*_photos().upload(album_id, request.stream, unquote(request.headers.get("X-File-Name", ""))[:200],
                                         person, household))

    @app.route("/api/photos/delete", methods=["POST"])
    def api_photo_delete():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        person, household, _ = _who()
        return _result(*_photos().delete_photo(str(body.get("path") or "")[:1000], person, household))

    @app.route("/api/photos/rescan", methods=["POST"])
    def api_photo_rescan():
        err = _config_guard()
        if err:
            return err
        return _result(True, "Looking through the drive again.") if _photos().rescan() else \
            _result(False, "It's already looking, or the backup drive isn't plugged in.")
