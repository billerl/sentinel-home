"""
Who may do what on the web pages: every route's minimum role, checked in one place before the route runs.

Roles, lowest first: guest < user < admin < owner. The shared household login (settings web.auth) is the owner
and can't be anything else, so it can never be locked out; a family member's own login carries the role on their
Family page card (people.role in location.db, "user" unless changed).

ACCESS names every route. One that is missing is treated as owner-only (and tests/test_roles.py fails), so a new
route can't slip through open by being forgotten. Rules that depend on which person or routine is touched (your
own picture, your own routines, another admin) are next to the code they guard, in sentinel/web/routes/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Union

from flask import Flask, g, jsonify, render_template_string, request

from sentinel.logging_utils import app_logger

GUEST, USER, ADMIN, OWNER = "guest", "user", "admin", "owner"
RANK = {GUEST: 0, USER: 1, ADMIN: 2, OWNER: 3}
PUBLIC = None  # no login at all, or the route has its own kind (a phone's key, a one-time link)

# The "keys": settings only the owner may change (accounts, where alerts go, the way in from outside, backups).
OWNER_SETTINGS = frozenset({
    "telegram_bot_token", "telegram_chat_ids", "assistant_enabled", "assistant_daily_limit", "assistant_model",
    "spotify_client_id", "remote_web_url", "remote_rdp_user", "backup_enabled", "backup_time", "backup_video_days",
    "backup_remote",
})


@dataclass
class Viewer:
    """Who a request comes from."""
    role: str
    person: Optional[Any] = None  # sentinel.engine.location.Person; None = the shared household login
    session_name: str = ""

    @property
    def name(self) -> str:
        return self.person.name if self.person is not None else "Household login"

    @property
    def person_id(self) -> Optional[int]:
        return self.person.id if self.person is not None else None

    def at_least(self, role: str) -> bool:
        return RANK[self.role] >= RANK[role]


def _by_action(default: str, **special: str) -> Callable[[Dict[str, Any]], str]:
    """For /…/<action> routes: the role each action needs."""
    return lambda args: special.get(str(args.get("action") or "").replace("-", "_"), default)


Rule = Union[None, str, Callable[[Dict[str, Any]], str]]

ACCESS: Dict[str, Rule] = {
    # ---- no role: before signing in, and devices with their own key
    "static": PUBLIC, "favicon": PUBLIC, "login": PUBLIC, "logout": PUBLIC, "handoff": PUBLIC,
    "login_pair_start": PUBLIC, "login_pair_poll": PUBLIC,
    "owntracks": PUBLIC, "mesh_bridge": PUBLIC,  # phones and the base station (HTTP Basic, their own key)
    "join_page": PUBLIC, "join_status": PUBLIC, "join_config_file": PUBLIC, "account_page": PUBLIC,  # one-time links
    "help_page": PUBLIC,  # (only sends on to the Settings page)

    # ---- guest: live cameras, switches, the lost-dog page, recordings from the day their pass began
    "index": GUEST, "live_page": GUEST, "api_cameras": GUEST, "api_slow": GUEST, "api_me": GUEST,
    "api_weather": GUEST, "api_weather_alerts": GUEST, "snapshot": GUEST, "stream": GUEST,
    "api_recordings": GUEST, "recording_video": GUEST,
    "api_switches": GUEST, "api_switches_set": GUEST,
    "lost_page": GUEST, "api_lost": GUEST, "api_lost_action": GUEST, "map_tile": GUEST, "person_photo": GUEST,
    "api_tv_page": GUEST,  # (a TV's camera page asking what to show)
    "settings_page": GUEST,  # the Help section; the page shows each role its own sections

    # ---- user: use everything
    "pair_approve": USER,
    "api_family": USER, "people_page": USER, "api_people": USER, "where_page": USER, "api_where": USER,
    "planes_page": USER, "api_planes": USER,
    "timeline_page": USER, "api_timeline_people": USER, "api_timeline_day": USER,
    "api_timeline_delete": USER,  # (only their own: checked in the route)
    "api_people_photo": USER,  # (their own; an admin anyone's: checked in the route)
    "api_change_password": USER,
    "api_tv": USER, "api_tv_icon": USER, "api_tv_screen": USER, "api_tv_shortcuts": USER,
    "api_tv_action": _by_action(USER, record=ADMIN, record_save=ADMIN, record_cancel=ADMIN, shortcut_delete=ADMIN),
    "remotes_page": USER, "api_remotes": USER, "api_remotes_action": _by_action(ADMIN, press=USER),
    "api_heater": USER, "api_heater_action": _by_action(ADMIN, hold=USER, release=USER),
    "api_spotify": USER, "api_spotify_control": USER, "api_spotify_devices": USER, "api_spotify_speaker": USER,
    "api_talk": USER, "api_talk_answer": USER, "api_heard": USER, "api_heard_voice": USER,
    "mesh_page": USER, "api_mesh": USER, "api_mesh_thread": USER, "api_mesh_send": USER, "api_mesh_unread": USER,
    "house_page": USER, "api_house": USER, "api_house_live": USER,
    "sensors_page": USER, "sensors_history_page": USER, "api_wifi_motion": USER, "api_wifi_motion_events": USER,
    "recipes_page": USER, "recipe_photo": USER, "api_recipes": USER, "api_recipe": USER, "api_recipes_save": USER,
    "api_recipes_job": USER, "api_recipe_version": USER, "api_recipe_revert": USER, "api_recipe_category": USER,
    "api_recipe_photo": USER, "api_recipe_delete": USER, "api_recipe_swap": USER, "api_recipe_twist": USER,
    "api_recipes_can_make": USER, "api_recipes_cooking": USER, "api_recipes_timer": USER,
    # notes: private ones are their writer's alone, whatever the role (decided in sentinel/engine/notes.py)
    "notes_page": USER, "api_notes": USER, "api_notes_save": USER, "api_notes_items": USER, "api_notes_delete": USER,
    # routines: a user makes and changes only their own (checked in the route)
    "routines_page": USER, "api_routines": USER, "api_routines_save": USER, "api_routines_check": USER,
    "api_routines_action": USER,

    # ---- admin: the shared setup
    "zones_page": ADMIN, "api_camera_configs": ADMIN, "api_update_camera_config": ADMIN,
    "api_reconnect_camera": ADMIN, "api_camera_snapshot": ADMIN,
    "vehicles_page": ADMIN, "api_household": ADMIN, "api_household_save": ADMIN, "api_household_delete": ADMIN,
    "api_household_assign": ADMIN, "household_photo": ADMIN,
    "api_people_save": ADMIN, "api_yard_save": ADMIN, "api_places_save": ADMIN, "api_places_delete": ADMIN,
    "api_location_alerts": ADMIN, "api_people_qr": ADMIN,
    "api_people_action": _by_action(ADMIN, login_link=OWNER, remove_login=OWNER),  # (+ another admin: in the route)
    "api_people_role": ADMIN,  # (which roles an admin may give: in the route)
    "api_switches_rename": ADMIN, "api_switches_scan": ADMIN,
    "api_wifi_motion_rename": ADMIN, "api_wifi_motion_board": ADMIN, "api_wifi_motion_adjust": ADMIN,
    "api_house_device": ADMIN, "api_house_drawing": ADMIN,
    "api_mesh_family_channel": ADMIN, "api_mesh_base_name": ADMIN, "api_mesh_nickname": ADMIN,
    "api_mesh_speak": ADMIN, "api_mesh_alerts": ADMIN,
    "api_settings": ADMIN,  # (the owner's keys among them: OWNER_SETTINGS, checked in the route)
    "api_telegram_test": ADMIN, "api_status": ADMIN, "api_changes": ADMIN,

    # ---- owner: logins and the keys
    "api_people_login_link": OWNER,
    "api_add_camera": OWNER, "api_test_camera": OWNER, "api_remove_camera": OWNER,  # (camera passwords)
    "api_backup": OWNER,
    "api_spotify_login": OWNER, "api_spotify_finish": OWNER, "api_spotify_disconnect": OWNER,
    "api_spotify_speaker_login": OWNER, "api_spotify_speaker_finish": OWNER, "api_spotify_speaker_signout": OWNER,
}

SAYS = {USER: "Guests can't use this.", ADMIN: "Only an admin can do that.",
        OWNER: "Only the household (owner) login can do that."}


def needed(endpoint: str, view_args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The least role that may call this route (None = public). An unlisted route is the owner's only."""
    rule = ACCESS.get(endpoint, OWNER)
    return rule(view_args or {}) if callable(rule) else rule


# ---- the change log: which successful requests are setup changes, and what to call them ----
def _name(body: Dict[str, Any]) -> str:
    name = body.get("name")
    return name if isinstance(name, str) else ""


def _routine_name(body: Dict[str, Any]) -> str:
    spec = body.get("spec")
    return _name(spec) if isinstance(spec, dict) else ""


def _action(labels: Dict[str, str]) -> Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]:
    return lambda args, body: labels.get(str(args.get("action") or ""))


def _board(args: Dict[str, Any], body: Dict[str, Any]) -> Optional[str]:
    return None if body.get("identify") is True else "Renamed a sensor board"


# endpoint -> label, or a function(view_args, body) giving the label (None = not a change worth a line)
CHANGES: Dict[str, Union[str, Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]]] = {
    "api_update_camera_config": "Changed a camera's settings or zones",
    "api_add_camera": "Added a camera", "api_remove_camera": "Removed a camera",
    "api_household_save": "Saved a vehicle", "api_household_delete": "Deleted a vehicle",
    "api_people_save": "Saved a person or pet", "api_people_photo": "Changed a picture",
    "api_people_login_link": "Made a login link", "api_people_role": "Changed a role",
    "api_people_action": _action({"delete": "Deleted a person or pet", "forget": "Deleted someone's location history",
                                  "new-password": "Gave a phone a new key", "invite": "Made a phone setup link",
                                  "login-link": "Made a login link", "remove-login": "Removed a login"}),
    "api_timeline_delete": "Deleted their own location history",
    "api_yard_save": "Changed the yard", "api_places_save": "Saved a place", "api_places_delete": "Deleted a place",
    "api_location_alerts": "Switched arrival alerts",
    "api_routines_save": "Saved a routine",
    "api_routines_action": _action({"enable": "Switched a routine on or off", "delete": "Deleted a routine"}),
    "api_settings": "Changed settings", "api_backup": "Backup: started one or chose a drive",
    "api_change_password": "Changed their password",
    "api_switches_rename": "Renamed a switch",
    "api_wifi_motion_rename": "Renamed a sensor path", "api_wifi_motion_board": _board,
    "api_wifi_motion_adjust": "Adjusted a sensor path",
    "api_tv_action": _action({"record_save": "Recorded a TV shortcut", "shortcut_delete": "Deleted a TV shortcut"}),
    "api_remotes_action": _action({"learn": "Taught a remote button", "rename": "Renamed a remote or button",
                                   "delete": "Deleted a remote or button"}),
    "api_heater_action": _action({"buttons": "Chose the heater's buttons"}),
    "api_house_device": "Changed the devices on the house map", "api_house_drawing": "Changed the house drawing",
    "api_mesh_family_channel": "Made the mesh Family channel", "api_mesh_base_name": "Renamed the mesh base",
    "api_mesh_nickname": "Nicknamed a mesh radio", "api_mesh_speak": "Switched mesh messages aloud",
    "api_mesh_alerts": "Switched mesh alerts",
    "api_spotify_finish": "Connected Spotify", "api_spotify_disconnect": "Disconnected Spotify",
    "api_spotify_speaker_finish": "Signed this PC's speaker in to Spotify",
    "api_spotify_speaker_signout": "Signed this PC's speaker out of Spotify",
    "pair_approve": "Signed in another screen (QR code)",
}

DENIED_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sentinel</title>
<style>html{color-scheme:dark}body{font-family:system-ui,sans-serif;background:#000;color:#cfe;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center;padding:1rem}
a{color:#00ffff}h1{font-size:1.2rem;color:#fff}</style></head>
<body><div><h1>{{ message }}</h1><p>Your login ({{ role }}) doesn't include this page.</p>
<p><a href="/">Back to the home page</a></p></div></body></html>"""


def install(app: Flask, ctx: Any) -> None:
    """Add the role check, the change log and the page helpers to the app (once, from WebServer.start)."""

    @app.before_request
    def _role_gate():
        endpoint = request.endpoint
        if endpoint is None:
            return None  # no such page: Flask answers 404
        need = needed(endpoint, request.view_args)
        if need is None:
            return None
        viewer = ctx.viewer()
        if viewer is None:
            return None  # not signed in: the route sends them to the login page (or 401), as it always has
        if "person_id" in (request.view_args or {}) and ctx.engine is not None:  # (for the change log: a delete
            person = ctx.engine.locations.person(request.view_args["person_id"])  # leaves no name to look up)
            g.change_subject = person.name if person is not None else ""
        if viewer.at_least(need):
            return None
        app_logger.log("warning", "web_role_denied", who=viewer.name, role=viewer.role, needs=need, path=request.path,
                       component="web_server", action="roles")
        message = SAYS.get(need, SAYS[OWNER])
        if request.method != "GET" or request.path.startswith("/api/") or \
                not request.headers.get("Accept", "").startswith("text/html"):
            return jsonify({"error": message}), 403
        return render_template_string(DENIED_PAGE, message=message, role=viewer.role), 403

    @app.after_request
    def _note_change(resp):
        label = CHANGES.get(request.endpoint or "") if request.method == "POST" and resp.status_code < 400 else None
        if label is None or ctx.changes is None:
            return resp
        try:
            body = request.get_json(silent=True)
            body = body if isinstance(body, dict) else {}
            args = request.view_args or {}
            what = label(args, body) if callable(label) else label
            viewer = ctx.viewer()
            if what and viewer is not None:
                detail = g.get("change_subject") or _name(body) or _routine_name(body)
                if request.endpoint == "api_settings":
                    detail = ", ".join(sorted(str(k) for k in body))  # which settings, never their values
                elif request.endpoint == "api_people_role":
                    detail = f"{detail}: {str(body.get('role') or '')[:10]}"
                ctx.changes.add(viewer.name, viewer.role, what, detail)
        except Exception as e:  # (a change that worked must never fail because its note didn't)
            app_logger.log("warning", "web_change_log_failed", error_message=str(e), component="web_server",
                           action="roles")
        return resp

    @app.context_processor
    def _page_role():
        # every page: <body data-role="…"> + static/roles.css hide what the role can't use (data-min="admin")
        viewer = ctx.viewer() if request.endpoint and needed(request.endpoint, request.view_args) else None
        return {"role": viewer.role if viewer is not None else GUEST,
                "me_name": viewer.name if viewer is not None else ""}

    @app.route("/api/me")
    def api_me():
        viewer = ctx.viewer()
        if viewer is None:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"role": viewer.role, "person": viewer.person_id, "name": viewer.name})

    @app.route("/api/changes")
    def api_changes():
        """The change log, newest first: who changed what (owner and admins)."""
        if ctx.viewer() is None:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"changes": ctx.changes.recent(200) if ctx.changes is not None else []})
