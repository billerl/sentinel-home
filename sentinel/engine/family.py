"""
The family's whereabouts: phones (OwnTracks), pets' collar trackers and named places.

Answers "where is ...?", sends arrive/leave and yard messages, takes check-ins from the phones and the LoRa
base, and backs the Family and Timeline pages. Moved out of engine.py; the engine hands it what it needs.
"""

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

LOCATION_STALE_S = 10 * 60  # a family phone's position older than this: ask the phone for a fresh one
LOST_PIN_SECONDS = 300  # a lost pet: a fresh Telegram map pin at most this often ...
LOST_PIN_METRES = 30  # ... and only once it has moved this far


class Family:
    """Family phones, pets and places. presence (the driveway's car tracker) is looked up each time:
    it starts later than this, and only when object detection is on."""

    def __init__(self, settings: Dict[str, Any], settings_manager, locations, household, telegram, weather,
                 presence: Callable[[], Any], vehicle_statuses: Callable[[], Any],
                 household_answer: Callable[[Any], str], on_home: Callable[[str, int], None],
                 speak: Callable[[str], None]) -> None:
        self.settings = settings
        self.settings_manager = settings_manager
        self.locations = locations
        self.household = household
        self.telegram = telegram
        self.weather = weather
        self._presence = presence
        self._vehicle_statuses = vehicle_statuses
        self._household_answer = household_answer  # "who's home?" also lists the cars
        self._on_home = on_home  # (arrive | leave, person id) -> routines
        self._speak = speak
        self._place_pins: Dict[str, Tuple[float, float, float]] = {}  # chat -> pin waiting for a name (lat, lon, until)
        self._location_alerted: Dict[Tuple[int, int], float] = {}  # (person, place) -> last alert
        self._location_asked: Dict[int, float] = {}  # person -> last time we asked their phone for a fresh fix
        self._outings: Dict[int, float] = {}  # pet -> until when its tracker is asked often ("the dogs are going out")
        self._beeps: Dict[int, float] = {}  # pet -> when "recall Xena" was asked (the LoRa base picks it up)
        self._lost_pinned: Dict[int, Tuple[float, float, float]] = {}  # lost pet -> last pin sent (ts, lat, lon)

    def location_question(self, text: str):
        from sentinel.engine.location import location_question

        return location_question(text, self.locations.people(), self.locations.places(), self.household.vehicles())

    def _cars_of(self, person, statuses) -> list:
        """The person's own cars (by the Vehicles page's "whose is it?"), as CarLines."""
        from sentinel.engine.location import CarLine

        words = set(person.words())
        return [CarLine(s.name, s.here and not self._driving_off(v.id), s.last_arrival, s.last_departure)
                for v in self.household.vehicles() if v.owner and v.owner.lower() in words
                for s in [statuses.get(v.id)] if s is not None]

    def _driving_off(self, vehicle_id: int) -> bool:
        """The camera saw this car leave its spot and the spot is still empty. The driveway check only
        records "left" after three missed scans (~3 min), so a phone leaving home in between would
        otherwise hear the car is "still here" right after "Elizabeth's Fusion is leaving"."""
        presence = self._presence()
        spot = presence.where_is(vehicle_id) if presence is not None else None
        return spot is not None and spot[1].leaving_at > 0 and spot[1].empty_since > 0

    def person_views(self) -> Dict[int, Any]:
        from sentinel.engine.location import PersonView

        statuses = self._vehicle_statuses()[0] if self._presence() is not None else {}
        places = {p.id: p for p in self.locations.places()}
        yard = self.locations.yard()
        views = {}
        for person in self.locations.people():
            fix = self.locations.fix(person.id)
            views[person.id] = PersonView(person, fix, places.get((fix or {}).get("place_id")),
                                          self.locations.visits(person.id), self._cars_of(person, statuses),
                                          yard if person.tracker else [])
        return views

    def answer_location_question(self, q) -> Tuple[str, List[Tuple[str, float, float]]]:
        """Reply and map pins; "who's home?" also lists the cars (people swap cars)."""
        from sentinel.engine.location import answer_location
        from sentinel.engine.questions import HouseholdQuestion

        text, pins = answer_location(q, self.person_views(), self.locations.places())
        if q.kind == "list" and self._presence() is not None and self.household.vehicles():
            text += "\n\nCars:\n" + self._household_answer(HouseholdQuestion("list"))
        return text, pins

    def start_outing(self, kind: str, pets) -> str:
        """"The dogs are going out": ask their trackers for a position every OUTING_POLL_SECONDS for up to an
        hour (the LoRa base pings the collar), "the dogs are in" stops. The reply."""
        from sentinel.engine.location import OUTING_POLL_SECONDS, OUTING_SECONDS

        names = " and ".join(p.name for p in pets)
        now = time.time()
        for p in pets:
            if kind == "out":
                self._outings[p.id] = now + OUTING_SECONDS
            else:
                self._outings.pop(p.id, None)
        app_logger.log("info", "pet_outing", kind=kind, pets=names, component="engine", action="location")
        if kind == "in":
            return f"OK, {names} {'are' if len(pets) > 1 else 'is'} in. Back to normal checking."
        yard = "" if self.locations.yard() else " (Draw the yard on the Family page so I can tell you if they leave it.)"
        return (f"OK, I'll keep a close eye on {names}: a position every {OUTING_POLL_SECONDS} seconds for the next "
                f"hour, or until you say they're in.{yard}")

    def beep_pets(self, pets) -> str:
        """"Recall Xena" / "beep the dogs": the collar's buzzer sounds (the LoRa base sends it an alert-bell
        message on its next check-in, within ~5 s). The reply."""
        for p in pets:
            self._beeps[p.id] = time.time()
        names = " and ".join(p.name for p in pets)
        app_logger.log("info", "pet_beep", pets=names, component="engine", action="location")
        return f"Beeping {names}'s collar{'s' if len(pets) > 1 else ''}."

    # ---- a lost pet: the collar is asked for a position every 30 s until it's found; the Lost dog page
    def lost(self) -> Dict[int, float]:
        """Pets being searched for: id -> since. Kept in the settings, so a restart doesn't end the search."""
        out = {}
        for key, since in (self.settings.get("lost_pets") or {}).items():
            try:
                out[int(key)] = float(since)
            except (TypeError, ValueError):
                continue
        return out

    def pets(self, ids=None) -> list:
        """The people with a collar tracker (all, or those with these ids)."""
        return [p for p in self.locations.people() if p.tracker and (ids is None or p.id in ids)]

    def set_lost(self, kind: str, pets) -> str:
        """"Xena is lost": every chat gets the news with the live map's link, the collar is asked for its
        position every OUTING_POLL_SECONDS until "we found Xena". The reply."""
        from sentinel.engine.location import OUTING_POLL_SECONDS

        lost, now = self.lost(), time.time()
        changed = [p for p in pets if (p.id in lost) != (kind == "lost")]
        names = " and ".join(p.name for p in pets)
        be = "are" if len(pets) > 1 else "is"
        if not changed:
            return f"{names} {be} already marked lost." if kind == "lost" else f"{names} wasn't marked lost."
        for p in changed:
            if kind == "lost":
                lost[p.id] = now
            else:
                lost.pop(p.id, None)
                self._outings.pop(p.id, None)
                self._lost_pinned.pop(p.id, None)
        self.settings["lost_pets"] = {str(k): v for k, v in lost.items()}
        self.settings_manager.save_settings(force=True)
        changed_names = " and ".join(p.name for p in changed)
        app_logger.log("info", "pet_lost" if kind == "lost" else "pet_found", pets=changed_names, component="engine",
                       action="location")
        if kind == "found":
            self._send_location_alert(f"✅ {changed_names} found. Back to normal checking.")
            return f"Good news. {changed_names} marked found."
        url = str(self.settings.get("remote_web_url") or "").strip().rstrip("/")
        link = f"Live map: {url}/lost" if url else "Live map: Sentinel's Home page → Lost dog."
        many = len(changed) > 1
        text = (f"🚨 {changed_names} {'are' if many else 'is'} lost. {'Their collars are' if many else 'The collar is'} "
                f"asked for a position every {OUTING_POLL_SECONDS} seconds until you say found.\n{link}")
        fixes = [(p, self.locations.fix(p.id)) for p in changed]
        for chat_id in self.telegram.unpaused_chats():
            def send(chat=chat_id):
                self.telegram.reply_to_chat(chat, text)
                for _p, fix in fixes:
                    if fix:
                        self.telegram.send_location(chat, fix["lat"], fix["lon"])
            threading.Thread(target=send, name="pet-lost", daemon=True).start()
        self._speak(f"{changed_names} {'are' if many else 'is'} lost. The live map is on Sentinel's lost dog page.")
        return (f"{changed_names} marked lost. Everyone on Telegram got the live map link, and "
                f"{'their collars' if many else 'the collar'} will report every {OUTING_POLL_SECONDS} seconds.")

    def _lost_pin(self, person, fix: Optional[Dict[str, Any]]) -> None:
        """A lost pet moved: a fresh map pin for the family out looking (at most every few minutes)."""
        from sentinel.engine.location import distance_m

        if not fix:
            return
        last = self._lost_pinned.get(person.id)
        if last and (fix["ts"] - last[0] < LOST_PIN_SECONDS or distance_m(last[1], last[2], fix["lat"], fix["lon"]) < LOST_PIN_METRES):
            return
        self._lost_pinned[person.id] = (fix["ts"], fix["lat"], fix["lon"])
        if last is None:
            return  # the first position after "lost" only sets the start: the alert had its pin
        text = f"🐕 {person.name} is here now ({time.strftime('%-I:%M %p', time.localtime(fix['ts']))})."
        for chat_id in self.telegram.unpaused_chats():
            def send(chat=chat_id):
                self.telegram.reply_to_chat(chat, text)
                self.telegram.send_location(chat, fix["lat"], fix["lon"])
            threading.Thread(target=send, name="pet-lost-pin", daemon=True).start()

    def lost_view(self) -> Dict[str, Any]:
        """The Lost dog page: each collar's last position, its trail and whether it's being searched for."""
        from sentinel.engine.location import OUTING_POLL_SECONDS

        lost, now = self.lost(), time.time()
        pets = []
        for p in self.pets():
            fix = self.locations.fix(p.id) or {}
            since = lost.get(p.id)
            start = max(now - 3 * 3600, since - 15 * 60 if since else now - 30 * 60)
            trail = [[round(lat, 6), round(lon, 6), round(ts)] for ts, lat, lon, _acc, _vel
                     in self.locations.points(p.id, start, now + 60)]
            pets.append({"id": p.id, "name": p.name, "lost_since": since, "lat": fix.get("lat"), "lon": fix.get("lon"),
                         "ts": fix.get("ts"), "acc": fix.get("acc"), "batt": fix.get("batt"),
                         "in_yard": None if fix.get("in_yard") is None else bool(fix.get("in_yard")), "trail": trail})
        home = next((pl for pl in self.locations.places() if pl.is_home), None)
        return {"pets": pets, "yard": self.locations.yard(), "home": [home.lat, home.lon] if home else None,
                "poll_seconds": OUTING_POLL_SECONDS, "now": now}

    def owntracks_post(self, username: str, password: str, payload: Any) -> Optional[List[Dict[str, Any]]]:
        """A message from a family phone. Returns what to send back to the phone, or None if the
        login is wrong. A phone left alone mostly re-sends its last position (its 15-minute check-in
        reuses an old reading), so when the one we have is stale we ask it for a fresh one."""
        from sentinel.engine.location import change_message

        person = self.locations.authenticate(username, password)
        if person is None:
            return None
        changes = self.locations.record(person, payload) if isinstance(payload, dict) else []
        now = time.time()
        fix = self.locations.fix(person.id)
        reply: List[Dict[str, Any]] = []
        if (not person.tracker and (fix is None or now - fix["ts"] > LOCATION_STALE_S)
                and now - self._location_asked.get(person.id, 0) > LOCATION_STALE_S):
            self._location_asked[person.id] = now
            reply = [{"_type": "cmd", "action": "reportLocation"}]
        msg = payload if isinstance(payload, dict) else {}
        if person.tracker:  # the LoRa base checks in every 5 s: "ping or beep the collar now?"
            from sentinel.engine.location import OUTING_POLL_SECONDS

            if ((self._outings.get(person.id, 0) > now or person.id in self.lost()) and (fix is None or now - fix["ts"] >= OUTING_POLL_SECONDS - 5)
                    and now - self._location_asked.get(person.id, 0) >= OUTING_POLL_SECONDS - 5):
                self._location_asked[person.id] = now
                reply = [{"_type": "cmd", "action": "reportLocation"}]
            asked = self._beeps.pop(person.id, None)
            if asked is not None and now - asked < 60:  # (a beep a minute late would only confuse her)
                reply = reply + [{"_type": "cmd", "action": "beep"}]
            if msg.get("_type") != "location":
                return reply  # (check-ins aren't logged: one every 20 s)
        try:
            age = round(now - float(msg["tst"])) if "tst" in msg else None
        except (TypeError, ValueError):
            age = None
        app_logger.log("info", "location_post", person=person.name, kind=msg.get("_type"), trigger=msg.get("t"),
                       age_s=age, fix_age_s=round(now - fix["ts"]) if fix else None, asked=bool(reply),
                       component="engine", action="location")
        if person.tracker and msg.get("_type") == "location" and person.id in self.lost():
            self._lost_pin(person, fix)
        if changes:
            statuses = self._vehicle_statuses()[0] if self._presence() is not None else {}
            for change in changes:
                app_logger.log("info", "location_" + change.kind, person=person.name,
                               place=change.place.name if change.place else None, component="engine",
                               action="location")
                if change.kind in ("left_yard", "back_in_yard"):
                    self._yard_alert(change, fix)
                    continue
                if person.tracker and change.kind != "started":
                    continue  # a pet: the yard says it better than "left home" 100+ m later
                if change.kind in ("arrived", "left") and change.place is not None and change.place.is_home:
                    self._on_home("arrive" if change.kind == "arrived" else "leave", person.id)
                if change.kind != "started" and not (self.settings.get("location_alerts_enabled", True)
                                                     and change.place.alerts):
                    continue
                if change.kind != "started":
                    key = (person.id, change.place.id)
                    if time.time() - self._location_alerted.get(key, 0) < 15 * 60:
                        continue  # GPS wobbling at the edge of a place: one message is enough
                    self._location_alerted[key] = time.time()
                text = change_message(change, self._cars_of(person, statuses))
                if text:
                    self._send_location_alert(text)
        return reply

    def _yard_alert(self, change, fix: Optional[Dict[str, Any]]) -> None:
        """A pet left the yard (or came back): Telegram with a map pin, and said out loud in the house."""
        from sentinel.engine.location import change_message

        if not self.settings.get("location_alerts_enabled", True):
            return
        key = (change.person.id, change.kind)
        if time.time() - self._location_alerted.get(key, 0) < 5 * 60:
            return  # nosing along the fence: one message is enough
        self._location_alerted[key] = time.time()
        text = change_message(change, [])
        url = str(self.settings.get("remote_web_url") or "").strip().rstrip("/")
        if change.kind == "left_yard" and url:
            text += f"\nLive map: {url}/lost"
        for chat_id in self.telegram.unpaused_chats():
            def send(chat=chat_id):
                self.telegram.reply_to_chat(chat, text)
                if fix and change.kind == "left_yard":
                    self.telegram.send_location(chat, fix["lat"], fix["lon"])
            threading.Thread(target=send, name="yard-alert", daemon=True).start()
        if change.kind == "left_yard":
            self._speak(f"Heads up: {change.person.name} left the yard.")  # (no-op when the speakers are off)

    def _send_location_alert(self, text: str) -> None:
        for chat_id in self.telegram.unpaused_chats():
            threading.Thread(target=self.telegram.reply_to_chat, args=(chat_id, text),
                             name="location-alert", daemon=True).start()

    # Telegram: send a 📍 pin, then reply with a name, to add a place without typing coordinates.
    def on_telegram_pin(self, chat_id: str, lat: float, lon: float, title: str = "") -> None:
        self._place_pins[chat_id] = (lat, lon, time.time() + 600)
        has_home = any(p.is_home for p in self.locations.places())
        example = "the salon, school, work, Grandma's" if has_home else "home, the salon, school, work"
        hint = f' (“{title}”?)' if title else ""
        self.telegram.reply_to_chat(chat_id, f"📍 Got the pin{hint}. What should I call this place? Reply with a name "
                                             f"the way you'd say it after “at” — e.g. {example}. Or reply cancel.")

    def name_pinned_place(self, chat_id: str, text: str) -> bool:
        """The reply naming a pinned place; anything that looks like another request passes through."""
        pin = self._place_pins.get(chat_id)
        if pin is None:
            return False
        if time.time() > pin[2]:
            self._place_pins.pop(chat_id, None)
            return False
        name = re.sub(r"\s+", " ", text).strip().strip(".!\"'“”")
        if re.fullmatch(r"(?i)(cancel|no|never ?mind|stop)", name):
            self._place_pins.pop(chat_id, None)
            self.telegram.reply_to_chat(chat_id, "Okay, not saved.")
            return True
        from sentinel.engine.questions import is_question

        if not name or len(name) > 40 or name.endswith("?") or is_question(name) or self.telegram.is_command(name):
            return False
        name = re.sub(r"^(?i:at|call it|it'?s|its)\s+", "", name)
        if name.lower() == "home":
            name = "Home"
        self._place_pins.pop(chat_id, None)
        existing = next((p for p in self.locations.places() if p.name.lower() == name.lower()), None)
        if existing is not None:
            self.locations.save_place(existing.name, pin[0], pin[1], existing.radius, existing.alerts, existing.id)
            self.telegram.reply_to_chat(chat_id, f"📍 Moved “{existing.name}” to the new pin.")
        else:
            self.locations.save_place(name, pin[0], pin[1])
            self.telegram.reply_to_chat(chat_id, f"📍 Saved “{name}”. I'll say when anyone arrives or leaves "
                                                 "(change its size or alerts on the Family page).")
        app_logger.log("info", "location_place_saved", source="telegram", component="engine", action="location")
        return True

    def home_coords(self) -> Optional[Tuple[float, float]]:
        """Where the house is, for sunrise/sunset: the Home place (Family page), else the weather town."""
        home = next((p for p in self.locations.places() if p.is_home), None)
        if home is not None:
            return home.lat, home.lon
        place = getattr(self.weather, "_place", None)
        if place and place.get("lat") is not None:
            return float(place["lat"]), float(place["lon"])
        return None

    def nobody_home(self) -> bool:
        """Every phone that shares its location is away (False when no phone shares one: we don't know)."""
        views = [v for v in self.person_views().values() if v.fix and not v.person.tracker]  # (a dog isn't someone)
        return bool(views) and not any(v.place is not None and v.place.is_home for v in views)

    # ---- web: People page, join links, Live page lights
    def _owntracks_endpoint(self, base_url: str) -> str:
        return (str(self.settings.get("remote_web_url") or "").strip() or base_url).rstrip("/") + "/owntracks"

    def join_url(self, base_url: str, code: str, path: str = "join") -> str:
        return (str(self.settings.get("remote_web_url") or "").strip() or base_url).rstrip("/") + f"/{path}/" + code

    # ---- the family Timeline page and each person's own web login
    def person_login(self, username: str, password: str):
        """The family member signing in to the web page with their own password, or None."""
        return self.locations.web_login(username, password)

    def timeline_people(self) -> List[Dict[str, Any]]:
        return [{"id": p.id, "name": p.name, "days": self.locations.point_days(p.id)} for p in self.locations.people()]

    def timeline_day(self, person_id: int, date: str) -> Optional[Dict[str, Any]]:
        from sentinel.engine.timeline import day_view, day_window

        if self.locations.person(person_id) is None:
            return None
        try:
            start, end = day_window(date)
        except (ValueError, OverflowError):
            return None
        # A day and a half either side, so a stay across midnight (asleep at home) is whole.
        reach = (start - 36 * 3600, end + 36 * 3600)
        points = self.locations.points(person_id, *reach)
        fix = self.locations.fix(person_id)
        view = day_view(points, self.locations.places(), start, end,
                        latest=bool(fix) and fix["ts"] < reach[1])
        view["date"] = date
        return view

    def timeline_delete(self, person_id: int, before: Optional[str]) -> Tuple[bool, str]:
        from sentinel.engine.timeline import day_window

        cutoff = None
        if before:
            try:
                cutoff = day_window(str(before))[0]
            except (ValueError, OverflowError):
                return False, "Pick a date."
        n = self.locations.delete_points(person_id, cutoff)
        app_logger.log("info", "timeline_deleted", person_id=person_id, before=before, points=n,
                       component="engine", action="location")
        return True, "Deleted." if n else "There was nothing to delete."

    def family_lights(self) -> List[Dict[str, Any]]:
        """Live page: one light per person (home / out / not sharing) with a map position."""
        from sentinel.engine.location import whereabouts

        places = self.locations.places()
        now = time.time()
        out = []
        for v in self.person_views().values():
            if not v.fix:
                out.append({"id": v.person.id, "name": v.person.name, "state": "off", "label": "not sharing location"})
                continue
            home = v.place is not None and v.place.is_home
            out.append({"id": v.person.id, "name": v.person.name, "state": "home" if home else "out",
                        "label": whereabouts(v, places, now), "lat": round(v.fix["lat"], 6),
                        "lon": round(v.fix["lon"], 6), "ts": v.fix["ts"]})
        return out

    def people_view(self, base_url: str) -> Dict[str, Any]:
        from sentinel.engine.location import car_name_words, whereabouts

        places = self.locations.places()
        now = time.time()
        people = []
        names = set()
        for v in self.person_views().values():
            p = v.person
            names.update(p.words())
            invite = self.locations.invite(p.id)
            fix = v.fix or {}
            people.append({
                "id": p.id, "name": p.name, "aliases": p.aliases, "sharing": bool(v.fix),
                "where": whereabouts(v, places, now) if v.fix else None,
                "home": bool(v.place and v.place.is_home), "ts": fix.get("ts"), "batt": fix.get("batt"),
                "lat": fix.get("lat"), "lon": fix.get("lon"), "cars": [c.name for c in v.cars],
                "invite": {"url": self.join_url(base_url, invite[0]), "expires": invite[1]} if invite else None,
                "username": p.username, "has_login": self.locations.has_web_login(p.id),
                "tracker": p.tracker, "in_yard": fix.get("in_yard"),
            })
        vehicles = self.household.vehicles()
        car_words = set(car_name_words(vehicles))
        suggestions = []
        for veh in vehicles:
            owner = veh.owner.strip()
            if not owner or owner.lower() in names or owner.lower() == veh.name.lower() or owner.lower() in car_words:
                continue
            aliases = [a for a in veh.aliases if a.lower() not in (owner.lower(),) and a.lower() not in car_words]
            suggestions.append({"name": owner, "aliases": aliases})
            names.add(owner.lower())
        return {
            "people": people, "suggestions": suggestions,
            "places": [{"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon, "radius": p.radius,
                        "alerts": p.alerts, "home": p.is_home} for p in places],
            "yard": self.locations.yard(),
            "alerts_enabled": bool(self.settings.get("location_alerts_enabled", True)),
            "public_url": str(self.settings.get("remote_web_url") or ""),
            "telegram": self.telegram.is_configured(),
        }

    @staticmethod
    def _clean_name(value: Any, limit: int = 30) -> str:
        return re.sub(r"\s+", " ", value if isinstance(value, str) else "").strip()[:limit]

    def people_save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        name = self._clean_name(body.get("name"))
        if not name:
            return False, "Enter the person's name."
        aliases = body.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        aliases = [self._clean_name(a) for a in aliases if isinstance(a, str)][:10]
        person_id = body.get("id")
        if person_id is not None and (not isinstance(person_id, int) or self.locations.person(person_id) is None):
            return False, "That person no longer exists."
        others = [p for p in self.locations.people() if p.id != person_id]
        if any(name.lower() in o.words() for o in others):
            return False, f"There's already someone called {name}."
        tracker = body.get("tracker")
        if tracker is not None:
            tracker = tracker.strip().lower() if isinstance(tracker, str) else ""
            if tracker and not re.fullmatch(r"!?[0-9a-f]{8}", tracker):
                return False, "A tracker id looks like !a1cacb2a (shown in the Meshtastic app)."
            tracker = ("!" + tracker.lstrip("!")) if tracker else ""
        new_id = self.locations.save_person(name, aliases, person_id)
        if tracker is not None:
            self.locations.set_tracker(new_id, tracker)
        app_logger.log("info", "location_person_saved", new=person_id is None, component="engine", action="location")
        return True, str(new_id)

    def people_action(self, person_id: int, action: str, base_url: str = "") -> Tuple[bool, str]:
        person = self.locations.person(person_id)
        if person is None:
            return False, "That person no longer exists."
        if action == "delete":
            self.locations.delete_person(person_id)
        elif action == "forget":
            self.locations.forget(person_id)
        elif action == "new-password":
            self.locations.new_token(person_id)
        elif action == "invite":
            return True, self.join_url(base_url, self.locations.new_invite(person_id))
        elif action == "login-link":  # the person chooses their own Sentinel password
            return True, self.join_url(base_url, self.locations.new_login_code(person_id)[0], "account")
        elif action == "remove-login":
            self.locations.remove_web_login(person_id)
        else:
            return False, "Unknown action."
        app_logger.log("info", "location_person_" + action.replace("-", "_"), component="engine", action="location")
        return True, "Done."

    def yard_save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        """The yard's outline for pets' trackers: {"points": [[lat, lon], ...]} (3-40 corners, or none)."""
        points = body.get("points") if isinstance(body, dict) else None
        try:
            outline = [(float(a), float(b)) for a, b in points or []]
        except (TypeError, ValueError):
            return False, "That outline didn't make sense."
        if outline and not (3 <= len(outline) <= 40 and all(-90 <= a <= 90 and -180 <= b <= 180 for a, b in outline)):
            return False, "Tap at least 3 corners around the yard."
        self.locations.set_yard(outline)
        app_logger.log("info", "location_yard_saved", corners=len(outline), component="engine", action="location")
        return True, "Yard saved." if outline else "Yard removed."

    def place_save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        from sentinel.engine.location import (DEFAULT_RADIUS_M, _miles, bearing_word, distance_m,
                                              parse_coordinates, place_phrase, resolve_map_link)

        name = self._clean_name(body.get("name"), 40)
        if not name:
            return False, "Give the place a name."
        if name.lower() == "home":
            name = "Home"
        place_id = body.get("id")
        existing = self.locations.place(place_id) if isinstance(place_id, int) else None
        if place_id is not None and existing is None:
            return False, "That place no longer exists."
        if any(p.name.lower() == name.lower() and p.id != place_id for p in self.locations.places()):
            return False, f"There's already a place called {name}."
        coords = None
        if isinstance(body.get("from_person"), int):
            fix = self.locations.fix(body["from_person"])
            if not fix:
                return False, "That person's phone hasn't sent a location yet."
            coords = (fix["lat"], fix["lon"])
        elif body.get("where"):
            coords = parse_coordinates(str(body["where"])) or resolve_map_link(str(body["where"]))
            if coords is None:
                return False, ("I couldn't find the spot in that. In Google Maps, press and hold the spot, "
                               "then copy the numbers that appear (like 41.1145, -88.1780) — or tap Share on "
                               "that dropped pin and paste its link. (A shared business link has no spot in it.)")
        elif existing is not None:
            coords = (existing.lat, existing.lon)
        if coords is None:
            return False, "Choose where the place is."
        try:
            radius = float(body.get("radius") or (existing.radius if existing else DEFAULT_RADIUS_M))
        except (TypeError, ValueError):
            return False, "Size must be a number of metres."
        alerts = body.get("alerts", existing.alerts if existing else True)
        self.locations.save_place(name, coords[0], coords[1], radius, alerts is True, place_id)
        app_logger.log("info", "location_place_saved", source="web", component="engine", action="location")
        # Say where it landed, so a place that ended up in the wrong spot is obvious at once.
        others = [p for p in self.locations.places() if p.name != name]
        ref = next((p for p in others if p.name == "Home"), others[0] if others else None)
        if ref is None:
            return True, "Saved"
        d = distance_m(ref.lat, ref.lon, coords[0], coords[1])
        if d < 100:
            return True, f"Saved — right at {place_phrase(ref)}"
        return True, (f"Saved — about {_miles(d)} {bearing_word(ref.lat, ref.lon, coords[0], coords[1])} "
                      f"of {place_phrase(ref)}")

    def place_delete(self, place_id: int) -> Tuple[bool, str]:
        if self.locations.place(place_id) is None:
            return False, "That place no longer exists."
        self.locations.delete_place(place_id)
        return True, "Deleted."

    def location_alerts(self, on: Any) -> Tuple[bool, str]:
        if not isinstance(on, bool):
            return False, "expected true or false"
        self.settings["location_alerts_enabled"] = on
        self.settings_manager.save_settings(force=True)
        return True, "Arrive/leave messages " + ("on." if on else "off.")

    def join_view(self, code: str, base_url: str) -> Optional[Dict[str, Any]]:
        """The setup page a person opens on their own phone (no Sentinel login: the link is the key)."""
        from sentinel.engine.location import owntracks_config, owntracks_link

        person = self.locations.by_invite(code)
        if person is None:
            return None
        endpoint = self._owntracks_endpoint(base_url)
        config = owntracks_config(person, endpoint, self.locations.places())
        return {"name": person.name, "link": owntracks_link(config), "endpoint": endpoint,
                "username": person.username, "password": person.token, "config": config,
                "sharing": self.locations.fix(person.id) is not None,
                "names": [p.name for p in self.locations.people()]}
