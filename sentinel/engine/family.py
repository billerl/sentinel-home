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

LIGHT_STOPPED_S = 15 * 60  # the live page's light: this long in one unsaved spot = stopped there (orange), not on the way
LOCATION_STALE_S = 10 * 60  # a family phone's position older than this: ask the phone for a fresh one
# A phone that was offline sends its saved positions in a burst when it's back (seen 2026-11-13: ~3 h of positions at
# once, and "got to <a place>" went out while the person was home). Arrivals/departures older than this when they
# reach Sentinel are kept in the history but not announced, and don't start the home routines.
LATE_CHANGE_S = 10 * 60
SPOT_CHECK_HOUR = 18  # new spots worth saving are looked for once a day, from this hour on
SPOT_ASK_AGAIN_DAYS = 14  # a suggestion nobody answered may come back after this
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
        self._spots_checked = ""  # the day frequent spots were last looked for
        self._places_asked: Dict[int, float] = {}  # person -> last time we asked their phone which places it has
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
            return f"Okay, {names} {'are' if len(pets) > 1 else 'is'} in. Back to normal checking."
        yard = "" if self.locations.yard() else " (Draw the yard on the Family page so I can tell you if they leave it.)"
        return (f"Okay, watching {names}. I'll check on {'them' if len(pets) > 1 else names} every "
                f"{OUTING_POLL_SECONDS} seconds for the next hour, or until you say they're in.{yard}")

    def beep_pets(self, pets) -> str:
        """"Recall Xena" / "beep the dogs": the collar's buzzer sounds (the LoRa base sends it an alert-bell
        message on its next check-in, within ~5 s). The reply."""
        for p in pets:
            self._beeps[p.id] = time.time()
        names = " and ".join(p.name for p in pets)
        app_logger.log("info", "pet_beep", pets=names, component="engine", action="location")
        return f"Okay, beeping {names}'s collar{'s' if len(pets) > 1 else ''}."

    # ---- a lost pet: the collar is asked for a position every 30 s until it's found; the Pet tracker page
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
            return f"Good news, {changed_names} {'are' if len(changed) > 1 else 'is'} found. Back to normal checking."
        url = str(self.settings.get("remote_web_url") or "").strip().rstrip("/")
        link = f"Live map: {url}/lost" if url else "Live map: Sentinel's Home page → Pet tracker."
        many = len(changed) > 1
        text = (f"🚨 {changed_names} {'are' if many else 'is'} lost. {'Their collars are' if many else 'The collar is'} "
                f"reporting about every {OUTING_POLL_SECONDS} seconds while moving (every 3 minutes or so when still) until you say "
                f"found.\n{link}")
        fixes = [(p, self.locations.fix(p.id)) for p in changed]
        for chat_id in self.telegram.unpaused_chats():
            def send(chat=chat_id):
                self.telegram.reply_to_chat(chat, text)
                for _p, fix in fixes:
                    if fix:
                        self.telegram.send_location(chat, fix["lat"], fix["lon"])
            threading.Thread(target=send, name="pet-lost", daemon=True).start()
        self._speak(f"{changed_names} {'are' if many else 'is'} lost. The live map is on Sentinel's pet tracker page.")
        return (f"Okay, {changed_names} marked lost. Everyone on Telegram got the live map link, and "
                f"{'their collars' if many else 'the collar'} will report as often as {'they' if many else 'it'} can.")

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
        """The Pet tracker page: each collar's last position, its trail and whether it's being searched for."""
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

    def _sync_places(self, person, msg: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
        """Keeps the places on a family phone the same as Sentinel's, so the phone itself says the
        moment someone arrives or leaves one added after it was set up. When they differ, the phone
        is asked for its list (it posts {"_type": "waypoints"}) and the answer to that brings it up
        to date without doubling the places it already has."""
        from sentinel.engine.location import places_signature, set_waypoints

        places = self.locations.places()
        signature, sent_before = self.locations.phone_places(person.id)
        if msg.get("_type") == "waypoints":
            command, sent = set_waypoints(msg, places, sent_before)
            self.locations.phone_places_sent(person.id, places, sent)
            app_logger.log("info", "places_synced", person=person.name, places=len(places),
                           on_phone=len(msg.get("waypoints") or []), component="engine", action="location")
            return [command]
        if signature != places_signature(places) and now - self._places_asked.get(person.id, 0) > LOCATION_STALE_S:
            self._places_asked[person.id] = now
            return [{"_type": "cmd", "action": "waypoints"}]
        return []

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
        if not person.tracker:
            reply += self._sync_places(person, msg, now)
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if self._spots_checked != day and time.localtime(now).tm_hour >= SPOT_CHECK_HOUR:
                self._spots_checked = day
                threading.Thread(target=self.suggest_spot, args=(now,), name="spots", daemon=True).start()
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
                if change.kind != "started" and age is not None and age > LATE_CHANGE_S:
                    app_logger.log("info", "location_late_change", person=person.name, change=change.kind,
                                   place=change.place.name if change.place else None, age_s=age,
                                   component="engine", action="location")
                    continue  # (old news from a phone catching up: not said, no routines)
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

    # A spot someone keeps going back to (a new job, a friend's house): offer to save it as a place.
    def suggest_spot(self, now: Optional[float] = None) -> bool:
        """Looks for one unsaved spot visited on several days and asks on Telegram; True if it asked."""
        from sentinel.engine.location import SPOT_DAYS, frequent_spots, stops_in

        now = time.time() if now is None else now
        people = [p for p in self.locations.people() if not p.tracker]
        stops = [s for p in people for s in stops_in(p.id, self.locations.points(p.id, now - SPOT_DAYS * 86400, now))]
        skip = [(a["lat"], a["lon"]) for a in self.locations.spot_asks()
                if a["answer"] or now - a["ts"] < SPOT_ASK_AGAIN_DAYS * 86400]
        spots = frequent_spots(stops, self.locations.places(), skip)
        chats = self.telegram.unpaused_chats()
        if not spots or not chats:
            return False
        spot = spots[0]
        ask_id = self.locations.add_spot_ask(spot.lat, spot.lon, now)
        names = {p.id: p.name for p in people}
        who = sorted({names.get(s.person_id, "?") for s in spot.stops})
        who_text = who[0] if len(who) == 1 else ", ".join(who[:-1]) + " and " + who[-1]
        latest = sorted(spot.stops, key=lambda s: -s.start)[:4]
        when = "; ".join(f"{names.get(s.person_id, '?')} {_day_time(s.start)}–{_clock(s.end)}" for s in reversed(latest))
        text = (f"📍 {who_text} {'has' if len(who) == 1 else 'have'} stopped at this spot on {len(spot.days())} "
                f"different days lately ({when}). It isn't one of your places. Save it, so I can say when "
                "anyone arrives or leaves?")
        for chat in chats:
            self.telegram.send_location(chat, spot.lat, spot.lon)
            self.telegram.send_text_with_buttons(chat, text, [[("📍 Save it", f"spot:{ask_id}"),
                                                               ("No thanks", f"spot:{ask_id}:no")]])
        app_logger.log("info", "location_spot_suggested", days=len(spot.days()), stops=len(spot.stops),
                       component="engine", action="location")
        return True

    def handle_button(self, chat_id: str, message_id: int, data: str, callback_id: str) -> bool:
        """A tap on "Save it" / "No thanks" under a suggested spot; False when it is something else."""
        m = re.fullmatch(r"spot:(\d+)(:no)?", data or "")
        if not m:
            return False
        ask = self.locations.answer_spot(int(m.group(1)), "no" if m.group(2) else "save")
        if ask is None:
            self.telegram.answer_button(callback_id, "That suggestion has expired")
        elif m.group(2):
            self.telegram.answer_button(callback_id, "OK")
            self.telegram.edit_text(chat_id, message_id, "OK, I won't ask about that spot again.")
        else:
            self.telegram.answer_button(callback_id, "Name it")
            self.telegram.edit_text(chat_id, message_id, "📍 Saving that spot…")
            self.on_telegram_pin(chat_id, ask["lat"], ask["lon"])  # asks for its name
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

    def timeline_people(self, viewer: Optional[int] = None, others: Optional[float] = None,
                        private: bool = False) -> List[Dict[str, Any]]:
        """Whose history the Timeline page offers, and which days. ``private``: a person's history is their own;
        someone else sees it only from ``others`` on (None = not at all). Pets' history is everyone's."""
        out = []
        for p in self.locations.people():
            days = self.locations.point_days(p.id)
            if private and p.id != viewer and not p.is_pet:
                if others is None:
                    continue
                first = time.strftime("%Y-%m-%d", time.localtime(others))
                days = [d for d in days if d >= first]
            out.append({"id": p.id, "name": p.name, "days": days})
        return out

    def timeline_day(self, person_id: int, date: str, since: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """One day of someone's history; ``since``: nothing from before then (someone else looking)."""
        from sentinel.engine.timeline import day_view, day_window

        if self.locations.person(person_id) is None:
            return None
        try:
            start, end = day_window(date)
        except (ValueError, OverflowError):
            return None
        # A day and a half either side, so a stay across midnight (asleep at home) is whole.
        reach = (start - 36 * 3600, end + 36 * 3600)
        if since is not None:
            if end <= since:
                return None
            start, reach = max(start, since), (max(reach[0], since), reach[1])
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

    def _stopped_since(self, person_id: int, fix: Dict[str, Any], now: float) -> float:
        """Since when this phone has been where it is now: the first of its latest run of positions within
        SPOT_STAY_M of the newest one (a phone that goes quiet is taken to have stayed put)."""
        from sentinel.engine.location import SPOT_STAY_M, distance_m

        since = fix["ts"]
        for ts, lat, lon, acc, _vel in reversed(self.locations.points(person_id, now - 12 * 3600, now)):
            if distance_m(fix["lat"], fix["lon"], lat, lon) > SPOT_STAY_M + min(acc or 0, 200.0):
                break
            since = min(since, ts)
        return since

    def family_lights(self) -> List[Dict[str, Any]]:
        """Live page: one light per person with a map position. home (green), place = at another saved place
        (blue), moving = out and on the way (yellow), stopped = somewhere unsaved for LIGHT_STOPPED_S or more
        (orange), off = not sharing (grey)."""
        from sentinel.engine.location import whereabouts

        places = self.locations.places()
        now = time.time()
        out = []
        for v in self.person_views().values():
            photo = self.locations.photo_version(v.person.id)
            if not v.fix:
                out.append({"id": v.person.id, "name": v.person.name, "state": "off", "label": "not sharing location",
                            "photo": photo})
                continue
            if v.place is not None:
                state = "home" if v.place.is_home else "place"
            else:
                stopped = now - self._stopped_since(v.person.id, v.fix, now) >= LIGHT_STOPPED_S
                state = "stopped" if stopped else "moving"
            out.append({"id": v.person.id, "name": v.person.name, "state": state,
                        "label": whereabouts(v, places, now), "lat": round(v.fix["lat"], 6),
                        "lon": round(v.fix["lon"], 6), "ts": v.fix["ts"], "photo": photo})
        return out

    def set_photo(self, person_id: int, data_url: Any) -> Tuple[bool, str]:
        """A profile picture from the People page (a data: URL; the page has already made it small), or None to
        remove it. Re-encoded here as a square JPEG, so whatever was sent, only a plain picture is kept."""
        import base64
        import binascii

        import cv2
        import numpy as np

        from sentinel.engine.location import PHOTO_SIZE

        person = self.locations.person(person_id)
        if person is None:
            return False, "That person no longer exists."
        if data_url is None:
            self.locations.set_photo(person_id, None)
            return True, "Picture removed."
        m = re.match(r"^data:image/[\w.+-]+;base64,(.+)$", data_url, re.S) if isinstance(data_url, str) else None
        try:
            raw = base64.b64decode(m.group(1), validate=False) if m else b""
        except (binascii.Error, ValueError):
            raw = b""
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
        if img is None:
            return False, "That doesn't look like a picture."
        h, w = img.shape[:2]
        side = min(h, w)
        img = img[(h - side) // 2:(h - side) // 2 + side, (w - side) // 2:(w - side) // 2 + side]
        img = cv2.resize(img, (PHOTO_SIZE, PHOTO_SIZE), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return False, "Couldn't save that picture."
        self.locations.set_photo(person_id, jpeg.tobytes())
        return True, f"Saved {person.name}'s picture."

    def map_lookup(self, words: str) -> Optional[Tuple[str, str]]:
        """"xena's location" / "where lincoln is" / "everyone on the map" -> ("Xena's location", "#map=3"), for
        showing it on a TV the way a camera is shown; None when the words don't ask for a map."""
        from sentinel.engine.location import _words_in

        w = re.sub(r"\s+", " ", (words or "").lower()).strip()
        m = (re.match(r"^(?:the )?(.+?)(?:'s|s')? (?:location|locations|position|whereabouts)$", w)
             or re.match(r"^where (.+?) (?:is|are)(?: at| right now| now)?$", w)
             or re.match(r"^(.+?) on (?:the |a )?map$", w))
        who = m.group(1) if m else ("" if re.match(r"^(?:the )?(?:family )?map$", w) else None)
        if who is None:
            return None
        if who in ("", "everyone", "everybody", "the family", "family", "all of us", "us", "the kids"):
            return "everyone's location", "#map=all"
        people = [p for p in self.locations.people() if _words_in(" " + who + " ", p.words())]
        if not people:
            return None
        names = [p.name for p in people]
        label = (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]) + "'s location"
        return label, "#map=" + ",".join(str(p.id) for p in people)

    def map_view(self, which: str) -> Dict[str, Any]:
        """The TV's location map: the people asked for ("all", or ids "3,5"), with home and the yard."""
        ids = None if which == "all" else {int(x) for x in which.split(",") if x.strip().isdigit()}
        pets = {p.id for p in self.pets()}
        people = [dict(p, pet=p["id"] in pets, label=re.sub(r"^home\b", "at home", p["label"]))  # (beside a name)
                  for p in self.family_lights() if ids is None or p["id"] in ids]
        home = self.home_coords()
        return {"people": people, "home": list(home) if home else None,
                "yard": self.locations.yard() if any(p["pet"] for p in people) else [], "now": time.time()}

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
                "username": p.username, "has_login": self.locations.has_web_login(p.id), "role": p.role, "pet": p.is_pet,
                "tracker": p.tracker, "in_yard": fix.get("in_yard"), "photo": self.locations.photo_version(p.id),
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
        if isinstance(body.get("pet"), bool):
            self.locations.set_pet(new_id, body["pet"])
        app_logger.log("info", "location_person_saved", new=person_id is None, component="engine", action="location")
        return True, str(new_id)

    def people_action(self, person_id: int, action: str, base_url: str = "") -> Tuple[bool, str]:
        person = self.locations.person(person_id)
        if person is None:
            return False, "That person no longer exists."
        if person.is_pet and action in ("invite", "login-link", "new-password"):
            return False, f"{person.name} is a pet: no phone or login."
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


def _clock(ts: float) -> str:
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0").lower().replace(" ", "")


def _day_time(ts: float) -> str:
    return time.strftime("%a ", time.localtime(ts)) + _clock(ts)
