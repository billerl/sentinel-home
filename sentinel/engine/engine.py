"""
Sentinel engine: wires cameras, recordings, Telegram, audio and the web viewer.

Runs headless (no GUI). The web viewer is the user interface; Telegram is the
remote control. Replaces the old Qt MainWindow, which ran invisibly under Xvfb.
"""

import copy
import json
import os
import itertools
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import cv2
import numpy as np

from sentinel.constants import CAPTURE_DIR, VIDEO_DIR
from sentinel.engine import admin
from sentinel.engine.camera import CLIP_REQUEST_REASON, CameraPipeline, camera_host
from sentinel.engine.assistant import Assistant, build_context
from sentinel.engine.spotify import SpotifyService, parse_music_command
from sentinel.engine.weather import WeatherService
from sentinel.logging_utils import app_logger
from sentinel.notifications import TelegramNotifier
from sentinel.recordings import RecordingStore
from sentinel.settings import SettingsManager

# OpenCV's default thread pool busy-spins between the small per-frame jobs we
# give it (blur, MOG2, dilate) and was the single largest CPU cost. Each camera
# already has its own thread, so run OpenCV single-threaded.
cv2.setNumThreads(1)

# Camera profile fields the web editor may change, with their validators.
_ENTRY_DIRECTIONS = ("outside_to_inside", "inside_to_outside", "both")


def _clean_points(points: Any) -> List[List[float]]:
    out = []
    for p in points if isinstance(points, list) else []:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            x, y = float(p[0]), float(p[1])
            out.append([round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)])
    return out


def _clean_zones(value: Any) -> List[List[List[float]]]:
    zones = [_clean_points(z) for z in (value if isinstance(value, list) else [])]
    return [z for z in zones if len(z) >= 3][:16]


def _clean_lines(value: Any) -> List[List[List[float]]]:
    lines = [_clean_points(l) for l in (value if isinstance(value, list) else [])]
    return [l for l in lines if len(l) == 2][:8]


def _clean_zone_names(value: Any) -> List[str]:
    names = value if isinstance(value, list) else []
    return [re.sub(r"\s+", " ", n).strip()[:30] if isinstance(n, str) else "" for n in names][:16]


def _repair_zone_names(profiles: Dict[str, Dict[str, Any]]) -> bool:
    """An older zone editor saved unnamed zones as the text "None"; blank them. True if changed."""
    changed = False
    for profile in profiles.values():
        names = profile.get("zone_names")
        if isinstance(names, list) and "None" in names:
            profile["zone_names"] = ["" if n == "None" else n for n in names]
            changed = True
    return changed


def _display_name(value: Any) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:60] if text else None


EDITABLE_PROFILE_FIELDS = {
    "display": _display_name,
    "enabled": bool,
    "active_zones": _clean_zones,
    "zone_names": _clean_zone_names,
    "entry_lines": _clean_lines,
    "entry_direction": lambda v: v if v in _ENTRY_DIRECTIONS else None,
    "min_contour_area": lambda v: max(100, min(50000, int(v))),
    "motion_cooldown": lambda v: max(0.0, min(300.0, float(v))),
    "recording_enabled": bool,
    "night_bug_filter_enabled": bool,
    "hd_recording": bool,
    # Lower = recognises people in poorer conditions (night, far away) but more false alarms.
    "person_min_score": lambda v: round(max(0.3, min(0.9, float(v))), 2),
}


class Engine:
    """Headless Sentinel runtime."""

    def __init__(self, settings_manager: Optional[SettingsManager] = None) -> None:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        self.settings_manager = settings_manager or SettingsManager()
        self.settings = self.settings_manager.settings
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._started = False

        self.recording_store = RecordingStore(VIDEO_DIR)
        from sentinel.engine.household import HouseholdStore

        self.household = HouseholdStore(VIDEO_DIR)
        from sentinel.engine.location import LocationStore

        self.locations = LocationStore(VIDEO_DIR)  # family phones (OwnTracks) and named places
        self._place_pins: Dict[str, Tuple[float, float, float]] = {}  # chat -> pin waiting for a name (lat, lon, until)
        self._location_alerted: Dict[Tuple[int, int], float] = {}  # (person, place) -> last alert
        self.presence = None  # PresenceTracker, when object detection + CLIP are available
        self.telegram = TelegramNotifier(self.settings)
        self.weather = WeatherService(self.settings)
        self.assistant = Assistant(self.settings)
        self.spotify = SpotifyService(self.settings, lambda: self.settings_manager.save_settings(force=True))
        self._command_offers: Dict[int, Tuple[str, str, float]] = {}  # "Do it" buttons under Claude's replies
        self._offer_ids = itertools.count(1)
        self.audio = self._init_audio()
        self.voice = None  # "Hey Jarvis" listener (sentinel.audio.voice), when enabled
        self.frame_broker = None
        self.web_server = None
        self.cameras: Dict[str, CameraPipeline] = {}
        self._command_thread: Optional[threading.Thread] = None
        self.started_at = time.time()
        self._cpu = admin.CpuMeter()
        self.detection_worker = None
        self.detector_error: Optional[str] = None
        self._object_alert_times: Dict[Tuple[str, str], float] = {}
        # Open "whose car is this?" questions: sighting id -> {camera, parked, messages, ts}
        self._car_asks: Dict[int, Dict[str, Any]] = {}
        self._car_ask_days: Dict[str, int] = self._load_ask_days()  # date -> questions sent (survives restarts)
        # Car alerts with a "Wrong car?" button: id -> {camera, frame, box, caption, ts, named}
        self._car_fixes: Dict[int, Dict[str, Any]] = {}
        self._fix_ids = itertools.count(int(time.time()))  # unique across restarts: old buttons never hit a new alert
        # Photo alert waiting for its clip: camera host -> (time, sentence). One clip per alert.
        self._clip_for_alert: Dict[str, Tuple[float, str]] = {}
        self._last_description: Dict[str, Tuple[float, str]] = {}
        self._video_query_lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def _init_audio(self):
        if float(self.settings.get("volume", 0) or 0) <= 0:
            return None  # speakers muted; skip loading the TTS model
        try:
            from sentinel.audio import AudioAlertSystem

            return AudioAlertSystem(self.settings)
        except Exception as e:  # missing TTS backends must not stop the engine
            print(f"Audio alerts unavailable: {e}")
            return None

    def _init_web(self) -> None:
        web_cfg = self.settings.get("web")
        if not (isinstance(web_cfg, dict) and web_cfg.get("enabled")):
            return
        try:
            from sentinel.web.frame_broker import FrameBroker
            from sentinel.web.server import WebServer
        except ImportError as e:
            print(f"Web viewer: need Flask and bcrypt ({e}). Run: pip install -r requirements.txt")
            return
        self.frame_broker = FrameBroker()
        self.web_server = WebServer(
            self.settings_manager, self.frame_broker, self.recording_store, engine=self
        )
        self.web_server.start()

    def _camera_profiles(self) -> Dict[str, Dict[str, Any]]:
        profiles = self.settings.get("camera_profiles", {})
        return profiles if isinstance(profiles, dict) else {}

    def _make_pipeline(self, camera_id: str, profile: Dict[str, Any]) -> CameraPipeline:
        source = profile.get("source", camera_id)
        if isinstance(source, str) and source.isdigit():
            source = int(source)
        return CameraPipeline(
            camera_id,
            source,
            profile.get("display") or f"Camera {camera_host(source)}",
            profile,
            recording_store=self.recording_store,
            frame_broker=self.frame_broker,
            settings=lambda: self.settings,
            on_motion=self._on_motion,
            on_clip=self._on_clip,
            on_status=self._on_camera_status,
            detection_worker=self.detection_worker,
            on_object=self._on_object,
        )

    def _init_objects(self) -> None:
        if not self.settings.get("object_detection_enabled", True):
            return
        from sentinel.detection.objects import MODELS_DIR, ObjectDetector
        from sentinel.engine.objects import DetectionWorker

        model = os.path.join(MODELS_DIR, f"{self.settings.get('detector_model', 'yolox_tiny')}.onnx")
        started = time.time()
        device = str(self.settings.get("inference_device", "auto"))
        detector = ObjectDetector(model, device=device)
        if not detector.available:
            self.detector_error = detector.error
            app_logger.log("error", "object_detector_unavailable", error_message=detector.error,
                           component="engine", action="init_objects")
            return
        classifier = None
        if self.settings.get("vehicle_types_enabled", True):
            from sentinel.detection.vehicle_type import VehicleClassifier

            classifier = VehicleClassifier(device=device)
            if not classifier.available:
                app_logger.log("warning", "vehicle_classifier_unavailable", error_message=classifier.error,
                               component="engine", action="init_objects")
                classifier = None
        self.detection_worker = DetectionWorker(detector, vehicle_classifier=classifier)
        self.detection_worker.household = self.household
        if classifier is not None:
            from sentinel.detection.subject_check import SubjectChecker

            checker = SubjectChecker(classifier)
            self.detection_worker.subject_checker = checker if checker.available else None
        if classifier is not None:
            coarse = {"car": "car", "suv": "suv", "pickup": "pickup", "minivan": "van", "van": "van"}

            def _body(embedding):
                probs = {k: p for k, p in classifier.probabilities(None, None, embedding).items() if k in coarse}
                return coarse[max(probs, key=probs.get)]

            self.household.body_of = _body
        self.detection_worker.start()
        if classifier is not None and self.settings.get("household_tracking_enabled", True):
            from sentinel.engine.household import PresenceTracker

            self.presence = PresenceTracker(
                self.household, detector, classifier,
                cameras=lambda: [p for p in self.cameras.values() if p.connected],
                on_change=self._on_household_change,
                state_path=os.path.join(self.household.crops_dir, "presence.json"),
                on_unknown=self._ask_whose_car,
            )
            self.detection_worker.presence = self.presence
        app_logger.log("info", "object_detector_ready", model=os.path.basename(model), device=detector.device,
                       vehicle_types=classifier is not None,
                       classifier_device=classifier.device if classifier is not None else None,
                       load_ms=int((time.time() - started) * 1000), component="engine", action="init_objects")

    def objects_active(self) -> bool:
        return self.detection_worker is not None and self.settings.get("alert_mode", "objects") == "objects"

    def _build_cameras(self) -> None:
        if _repair_zone_names(self._camera_profiles()):
            self.settings_manager.save_settings(force=True)
        for camera_id, profile in self._camera_profiles().items():
            pipeline = self._make_pipeline(camera_id, profile)
            self.cameras[pipeline.host] = pipeline

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.recording_store.start_maintenance(lambda: self.settings)
        self._init_web()
        self._init_objects()
        self._build_cameras()
        try:
            imported = self.backfill_events_from_log()
            if imported:
                app_logger.log("info", "events_backfilled", count=imported, component="engine", action="start")
        except Exception as e:
            app_logger.log("error", "events_backfill_failed", error_message=str(e), component="engine",
                           action="start")
        if not self.cameras:
            print("No cameras configured (settings.json camera_profiles is empty).")
        for pipeline in self.cameras.values():
            if pipeline.profile.get("enabled", True):
                pipeline.start()
        if self.presence is not None:
            self.presence.start()

        def _household_filter(text: str) -> bool:
            from sentinel.engine.questions import household_question

            return (household_question(text, self.household.vehicles()) is not None
                    or self.location_question(text) is not None)

        self.telegram.question_filter = _household_filter
        self.telegram.location_handler = self._on_telegram_pin
        self.telegram.text_interceptor = self._name_pinned_place
        self.telegram.assistant_available = self.assistant.enabled
        if self.assistant.enabled():
            threading.Thread(target=self._check_assistant, name="assistant-check", daemon=True).start()
        self._command_thread = threading.Thread(
            target=self._command_loop, name="telegram-commands", daemon=True
        )
        self._command_thread.start()
        self._start_voice()
        app_logger.log(
            "info", "engine_started", cameras=list(self.cameras), component="engine", action="start"
        )

    def stop(self) -> None:
        """Stop cameras (finalizing clips), Telegram, maintenance; save settings."""
        if self._stop.is_set():
            return
        self._stop.set()
        for pipeline in self.cameras.values():
            pipeline.stop()
        if self.detection_worker is not None:
            self.detection_worker.stop()
        if self.presence is not None:
            self.presence.stop()
        self._stop_voice()
        self.telegram.cleanup()
        self.recording_store.stop()
        if self.audio is not None:
            try:
                self.audio.cleanup()
            except Exception:
                pass
        self.settings_manager.save_settings(force=True)
        app_logger.log("info", "engine_stopped", component="engine", action="stop")

    def wait(self) -> None:
        self._stop.wait()

    # --------------------------------------------------------- camera events
    def _on_camera_status(self, pipeline: CameraPipeline, connected: bool, error: Optional[str]) -> None:
        if not connected:
            # Tell people waiting on a clip from a dead camera.
            with pipeline._lock:
                waiting, pipeline._pending_clip_chats = pipeline._pending_clip_chats, []
            for chat_id in waiting:
                self.telegram.reply_to_chat(
                    chat_id, f"Sentinel: {pipeline.display_name} is offline; clip not recorded."
                )

    def _on_object(self, pipeline: CameraPipeline, event) -> None:
        """A person/vehicle/animal was recognised (runs on the detection thread)."""
        group = event.alert_group
        self._last_description[pipeline.host] = (event.timestamp, event.description)
        vt = event.track.vehicle_type
        app_logger.log("info", "object_detected", camera_display=pipeline.display_name,
                       label=event.track.label, zone=event.zone_name, score=round(event.track.best_score, 2),
                       vehicle_type=vt.key if vt else None, type_prob=round(vt.prob, 2) if vt else None,
                       known_vehicle=event.track.known_vehicle.name if event.track.known_vehicle else None,
                       description=event.description, component="engine", action="on_object")
        ok, encoded = cv2.imencode(".jpg", event.snapshot, [cv2.IMWRITE_JPEG_QUALITY, 85])
        jpeg = encoded.tobytes() if ok else None
        try:  # history for "did anyone come by?"
            self.recording_store.add_event(
                event.timestamp, pipeline.host, pipeline.display_name, group, event.description,
                label=event.track.label, vehicle_type=vt.key if vt else None, zone=event.zone_name,
                snapshot_jpeg=jpeg,
            )
        except Exception as e:
            app_logger.log("error", "event_store_failed", error_message=str(e), component="engine",
                           action="on_object")
        wanted = self.settings.get("object_alert_groups", [])
        # Deliveries are vehicles too: "Vehicles" covers them, "Deliveries" alone means only those.
        if not self.objects_active() or not (group in wanted or (group == "delivery" and "vehicle" in wanted)):
            return
        key = (pipeline.host, group)
        cooldown = float(self.settings.get("object_alert_cooldown", 60))
        if event.timestamp - self._object_alert_times.get(key, 0.0) < cooldown:
            return
        self._object_alert_times[key] = event.timestamp
        if self.audio is not None:
            self.audio.trigger_alert(event.description.split(" ", 1)[1])  # drop the emoji
        if self.settings.get("telegram_motion_alerts", True) and jpeg:
            self.telegram.trigger_photo_alert(event.description, jpeg, self._car_fix_button(pipeline, event))
            self._clip_for_alert[pipeline.host] = (event.timestamp, event.description)

    # ----------------------------------------------- "Wrong car?" under car alerts
    CAR_FIX_KEEP = 40  # newest alerts that can still be corrected (each holds a camera frame)
    CAR_FIX_HOURS = 24

    def _car_fix_button(self, pipeline, event) -> Optional[List[List[Tuple[str, str]]]]:
        """A button under a car alert so the family can say whose car it really was."""
        track = event.track
        frame, box = (track.big_frame, track.big_box) if track.big_box is not None else (track.best_frame, track.best_box)
        if (event.alert_group != "vehicle" or track.label in ("bicycle", "motorcycle") or frame is None or box is None
                or not self.household.vehicles() or getattr(self.detection_worker, "vehicle_classifier", None) is None):
            return None
        fix_id = next(self._fix_ids)
        self._car_fixes[fix_id] = {"camera": pipeline, "frame": frame, "box": tuple(box), "caption": event.description,
                                   "ts": event.timestamp, "named": track.known_vehicle.id if track.known_vehicle else None}
        cutoff = time.time() - self.CAR_FIX_HOURS * 3600
        for old in sorted(self._car_fixes)[:-self.CAR_FIX_KEEP] + [k for k, v in self._car_fixes.items() if v["ts"] < cutoff]:
            self._car_fixes.pop(old, None)
        return [[("✏️ Wrong car?" if track.known_vehicle else "🚗 Whose car is this?", f"fx:{fix_id}")]]

    def _fix_car(self, chat_id: str, message_id: int, fix_id: int, choice: Optional[str], callback_id: str) -> None:
        """Taps on "Wrong car?": show the family's cars, then file the photo under the one chosen."""
        from sentinel.engine.household import sentence_case

        fix = self._car_fixes.get(fix_id)
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
        classifier = getattr(self.detection_worker, "vehicle_classifier", None)
        if classifier is None:
            self.telegram.answer_button(callback_id, "Car recognition is off right now")
            return
        from sentinel.detection.vehicle_type import crop_for_clip

        self._car_fixes.pop(fix_id, None)
        pipeline, frame, box = fix["camera"], fix["frame"], fix["box"]
        embedding = classifier.embed(crop_for_clip(frame, box))
        sid = self.household.add_sighting(fix["ts"], pipeline.host, pipeline.display_name, frame, box, embedding)
        self.household.assign([sid], vehicle.id if vehicle else None, ignore=vehicle is None)
        if self.presence is not None:  # still parked here? then that parking is this car from now on
            parked = self.presence.parked_spot(pipeline.host, box)
            if parked is not None:
                self.presence.set_identity(parked, vehicle.id if vehicle else None, ignored=vehicle is None)
        note = (f"✏️ Corrected: that was {sentence_case(vehicle.name)}. I'll remember." if vehicle
                else "✏️ Corrected: not one of ours. I'll remember.")
        self.telegram.answer_button(callback_id, "Thanks!")
        self.telegram.edit_caption(chat_id, message_id, f"{fix['caption']}\n{note}")
        app_logger.log("info", "car_corrected", choice="named" if vehicle else "x", component="engine",
                       action="handle_button")

    def _on_household_change(self, kind: str, pipeline, vehicle, ts: float, jpeg: Optional[bytes]) -> None:
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

    def _on_motion(self, pipeline: CameraPipeline) -> None:
        if self.objects_active():
            return  # alerts come from recognised objects instead (see _on_object)
        if self.audio is not None:
            self.audio.trigger_alert()
        if self.settings.get("telegram_motion_alerts", True) and not self.telegram.include_video:
            self.telegram.trigger_alert(message=f"Sentinel: motion detected on {pipeline.display_name}.")

    def _on_clip(self, pipeline: CameraPipeline, path: str, reason: str, chats: List[str]) -> None:
        if reason == CLIP_REQUEST_REASON:
            self.telegram.trigger_alert(
                message=f"Sentinel: requested clip from {pipeline.display_name}.",
                video_path=path,
                target_chat_ids=chats or None,
                force_video=True,
                bypass_cooldown=True,
            )
            return
        if not (self.settings.get("telegram_motion_alerts", True) and self.telegram.include_video):
            return
        if self.objects_active():
            # Only clips that go with a photo alert (which has its own per-camera cooldown):
            # people loading a car used to send a clip every half-minute.
            pending = self._clip_for_alert.pop(pipeline.host, None)
            if pending is None or time.time() - pending[0] > 180:
                return
            self.telegram.trigger_alert(message=f"{pending[1]} (clip)", video_path=path, bypass_cooldown=True)
            return
        self.telegram.trigger_alert(
            message=f"Sentinel: motion detected on {pipeline.display_name}.",
            video_path=path,
        )

    # ------------------------------------------------------- telegram control
    def _command_loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                for chat_id in self.telegram.consume_detailed_status_requests():
                    self.telegram.reply_to_chat(chat_id, self.status_message(chat_id))
                for chat_id in self.telegram.consume_access_requests():
                    self.telegram.reply_to_chat(chat_id, admin.access_message(self.settings))
                for chat_id, changes in self.telegram.consume_remote_feature_commands():
                    self.apply_feature_toggles(chat_id, changes)
                for chat_id, text in self.telegram.consume_video_queries():
                    threading.Thread(
                        target=self.answer_video_query, args=(chat_id, text), name="video-query", daemon=True
                    ).start()
                for chat_id, text in self.telegram.consume_questions():
                    threading.Thread(
                        target=self.answer_question, args=(chat_id, text), name="question", daemon=True
                    ).start()
                for chat_id, text in self.telegram.consume_free_text():
                    threading.Thread(
                        target=self.answer_with_assistant, args=(chat_id, text), name="assistant", daemon=True
                    ).start()
                for chat_id, message_id, data, callback_id in self.telegram.consume_button_presses():
                    self.handle_button(chat_id, message_id, data, callback_id)
                clip_chats = self.telegram.consume_clip_requests()
                if clip_chats:
                    self._request_clips(clip_chats)
            except Exception as e:
                app_logger.log(
                    "error", "telegram_command_failed", error_message=str(e),
                    component="engine", action="command_loop",
                )

    def answer_question(self, chat_id: str, text: str) -> None:
        """Reply to 'did anyone come by?'-style questions from the event history."""
        from sentinel.engine.questions import _fmt_clock, household_question

        lq = self.location_question(text)
        if lq is not None:
            reply, pins = self.answer_location_question(lq)
            self.telegram.reply_to_chat(chat_id, reply)
            for _name, lat, lon in pins[:3]:
                self.telegram.send_location(chat_id, lat, lon)
            app_logger.log("info", "question_answered", kind="location_" + lq.kind, component="engine",
                           action="answer_question")
            return
        hq = household_question(text, self.household.vehicles())
        if hq is not None:
            self.telegram.reply_to_chat(chat_id, self.answer_household_question(hq))
            app_logger.log("info", "question_answered", kind="household_" + hq.kind, component="engine",
                           action="answer_question")
            return
        reply, photos = self.event_question_reply(text)
        self.telegram.reply_to_chat(chat_id, reply)
        for event in photos:
            path = self.recording_store.event_snapshot_path(event)
            if not path:
                continue
            try:
                with open(path, "rb") as handle:
                    jpeg = handle.read()
            except OSError:
                continue
            self.telegram.send_photo(chat_id, jpeg, f"{_fmt_clock(event['ts'])} — {event['description']}")

    def event_question_reply(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Answer text for an event question, and the events whose snapshots go with it."""
        from sentinel.engine.questions import answer_question, parse_question

        names = {p.display_name: key for key, p in self.cameras.items()}
        q = parse_question(text, names.keys())
        keys = [names[n] for n in q.cameras] or None
        groups = sorted(q.groups) or None
        store = self.recording_store
        events = store.events(q.start, q.end, groups, keys)
        others: Dict[str, int] = {}
        if groups:
            for e in store.events(q.start, q.end, None, keys):
                if e["grp"] not in q.groups:
                    others[e["grp"]] = others.get(e["grp"], 0) + 1
        last = store.last_event(q.start, groups, keys) if not events else None
        reply, photos = answer_question(q, events, last, others=others)
        if self.detection_worker is None:
            reply = ("Note: object recognition is off, so I only know about sightings from before it was "
                     "turned off.\n\n" + reply)
        app_logger.log("info", "question_answered", period=q.period, groups=groups, cameras=q.cameras,
                       matches=len(events), component="engine", action="answer_question")
        return reply, photos

    # ------------------------------------------------------ household vehicles (web)
    VEHICLE_KINDS = ("car", "sedan", "suv", "pickup", "van", "minivan", "motorcycle")

    def household_view(self) -> Dict[str, Any]:
        store = self.household
        store.auto_file()  # only ask about photos it can't file by itself
        refs: Dict[int, List[Dict[str, Any]]] = {}
        for s in store.sightings(labelled=True, limit=5000):
            refs.setdefault(s["vehicle_id"], []).append({"id": s["id"], "night": bool(s["night"])})
        vehicles = []
        for v in store.vehicles():
            spot = self.presence.where_is(v.id) if self.presence is not None else None
            vehicles.append({
                "id": v.id, "name": v.name, "owner": v.owner, "aliases": v.aliases, "kind": v.kind,
                "here": spot is not None,
                "camera": self.cameras[spot[0]].display_name if spot and spot[0] in self.cameras else None,
                "refs": refs.get(v.id, [])[:30], "ref_count": len(refs.get(v.id, [])),
                "night_refs": sum(1 for r in refs.get(v.id, []) if r["night"]),
            })
        groups = [dict(g, count=len(g["ids"]), sample=g["ids"][:16]) for g in store.suggestion_groups()]
        return {"tracking": self.presence is not None, "vehicles": vehicles, "groups": groups,
                "kinds": list(self.VEHICLE_KINDS)}

    def household_save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
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

    def household_delete(self, vehicle_id: int) -> Tuple[bool, str]:
        if self.household.vehicle(vehicle_id) is None:
            return False, "unknown vehicle"
        self.household.delete_vehicle(vehicle_id)
        if self.presence is not None:
            self.presence.forget_identities()
        return True, "deleted"

    def household_assign(self, body: Dict[str, Any]) -> Tuple[bool, str]:
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
        if self.presence is not None:
            self.presence.forget_identities()
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
                json.dump(self._car_ask_days, handle)
            os.replace(tmp, self._ask_days_path())
        except OSError:
            pass

    def _ask_whose_car(self, pipeline, parked, frame, sighting_id: int, scores: Dict[int, float]) -> None:
        """A car has been parked a few minutes and we can't name it: ask, with a button per family car."""
        s = self.settings
        vehicles = self.household.vehicles()
        now = time.time()
        hour = time.localtime(now).tm_hour
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if (not vehicles or not s.get("household_ask_enabled", True) or not self.telegram.enabled
                or not self.telegram.is_configured()
                or not int(s.get("household_ask_start_hour", 8)) <= hour < int(s.get("household_ask_end_hour", 21))
                or self._car_ask_days.get(today, 0) >= int(s.get("household_ask_per_day", 3))):
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
            self._car_ask_days = {today: self._car_ask_days.get(today, 0) + 1}
            self._save_ask_days()
            self._car_asks[sighting_id] = {"parked": parked, "messages": sent, "ts": now}
            app_logger.log("info", "household_asked", camera_display=pipeline.display_name, component="engine",
                           action="ask_whose_car")

    def handle_button(self, chat_id: str, message_id: int, data: str, callback_id: str) -> None:
        """A tap on one of our Telegram buttons."""
        c = re.fullmatch(r"cmd:(\d+)(:no)?", data or "")
        if c:
            offer = self._command_offers.pop(int(c.group(1)), None)
            if offer is None or offer[0] != chat_id or time.time() - offer[2] > 3600:
                self.telegram.answer_button(callback_id, "That suggestion has expired")
                return
            command = offer[1]
            if c.group(2):
                self.telegram.answer_button(callback_id, "OK")
                self.telegram.edit_text(chat_id, message_id, "OK, I left it as it is.")
                return
            self.telegram.answer_button(callback_id, "Done")
            self.telegram.edit_text(chat_id, message_id, f"✅ Sent: {command}")
            app_logger.log("info", "assistant_command_run", command=command, component="engine", action="button")
            self.telegram.route_text(chat_id, command, from_button=True)  # exactly as if typed
            return
        f = re.fullmatch(r"fx:(\d+)(?::(\d+|x|c))?", data or "")
        if f:
            self._fix_car(chat_id, message_id, int(f.group(1)), f.group(2), callback_id)
            return
        m = re.fullmatch(r"hh:(\d+):(\d+|x|s)", data or "")
        if not m:
            self.telegram.answer_button(callback_id)
            return
        sighting_id, choice = int(m.group(1)), m.group(2)
        ask = self._car_asks.pop(sighting_id, None)
        messages = ask["messages"] if ask else [(chat_id, message_id)]
        parked = ask["parked"] if ask else None
        if choice == "s":
            caption, toast = "Skipped — I won't ask about this one again.", "Skipped"
        elif choice == "x":
            self.household.assign([sighting_id], None, ignore=True)
            if parked is not None and self.presence is not None:
                self.presence.set_identity(parked, None, ignored=True)
            caption, toast = "OK — not one of ours. I won't ask about it again.", "Not ours"
        else:
            vehicle = self.household.vehicle(int(choice))
            if vehicle is None:
                self.telegram.answer_button(callback_id, "That car isn't on the list any more")
                return
            self.household.assign([sighting_id], vehicle.id)
            if parked is not None and self.presence is not None:
                self.presence.set_identity(parked, vehicle.id)  # its arrival gets recorded on the next check
            name = vehicle.name[:1].upper() + vehicle.name[1:]
            caption, toast = f"✓ That's {name} — thanks! I'll recognise it next time.", "Thanks!"
        self.telegram.answer_button(callback_id, toast)
        for chat, mid in messages:
            self.telegram.edit_caption(chat, mid, caption)
        app_logger.log("info", "household_answer", choice="named" if choice.isdigit() else choice,
                       component="engine", action="handle_button")

    def _check_assistant(self) -> None:
        ok, message = self.assistant.check_login()
        app_logger.log("info" if ok else "warning", "assistant_ready" if ok else "assistant_unavailable",
                       detail=message, component="assistant", action="check_login")

    def assistant_context(self) -> str:
        from sentinel.engine.questions import HouseholdQuestion

        now = time.time()
        events = self.recording_store.events(now - 48 * 3600, now + 1)
        cameras = [(p.display_name, bool(p.connected)) for p in self.cameras.values()]
        home = self.answer_household_question(HouseholdQuestion("list")) if self.presence is not None else ""
        weather = self.weather.current() if self.settings.get("weather_location") else None
        return build_context(now, events, cameras, home, self.household.vehicles(), weather,
                             self.telegram._commands_help_text())

    def answer_with_assistant(self, chat_id: str, text: str) -> None:
        """A message none of the built-in commands understood: ask Claude (opt-in), else show help."""
        from sentinel.engine.questions import QUESTION_HELP

        app_logger.log("info", "telegram_not_understood", text=text[:200], component="engine",
                       action="answer_with_assistant")  # to teach Sentinel the common ones later
        reply = None
        if self.assistant.remaining_today() > 0:
            self.telegram.reply_to_chat(chat_id, "🤔 Let me think…")
            try:
                reply = self.assistant.ask(text, self.assistant_context())
            except Exception as e:  # never let it break the command loop
                self.assistant.last_error = str(e)
        else:
            self.assistant.last_error = "today's limit of Claude questions is used up"
        if reply:
            from sentinel.engine.assistant import split_command

            text_part, command = split_command(reply)
            if command and self.telegram.is_command(command):
                offer = next(self._offer_ids)
                self._command_offers[offer] = (chat_id, command, time.time())
                self.telegram.send_text_with_buttons(chat_id, text_part or reply, [[(f"✅ {command}", f"cmd:{offer}"),
                                                                                    ("No thanks", f"cmd:{offer}:no")]])
            else:
                self.telegram.reply_to_chat(chat_id, text_part or reply)
            return
        fallback = ("Sorry, I didn't catch that. " + QUESTION_HELP if text.rstrip().endswith("?")
                    else self.telegram._commands_help_text())
        self.telegram.reply_to_chat(chat_id, f"(Claude couldn't help: {self.assistant.last_error}.)\n\n{fallback}")

    # ------------------------------------------------------ voice ("Hey Jarvis")
    VOICE_FEATURES = {"recording": "recording", "messages": "Telegram motion alerts", "clips": "clip uploads",
                      "night_bug": "the night bug filter", "reconnect": "auto reconnect"}

    def _start_voice(self) -> None:
        if not self.settings.get("voice_enabled") or self.voice is not None:
            return
        if self.audio is None:
            app_logger.log("warning", "voice_unavailable", error_message="the speaker volume is 0",
                           component="voice", action="start")
            return
        try:
            from sentinel.audio.voice import VoiceAssistant
        except ImportError as e:  # faster-whisper / openwakeword not installed
            app_logger.log("warning", "voice_unavailable", error_message=str(e), component="voice", action="start")
            return
        self.voice = VoiceAssistant(self.settings, self.answer_voice, self.audio)
        self.voice.start()

    def _stop_voice(self) -> None:
        if self.voice is not None:
            self.voice.stop()
            self.voice = None

    NOTES_FILE = os.path.join(os.path.expanduser("~"), "Documents", "sentinel_home", "claude-notes.md")
    # "for <one word>": Whisper hears "Claude" as "Pause", "cloud", "Klaus"... (2026-10-27), so any single
    # word after "take a note for" counts as Claude rather than as the note.
    _NOTE = re.compile(r"^(?:please )?(?:(?:take|make|leave|write|save|add)(?: down)? (?:a |another )?note"
                       r"(?: for [\w']+)?|note for (?:claude|cloud|clod|claud|klaus|claws|clause)|"
                       r"tell (?:claude|cloud) (?:that )?)[:,.]?\s*(.*)$", re.IGNORECASE)

    def save_claude_note(self, text: str) -> str:
        """A spoken request for Claude (the coding assistant), kept until the next session reads it."""
        text = (text or "").strip()
        if not text:
            return "I didn't catch anything, so I didn't save a note."
        os.makedirs(os.path.dirname(self.NOTES_FILE), exist_ok=True)
        new = not os.path.exists(self.NOTES_FILE)
        with open(self.NOTES_FILE, "a", encoding="utf-8") as f:
            if new:
                f.write("# Notes for Claude (said to Jezebel)\n\nClaude: read these when asked to \"check notes\"; "
                        "move finished ones under ## Done.\n\n## To do\n")
            f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} — {text}\n")
        app_logger.log("info", "claude_note_saved", length=len(text), component="voice", action="note")
        for chat_id in self.telegram.chat_ids:
            self.telegram.reply_to_chat(chat_id, f"📝 Note for Claude (as Jezebel heard it):\n{text}")
        return "Got it, I've saved that for Claude."

    def _voice_toggle(self, changes: Dict[str, bool]):
        from sentinel.audio.voice import VoiceReply

        what = " and ".join(f"turn {'on' if on else 'off'} {self.VOICE_FEATURES.get(key, key)}"
                            for key, on in changes.items())

        def run() -> str:
            applied = self.apply_feature_changes(changes, source="voice")
            return "Done. " + ". ".join(applied) + "." if applied else "Nothing needed changing."

        return VoiceReply(f"Should I {what}? Say yes or no.", run)

    def answer_voice(self, text: str, say: Callable[[str], None]):
        """What to say back to a spoken request: Sentinel's own answers first, then Claude (opt-in)."""
        from sentinel.audio.voice import VoiceReply, to_speech
        from sentinel.engine.assistant import split_command
        from sentinel.engine.questions import _fmt_clock, household_question, is_question

        plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s'?:-]", " ", text)).strip()
        low = plain.lower().rstrip("?")
        app_logger.log("info", "voice_request", text=text[:200], component="voice", action="answer")
        note = self._NOTE.match(plain)
        if note:
            said = note.group(1).strip(" .")
            if said:
                return VoiceReply(self.save_claude_note(said))
            return VoiceReply("Go ahead.", dictate=self.save_claude_note)
        music = parse_music_command(low)
        if music and self.spotify.connected():
            return VoiceReply(self.spotify.command(*music))
        if re.fullmatch(r"(never ?mind|cancel|stop|nothing|no|forget it|no thanks)", low):
            return VoiceReply("Okay.")
        if music and music[0] == "play":
            return VoiceReply("Spotify isn't connected yet. Connect it on the Settings page, under Spotify.")
        if re.search(r"\bwhat time is it\b|\bwhat'?s the time\b|\bwhat is the time\b", low):
            return VoiceReply(f"It's {_fmt_clock(time.time())}.")
        if re.search(r"\b(weather|temperature|how (hot|cold|warm)|rain\w*|snow\w*)\b", low) \
                and self.settings.get("weather_location"):
            w = self.weather.current()
            if w.get("temp") is not None:
                return VoiceReply(f"It's {w['temp']} degrees and {w.get('words', '').lower()} in {w.get('place')}. "
                                  f"High of {w.get('high')}, low of {w.get('low')}, "
                                  f"and a {w.get('rain_today')} percent chance of rain today.")
        changes, _err = self.telegram._parse_feature_toggle_command(low)
        if changes:
            return self._voice_toggle(changes)
        lq = self.location_question(low)
        if lq is not None:
            return VoiceReply(to_speech(self.answer_location_question(lq)[0]))
        hq = household_question(low, self.household.vehicles())
        if hq is not None:
            return VoiceReply(to_speech(self.answer_household_question(hq)))
        if is_question(low):
            return VoiceReply(to_speech(self.event_question_reply(low)[0]))
        if not self.assistant.enabled():
            return VoiceReply("Sorry, I can't answer that one. Try asking who's home, or did anyone come by today.")
        reply = None
        if self.assistant.remaining_today() > 0:
            say("Let me think.")
            try:
                reply = self.assistant.ask(text, self.assistant_context(), voice=True)
            except Exception as e:
                self.assistant.last_error = str(e)
        else:
            self.assistant.last_error = "today's limit of Claude questions is used up"
        if not reply:
            return VoiceReply(f"Sorry, I couldn't get an answer: {self.assistant.last_error}.")
        text_part, command = split_command(reply)
        spoken = to_speech(text_part or reply, max_lines=6, max_chars=600)
        changes = self.telegram._parse_feature_toggle_command(command)[0] if command else None
        if changes:
            return VoiceReply(spoken + " Should I do that? Say yes or no.", self._voice_toggle(changes).confirm)
        return VoiceReply(spoken)

    def answer_household_question(self, hq) -> str:
        from sentinel.engine.questions import answer_household

        if self.presence is None:
            return "Keeping track of the family's cars needs object recognition, which is off right now."
        statuses, unknown = self.vehicle_statuses()
        return answer_household(hq, statuses, unknown, page_url=str(self.settings.get("remote_web_url") or ""))

    def vehicle_statuses(self):
        """({vehicle id: VehicleStatus}, names of cameras with unrecognised parked cars); needs presence."""
        from sentinel.engine.questions import VehicleStatus

        store = self.recording_store
        statuses = {}
        for v in self.household.vehicles():
            arr = store.last_household_event(v.id, "arrived")
            dep = store.last_household_event(v.id, "left")
            arr_ts, dep_ts = (arr or {}).get("ts"), (dep or {}).get("ts")
            spot = self.presence.where_is(v.id)
            camera = self.cameras[spot[0]].display_name if spot and spot[0] in self.cameras else None
            since = arr_ts if spot and arr_ts and (not dep_ts or arr_ts > dep_ts) else None
            statuses[v.id] = VehicleStatus(v.name, spot is not None, camera, since, arr_ts, dep_ts)
        unknown = [self.cameras[key].display_name for key, cars in self.presence.present().items()
                   for p in cars if p.vehicle_id is None and key in self.cameras]
        return statuses, unknown

    # ------------------------------------------------------ family locations (OwnTracks)
    def location_question(self, text: str):
        from sentinel.engine.location import location_question

        return location_question(text, self.locations.people(), self.locations.places(), self.household.vehicles())

    def _cars_of(self, person, statuses) -> list:
        """The person's own cars (by the Vehicles page's "whose is it?"), as CarLines."""
        from sentinel.engine.location import CarLine

        words = set(person.words())
        return [CarLine(s.name, s.here, s.last_arrival, s.last_departure)
                for v in self.household.vehicles() if v.owner and v.owner.lower() in words
                for s in [statuses.get(v.id)] if s is not None]

    def person_views(self) -> Dict[int, Any]:
        from sentinel.engine.location import PersonView

        statuses = self.vehicle_statuses()[0] if self.presence is not None else {}
        places = {p.id: p for p in self.locations.places()}
        views = {}
        for person in self.locations.people():
            fix = self.locations.fix(person.id)
            views[person.id] = PersonView(person, fix, places.get((fix or {}).get("place_id")),
                                          self.locations.visits(person.id), self._cars_of(person, statuses))
        return views

    def answer_location_question(self, q) -> Tuple[str, List[Tuple[str, float, float]]]:
        """Reply and map pins; "who's home?" also lists the cars (people swap cars)."""
        from sentinel.engine.location import answer_location
        from sentinel.engine.questions import HouseholdQuestion

        text, pins = answer_location(q, self.person_views(), self.locations.places())
        if q.kind == "list" and self.presence is not None and self.household.vehicles():
            text += "\n\nCars:\n" + self.answer_household_question(HouseholdQuestion("list"))
        return text, pins

    def owntracks_post(self, username: str, password: str, payload: Any) -> Optional[str]:
        """A position from a family phone. Returns the person's name, or None if the login is wrong."""
        from sentinel.engine.location import change_message

        person = self.locations.authenticate(username, password)
        if person is None:
            return None
        changes = self.locations.record(person, payload) if isinstance(payload, dict) else []
        if changes:
            statuses = self.vehicle_statuses()[0] if self.presence is not None else {}
            for change in changes:
                app_logger.log("info", "location_" + change.kind, person=person.name,
                               place=change.place.name if change.place else None, component="engine",
                               action="location")
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
        return person.name

    def _send_location_alert(self, text: str) -> None:
        now = time.time()
        for chat_id in self.telegram.chat_ids:
            if not self.telegram._is_paused(chat_id, now):
                threading.Thread(target=self.telegram.reply_to_chat, args=(chat_id, text),
                                 name="location-alert", daemon=True).start()

    # Telegram: send a 📍 pin, then reply with a name, to add a place without typing coordinates.
    def _on_telegram_pin(self, chat_id: str, lat: float, lon: float, title: str = "") -> None:
        self._place_pins[chat_id] = (lat, lon, time.time() + 600)
        has_home = any(p.is_home for p in self.locations.places())
        example = "the salon, school, work, Grandma's" if has_home else "home, the salon, school, work"
        hint = f' (“{title}”?)' if title else ""
        self.telegram.reply_to_chat(chat_id, f"📍 Got the pin{hint}. What should I call this place? Reply with a name "
                                             f"the way you'd say it after “at” — e.g. {example}. Or reply cancel.")

    def _name_pinned_place(self, chat_id: str, text: str) -> bool:
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

    # ---- web: People page, join links, Live page lights
    def _owntracks_endpoint(self, base_url: str) -> str:
        return (str(self.settings.get("remote_web_url") or "").strip() or base_url).rstrip("/") + "/owntracks"

    def _join_url(self, base_url: str, code: str) -> str:
        return (str(self.settings.get("remote_web_url") or "").strip() or base_url).rstrip("/") + "/join/" + code

    def family_lights(self) -> List[Dict[str, Any]]:
        """Live page: one light per person (home / out / not sharing) with a map position."""
        from sentinel.engine.location import whereabouts

        places = self.locations.places()
        now = time.time()
        out = []
        for v in self.person_views().values():
            if not v.fix:
                out.append({"name": v.person.name, "state": "off", "label": "not sharing location"})
                continue
            home = v.place is not None and v.place.is_home
            out.append({"name": v.person.name, "state": "home" if home else "out",
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
                "invite": {"url": self._join_url(base_url, invite[0]), "expires": invite[1]} if invite else None,
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
        new_id = self.locations.save_person(name, aliases, person_id)
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
            return True, self._join_url(base_url, self.locations.new_invite(person_id))
        else:
            return False, "Unknown action."
        app_logger.log("info", "location_person_" + action.replace("-", "_"), component="engine", action="location")
        return True, "Done."

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
                               "then copy the numbers that appear (like 41.1145, -83.1780) — or tap Share on "
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

    def backfill_events_from_log(self) -> int:
        """One-off: import sightings logged before the events table existed (no photos)."""
        if self.recording_store.event_count():
            return 0
        from sentinel.detection.objects import CLASS_GROUPS
        from sentinel.detection.vehicle_type import VEHICLE_TYPES

        keys = {p.display_name: key for key, p in self.cameras.items()}
        added = 0
        for record in admin.object_log_records():
            vt = record.get("vehicle_type")
            group = "delivery" if vt in VEHICLE_TYPES and VEHICLE_TYPES[vt][1] else CLASS_GROUPS.get(record.get("label"))
            if not group or group == "delivery" and not vt:
                continue
            name = record.get("camera_display") or "Camera"
            self.recording_store.add_event(
                record["ts"], keys.get(name, name), name, group, record.get("description") or "",
                label=record.get("label"), vehicle_type=vt, zone=record.get("zone"),
            )
            added += 1
        return added

    def answer_video_query(self, chat_id: str, text: str, max_clips: int = 8) -> None:
        """Reply to 'videos <when> [people|vehicles|animals] [camera]' with matching clips."""
        from sentinel.detection.objects import CLASS_GROUPS
        from sentinel.engine.queries import parse_video_query

        names = {p.display_name: key for key, p in self.cameras.items()}
        query, error = parse_video_query(text, names.keys())
        if query is None:
            self.telegram.reply_to_chat(chat_id, error)
            return
        clips = self.recording_store.query(query.start, query.end)
        if query.cameras:
            keys = {names[n] for n in query.cameras}
            clips = [c for c in clips if c["camera_key"] in keys]
        if query.groups:
            clips = [c for c in clips
                     if any(CLASS_GROUPS.get(l) in query.groups for l in (c.get("labels") or "").split(","))]
        what = (" with " + "/".join(sorted(query.groups))) if query.groups else ""
        where = (" on " + ", ".join(query.cameras)) if query.cameras else ""
        if not clips:
            self.telegram.reply_to_chat(chat_id, f"No recorded clips{what}{where} ({query.label}).")
            return
        chosen = clips
        if len(clips) > max_clips:
            # Prefer clips where something was recognised, then the most recent.
            labelled = [c for c in clips if c.get("labels")]
            pool = labelled if len(labelled) >= max_clips else clips
            chosen = sorted(pool, key=lambda c: c["start_ts"])[-max_clips:]
            note = (f"Found {len(clips)} clips{what}{where} ({query.label}). Sending the {len(chosen)} most recent"
                    f"{' with something recognised' if pool is labelled else ''}. Ask for a shorter time range for others.")
        else:
            note = f"Sending {len(chosen)} clip{'s' if len(chosen) > 1 else ''}{what}{where} ({query.label})."
        self.telegram.reply_to_chat(chat_id, note)
        with self._video_query_lock:
            for clip in chosen:
                record = self.recording_store.get(clip["id"]) or clip
                path = self.recording_store.hd_absolute_path(record) or self.recording_store.web_playable_path(clip["id"])
                when = time.strftime("%a %H:%M:%S", time.localtime(clip["start_ts"]))
                labels = (clip.get("labels") or "").replace(",", ", ")
                camera = self.cameras[clip["camera_key"]].display_name if clip["camera_key"] in self.cameras else clip["camera_name"]
                caption = f"{camera} · {when}" + (f" · {labels}" if labels else "")
                if not path or not self.telegram.send_video(chat_id, path, caption):
                    self.telegram.reply_to_chat(chat_id, f"{caption}: couldn't send this clip.")

    def _request_clips(self, chat_ids: List[str]) -> None:
        live = [p for p in self.cameras.values() if p.connected and p.profile.get("enabled", True)]
        if not live:
            for chat_id in chat_ids:
                self.telegram.reply_to_chat(chat_id, "Sentinel: no camera connected; clip not recorded.")
            return
        for pipeline in live:
            if not pipeline.detector.recording_enabled:
                for chat_id in chat_ids:
                    self.telegram.reply_to_chat(
                        chat_id, f"Sentinel: recording is off for {pipeline.display_name}."
                    )
                continue
            pipeline.request_clip(chat_ids)

    def status_message(self, chat_id: str) -> str:
        s = self.settings
        on = lambda flag: "on" if flag else "off"
        remaining = self.telegram.remaining_pause_minutes(chat_id)
        lines = ["Sentinel status"]
        lines.append(f"Pause: {remaining} minute(s) left for this chat" if remaining > 0 else "Pause: not paused")
        for pipeline in self.cameras.values():
            if not pipeline.profile.get("enabled", True):
                lines.append(f"{pipeline.display_name}: disabled")
                continue
            state = "online" if pipeline.connected else "OFFLINE"
            rec = on(pipeline.detector.recording_enabled)
            lines.append(f"{pipeline.display_name}: {state}, recording {rec}")
        lines.append(f"Telegram motion alerts: {on(s.get('telegram_motion_alerts', True))}")
        lines.append(f"Motion clip uploads (Telegram): {on(s.get('telegram_include_video', False))}")
        lines.append(f"Night bug filter: {on(s.get('night_bug_filter_enabled', True))}")
        lines.append(f"Auto reconnect: {on(s.get('auto_reconnect_enabled', True))}")
        lines.append(f"Alert cooldown: {int(float(s.get('telegram_cooldown', 10)))}s")
        return "\n".join(lines)

    def apply_feature_toggles(self, chat_id: str, changes: Dict[str, bool]) -> None:
        applied = self.apply_feature_changes(changes, source="telegram")
        self.telegram.reply_to_chat(
            chat_id, "Applied:\n" + "\n".join(applied) if applied else "No changes applied."
        )

    def apply_feature_changes(self, changes: Dict[str, bool], source: str) -> List[str]:
        applied: List[str] = []
        for key, val in changes.items():
            if key == "recording":
                self.settings_manager.set("recording_enabled", val, save=False)
                self._set_all_profiles({"recording_enabled": val})
                applied.append(f"Recording: {'on' if val else 'off'}")
            elif key == "messages":
                self.settings_manager.set("telegram_motion_alerts", val, save=False)
                applied.append(f"Telegram motion alerts: {'on' if val else 'off'}")
            elif key == "clips":
                self.settings_manager.set("telegram_include_video", val, save=False)
                self.telegram.update_settings({"telegram_include_video": val})
                applied.append(f"Motion clip uploads: {'on' if val else 'off'}")
            elif key == "night_bug":
                self.settings_manager.set("night_bug_filter_enabled", val, save=False)
                self._set_all_profiles({"night_bug_filter_enabled": val})
                applied.append(f"Night bug filter: {'on' if val else 'off'}")
            elif key == "reconnect":
                self.settings_manager.set("auto_reconnect_enabled", val, save=False)
                applied.append(f"Auto reconnect: {'on' if val else 'off'}")
                if val:
                    for pipeline in self.cameras.values():
                        pipeline.start()  # no-op if its thread is still running
        self.settings_manager.save_settings(force=True)
        app_logger.log("info", "remote_feature_toggle", changes=changes, component="engine", action=source)
        return applied

    def _set_all_profiles(self, changes: Dict[str, Any]) -> None:
        with self._lock:
            for pipeline in self.cameras.values():
                profile = dict(pipeline.profile, **changes)
                self._camera_profiles()[pipeline.camera_id] = profile
                pipeline.apply_profile(profile)

    # ------------------------------------------------------ web config API
    def camera_configs(self) -> List[Dict[str, Any]]:
        """Credential-free camera settings for the web editor."""
        out = []
        for key, pipeline in sorted(self.cameras.items(), key=lambda kv: kv[1].display_name):
            p = pipeline.profile
            source = urlparse(str(pipeline.source))
            out.append(
                {
                    "key": key,
                    "name": pipeline.display_name,
                    "online": pipeline.connected,
                    "stream": f"{source.scheme}://{source.hostname}:{source.port or 554}{source.path}"
                    if source.hostname else "local device",
                    "username": source.username or "",
                    "last_frame_age": round(time.time() - pipeline.last_frame_time, 1)
                    if pipeline.last_frame_time else None,
                    "recording": pipeline.detector.recording,
                    "hd_available": pipeline.hd_source() is not None,
                    "hd_running": bool(pipeline.hd and pipeline.hd.running),
                    **{field: copy.deepcopy(p.get(field)) for field in EDITABLE_PROFILE_FIELDS},
                }
            )
            out[-1]["enabled"] = p.get("enabled", True) is not False
            out[-1]["hd_recording"] = p.get("hd_recording", True) is not False
            out[-1]["display"] = pipeline.display_name
        return out

    def update_camera_config(self, key: str, changes: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate and apply profile changes from the web editor."""
        pipeline = self.cameras.get(key)
        if pipeline is None:
            return False, "unknown camera"
        cleaned: Dict[str, Any] = {}
        try:
            for field, value in changes.items():
                validator = EDITABLE_PROFILE_FIELDS.get(field)
                if validator is None:
                    return False, f"field not editable: {field}"
                result = validator(value)
                if result is None:
                    return False, f"invalid value for {field}"
                cleaned[field] = result
        except (TypeError, ValueError):
            return False, "invalid value"
        with self._lock:
            profile = dict(pipeline.profile, **cleaned)
            self._camera_profiles()[pipeline.camera_id] = profile
            pipeline.apply_profile(profile)
            if "display" in cleaned:
                pipeline.display_name = cleaned["display"]
                pipeline.detector.camera_name = cleaned["display"]
            self.settings_manager.save_settings(force=True)
        if "hd_recording" in cleaned and "enabled" not in cleaned and pipeline.profile.get("enabled", True):
            self.reconnect_camera(key)  # restart to start/stop the main-stream recorder
        if "enabled" in cleaned:
            if cleaned["enabled"]:
                self.reconnect_camera(key)
            else:
                pipeline.stop()
        app_logger.log(
            "info", "camera_config_updated", camera_ip=key, fields=sorted(cleaned),
            component="engine", action="update_camera_config",
        )
        return True, "saved"

    def camera_snapshot(self, key: str) -> Optional[np.ndarray]:
        pipeline = self.cameras.get(key)
        return pipeline.snapshot() if pipeline else None

    # ------------------------------------------------- camera management
    def reconnect_camera(self, key: str) -> Tuple[bool, str]:
        """Restart a camera's pipeline with a fresh connection."""
        with self._lock:
            old = self.cameras.get(key)
            if old is None:
                return False, "unknown camera"
            old.stop(timeout=3)
            pipeline = self._make_pipeline(old.camera_id, old.profile)
            self.cameras[key] = pipeline
        if pipeline.profile.get("enabled", True):
            pipeline.start()
        return True, "reconnecting"

    @staticmethod
    def _camera_spec(spec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Validate the Add-a-camera form: (fields, "") or (None, error message)."""
        host = str(spec.get("host", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
            return None, "enter the camera's IP address or hostname"
        try:
            port = int(spec.get("port") or 554)
        except (TypeError, ValueError):
            return None, "port must be a number"
        if not 1 <= port <= 65535:
            return None, "port must be 1-65535"
        paths = {}
        for key, default in (("path", "/"), ("main_path", "")):
            value = str(spec.get(key) or default).strip()
            if value and (not value.startswith("/") or re.search(r"\s", value) or len(value) > 200):
                return None, "stream paths must start with / and have no spaces"
            paths[key] = value
        user = str(spec.get("username") or "")
        password = str(spec.get("password") or "")
        auth = ""
        if user:
            auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
        source = f"rtsp://{auth}{host}:{port}{paths['path']}"
        main_source = f"rtsp://{auth}{host}:{port}{paths['main_path']}" if paths["main_path"] else None
        return {"host": host, "port": port, "path": paths["path"], "user": user, "password": password,
                "source": source, "main_source": main_source,
                "name": _display_name(spec.get("name") or "") or f"Camera {host}"}, ""

    def test_camera(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Try a camera before adding it (Settings → Add a camera → Test connection)."""
        from sentinel.engine.camera_probe import guess_main_source, probe_camera

        fields, error = self._camera_spec(spec)
        if fields is None:
            return {"ok": False, "message": error}
        result = probe_camera(fields["host"], fields["port"], fields["path"], fields["user"], fields["password"],
                              fields["source"], fields["main_source"] or guess_main_source(fields["source"]))
        app_logger.log("info", "camera_tested", camera_ip=fields["host"], ok=result["ok"],
                       component="engine", action="test_camera")
        return result

    def add_camera(self, spec: Dict[str, Any]) -> Tuple[bool, str]:
        """Add an RTSP camera from {name, host, port, path, main_path, username, password}."""
        fields, error = self._camera_spec(spec)
        if fields is None:
            return False, error
        host, name, source = fields["host"], fields["name"], fields["source"]
        if host in self.cameras:
            return False, "that camera is already added"
        profile = {
            "source": source,
            "display": name,
            "enabled": True,
            "active_zones": [],
            "entry_lines": [],
            "entry_direction": "outside_to_inside",
            "min_contour_area": 2000,
            "motion_cooldown": 30.0,
            "recording_enabled": True,
            "night_bug_filter_enabled": bool(self.settings.get("night_bug_filter_enabled", True)),
            "night_bug_start_hour": int(self.settings.get("night_bug_start_hour", 20)),
            "night_bug_end_hour": int(self.settings.get("night_bug_end_hour", 6)),
        }
        if fields["main_source"]:
            profile["record_source"] = fields["main_source"]  # high-res stream given by hand
        with self._lock:
            self._camera_profiles()[source] = profile
            self.settings["camera_profiles"] = self._camera_profiles()
            self.settings_manager.save_settings(force=True)
            pipeline = self._make_pipeline(source, profile)
            self.cameras[pipeline.host] = pipeline
        pipeline.start()
        app_logger.log("info", "camera_added", camera_ip=host, camera_display=name,
                       component="engine", action="add_camera")
        return True, pipeline.host

    def remove_camera(self, key: str) -> Tuple[bool, str]:
        with self._lock:
            pipeline = self.cameras.pop(key, None)
            if pipeline is None:
                return False, "unknown camera"
            self._camera_profiles().pop(pipeline.camera_id, None)
            self.settings_manager.save_settings(force=True)
        pipeline.stop(timeout=3)
        app_logger.log("info", "camera_removed", camera_ip=key, camera_display=pipeline.display_name,
                       component="engine", action="remove_camera")
        return True, "removed"

    # ---------------------------------------------------- general settings
    def settings_view(self) -> Dict[str, Any]:
        """Editable settings for the web page (secrets reported only as set/unset)."""
        values = {key: admin.get_setting(self.settings, key) for key in admin.SETTINGS_SCHEMA}
        values["telegram_chat_ids"] = admin._chat_ids(
            self.settings.get("telegram_chat_ids") or self.settings.get("telegram_chat_id") or ""
        )
        return {
            "values": values,
            "secrets": {key: bool(self.settings.get(key)) for key in admin.WRITE_ONLY_SETTINGS},
            "voices": admin.available_voices(),
            "detector_models": admin.available_detector_models(),
        }

    def update_settings(self, changes: Dict[str, Any]) -> Tuple[bool, str]:
        cleaned: Dict[str, Any] = {}
        for key, value in changes.items():
            validator = admin.SETTINGS_SCHEMA.get(key) or admin.WRITE_ONLY_SETTINGS.get(key)
            if validator is None:
                return False, f"not editable: {key}"
            try:
                cleaned[key] = validator(value)
            except (TypeError, ValueError) as e:
                return False, f"{key}: {e}"
        with self._lock:
            for key, value in cleaned.items():
                admin.set_setting(self.settings, key, value)
            if "telegram_chat_ids" in cleaned:
                self.settings["telegram_chat_id"] = ""  # legacy single-id field
            self._apply_settings_side_effects(cleaned)
            self.settings_manager.save_settings(force=True)
        app_logger.log("info", "settings_updated", fields=sorted(cleaned), component="engine",
                       action="update_settings")
        return True, "saved"

    def _apply_settings_side_effects(self, cleaned: Dict[str, Any]) -> None:
        telegram_keys = {k: v for k, v in cleaned.items() if k.startswith("telegram_")}
        if telegram_keys:
            update = dict(telegram_keys)
            if "telegram_chat_ids" in update:
                update["telegram_chat_id"] = ""
            self.telegram.update_settings(update)
        if {"night_bug_start_hour", "night_bug_end_hour"} & cleaned.keys():
            self._set_all_profiles({
                "night_bug_start_hour": int(self.settings.get("night_bug_start_hour", 20)),
                "night_bug_end_hour": int(self.settings.get("night_bug_end_hour", 6)),
            })
        if {"volume", "speech_speed", "voice_id", "audio_output"} & cleaned.keys():
            if self.audio is None:
                self.audio = self._init_audio()
            else:
                self.audio.update_settings(self.settings)
        if {"voice_enabled", "volume"} & cleaned.keys() and self._started:
            self._stop_voice()
            self._start_voice()

    def send_telegram_test(self) -> Tuple[bool, str]:
        if not self.telegram.enabled:
            return False, "Telegram is turned off"
        if not self.telegram.is_configured():
            return False, "add a bot token and at least one chat id first"
        if self.telegram.send_test_message():
            return True, "test message sent"
        return False, "Telegram did not accept the message (check the token and chat ids)"

    def system_status(self) -> Dict[str, Any]:
        cams = [
            {
                "key": c["key"],
                "name": c["name"],
                "enabled": c["enabled"],
                "online": c["online"],
                "recording": c["recording"],
                "last_frame_age": c["last_frame_age"],
            }
            for c in self.camera_configs()
        ]
        return {
            "uptime_seconds": int(time.time() - self.started_at),
            "cpu_percent": self._cpu.percent(),
            "disk": admin.disk_status(),
            "recordings": self.recording_store.stats(),
            "cameras": cams,
            "telegram": {"enabled": self.telegram.enabled, "configured": self.telegram.is_configured()},
            "remote": admin.remote_access_info(self.settings),
            "events": admin.recent_events(),
            "objects": {
                "enabled": self.detection_worker is not None,
                "model": self.settings.get("detector_model"),
                "device": self.detection_worker.detector.device if self.detection_worker else None,
                "inference_ms": round(self.detection_worker.inference_ms) if self.detection_worker else None,
                "error": self.detector_error,
                "recent": admin.recent_objects(),
            },
        }
