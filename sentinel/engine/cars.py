"""
The family's cars: the Vehicles page, "whose car is this?" and "Wrong car?" on Telegram, and the driveway's
arrive/leave events behind "when did X get home?".

Moved out of engine.py; the engine hands it what it needs.
"""

import itertools
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2

from sentinel.logging_utils import app_logger


class Cars:
    """Family cars. presence (the driveway's car tracker), the cameras and the car classifier are looked up
    each time: they start later than this, and only when object detection is on."""

    VEHICLE_KINDS = ("car", "sedan", "suv", "pickup", "van", "minivan", "motorcycle", "tractor")
    CAR_FIX_KEEP = 40  # newest alerts that can still be corrected (each holds a camera frame)
    CAR_FIX_HOURS = 24

    def __init__(self, settings: Dict[str, Any], household, telegram, recording_store,
                 presence: Callable[[], Any], cameras: Callable[[], Dict[str, Any]],
                 classifier: Callable[[], Any], on_car: Callable[[str, int], None]) -> None:
        self.settings = settings
        self.household = household
        self.telegram = telegram
        self.recording_store = recording_store
        self._presence = presence
        self._cameras = cameras
        self._classifier = classifier  # the detection worker's VehicleClassifier, or None
        self._on_car = on_car  # (arrive | leave, vehicle id) -> routines
        # Open "whose car is this?" questions: sighting id -> {camera, parked, messages, ts}
        self._asks: Dict[int, Dict[str, Any]] = {}
        self._ask_days: Dict[str, int] = self._load_ask_days()  # date -> questions sent (survives restarts)
        # Car alerts with a "Wrong car?" button: id -> {camera, frame, box, caption, ts, named}
        self._fixes: Dict[int, Dict[str, Any]] = {}
        self._fix_ids = itertools.count(int(time.time()))  # unique across restarts: old buttons never hit a new alert

    # ----------------------------------------------- "Wrong car?" under car alerts
    def fix_button(self, pipeline, event) -> Optional[List[List[Tuple[str, str]]]]:
        """A button under a car alert so the family can say whose car it really was."""
        track = event.track
        frame, box = (track.big_frame, track.big_box) if track.big_box is not None else (track.best_frame, track.best_box)
        if (event.alert_group != "vehicle" or track.label in ("bicycle", "motorcycle") or frame is None or box is None
                or not self.household.vehicles() or self._classifier() is None):
            return None
        fix_id = next(self._fix_ids)
        self._fixes[fix_id] = {"camera": pipeline, "frame": frame, "box": tuple(box), "caption": event.description,
                               "ts": event.timestamp, "named": track.known_vehicle.id if track.known_vehicle else None}
        cutoff = time.time() - self.CAR_FIX_HOURS * 3600
        for old in sorted(self._fixes)[:-self.CAR_FIX_KEEP] + [k for k, v in self._fixes.items() if v["ts"] < cutoff]:
            self._fixes.pop(old, None)
        return [[("✏️ Wrong car?" if track.known_vehicle else "🚗 Whose car is this?", f"fx:{fix_id}")]]

    def _fix_car(self, chat_id: str, message_id: int, fix_id: int, choice: Optional[str], callback_id: str) -> None:
        """Taps on "Wrong car?": show the family's cars, then file the photo under the one chosen."""
        from sentinel.engine.household import sentence_case

        fix = self._fixes.get(fix_id)
        if fix is None:
            self.telegram.answer_button(callback_id, "That alert is too old to correct")
            self.telegram.edit_buttons(chat_id, message_id, [])
            return
        first = [[("✏️ Wrong car?" if fix["named"] else "🚗 Whose car is this?", f"fx:{fix_id}")]]
        if choice is None:  # show the family's cars
            names = [(sentence_case(v.name), f"fx:{fix_id}:{v.id}") for v in self.household.vehicles()
                     if v.id != fix["named"]]
            rows = [names[i:i + 2] for i in range(0, len(names), 2)]
            rows.append([("Not ours", f"fx:{fix_id}:x"), ("Cancel", f"fx:{fix_id}:c")])
            self.telegram.answer_button(callback_id)
            self.telegram.edit_buttons(chat_id, message_id, rows)
            return
        if choice == "c":
            self.telegram.answer_button(callback_id)
            self.telegram.edit_buttons(chat_id, message_id, first)
            return
        vehicle = self.household.vehicle(int(choice)) if choice.isdigit() else None
        if choice != "x" and vehicle is None:
            self.telegram.answer_button(callback_id, "That car isn't on the list any more")
            return
        classifier = self._classifier()
        if classifier is None:
            self.telegram.answer_button(callback_id, "Car recognition is off right now")
            return
        from sentinel.detection.vehicle_type import crop_for_clip

        self._fixes.pop(fix_id, None)
        pipeline, frame, box = fix["camera"], fix["frame"], fix["box"]
        embedding = classifier.embed(crop_for_clip(frame, box))
        sid = self.household.add_sighting(fix["ts"], pipeline.host, pipeline.display_name, frame, box,
                                          self.household.look(frame, box, embedding), clip=embedding)
        self.household.assign([sid], vehicle.id if vehicle else None, ignore=vehicle is None)
        presence = self._presence()
        if presence is not None:  # still parked here? then that parking is this car from now on
            parked = presence.parked_spot(pipeline.host, box)
            if parked is not None:
                presence.set_identity(parked, vehicle.id if vehicle else None, ignored=vehicle is None)
        note = (f"✏️ Corrected: that was {sentence_case(vehicle.name)}. I'll remember." if vehicle
                else "✏️ Corrected: not one of ours. I'll remember.")
        self.telegram.answer_button(callback_id, "Thanks!")
        self.telegram.edit_caption(chat_id, message_id, f"{fix['caption']}\n{note}")
        app_logger.log("info", "car_corrected", choice="named" if vehicle else "x", component="engine",
                       action="handle_button")

    def use_fingerprints(self, classifier, device: str) -> None:
        """DINOv2 tells the family's cars apart better than CLIP (car_fingerprint.py); without its model,
        CLIP. Saved photos made with the other model are redone first (a minute or two, cars unnamed)."""
        from sentinel.detection.car_fingerprint import CarFingerprint

        fp = CarFingerprint(device=device)
        name, embed = (fp.name, fp.embed) if fp.available else ("clip", classifier.embed)
        started = time.time()
        try:
            done = self.household.use_fingerprint(name, embed, clip_embed=classifier.embed)
        except Exception as e:
            app_logger.log("error", "car_fingerprints_failed", error_message=str(e), component="engine",
                           action="init_objects")
            return
        app_logger.log("info", "car_fingerprints_ready", model=name, device=fp.device if fp.available else None,
                       error_message=fp.error, redone=done, seconds=round(time.time() - started),
                       component="engine", action="init_objects")

    # ------------------------------------------------------- arrive / leave
    def on_change(self, kind: str, pipeline, vehicle, ts: float, jpeg: Optional[bytes]) -> None:
        """A family car parked ('arrived') or drove off ('left'); kept for "when did X get home?"."""
        from sentinel.engine.household import sentence_case

        if kind == "arrived":
            text = f"🏠 {sentence_case(vehicle.name)} got home ({pipeline.display_name})."
        else:
            text = f"👋 {sentence_case(vehicle.name)} left ({pipeline.display_name})."
        try:
            self.recording_store.add_event(ts, pipeline.host, pipeline.display_name, "household", text,
                                           label=kind, vehicle_type=str(vehicle.id), snapshot_jpeg=jpeg)
        except Exception as e:
            app_logger.log("error", "event_store_failed", error_message=str(e), component="engine",
                           action="household")
        app_logger.log("info", "household_" + kind, vehicle=vehicle.name, camera_display=pipeline.display_name,
                       component="engine", action="presence")
        self._on_car("arrive" if kind == "arrived" else "leave", vehicle.id)

    def on_renamed(self, pipeline, parked, old, new) -> None:
        """Daylight showed that a car named in the dark is another one: put its "got home" right (or remove it
        until we know whose car it is; the arrival is recorded again once it has a name)."""
        from sentinel.engine.household import sentence_case

        if old is None:
            return
        text = f"🏠 {sentence_case(new.name)} got home ({pipeline.display_name})." if new is not None else ""
        try:
            self.recording_store.relabel_household_event(old.id, parked.first_seen,
                                                         new.id if new is not None else None, text)
        except Exception as e:
            app_logger.log("error", "event_store_failed", error_message=str(e), component="engine",
                           action="household")
        app_logger.log("info", "household_renamed", vehicle=old.name, now=new.name if new is not None else None,
                       camera_display=pipeline.display_name, component="engine", action="presence")

    def answer_question(self, hq) -> str:
        from sentinel.engine.questions import answer_household

        if self._presence() is None:
            return "Keeping track of the family's cars needs object recognition, which is off right now."
        statuses, unknown = self.statuses()
        return answer_household(hq, statuses, unknown, page_url=str(self.settings.get("remote_web_url") or ""))

    def statuses(self):
        """({vehicle id: VehicleStatus}, names of cameras with unrecognised parked cars); needs presence."""
        from sentinel.engine.questions import VehicleStatus

        store = self.recording_store
        presence, cameras = self._presence(), self._cameras()
        statuses = {}
        for v in self.household.vehicles():
            arr = store.last_household_event(v.id, "arrived")
            dep = store.last_household_event(v.id, "left")
            arr_ts, dep_ts = (arr or {}).get("ts"), (dep or {}).get("ts")
            spot = presence.where_is(v.id)
            camera = cameras[spot[0]].display_name if spot and spot[0] in cameras else None
            since = arr_ts if spot and arr_ts and (not dep_ts or arr_ts > dep_ts) else None
            statuses[v.id] = VehicleStatus(v.name, spot is not None, camera, since, arr_ts, dep_ts)
        unknown = [cameras[key].display_name for key, cars in presence.present().items()
                   for p in cars if p.vehicle_id is None and key in cameras]
        return statuses, unknown

    # ------------------------------------------------------ household vehicles (web)
    def view(self) -> Dict[str, Any]:
        store = self.household
        presence, cameras = self._presence(), self._cameras()
        store.auto_file()  # only ask about photos it can't file by itself
        refs: Dict[int, List[Dict[str, Any]]] = {}
        for s in store.sightings(labelled=True, limit=5000):
            refs.setdefault(s["vehicle_id"], []).append({"id": s["id"], "night": bool(s["night"])})
        vehicles = []
        for v in store.vehicles():
            spot = presence.where_is(v.id) if presence is not None else None
            vehicles.append({
                "id": v.id, "name": v.name, "owner": v.owner, "aliases": v.aliases, "kind": v.kind,
                "here": spot is not None,
                "camera": cameras[spot[0]].display_name if spot and spot[0] in cameras else None,
                "refs": refs.get(v.id, [])[:30], "ref_count": len(refs.get(v.id, [])),
                "night_refs": sum(1 for r in refs.get(v.id, []) if r["night"]),
            })
        groups = [dict(g, count=len(g["ids"]), sample=g["ids"][:16]) for g in store.suggestion_groups()]
        return {"tracking": presence is not None, "vehicles": vehicles, "groups": groups,
                "kinds": list(self.VEHICLE_KINDS)}

    def save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()
        owner = re.sub(r"\s+", " ", str(body.get("owner") or "")).strip()
        aliases = body.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        aliases = [re.sub(r"\s+", " ", str(a)).strip() for a in aliases][:10]
        kind = str(body.get("kind") or "car")
        if not 1 <= len(name) <= 40 or not re.fullmatch(r"[\w '.\-]+", name):
            return False, "give the vehicle a name (letters, numbers, spaces, ' . -; up to 40)"
        if len(owner) > 30 or (owner and not re.fullmatch(r"[\w '.\-]+", owner)):
            return False, "owner name: letters and spaces, up to 30"
        if any(len(a) > 30 or not re.fullmatch(r"[\w '.\-]*", a) for a in aliases):
            return False, "other names: letters and spaces, up to 30 each"
        if kind not in self.VEHICLE_KINDS:
            return False, "unknown vehicle kind"
        vehicle_id = body.get("id")
        if vehicle_id is not None:
            if not isinstance(vehicle_id, int) or self.household.vehicle(vehicle_id) is None:
                return False, "unknown vehicle"
        new_id = self.household.save_vehicle(name, owner, [a for a in aliases if a], kind, vehicle_id)
        app_logger.log("info", "household_vehicle_saved", vehicle=name, component="engine", action="household")
        return True, str(new_id)

    def delete(self, vehicle_id: int) -> Tuple[bool, str]:
        if self.household.vehicle(vehicle_id) is None:
            return False, "unknown vehicle"
        self.household.delete_vehicle(vehicle_id)
        presence = self._presence()
        if presence is not None:
            presence.forget_identities()
        return True, "deleted"

    def assign(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        """Label photos: {"ids": [...], "vehicle": <id> | "ignore" | "none"}."""
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 2000 or not all(isinstance(i, int) for i in ids):
            return False, "pick some photos"
        target = body.get("vehicle")
        if target == "ignore":
            n = self.household.assign(ids, None, ignore=True)
        elif target == "none":
            n = self.household.assign(ids, None)
        elif isinstance(target, int) and self.household.vehicle(target) is not None:
            n = self.household.assign(ids, target)
        else:
            return False, "pick a vehicle"
        presence = self._presence()
        if presence is not None:
            presence.forget_identities()
        return True, f"{n} photos updated"

    # ------------------------------------------------- "whose car is this?" (Telegram)
    def _ask_days_path(self) -> str:
        return os.path.join(self.household.crops_dir, "asks.json")

    def _load_ask_days(self) -> Dict[str, int]:
        try:
            with open(self._ask_days_path()) as handle:
                return {str(k): int(v) for k, v in json.load(handle).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_ask_days(self) -> None:
        try:
            tmp = self._ask_days_path() + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(self._ask_days, handle)
            os.replace(tmp, self._ask_days_path())
        except OSError:
            pass

    def ask_whose_car(self, pipeline, parked, frame, sighting_id: int, scores: Dict[int, float]) -> None:
        """A car has been parked a few minutes and we can't name it: ask, with a button per family car."""
        s = self.settings
        vehicles = self.household.vehicles()
        now = time.time()
        hour = time.localtime(now).tm_hour
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if (not vehicles or not s.get("household_ask_enabled", True) or not self.telegram.enabled
                or not self.telegram.is_configured()
                or not int(s.get("household_ask_start_hour", 8)) <= hour < int(s.get("household_ask_end_hour", 21))
                or self._ask_days.get(today, 0) >= int(s.get("household_ask_per_day", 3))):
            return
        from sentinel.engine.camera import zoomed_view
        from sentinel.engine.questions import _fmt_clock

        image, (x, y, w, h) = zoomed_view(frame, parked.box, width=640)
        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 200, 255), 2)
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return
        ranked = sorted(vehicles, key=lambda v: -scores.get(v.id, 0.0))  # likeliest first
        names = [(v.name[:1].upper() + v.name[1:], f"hh:{sighting_id}:{v.id}") for v in ranked]
        rows = [names[i:i + 2] for i in range(0, len(names), 2)]
        rows.append([("Not ours", f"hh:{sighting_id}:x"), ("Skip", f"hh:{sighting_id}:s")])
        caption = (f"🚗 Whose car is this? Parked at the {pipeline.display_name} since {_fmt_clock(parked.first_seen)}.\n"
                   "Tap a name and I'll recognise it next time.")
        sent = []
        for chat_id in self.telegram.chat_ids:
            message_id = self.telegram.send_photo_with_buttons(chat_id, jpeg.tobytes(), caption, rows)
            if message_id:
                sent.append((chat_id, message_id))
        if sent:
            self._ask_days = {today: self._ask_days.get(today, 0) + 1}
            self._save_ask_days()
            self._asks[sighting_id] = {"parked": parked, "messages": sent, "ts": now}
            app_logger.log("info", "household_asked", camera_display=pipeline.display_name, component="engine",
                           action="ask_whose_car")

    def handle_button(self, chat_id: str, message_id: int, data: str, callback_id: str) -> bool:
        """A tap on a "Wrong car?" (fx:) or "whose car is this?" (hh:) button; False when it is neither."""
        f = re.fullmatch(r"fx:(\d+)(?::(\d+|x|c))?", data or "")
        if f:
            self._fix_car(chat_id, message_id, int(f.group(1)), f.group(2), callback_id)
            return True
        m = re.fullmatch(r"hh:(\d+):(\d+|x|s)", data or "")
        if not m:
            return False
        sighting_id, choice = int(m.group(1)), m.group(2)
        ask = self._asks.pop(sighting_id, None)
        messages = ask["messages"] if ask else [(chat_id, message_id)]
        parked = ask["parked"] if ask else None
        presence = self._presence()
        if choice == "s":
            caption, toast = "Skipped — I won't ask about this one again.", "Skipped"
        elif choice == "x":
            self.household.assign([sighting_id], None, ignore=True)
            if parked is not None and presence is not None:
                presence.set_identity(parked, None, ignored=True)
            caption, toast = "OK — not one of ours. I won't ask about it again.", "Not ours"
        else:
            vehicle = self.household.vehicle(int(choice))
            if vehicle is None:
                self.telegram.answer_button(callback_id, "That car isn't on the list any more")
                return True
            self.household.assign([sighting_id], vehicle.id)
            if parked is not None and presence is not None:
                presence.set_identity(parked, vehicle.id)  # its arrival gets recorded on the next check
            name = vehicle.name[:1].upper() + vehicle.name[1:]
            caption, toast = f"✓ That's {name} — thanks! I'll recognise it next time.", "Thanks!"
        self.telegram.answer_button(callback_id, toast)
        for chat, mid in messages:
            self.telegram.edit_caption(chat, mid, caption)
        app_logger.log("info", "household_answer", choice="named" if choice.isdigit() else choice,
                       component="engine", action="handle_button")
        return True
