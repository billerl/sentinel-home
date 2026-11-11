"""
The Settings page (with Help), the web password, the Telegram test and the status panel.
"""

from __future__ import annotations


from flask import Flask, jsonify, redirect, render_template, request, url_for

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import WebContext
from sentinel.web.roles import OWNER, OWNER_SETTINGS


def register(app: Flask, ctx: WebContext) -> None:
    server, settings, engine, auth = ctx.server, ctx.settings, ctx.engine, ctx.auth
    _require_session, _viewer_person_id = ctx.require_session, ctx.viewer_person_id
    _config_guard, _result = ctx.config_guard, ctx.result

    @app.route("/settings")
    def settings_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("settings.html")

    @app.route("/help")
    def help_page():
        return redirect(url_for("settings_page") + "#help")

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "GET":
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            view = engine.settings_view()
            view["web_username"] = str(server._auth_cfg().get("username", ""))
            view["owner_only"] = sorted(OWNER_SETTINGS)  # (the page greys these out for an admin)
            return jsonify(view)
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        if not ctx.viewer().at_least(OWNER):
            # The page saves a whole section at once, so an admin's save carries the owner's settings too:
            # the ones left as they were are simply not theirs to save; a changed one is refused.
            current = engine.settings_view()["values"]
            from sentinel.engine import admin

            for key in OWNER_SETTINGS & body.keys():
                validator = admin.SETTINGS_SCHEMA.get(key)
                was, now = current.get(key), body[key]
                same = was == now or (was in ("", None) and now in ("", None))
                try:
                    same = same or (validator is not None and validator(now) == validator(was))
                except (TypeError, ValueError):
                    pass
                if not same and not (validator is None and body[key] in ("", None)):  # (a secret left empty)
                    return ctx.denied(f"Only the household (owner) login can change this ({key.replace('_', ' ')}).")
                body = {k: v for k, v in body.items() if k != key}
            if not body:
                return _result(True, "saved")
        return _result(*engine.update_settings(body))

    @app.route("/api/backup", methods=["GET", "POST"])
    def api_backup():
        # GET: the USB drives plugged in and last night's result; POST: {"now": true} or {"use": "/dev/sdX1"}
        if request.method == "GET":
            redir = _require_session()
            if redir:
                return jsonify({"error": "unauthorized"}), 401
            if engine is None:
                return jsonify({"error": "configuration unavailable"}), 503
            return jsonify(engine.backup.status())
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        if body.get("use"):
            ok, message = engine.backup.use_drive(str(body["use"]))
            if ok:
                engine.backup.start()
            return _result(ok, message)
        if body.get("now"):
            return _result(True, "Backing up now." if engine.backup.start() else "A backup is already running.")
        return jsonify({"error": "nothing to do"}), 400

    @app.route("/api/settings/telegram-test", methods=["POST"])
    def api_telegram_test():
        err = _config_guard()
        if err:
            return err
        return _result(*engine.send_telegram_test())

    @app.route("/api/settings/password", methods=["POST"])
    def api_change_password():
        err = _config_guard()
        if err:
            return err
        ip = _client_ip(request.remote_addr or "", request.headers)
        locked, retry_after = auth.is_locked(ip)
        if locked:
            return jsonify({"error": f"too many attempts; try again in {int(retry_after) + 1}s"}), 429
        body = request.get_json(silent=True) or {}
        current = str(body.get("current", ""))
        new = str(body.get("new", ""))
        me = _viewer_person_id()
        if me is not None:  # signed in as themselves: their own password
            person = engine.locations.person(me)
            if person is None or engine.family.person_login(person.username, current) is None:
                auth.record_failure(ip)
                return jsonify({"error": "current password is wrong"}), 400
            from sentinel.engine.location import MIN_WEB_PASSWORD

            if len(new) < MIN_WEB_PASSWORD:
                return jsonify({"error": f"new password must be at least {MIN_WEB_PASSWORD} characters"}), 400
            engine.locations.set_web_password(me, new)
            app_logger.log("info", "web_password_changed", ip=ip, person=person.name,
                           component="web_server", action="change_password")
            return jsonify({"ok": True, "message": "password changed"})
        username = str(server._auth_cfg().get("username", ""))
        if not auth.verify_password(username, current):
            auth.record_failure(ip)
            app_logger.log("warning", "web_password_change_failed", ip=ip,
                           component="web_server", action="change_password")
            return jsonify({"error": "current password is wrong"}), 400
        if len(new) < 10:
            return jsonify({"error": "new password must be at least 10 characters"}), 400
        import bcrypt

        new_hash = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
        web = dict(server._web_cfg())
        web["auth"] = {**server._auth_cfg(), "password_hash": new_hash}
        settings.settings["web"] = web
        settings.save_settings(force=True)
        auth.reload_config(web["auth"])
        app_logger.log("info", "web_password_changed", ip=ip,
                       component="web_server", action="change_password")
        return jsonify({"ok": True, "message": "password changed"})

    @app.route("/api/status")
    def api_status():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "status unavailable"}), 503
        return jsonify(engine.system_status())
