"""
Family locations: OwnTracks posts, setup links, personal logins, the People and Timeline pages
(sentinel/engine/location.py).
"""

from __future__ import annotations

import time
from typing import Dict, List

import segno
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import PERSON_PREFIX, WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, auth, _require_session, _viewer_person_id = ctx.engine, ctx.auth, ctx.require_session, ctx.viewer_person_id
    _set_session_cookie, _base_url = ctx.set_session_cookie, ctx.base_url
    _config_guard, _result = ctx.config_guard, ctx.result

    location_failures: Dict[str, List[float]] = {}

    def _location_blocked(ip: str) -> bool:
        """Too many wrong phone logins / setup links from one address: refuse for a while."""
        now = time.time()
        recent = [t for t in location_failures.get(ip, []) if now - t < 600]
        location_failures[ip] = recent
        return len(recent) >= 20

    def _location_failed(ip: str, what: str) -> None:
        location_failures.setdefault(ip, []).append(time.time())
        app_logger.log("warning", "location_auth_failed", what=what, ip=ip, component="web_server",
                       action="location")


    @app.route("/owntracks", methods=["POST"])
    def owntracks():
        # The phone app, not a browser: HTTP Basic login with the person's own username and key.
        ip = _client_ip(request.remote_addr or "", request.headers)
        if engine is None:
            return jsonify([]), 503
        if _location_blocked(ip):
            return jsonify([]), 429
        creds = request.authorization
        payload = request.get_json(silent=True, force=True)
        reply = engine.owntracks_post(creds.username if creds else "", creds.password if creds else "", payload)
        if reply is None:
            _location_failed(ip, "phone login")
            return Response("[]", status=401, mimetype="application/json",
                            headers={"WWW-Authenticate": 'Basic realm="Sentinel"'})
        return jsonify(reply)

    def _join_or_404(code: str):
        ip = _client_ip(request.remote_addr or "", request.headers)
        if engine is None or _location_blocked(ip):
            return None
        view = engine.join_view(code, _base_url())
        if view is None:
            _location_failed(ip, "setup link")
        return view

    def _phone_kind() -> str:
        agent = request.headers.get("User-Agent", "")
        kind = "iPhone" if ("iPhone" in agent or "iPad" in agent) else "Android" if "Android" in agent else "other"
        if "GSA/" in agent or "FBAN" in agent or "Instagram" in agent or "; wv)" in agent:
            kind += " (inside another app)"  # e.g. opened from the Google app: OwnTracks links may not work
        return kind

    @app.route("/join/<code>")
    def join_page(code: str):
        view = _join_or_404(code)
        if view is None:
            return render_template("join.html", expired=True), 404
        # How far a setup got (a phone that never reports could have stopped anywhere).
        app_logger.log("info", "location_setup_opened", person=view["name"], phone=_phone_kind(),
                       sharing=view["sharing"], component="web_server", action="location")
        resp = Response(render_template("join.html", expired=False, v=view, code=code))
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/join/<code>/status")
    def join_status(code: str):
        view = _join_or_404(code)
        if view is None:
            return jsonify({"expired": True}), 404
        return jsonify({"sharing": view["sharing"]})

    @app.route("/join/<code>/sentinel.otrc")
    def join_config_file(code: str):
        view = _join_or_404(code)
        if view is None:
            return "This setup link has expired.", 404
        app_logger.log("info", "location_setup_file", person=view["name"], phone=_phone_kind(),
                       component="web_server", action="location")
        import json as _json

        return Response(_json.dumps(view["config"], indent=2), mimetype="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename=sentinel.otrc",
                                 "Cache-Control": "no-store"})

    # ---- a family member's own Sentinel login: the Family page makes a link, they pick a password ----
    def _account_or_none(code: str):
        ip = _client_ip(request.remote_addr or "", request.headers)
        if engine is None or _location_blocked(ip):
            return None
        person = engine.locations.by_login_code(code)
        if person is None:
            _location_failed(ip, "login link")
        return person

    @app.route("/account/<code>", methods=["GET", "POST"])
    def account_page(code: str):
        from sentinel.engine.location import MIN_WEB_PASSWORD

        person = _account_or_none(code)
        if person is None:
            return render_template("account.html", expired=True), 404
        error = None
        if request.method == "POST":
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            if len(pw) < MIN_WEB_PASSWORD:
                error = f"Use at least {MIN_WEB_PASSWORD} characters."
            elif pw != pw2:
                error = "The two passwords don't match."
            else:
                engine.locations.set_web_password(person.id, pw)
                ip = _client_ip(request.remote_addr or "", request.headers)
                token = auth.create_session(PERSON_PREFIX + person.username, ip, True)
                app_logger.log("info", "web_personal_login_created", person=person.name, ip=ip,
                               component="web_server", action="login")
                resp = redirect(url_for("timeline_page"))
                _set_session_cookie(resp, token, True)
                return resp
        resp = Response(render_template("account.html", expired=False, name=person.name,
                                        username=person.username, error=error, minimum=MIN_WEB_PASSWORD),
                        status=400 if error else 200)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/api/people/<int:person_id>/login-link", methods=["POST"])
    def api_people_login_link(person_id: int):
        err = _config_guard()
        if err:
            return err
        ok, url = engine.people_action(person_id, "login-link", _base_url())
        if not ok:
            return jsonify({"error": url}), 400
        return jsonify({"ok": True, "url": url,
                        "qr": segno.make(url, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)})

    @app.route("/timeline")
    def timeline_page():
        redir = _require_session()
        if redir:
            return redirect(url_for("login", next="/timeline"))
        return render_template("timeline.html")

    @app.route("/api/timeline")
    def api_timeline_people():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return jsonify({"me": _viewer_person_id(), "people": engine.timeline_people()})

    @app.route("/api/timeline/<int:person_id>/<date>")
    def api_timeline_day(person_id: int, date: str):
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        view = engine.timeline_day(person_id, date) if engine is not None else None
        if view is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(view)

    @app.route("/api/timeline/<int:person_id>/delete", methods=["POST"])
    def api_timeline_delete(person_id: int):
        err = _config_guard()
        if err:
            return err
        if _viewer_person_id() != person_id:  # only your own, signed in as yourself
            return jsonify({"error": "Only the person themselves can delete their history."}), 403
        return _result(*engine.timeline_delete(person_id, (request.get_json(silent=True) or {}).get("before")))

    @app.route("/people")
    def people_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("people.html")

    @app.route("/api/family")
    def api_family():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"people": engine.family_lights() if engine is not None else []})

    @app.route("/api/people")
    def api_people():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return jsonify(engine.people_view(_base_url()))

    @app.route("/api/people", methods=["POST"])
    def api_people_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        return _result(*engine.people_save(body))

    @app.route("/api/yard", methods=["POST"])
    def api_yard_save():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.yard_save(request.get_json(silent=True) or {}))

    @app.route("/api/people/<int:person_id>/<action>", methods=["POST"])
    def api_people_action(person_id: int, action: str):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.people_action(person_id, action, _base_url()))

    @app.route("/api/people/<int:person_id>/qr")
    def api_people_qr(person_id: int):
        # QR of the person's setup link, for scanning with their phone's camera in person.
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        invite = engine.locations.invite(person_id) if engine is not None else None
        if not invite:
            return jsonify({"error": "no setup link yet"}), 404
        url = engine._join_url(_base_url(), invite[0])
        return jsonify({"qr": segno.make(url, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)})

    @app.route("/api/places", methods=["POST"])
    def api_places_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        return _result(*engine.place_save(body))

    @app.route("/api/places/<int:place_id>/delete", methods=["POST"])
    def api_places_delete(place_id: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.place_delete(place_id))

    @app.route("/api/location/alerts", methods=["POST"])
    def api_location_alerts():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.location_alerts((request.get_json(silent=True) or {}).get("on")))
