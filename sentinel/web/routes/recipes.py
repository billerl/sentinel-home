"""
Recipes: the family recipe box page and its API (sentinel/engine/recipes.py).
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request, send_file

from sentinel.engine.recipes import CATEGORIES, split_have
from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    engine, _require_session, _config_guard, _result = ctx.engine, ctx.require_session, ctx.config_guard, ctx.result

    def _by() -> str:
        """Who is saving: the family member signed in with their own password, else nobody in particular."""
        pid = ctx.viewer_person_id()
        return next((p.name for p in engine.locations.people() if p.id == pid), "") if pid is not None else ""

    def _read_guard():
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if engine is None:
            return jsonify({"error": "configuration unavailable"}), 503
        return None

    def _body() -> dict:
        return request.get_json(silent=True) or {}

    @app.route("/recipes")
    def recipes_page():
        # also the phone's Share → Sentinel target (manifest share_target): ?url=…&text=… saves it straight away
        redir = _require_session()
        if redir:
            return redir
        return render_template("recipes.html")

    @app.route("/recipes/photo/<int:rid>")
    def recipe_photo(rid: int):
        redir = _require_session()
        if redir:
            return redir
        path = engine.recipes.photo_file(rid) if engine is not None else None
        if not path:
            return "", 404
        resp = send_file(path, mimetype="image/jpeg", max_age=30 * 86400)  # the URL changes with the photo (?v=)
        resp.headers["Cache-Control"] = "private, max-age=2592000"
        return resp

    @app.route("/api/recipes")
    def api_recipes():
        err = _read_guard()
        if err:
            return err
        return jsonify({"recipes": engine.recipes.recipes(), "categories": list(CATEGORIES),
                        "claude": engine.assistant.enabled()})

    @app.route("/api/recipes/<int:rid>")
    def api_recipe(rid: int):
        err = _read_guard()
        if err:
            return err
        found = engine.recipes.recipe(rid)
        return (jsonify(found), 200) if found else (jsonify({"error": "That recipe isn't in the box any more."}), 404)

    @app.route("/api/recipes/save", methods=["POST"])
    def api_recipes_save():
        # a link, or pasted text (with the link it came from, if any); runs in the background (a video takes minutes)
        err = _config_guard()
        if err:
            return err
        body = _body()
        if not str(body.get("url") or "").strip() and not str(body.get("text") or "").strip():
            return jsonify({"error": "Paste a recipe link or the recipe's text."}), 400
        return jsonify({"ok": True, "job": engine.recipes.start_save(body.get("url"), _by(), body.get("text"))})

    @app.route("/api/recipes/job/<job_id>")
    def api_recipes_job(job_id: str):
        err = _read_guard()
        if err:
            return err
        job = engine.recipes.job(job_id)
        return (jsonify(job), 200) if job else (jsonify({"error": "That save isn't running any more."}), 404)

    @app.route("/api/recipes/<int:rid>/version", methods=["POST"])
    def api_recipe_version(rid: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.recipes.save_version(rid, _body().get("doc"), _by()))

    @app.route("/api/recipes/<int:rid>/revert", methods=["POST"])
    def api_recipe_revert(rid: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.recipes.revert(rid))

    @app.route("/api/recipes/<int:rid>/category", methods=["POST"])
    def api_recipe_category(rid: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.recipes.set_category(rid, _body().get("category")))

    @app.route("/api/recipes/<int:rid>/photo", methods=["POST"])
    def api_recipe_photo(rid: int):
        # the page shrinks the picture to a JPEG first, so it fits the 256 KB request limit
        err = _config_guard()
        if err:
            return err
        body = _body()
        if body.get("remove"):
            return _result(*engine.recipes.remove_photo(rid))
        return _result(*engine.recipes.set_photo(rid, body.get("photo")))

    @app.route("/api/recipes/<int:rid>/delete", methods=["POST"])
    def api_recipe_delete(rid: int):
        err = _config_guard()
        if err:
            return err
        return _result(*engine.recipes.delete(rid))

    @app.route("/api/recipes/<int:rid>/swap", methods=["POST"])
    def api_recipe_swap(rid: int):
        err = _config_guard()
        if err:
            return err
        body = _body()
        ok, out = engine.recipes.swap(rid, body.get("doc"), body.get("ingredient"), body.get("note"))
        return (jsonify({"ok": True, "options": out}) if ok else (jsonify({"error": out}), 400))

    @app.route("/api/recipes/<int:rid>/twist", methods=["POST"])
    def api_recipe_twist(rid: int):
        err = _config_guard()
        if err:
            return err
        body = _body()
        ok, out = engine.recipes.twist(rid, body.get("doc"), body.get("request"))
        return (jsonify(dict(out, ok=True)) if ok else (jsonify({"error": out}), 400))

    @app.route("/api/recipes/can-make", methods=["POST"])
    def api_recipes_can_make():
        err = _config_guard()
        if err:
            return err
        have = split_have(_body().get("have"))
        return jsonify({"have": have, "found": engine.recipes.can_make(have)})

    @app.route("/api/recipes/cooking", methods=["GET", "POST"])
    def api_recipes_cooking():
        # cooking mode tells Sentinel where it is, so Jarvis can follow; the page follows Jarvis back
        if request.method == "GET":
            err = _read_guard()
            if err:
                return err
            return jsonify(engine.recipes.cooking_state())
        err = _config_guard()
        if err:
            return err
        body = _body()
        if body.get("stop"):
            engine.recipes.stop_cooking()
            return jsonify({"ok": True})
        ok, message = engine.recipes.start_cooking(body.get("id"), body.get("version"), body.get("servings"),
                                                   body.get("step"))
        return (jsonify(dict(engine.recipes.cooking_state(), ok=True)) if ok else (jsonify({"error": message}), 400))

    @app.route("/api/recipes/timer", methods=["POST"])
    def api_recipes_timer():
        err = _config_guard()
        if err:
            return err
        body = _body()
        if body.get("cancel"):
            engine.recipes.stop_timer()
            return jsonify({"ok": True, "message": "Timer cancelled."})
        try:
            mins = int(body.get("minutes"))
        except (TypeError, ValueError):
            return jsonify({"error": "How many minutes?"}), 400
        return jsonify({"ok": True, "message": engine.recipes.start_timer(mins)})
