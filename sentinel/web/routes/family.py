"""
Family locations: OwnTracks posts, setup links, personal logins, the People, Timeline and Pet tracker pages
(sentinel/engine/location.py).
"""

from __future__ import annotations

import re
import time

import segno
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import PERSON_PREFIX, WebContext
from sentinel.web.roles import ADMIN, GUEST, OWNER, USER


OTHERS_TIMELINE_HOURS = 48  # how far back the owner and admins see someone else's timeline


def register(app: Flask, ctx: WebContext) -> None:
    engine, auth, _require_session, _viewer_person_id = ctx.engine, ctx.auth, ctx.require_session, ctx.viewer_person_id
    _set_session_cookie, _base_url = ctx.set_session_cookie, ctx.base_url
    _config_guard, _result = ctx.config_guard, ctx.result

    _location_blocked, _location_failed = ctx.device_login_blocked, ctx.device_login_failed  # (shared with mesh)

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
        reply = engine.family.owntracks_post(creds.username if creds else "", creds.password if creds else "", payload)
        if reply is None:
            _location_failed(ip, "phone login")
            return Response("[]", status=401, mimetype="application/json",
                            headers={"WWW-Authenticate": 'Basic realm="Sentinel"'})
        return jsonify(reply)

    def _join_or_404(code: str):
        ip = _client_ip(request.remote_addr or "", request.headers)
        if engine is None or _location_blocked(ip):
            return None
        view = engine.family.join_view(code, _base_url())
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
        ok, url = engine.family.people_action(person_id, "login-link", _base_url())
        if not ok:
            return jsonify({"error": url}), 400
        return jsonify({"ok": True, "url": url,
                        "qr": segno.make(url, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)})

    def _others_since(viewer):
        """From when this login may see another person's history: the last 48 hours for the owner and admins
        (enough to help find someone), never for anyone else. Their own, and pets', are not limited."""
        return time.time() - OTHERS_TIMELINE_HOURS * 3600 if viewer.at_least(ADMIN) else None

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
        viewer = ctx.viewer()
        return jsonify({"me": viewer.person_id, "others_hours": OTHERS_TIMELINE_HOURS if viewer.at_least(ADMIN) else 0,
                        "people": engine.family.timeline_people(viewer.person_id, _others_since(viewer), private=True)})

    @app.route("/api/timeline/<int:person_id>/<date>")
    def api_timeline_day(person_id: int, date: str):
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "not found"}), 404
        viewer, person = ctx.viewer(), engine.locations.person(person_id)
        since = None
        if person is not None and person.id != viewer.person_id and not person.is_pet:
            since = _others_since(viewer)  # someone else's history: the last two days for an admin, else none
            if since is None:
                return ctx.denied("A person's timeline is their own.")
        view = engine.family.timeline_day(person_id, date, since)
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
        return _result(*engine.family.timeline_delete(person_id, (request.get_json(silent=True) or {}).get("before")))

    @app.route("/people")
    def people_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("people.html")

    # ---- a lost pet: live map of the collar, search on/off, beep (sentinel/engine/family.py) ----
    @app.route("/lost")
    def lost_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("lost.html")

    @app.route("/api/lost")
    def api_lost():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"pets": [], "yard": [], "home": None, "poll_seconds": 30, "now": time.time()})
        return jsonify(engine.family.lost_view())

    @app.route("/where")
    def where_page():
        # Full-screen map of where people are, for a TV ("show me Xena's location"); ?p=all or ?p=3,5
        redir = _require_session()
        if redir:
            return redir
        return render_template("where.html")

    @app.route("/api/where")
    def api_where():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        which = (request.args.get("p") or "all").strip()
        if engine is None:
            return jsonify({"people": [], "home": None, "yard": [], "now": time.time()})
        return jsonify(engine.family.map_view(which))

    @app.route("/planes")
    def planes_page():
        # Full-screen map of the planes near the house, for a TV ("show me the planes")
        redir = _require_session()
        if redir:
            return redir
        return render_template("planes.html")

    @app.route("/api/planes")
    def api_planes():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"home": None, "planes": [], "error": None, "now": time.time()})
        from sentinel.web.routes.talk import _spot

        # (a phone sends where it is, when its owner allowed it: the planes over them while they're away)
        return jsonify(engine.planes.map_view(_spot(request.args.get("lat"), request.args.get("lon"))))

    @app.route("/api/planes/overhead")
    def api_planes_overhead():
        # The TVs ask every 10 s: a plane about to pass over the house flies across their screen (flyby.js)
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"passes": []})
        try:
            return jsonify({"passes": engine.planes.overhead()})
        except (OSError, ValueError):
            return jsonify({"passes": []})

    @app.route("/api/planes/flyby_demo", methods=["POST"])
    def api_planes_flyby_demo():
        # The Planes page's "Test fly-by": a random plane across the TVs' camera page within ~10 s
        err = _config_guard()
        if err:
            return err
        p = engine.planes.demo_flyby()
        who = " ".join(x for x in (p["airline"], re.sub(r"^[A-Z]{3}(?=\d)", "", p["callsign"]) if p["airline"] else "") if x)
        return _result(True, f"Sending {who + ' · ' if who else ''}{p['kind']} across the TVs: it'll fly within 10 seconds "
                             "(they need to be showing the cameras).")

    tiles_box = []  # (made on first use: it needs the engine's data folder)

    @app.route("/map_tile/<source>/<int:z>/<int:x>/<int:y>.png")
    def map_tile(source, z, x, y):
        # The /where map's pictures through Sentinel (sentinel/web/map_tiles.py): the TV's browser was turned away
        redir = _require_session()
        if redir:
            return Response(status=401)
        if engine is None:
            return Response(status=404)
        if not tiles_box:
            import os

            from sentinel.web.map_tiles import MapTiles

            tiles_box.append(MapTiles(os.path.join(os.path.dirname(engine.locations.db_path), ".map_tiles")))
        got = tiles_box[0].get(source, z, x, y)
        if got is None:
            return Response(status=404)
        return Response(got[0], mimetype=got[1], headers={"Cache-Control": "private, max-age=86400"})

    @app.route("/api/lost/<action>", methods=["POST"])
    def api_lost_action(action):
        err = _config_guard()
        if err:
            return err
        ids = (request.get_json(silent=True) or {}).get("ids")
        pets = engine.family.pets({i for i in ids if isinstance(i, int)} if isinstance(ids, list) else None)
        if not pets:
            return jsonify({"error": "No pet with a collar tracker."}), 400
        if action in ("start", "found"):
            return _result(True, engine.family.set_lost("lost" if action == "start" else "found", pets))
        if action == "beep":
            return _result(True, engine.family.beep_pets(pets))
        return jsonify({"error": "unknown action"}), 404

    @app.route("/api/family")
    def api_family():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"people": engine.family.family_lights() if engine is not None else []})

    @app.route("/api/people")
    def api_people():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        view = engine.family.people_view(_base_url())
        viewer = ctx.viewer()
        view["me"] = {"role": viewer.role, "person": viewer.person_id}
        if not viewer.at_least(ADMIN):  # a phone's setup link is its key: only for those who set phones up
            for p in view["people"]:
                p["invite"] = None
        return jsonify(view)

    @app.route("/api/people", methods=["POST"])
    def api_people_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        # Marking someone a pet (or giving them a collar tracker) removes their login and role, so for a person who
        # has a login, or an admin, it's the owner's to do, like removing a login (bug hunt 2026-11-13: web#4)
        try:
            before = engine.locations.person(int(body["id"])) if body.get("id") not in (None, "") else None
        except (TypeError, ValueError):
            before = None
        becomes_pet = body.get("pet") is True or bool(str(body.get("tracker") or "").strip())
        had_login = before is not None and engine.locations.has_web_login(before.id)
        if (before is not None and not before.is_pet and becomes_pet and not ctx.viewer().at_least(OWNER)
                and (had_login or before.role == ADMIN)):
            return ctx.denied(f"{before.name} has a login: only the household (owner) login can make them a pet "
                              "or give them a tracker (it removes their login).")
        ok, message = engine.family.people_save(body)
        if ok and had_login and auth is not None:
            after = engine.locations.person(before.id)
            if after is not None and after.is_pet:
                auth.revoke_user(PERSON_PREFIX + before.username)  # their login is gone: signed out everywhere too
        return _result(ok, message)

    @app.route("/api/yard", methods=["POST"])
    def api_yard_save():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.family.yard_save(request.get_json(silent=True) or {}))

    @app.route("/api/people/<int:person_id>/<action>", methods=["POST"])
    def api_people_action(person_id: int, action: str):
        err = _config_guard()
        if err:
            return err
        person = engine.locations.person(person_id)
        if (person is not None and person.role == ADMIN and action in ("delete", "new-password")
                and not ctx.viewer().at_least(OWNER)):
            return ctx.denied("Only the household (owner) login can do that to an admin.")
        ok, message = engine.family.people_action(person_id, action, _base_url())
        if ok and person is not None and action in ("delete", "remove-login") and auth is not None:
            auth.revoke_user(PERSON_PREFIX + person.username)  # signed out everywhere, at once
        if ok and action == "delete":
            engine.notes.forget_person(person_id)  # their private notes go with them
        return _result(ok, message)

    @app.route("/api/people/<int:person_id>/role", methods=["POST"])
    def api_people_role(person_id: int):
        # {"role": "guest" | "user" | "admin"}: what this person's own login may do (sentinel/web/roles.py)
        err = _config_guard()
        if err:
            return err
        role = (request.get_json(silent=True) or {}).get("role")
        person = engine.locations.person(person_id)
        if person is None:
            return jsonify({"error": "That person no longer exists."}), 400
        if role not in (GUEST, USER, ADMIN):
            return jsonify({"error": "Choose guest, user or admin."}), 400
        if person.is_pet:
            return jsonify({"error": f"{person.name} is a pet: no login, so no role."}), 400
        if not ctx.viewer().at_least(OWNER) and ADMIN in (role, person.role):
            return ctx.denied("Only the household (owner) login can make someone an admin or change an admin.")
        engine.locations.set_role(person_id, role)
        app_logger.log("info", "web_role_set", person=person.name, role=role, by=ctx.viewer().name,
                       component="web_server", action="roles")
        return _result(True, f"{person.name} is now {'an admin' if role == ADMIN else 'a ' + role}.")

    @app.route("/api/people/<int:person_id>/photo", methods=["POST"])
    def api_people_photo(person_id: int):
        # {"image": "data:image/jpeg;base64,..."} (made small by the page) or {"image": null} to remove it
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "image" not in body:
            return jsonify({"error": "expected {\"image\": ...}"}), 400
        viewer = ctx.viewer()
        if viewer.person_id != person_id and not viewer.at_least(ADMIN):
            return ctx.denied("You can change your own picture; an admin changes the others.")
        return _result(*engine.family.set_photo(person_id, body["image"]))

    @app.route("/person_photo/<int:person_id>.jpg")
    def person_photo(person_id: int):
        redir = _require_session()
        if redir:
            return Response(status=401)
        path = engine.locations.photo_path(person_id) if engine is not None else ""
        try:
            with open(path, "rb") as f:
                jpeg = f.read()
        except OSError:
            return Response(status=404)
        # (the page asks with ?v=<when it changed>, so a long cache is safe)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=31536000"})

    @app.route("/api/people/<int:person_id>/qr")
    def api_people_qr(person_id: int):
        # QR of the person's setup link, for scanning with their phone's camera in person.
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        invite = engine.locations.invite(person_id) if engine is not None else None
        if not invite:
            return jsonify({"error": "no setup link yet"}), 404
        url = engine.family.join_url(_base_url(), invite[0])
        return jsonify({"qr": segno.make(url, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)})

    @app.route("/api/places", methods=["POST"])
    def api_places_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        return _result(*engine.family.place_save(body))

    @app.route("/api/places/<int:place_id>/delete", methods=["POST"])
    def api_places_delete(place_id: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.family.place_delete(place_id))

    @app.route("/api/location/alerts", methods=["POST"])
    def api_location_alerts():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.family.location_alerts((request.get_json(silent=True) or {}).get("on")))
