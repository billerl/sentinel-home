"""Roles: every route has a minimum role, checked before it runs; what each role may touch; the change log."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock

import bcrypt

from sentinel.web import roles
from sentinel.web.changelog import ChangeLog
from sentinel.web.roles import ADMIN, GUEST, OWNER, RANK, USER
from test_location import _free_port

SAY = {"name": "Good morning", "when": {"type": "time", "at": "07:00"}, "then": [{"do": "say", "text": "Good morning"}]}
PASSWORD = "their-own-pw-1"


class TestTable(unittest.TestCase):
    def test_action_rules(self):
        self.assertEqual(roles.needed("api_remotes_action", {"action": "press"}), USER)
        self.assertEqual(roles.needed("api_remotes_action", {"action": "learn"}), ADMIN)
        self.assertEqual(roles.needed("api_remotes_action", {"action": "anything-new"}), ADMIN)
        self.assertEqual(roles.needed("api_heater_action", {"action": "hold"}), USER)
        self.assertEqual(roles.needed("api_heater_action", {"action": "buttons"}), ADMIN)
        self.assertEqual(roles.needed("api_tv_action", {"action": "key"}), USER)
        self.assertEqual(roles.needed("api_tv_action", {"action": "record_save"}), ADMIN)
        self.assertEqual(roles.needed("api_people_action", {"action": "invite"}), ADMIN)
        self.assertEqual(roles.needed("api_people_action", {"action": "remove-login"}), OWNER)
        self.assertEqual(roles.needed("api_people_action", {"action": "login-link"}), OWNER)

    def test_a_forgotten_route_is_the_owners_only(self):
        self.assertEqual(roles.needed("a_route_added_next_year"), OWNER)
        self.assertIsNone(roles.needed("login"))

    def test_change_log_keeps_one_line_for_a_repeat(self):
        d = tempfile.mkdtemp(prefix="sentinel-changes-")
        try:
            log = ChangeLog(d)
            log.add("Alex", "admin", "Changed the devices on the house map", now=1000.0)
            log.add("Alex", "admin", "Changed the devices on the house map", now=1100.0)
            log.add("Household login", "owner", "Saved a place", "the library", now=1200.0)
            log.add("Alex", "admin", "Changed the devices on the house map", now=1300.0)
            got = log.recent()
            self.assertEqual([(c["who"], c["what"], c["times"]) for c in got],
                             [("Alex", "Changed the devices on the house map", 1), ("Household login", "Saved a place", 1),
                              ("Alex", "Changed the devices on the house map", 2)])
            self.assertEqual(got[1]["detail"], "the library")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestRoles(unittest.TestCase):
    """A real engine + web server, the household login and one person of each role."""

    @classmethod
    def setUpClass(cls):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager
        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        cls.dir = tempfile.mkdtemp(prefix="sentinel-roles-")
        cls.old_cwd = os.getcwd()
        os.chdir(cls.dir)
        sm = SettingsManager(settings_file=os.path.join(cls.dir, "settings.json"))
        sm.settings.update({"telegram_bot_token": "123456789:" + "A" * 35, "telegram_chat_id": "111", "volume": 0,
                            "telegram_enabled": False, "remote_web_url": "https://sentinel.example.org"})
        sm.settings["web"].update({"enabled": True, "port": _free_port()})
        sm.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"household-pw", bcrypt.gensalt(rounds=4)).decode()})
        cls.engine = Engine(sm)
        cls.server = WebServer(sm, FrameBroker(), cls.engine.recording_store, cls.engine)
        assert cls.server.start()
        loc = cls.engine.locations
        cls.people = {}
        for name, role in (("Grace", GUEST), ("Ben", USER), ("Kate", ADMIN), ("Liz", USER)):
            pid = loc.save_person(name)
            loc.set_role(pid, role)
            # (a quick hash: bcrypt's real cost would make this file slow)
            with loc._connect() as conn:
                conn.execute("UPDATE people SET web_hash = ? WHERE id = ?",
                             (bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(), pid))
            cls.people[name] = loc.person(pid)
        cls.clients = {OWNER: cls.signed_in("alex", "household-pw")}
        for name, role in (("Grace", GUEST), ("Ben", USER), ("Kate", ADMIN)):
            cls.clients[role] = cls.signed_in(name.lower(), PASSWORD)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def signed_in(cls, user, pw):
        c = cls.server._app.test_client()
        assert c.post("/login", data={"username": user, "password": pw, "remember": "1"}).status_code == 302, user
        return c

    @staticmethod
    def post(c, url, body=None):
        return c.post(url, data=json.dumps(body or {}), content_type="application/json",
                      headers={"X-Sentinel-Request": "1"})

    def rules(self):
        """(endpoint, a URL for it, its methods) for every route, with stand-in values in the path."""
        stand_ins = {"person_id": 999, "place_id": 999, "vehicle_id": 999, "routine_id": 999, "recording_id": 999,
                     "sighting_id": 999, "rid": 999, "note_id": 999, "alert_id": 999, "z": 1, "x": 1, "y": 1, "filename": "icon.svg"}
        for rule in self.server._app.url_map.iter_rules():
            args = {a: stand_ins.get(a, "x") for a in rule.arguments}
            yield rule.endpoint, rule.build(args)[1], rule.methods

    # ---------------------------------------------------------------- the table covers everything
    def test_every_route_has_a_rule_and_no_rule_is_stale(self):
        endpoints = {endpoint for endpoint, _, _ in self.rules()}
        self.assertEqual(endpoints - roles.ACCESS.keys(), set(), "routes with no role in sentinel/web/roles.py")
        self.assertEqual(roles.ACCESS.keys() - endpoints, set(), "roles for routes that no longer exist")
        self.assertEqual(roles.CHANGES.keys() - endpoints, set())
        for rule in roles.ACCESS.values():
            self.assertTrue(rule is None or callable(rule) or rule in RANK)

    def test_signed_out_gets_nothing(self):
        c = self.server._app.test_client()
        for endpoint, url, methods in self.rules():
            if roles.ACCESS[endpoint] is None:
                continue
            r = self.post(c, url) if "POST" in methods and "GET" not in methods else c.get(url)
            self.assertIn(r.status_code, (302, 401), url)
            if "POST" in methods:
                self.assertIn(self.post(c, url).status_code, (302, 401), url)

    def test_each_role_is_stopped_below_its_level(self):
        for role in (GUEST, USER, ADMIN):
            c = self.clients[role]
            for endpoint, url, methods in self.rules():
                rule = roles.ACCESS[endpoint]
                if rule is None or callable(rule):
                    continue
                for method in sorted(methods & {"GET", "POST"}):
                    if RANK[role] >= RANK[rule]:
                        continue
                    r = c.get(url) if method == "GET" else self.post(c, url)
                    self.assertEqual(r.status_code, 403, f"{role} {method} {url}")

    def test_each_role_reaches_its_own_pages(self):
        pages = {GUEST: ["/", "/live", "/lost", "/api/lost", "/api/switches", "/api/cameras", "/api/recordings",
                         "/settings", "/api/me"],
                 USER: ["/people", "/api/people", "/timeline", "/api/family", "/routines", "/api/routines", "/house",
                        "/sensors", "/remotes", "/api/heater", "/recipes", "/mesh", "/planes", "/where"],
                 ADMIN: ["/zones", "/vehicles", "/api/household", "/api/settings", "/api/status", "/api/changes",
                         "/api/config/cameras"],
                 OWNER: ["/api/backup"]}
        for role, c in self.clients.items():
            for need, urls in pages.items():
                for url in urls:
                    status = c.get(url).status_code
                    if RANK[role] >= RANK[need]:
                        self.assertEqual(status, 200, f"{role} {url}")
                    else:
                        self.assertEqual(status, 403, f"{role} {url}")
        self.assertEqual(self.clients[USER].get("/api/me").get_json(),
                         {"role": USER, "person": self.people["Ben"].id, "name": "Ben"})
        self.assertEqual(self.clients[OWNER].get("/api/me").get_json()["role"], OWNER)

    def test_a_refused_page_says_so_in_words_and_an_api_in_json(self):
        r = self.clients[USER].get("/vehicles", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Only an admin can do that.", r.get_data(as_text=True))
        r = self.post(self.clients[ADMIN], "/api/backup", {"now": True})
        self.assertEqual((r.status_code, r.get_json()["error"]), (403, "Only the household (owner) login can do that."))
        self.assertEqual(self.clients[GUEST].get("/api/family").get_json()["error"], "Guests can't use this.")

    def test_pages_carry_the_role_for_hiding(self):
        self.assertIn('<body data-role="user">', self.clients[USER].get("/").get_data(as_text=True))
        self.assertIn('<body data-role="owner">', self.clients[OWNER].get("/settings").get_data(as_text=True))
        self.assertNotIn("talk.js", self.clients[GUEST].get("/").get_data(as_text=True))
        self.assertIn("talk.js", self.clients[USER].get("/").get_data(as_text=True))
        with self.server._app.test_client().get("/static/roles.css") as r:
            css = r.get_data(as_text=True)
        self.assertIn('body[data-role="user"] [data-min="admin"]', css)

    # ---------------------------------------------------------------- the household login
    def test_the_household_login_is_the_owner_whatever_a_person_of_that_name_is(self):
        loc = self.engine.locations
        pid = loc.save_person("Alex")  # username "alex", like the household login
        loc.set_role(pid, GUEST)
        try:
            self.assertEqual(self.clients[OWNER].get("/api/me").get_json()["role"], OWNER)
            self.assertEqual(self.clients[OWNER].get("/api/backup").status_code, 200)
        finally:
            loc.delete_person(pid)

    # ---------------------------------------------------------------- setting roles
    def role_of(self, name):
        return self.engine.locations.person(self.people[name].id).role

    def test_who_may_set_which_role(self):
        liz, kate = self.people["Liz"].id, self.people["Kate"].id
        admin, owner, user = self.clients[ADMIN], self.clients[OWNER], self.clients[USER]
        self.assertEqual(self.post(user, f"/api/people/{liz}/role", {"role": GUEST}).status_code, 403)
        self.assertEqual(self.post(admin, f"/api/people/{liz}/role", {"role": GUEST}).status_code, 200)
        self.assertEqual(self.role_of("Liz"), GUEST)
        self.assertGreater(self.engine.locations.person(liz).role_since, time.time() - 60)
        self.assertEqual(self.post(admin, f"/api/people/{liz}/role", {"role": USER}).status_code, 200)
        # an admin can't make admins, change another admin, or change themselves
        self.assertEqual(self.post(admin, f"/api/people/{liz}/role", {"role": ADMIN}).status_code, 403)
        self.assertEqual(self.post(admin, f"/api/people/{kate}/role", {"role": USER}).status_code, 403)
        self.assertEqual((self.role_of("Liz"), self.role_of("Kate")), (USER, ADMIN))
        # nobody is made the owner, and nonsense is refused
        for bad in (OWNER, "boss", None, 3):
            self.assertEqual(self.post(owner, f"/api/people/{liz}/role", {"role": bad}).status_code, 400)
        self.assertEqual(self.post(owner, "/api/people/999/role", {"role": USER}).status_code, 400)
        # the owner can do both
        self.assertEqual(self.post(owner, f"/api/people/{liz}/role", {"role": ADMIN}).status_code, 200)
        self.assertEqual(self.role_of("Liz"), ADMIN)
        self.assertEqual(self.post(owner, f"/api/people/{liz}/role", {"role": USER}).status_code, 200)
        self.assertEqual(self.role_of("Liz"), USER)
        log = self.clients[ADMIN].get("/api/changes").get_json()["changes"]
        self.assertEqual((log[0]["who"], log[0]["what"], log[0]["detail"]), ("Household login", "Changed a role", "Liz: user"))
        self.assertIn(("Kate", "admin", "Liz: guest"), [(c["who"], c["role"], c["detail"]) for c in log])

    def test_a_new_role_applies_at_once(self):
        loc = self.engine.locations
        ben = self.people["Ben"].id
        self.assertEqual(self.clients[USER].get("/api/family").status_code, 200)
        loc.set_role(ben, GUEST)
        try:
            self.assertEqual(self.clients[USER].get("/api/family").status_code, 403)
        finally:
            loc.set_role(ben, USER)
        self.assertEqual(self.clients[USER].get("/api/family").status_code, 200)

    # ---------------------------------------------------------------- people and logins
    def test_admin_sets_up_people_but_logins_are_the_owners(self):
        admin, owner = self.clients[ADMIN], self.clients[OWNER]
        liz, kate = self.people["Liz"].id, self.people["Kate"].id
        self.assertEqual(self.post(admin, f"/api/people/{liz}/invite").status_code, 200)
        self.assertEqual(self.post(admin, f"/api/people/{liz}/login-link").status_code, 403)
        self.assertEqual(self.post(admin, f"/api/people/{liz}/remove-login").status_code, 403)
        self.assertEqual(self.post(admin, f"/api/people/{kate}/delete").status_code, 403)  # an admin: the owner's
        self.assertEqual(self.post(admin, f"/api/people/{kate}/new-password").status_code, 403)
        self.assertIsNotNone(self.engine.locations.person(kate))
        self.assertEqual(self.post(owner, f"/api/people/{liz}/login-link").status_code, 200)
        # the phone's setup link is shown to those who set phones up, not to everyone
        seen = {p["id"]: p for p in self.clients[USER].get("/api/people").get_json()["people"]}
        self.assertIsNone(seen[liz]["invite"])
        self.assertEqual(seen[liz]["role"], USER)
        seen = {p["id"]: p for p in admin.get("/api/people").get_json()["people"]}
        self.assertTrue(seen[liz]["invite"]["url"].startswith("https://"))
        self.assertEqual(admin.get("/api/people").get_json()["me"], {"role": ADMIN, "person": kate})

    def test_only_the_owner_turns_someone_with_a_login_into_a_pet(self):
        owner, admin, loc = self.clients[OWNER], self.clients[ADMIN], self.engine.locations
        gid = int(self.post(owner, "/api/people", {"name": "Morgan"}).get_json()["message"])
        try:
            loc.set_web_password(gid, PASSWORD)
            grace = self.signed_in("morgan", PASSWORD)
            self.assertEqual(grace.get("/api/me").status_code, 200)
            # an admin can't remove her login this way (it's the owner's, like remove-login)
            for change in ({"pet": True}, {"tracker": "!a1b2c3d4"}):
                r = self.post(admin, "/api/people", dict({"id": gid, "name": "Morgan"}, **change))
                self.assertEqual(r.status_code, 403, change)
            self.assertTrue(loc.has_web_login(gid))
            self.assertEqual(grace.get("/api/me").status_code, 200)
            # ordinary edits still work for the admin
            self.assertEqual(self.post(admin, "/api/people", {"id": gid, "name": "Morgan", "aliases": "Mo"}).status_code, 200)
            # the owner can, and she is signed out at once
            self.assertEqual(self.post(owner, "/api/people", {"id": gid, "name": "Morgan", "pet": True}).status_code, 200)
            self.assertFalse(loc.has_web_login(gid))
            self.assertEqual(grace.get("/api/me").status_code, 401)
        finally:
            self.post(owner, f"/api/people/{gid}/delete")

    def test_someone_sharing_their_phone_isnt_made_a_pet_by_an_admin(self):
        owner, admin, loc = self.clients[OWNER], self.clients[ADMIN], self.engine.locations
        pid = int(self.post(owner, "/api/people", {"name": "Robin"}).get_json()["message"])
        try:
            robin = loc.person(pid)
            self.assertTrue(robin.token)  # (a phone setup key is made with the person)
            loc.record(robin, {"_type": "location", "lat": 41.1145, "lon": -88.1780, "tst": int(time.time()), "acc": 10})
            r = self.post(admin, "/api/people", {"id": pid, "name": "Robin", "tracker": "!a1b2c3d4"})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(loc.person(pid).is_pet)  # their timeline stays theirs
        finally:
            self.post(owner, f"/api/people/{pid}/delete")

    def test_a_pet_has_no_login_or_role(self):
        owner, loc = self.clients[OWNER], self.engine.locations
        r = self.post(owner, "/api/people", {"name": "Biscuit"})
        pid = int(r.get_json()["message"])
        try:
            self.assertEqual(self.post(owner, "/api/people", {"id": pid, "name": "Biscuit", "pet": True}).status_code, 200)
            seen = {p["id"]: p for p in owner.get("/api/people").get_json()["people"]}
            self.assertTrue(seen[pid]["pet"])
            self.assertFalse(seen[self.people["Ben"].id]["pet"])
            self.assertEqual(self.post(owner, f"/api/people/{pid}/role", {"role": ADMIN}).status_code, 400)
            for action in ("login-link", "invite"):
                self.assertEqual(self.post(owner, f"/api/people/{pid}/{action}").status_code, 400, action)
            # a login it somehow had stops working when it's marked a pet
            self.post(owner, "/api/people", {"id": pid, "name": "Biscuit", "pet": False})
            loc.set_web_password(pid, PASSWORD)
            c = self.signed_in("biscuit", PASSWORD)
            self.assertEqual(c.get("/api/me").status_code, 200)
            self.post(owner, "/api/people", {"id": pid, "name": "Biscuit", "pet": True})
            self.assertEqual(c.get("/api/me").status_code, 401)
            self.assertFalse(loc.has_web_login(pid))
            # a collar tracker makes a pet too
            self.post(owner, "/api/people", {"id": pid, "name": "Biscuit", "pet": False, "tracker": "!a1b2c3d4"})
            self.assertTrue(loc.person(pid).is_pet)
        finally:
            loc.delete_person(pid)

    def test_removing_a_login_signs_it_out_everywhere(self):
        loc = self.engine.locations
        pid = loc.save_person("Elizabeth")
        with loc._connect() as conn:
            conn.execute("UPDATE people SET web_hash = ? WHERE id = ?",
                         (bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(), pid))
        c = self.signed_in("elizabeth", PASSWORD)
        self.assertEqual(c.get("/api/me").status_code, 200)
        self.assertEqual(self.post(self.clients[OWNER], f"/api/people/{pid}/remove-login").status_code, 200)
        self.assertEqual(c.get("/api/me").status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(self.server._auth.revoke_user("person:elizabeth"), 0)  # (already gone)
        loc.delete_person(pid)

    def test_your_own_picture_only(self):
        ben, liz = self.people["Ben"].id, self.people["Liz"].id
        user = self.clients[USER]
        self.assertEqual(self.post(user, f"/api/people/{liz}/photo", {"image": None}).status_code, 403)
        self.assertEqual(self.post(user, f"/api/people/{ben}/photo", {"image": None}).status_code, 200)
        self.assertEqual(self.post(self.clients[ADMIN], f"/api/people/{liz}/photo", {"image": None}).status_code, 200)
        self.assertEqual(self.post(self.clients[GUEST], f"/api/people/{liz}/photo", {"image": None}).status_code, 403)

    # ---------------------------------------------------------------- routines
    def test_a_user_changes_only_the_routines_they_made(self):
        user, admin, owner = self.clients[USER], self.clients[ADMIN], self.clients[OWNER]
        ben = self.people["Ben"].id
        house = int(self.post(owner, "/api/routines", {"spec": SAY}).get_json()["message"])
        r = self.post(user, "/api/routines", {"spec": dict(SAY, name="Ben's morning")})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        mine = int(r.get_json()["message"])
        seen = {x["id"]: x for x in user.get("/api/routines").get_json()["routines"]}
        self.assertEqual((seen[mine]["by"], seen[mine]["mine"], seen[mine]["owner"]), ("Ben", True, ben))
        self.assertEqual((seen[house]["by"], seen[house]["mine"]), ("", False))
        self.assertTrue(all(x["mine"] for x in admin.get("/api/routines").get_json()["routines"]))
        # theirs: edit, switch off, delete; the household's: none of it
        self.assertEqual(self.post(user, "/api/routines", {"id": mine, "spec": dict(SAY, name="Ben's day")}).status_code, 200)
        self.assertEqual(self.engine.routines.store.get(mine).owner, ben)  # (still theirs after the edit)
        self.assertEqual(self.post(user, f"/api/routines/{mine}/enable", {"on": False}).status_code, 200)
        self.assertEqual(self.post(user, "/api/routines", {"id": house, "spec": SAY}).status_code, 403)
        for action in ("enable", "delete", "try"):
            self.assertEqual(self.post(user, f"/api/routines/{house}/{action}", {"on": False}).status_code, 403, action)
        self.assertTrue(self.engine.routines.store.get(house).enabled)
        # a user's own routine can't message the household's Telegram chat
        spec = dict(SAY, then=[{"do": "telegram", "text": "hello"}])
        r = self.post(user, "/api/routines", {"spec": spec})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Telegram", r.get_json()["error"])
        self.assertEqual(self.post(user, "/api/routines", {"id": mine, "spec": spec}).status_code, 403)
        self.assertEqual(self.post(admin, "/api/routines", {"spec": spec}).status_code, 200)
        # an admin changes anyone's; it stays its maker's
        self.assertEqual(self.post(admin, "/api/routines", {"id": mine, "spec": dict(SAY, name="Ben's")}).status_code, 200)
        self.assertEqual(self.engine.routines.store.get(mine).owner, ben)
        self.assertEqual(self.post(admin, f"/api/routines/{house}/delete").status_code, 200)
        self.assertEqual(self.post(user, f"/api/routines/{mine}/delete").status_code, 200)
        self.assertEqual(self.post(self.clients[GUEST], "/api/routines", {"spec": SAY}).status_code, 403)

    # ---------------------------------------------------------------- notes
    def notes(self, c):
        return {n["title"]: n for n in c.get("/api/notes").get_json()["notes"]}

    def test_private_notes_are_the_writers_alone_and_shared_ones_everyones(self):
        user, admin, owner, guest = (self.clients[r] for r in (USER, ADMIN, OWNER, GUEST))
        r = self.post(user, "/api/notes", {"title": "Journal", "body": "Dear diary"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        journal = int(r.get_json()["message"])
        groceries = int(self.post(user, "/api/notes", {"kind": "list", "title": "Groceries", "shared": True}).get_json()["message"])
        mine = self.notes(user)
        self.assertEqual((mine["Journal"]["shared"], mine["Journal"]["mine"], mine["Groceries"]["shared"]), (False, True, True))
        self.assertEqual(user.get("/api/notes").headers["Cache-Control"], "no-store")
        # nobody else sees the private one, whatever their role; to them it doesn't exist
        for other in (admin, owner):
            seen = self.notes(other)
            self.assertNotIn("Journal", seen)
            self.assertNotIn("Dear diary", other.get("/api/notes").get_data(as_text=True))
            self.assertEqual(seen["Groceries"]["by"], "Ben")
            for url, body in (("/api/notes", {"id": journal, "title": "Mine now", "body": "x"}),
                              (f"/api/notes/{journal}/delete", {}), (f"/api/notes/{journal}/items", {"add": "x"})):
                r = self.post(other, url, body)
                self.assertEqual((r.status_code, r.get_json()["error"]), (400, "That note no longer exists."), url)
        self.assertEqual(self.notes(user)["Journal"]["body"], "Dear diary")
        # the shared list: everyone adds and ticks; only its writer changes who sees it
        self.assertEqual(self.post(admin, f"/api/notes/{groceries}/items", {"add": "  milk  "}).status_code, 200)
        self.assertEqual(self.post(owner, f"/api/notes/{groceries}/items", {"add": "eggs"}).status_code, 200)
        items = self.notes(user)["Groceries"]["items"]
        self.assertEqual([(i["text"], i["done"]) for i in items], [("milk", False), ("eggs", False)])
        self.assertEqual(self.post(user, f"/api/notes/{groceries}/items", {"toggle": items[0]["id"], "done": True}).status_code, 200)
        self.assertEqual(self.notes(admin)["Groceries"]["updated_by"], "Ben")
        self.assertEqual(self.post(admin, f"/api/notes/{groceries}/items", {"clear_done": True}).status_code, 200)
        self.assertEqual([i["text"] for i in self.notes(user)["Groceries"]["items"]], ["eggs"])
        self.assertEqual(self.post(admin, "/api/notes", {"id": groceries, "title": "Groceries", "shared": False}).status_code, 400)
        self.assertEqual(self.post(admin, "/api/notes", {"id": groceries, "title": "Shopping"}).status_code, 200)
        self.assertEqual([i["text"] for i in self.notes(user)["Shopping"]["items"]], ["eggs"])  # (a rename keeps the list)
        # its writer takes it private: gone for the others; and shares the journal: there for them
        self.assertEqual(self.post(user, "/api/notes", {"id": groceries, "title": "Shopping", "shared": False}).status_code, 200)
        self.assertNotIn("Shopping", self.notes(admin))
        self.assertEqual(self.post(user, "/api/notes", {"id": groceries, "title": "Shopping", "shared": True}).status_code, 200)
        # deleting: the writer, or an admin for a shared one; never someone else's by a user
        liz = self.signed_in("liz", PASSWORD)
        self.assertEqual(self.post(liz, f"/api/notes/{groceries}/delete").status_code, 400)
        self.assertEqual(self.post(admin, f"/api/notes/{groceries}/delete").status_code, 200)
        self.assertEqual(self.post(user, f"/api/notes/{journal}/delete").status_code, 200)
        self.assertEqual(self.notes(user), {})
        # guests have no notes; an empty note isn't saved
        self.assertEqual(guest.get("/api/notes").status_code, 403)
        self.assertEqual(self.post(guest, "/api/notes", {"title": "x"}).status_code, 403)
        self.assertEqual(self.post(user, "/api/notes", {"title": " ", "body": ""}).status_code, 400)

    def test_the_household_login_only_writes_shared_notes(self):
        owner = self.clients[OWNER]
        nid = int(self.post(owner, "/api/notes", {"title": "House rules", "body": "Shoes off", "shared": False}).get_json()["message"])
        self.assertTrue(self.notes(self.clients[USER])["House rules"]["shared"])
        self.assertEqual(self.notes(owner)["House rules"]["by"], "Household")
        self.assertEqual(self.post(owner, "/api/notes", {"id": nid, "title": "House rules", "shared": False}).status_code, 400)
        self.assertEqual(self.post(self.clients[ADMIN], f"/api/notes/{nid}/delete").status_code, 200)

    def test_a_deleted_persons_private_notes_go_with_them(self):
        loc, notes = self.engine.locations, self.engine.notes
        pid = loc.save_person("Elizabeth")
        notes.save(pid, False, {"title": "Private", "body": "secret"})
        notes.save(pid, False, {"title": "Chores", "kind": "list", "shared": True})
        self.assertEqual(self.post(self.clients[OWNER], f"/api/people/{pid}/delete").status_code, 200)
        with notes._connect() as conn:
            left = [(r["title"], r["owner"]) for r in conn.execute("SELECT * FROM notes")]
        self.assertEqual(left, [("Chores", None)])
        self.assertEqual(notes.delete(None, True, self.notes(self.clients[OWNER])["Chores"]["id"]), (True, "deleted"))

    # ---------------------------------------------------------------- the timeline is its person's own
    def test_a_timeline_is_its_persons_own(self):
        loc = self.engine.locations
        ben, liz = self.people["Ben"].id, self.people["Liz"].id
        now = time.time()
        dog = loc.save_person("Biscuit")
        loc.set_pet(dog, True)
        day = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts))  # noqa: E731
        with loc._connect() as conn:
            for pid in (ben, liz, dog):
                for ts in (now - 10 * 86400, now - 10 * 86400 + 600, now - 3600, now - 3000):
                    conn.execute("INSERT INTO points (person_id, ts, lat, lon, acc, vel) VALUES (?, ?, 41.1145, -88.178, 10, 0)",
                                 (pid, ts))
        try:
            old, recent = day(now - 10 * 86400), day(now - 3600)
            # a user: their own history in full, pets', and nobody else's
            got = {p["id"]: p["days"] for p in self.clients[USER].get("/api/timeline").get_json()["people"]}
            self.assertEqual(set(got), {ben, dog})
            self.assertIn(old, got[ben])
            self.assertEqual(self.clients[USER].get(f"/api/timeline/{ben}/{old}").status_code, 200)
            self.assertEqual(self.clients[USER].get(f"/api/timeline/{dog}/{old}").status_code, 200)
            for d in (old, recent):
                r = self.clients[USER].get(f"/api/timeline/{liz}/{d}")
                self.assertEqual((r.status_code, r.get_json()["error"]), (403, "A person's timeline is their own."))
            # an admin and the household login: someone else's last 48 hours, no further back
            for role in (ADMIN, OWNER):
                c = self.clients[role]
                view = c.get("/api/timeline").get_json()
                got = {p["id"]: p["days"] for p in view["people"]}
                self.assertEqual(view["others_hours"], 48)
                self.assertNotIn(old, got[liz])
                self.assertIn(recent, got[liz])
                self.assertIn(old, got[dog])
                self.assertEqual(c.get(f"/api/timeline/{liz}/{old}").status_code, 404, role)
                self.assertEqual(c.get(f"/api/timeline/{liz}/{recent}").status_code, 200, role)
            # the day that holds the 48-hour line shows nothing from before it
            with loc._connect() as conn:
                conn.execute("INSERT INTO points (person_id, ts, lat, lon, acc, vel) VALUES (?, ?, 41.2, -88.3, 10, 0)",
                             (liz, now - 49 * 3600))
            edge = self.clients[ADMIN].get(f"/api/timeline/{liz}/{day(now - 49 * 3600)}")
            if edge.status_code == 200:
                text = edge.get_data(as_text=True)
                self.assertNotIn("41.2", text)
                self.assertNotIn("-88.3", text)
        finally:
            with loc._connect() as conn:
                conn.execute("DELETE FROM points")
            loc.delete_person(dog)

    # ---------------------------------------------------------------- settings: the owner's keys
    def test_admin_saves_settings_but_not_the_owners(self):
        admin, owner = self.clients[ADMIN], self.clients[OWNER]
        view = admin.get("/api/settings").get_json()
        self.assertIn("telegram_chat_ids", view["owner_only"])
        # a whole section as the page sends it: the owner's settings unchanged, an admin's changed
        r = self.post(admin, "/api/settings", {"telegram_chat_ids": view["values"]["telegram_chat_ids"],
                                               "assistant_enabled": view["values"]["assistant_enabled"],
                                               "telegram_cooldown": 45})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.engine.settings["telegram_cooldown"], 45)
        for change in ({"telegram_chat_ids": "222"}, {"telegram_bot_token": "987654321:" + "B" * 35},
                       {"remote_web_url": "https://elsewhere.example.org"}, {"backup_enabled": not view["values"]["backup_enabled"]},
                       {"spotify_client_id": "a" * 32}, {"assistant_enabled": not view["values"]["assistant_enabled"], "telegram_cooldown": 50}):
            r = self.post(admin, "/api/settings", change)
            self.assertEqual(r.status_code, 403, change)
        self.assertEqual(self.engine.settings["telegram_cooldown"], 45)  # (nothing of a refused save is kept)
        self.assertEqual(self.engine.settings_view()["values"]["telegram_chat_ids"], "111")
        self.assertEqual(self.post(admin, "/api/settings", {"telegram_bot_token": ""}).status_code, 200)
        self.assertTrue(self.engine.settings["telegram_bot_token"])
        self.assertEqual(self.post(owner, "/api/settings", {"remote_web_url": "https://sentinel.example.org",
                                                            "telegram_cooldown": 30}).status_code, 200)
        self.assertEqual(self.post(self.clients[USER], "/api/settings", {"telegram_cooldown": 1}).status_code, 403)
        log = owner.get("/api/changes").get_json()["changes"]
        self.assertEqual((log[0]["what"], log[0]["detail"]), ("Changed settings", "remote_web_url, telegram_cooldown"))
        self.assertNotIn("example.org", json.dumps(log))  # names of settings, never their values

    def test_camera_passwords_are_the_owners(self):
        admin = self.clients[ADMIN]
        cam = {"name": "Shed", "host": "127.0.0.1", "port": 9, "path": "/live", "username": "u", "password": "p"}
        self.assertEqual(self.post(admin, "/api/config/cameras", cam).status_code, 403)
        self.assertEqual(self.post(admin, "/api/config/cameras/test", cam).status_code, 403)
        self.assertEqual(self.post(admin, "/api/config/cameras/x/delete").status_code, 403)
        self.assertNotEqual(self.post(admin, "/api/config/cameras/x", {"display": "Shed"}).status_code, 403)

    # ---------------------------------------------------------------- guests and recordings
    def test_a_guest_sees_recordings_from_the_day_they_became_one(self):
        store = self.engine.recording_store
        today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        old = store.add_recording(store.new_clip_path("cam", today - 86400 + 3600), "cam", "Cam", today - 86400 + 3600,
                                  today - 86400 + 3620, "motion")
        new = store.add_recording(store.new_clip_path("cam", today + 60), "cam", "Cam", today + 60, today + 80, "motion")
        yesterday = time.strftime("%Y-%m-%d", time.localtime(today - 43200))
        guest, user = self.clients[GUEST], self.clients[USER]
        self.assertEqual([c["id"] for c in user.get("/api/recordings?date=" + yesterday).get_json()["clips"]], [old])
        self.assertEqual(guest.get("/api/recordings?date=" + yesterday).get_json()["clips"], [])
        self.assertEqual([c["id"] for c in guest.get("/api/recordings").get_json()["clips"]], [new])
        # the clip itself too (neither file exists here: "not there" for the guest's day, refused before that)
        with unittest.mock.patch.object(store, "web_playable_path", return_value=None) as asked:
            self.assertEqual(guest.get(f"/recordings/{old}/video").status_code, 404)
            asked.assert_not_called()
            self.assertEqual(guest.get(f"/recordings/{new}/video").status_code, 404)
            asked.assert_called_once_with(new)
            self.assertEqual(user.get(f"/recordings/{old}/video").status_code, 404)
            self.assertEqual(asked.call_count, 2)

    def test_a_guest_uses_switches_and_the_lost_dog_page_only(self):
        guest = self.clients[GUEST]
        self.assertNotEqual(self.post(guest, "/api/switches/set", {"id": "none", "on": True}).status_code, 403)
        self.assertNotEqual(self.post(guest, "/api/lost/start", {"ids": []}).status_code, 403)
        for url, body in (("/api/switches/rename", {}), ("/api/tv/x/key", {"key": "KEY_HOME"}), ("/api/talk", {}),
                          ("/api/remotes/press", {}), ("/api/heater/hold", {}), ("/api/settings/password", {}),
                          ("/api/mesh/send", {}), ("/api/spotify/control", {"action": "pause"})):
            self.assertEqual(self.post(guest, url, body).status_code, 403, url)
        for url in ("/api/where", "/api/timeline", "/api/people", "/api/tv/x", "/api/spotify", "/api/mesh/unread",
                    "/api/heard", "/api/heard/0.wav"):
            self.assertEqual(guest.get(url).status_code, 403, url)

    def test_a_page_hears_new_alerts_in_jarvis_voice(self):
        user = self.clients[USER]
        start = user.get("/api/heard").get_json()
        self.assertEqual(start["items"], [])
        self.engine.news.tell_everyone("⛈️ Severe thunderstorm warning until 6 PM.")
        got = user.get(f"/api/heard?after={start['latest']}").get_json()
        self.assertEqual([i["text"] for i in got["items"]], ["Severe thunderstorm warning until 6 PM."])
        url = f"/api/heard/{got['items'][0]['id']}.wav"
        speaker = unittest.mock.Mock()
        speaker.synthesize.return_value = b"RIFFsound"
        with unittest.mock.patch.object(self.engine, "audio", speaker):
            r = user.get(url)
            self.assertEqual((r.status_code, r.data, r.mimetype), (200, b"RIFFsound", "audio/wav"))
        with unittest.mock.patch.object(self.engine, "audio", None):
            self.assertEqual(user.get("/api/heard/0.wav").status_code, 404)  # (no speaker: the browser's own voice)

    def test_a_user_uses_but_does_not_set_up(self):
        user = self.clients[USER]
        for url in ("/api/remotes/press", "/api/heater/release", "/api/tv/x/key", "/api/lost/beep"):
            self.assertNotEqual(self.post(user, url, {}).status_code, 403, url)
        for url in ("/api/remotes/learn", "/api/remotes/delete", "/api/heater/buttons", "/api/tv/x/record",
                    "/api/tv/x/shortcut_delete", "/api/switches/rename", "/api/wifi_motion/adjust", "/api/house/device",
                    "/api/places", "/api/yard", "/api/people", "/api/household/vehicles", "/api/mesh/alerts",
                    "/api/location/alerts", "/api/spotify/disconnect", "/api/settings/telegram-test"):
            self.assertEqual(self.post(user, url, {}).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
