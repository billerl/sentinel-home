"""
Routines: "when this happens, do that" (sentinel/engine/routines.py).
"""

from __future__ import annotations


from flask import Flask, jsonify, render_template, request

from sentinel.web.context import WebContext
from sentinel.web.roles import ADMIN


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    # ---- routines: "when this happens, do that" (sentinel/engine/routines.py) ----
    @app.route("/routines")
    def routines_page():
        redir = _require_session()
        if redir:
            return redir
        return render_template("routines.html")

    @app.route("/api/routines")
    def api_routines():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"routines": [], "catalog": {}})
        view = engine.routines_view()
        viewer = ctx.viewer()
        names = {p.id: p.name for p in engine.locations.people()}
        for r in view["routines"]:  # "made by", and whether this login may change it
            r["by"] = names.get(r["owner"], "") if r["owner"] is not None else ""
            r["mine"] = viewer.at_least(ADMIN) or (r["owner"] is not None and r["owner"] == viewer.person_id)
        view["me"] = {"role": viewer.role, "person": viewer.person_id}
        return jsonify(view)

    def _not_theirs(routine_id):
        """A user changes only the routines they made; household ones (and other people's) are an admin's."""
        viewer = ctx.viewer()
        if viewer.at_least(ADMIN):
            return None
        r = engine.routines.store.get(routine_id) if isinstance(routine_id, int) else None
        if r is not None and (r.owner is None or r.owner != viewer.person_id):
            return ctx.denied("You can change the routines you made yourself; an admin changes the others.")
        return None

    @app.route("/api/routines", methods=["POST"])
    def api_routines_save():
        err = _config_guard()
        if err:
            return err
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        viewer = ctx.viewer()
        denied = _not_theirs(body.get("id"))
        if denied:
            return denied
        if not viewer.at_least(ADMIN):
            # (until each person has their own Telegram chat, a message would go to the household's)
            steps = body["spec"].get("then") if isinstance(body.get("spec"), dict) else None
            if any(isinstance(a, dict) and a.get("do") == "telegram" for a in (steps if isinstance(steps, list) else [])):
                return ctx.denied("Your own routines can't send Telegram messages yet: those go to the household's "
                                  "chat. Ask an admin to make this one.")
        # whoever makes it with their own login is its "made by" (kept through later edits)
        return _result(*engine.routine_save(body, owner=viewer.person_id if body.get("id") is None else None))

    @app.route("/api/routines/check", methods=["POST"])
    def api_routines_check():
        err = _config_guard()
        if err:
            return err
        return jsonify(engine.routine_check(request.get_json(silent=True) or {}))

    @app.route("/api/routines/<int:routine_id>/<action>", methods=["POST"])
    def api_routines_action(routine_id, action):
        err = _config_guard()
        if err:
            return err
        denied = _not_theirs(routine_id)
        if denied:
            return denied
        return _result(*engine.routine_action(routine_id, action, (request.get_json(silent=True) or {}).get("on")))
