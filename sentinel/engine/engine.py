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
from sentinel.engine.commands import Command, Commands, parse_feature_toggle
from sentinel.engine.spotify import NO_SUCH_SPEAKER, SpotifyService, parse_music_command
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


# "Turn the PC down" (the whole PC: voice, alerts and music) vs "speak quieter" (just the voice).
_PC = r"(?:the |this |your )?(?:p ?c|computer|speakers?|system)(?:'s)?"
_STEP = r"(?: a (?:bit|little|lot))?"


def parse_volume_command(text: str) -> Optional[Tuple[str, str]]:
    """("pc", "+" / "-" / "40") or ("voice", ...), or None. Music volume is Spotify's (parse_music_command)."""
    from sentinel.engine.spotify import _NUMBERS

    t = re.sub(r"^(please |can you |could you |would you )|( please)$", "", (text or "").lower().strip(" .?!"))
    t = re.sub(r"\s+", " ", t)

    def level(word: str) -> Optional[str]:
        if word.isdigit():
            return str(min(100, int(word)))
        return str(_NUMBERS[word]) if word in _NUMBERS else None

    m = re.fullmatch(rf"(?:turn |set |put )?{_PC} volume (?:to |at )?(\d{{1,3}}|[a-z ]+?)(?: percent)?", t)
    if m and level(m.group(1)):
        return ("pc", level(m.group(1)))
    m = (re.fullmatch(rf"turn {_PC}(?: volume)? (up|down){_STEP}", t)
         or re.fullmatch(rf"turn (up|down) {_PC}(?: volume)?{_STEP}", t)
         or re.fullmatch(rf"{_PC} volume (up|down){_STEP}", t)
         or re.fullmatch(rf"make {_PC} (louder|quieter|softer){_STEP}", t))
    if m:
        return ("pc", "+" if m.group(1) in ("up", "louder") else "-")
    m = re.fullmatch(r"(?:set )?your (?:voice |speaking )?volume (?:to |at )?(\d{1,3}|[a-z ]+?)(?: percent)?", t)
    if m and level(m.group(1)):
        return ("voice", level(m.group(1)))
    m = (re.fullmatch(rf"(?:speak|talk){_STEP} (up|louder|quieter|softer|more quietly|more softly|down)", t)
         or re.fullmatch(rf"turn (?:your voice|yourself) (up|down){_STEP}", t)
         or re.fullmatch(rf"(?:you'?re|you are) too (loud|quiet|soft)", t))
    if m:
        return ("voice", "+" if m.group(1) in ("up", "louder", "quiet", "soft") else "-")
    return None


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
        from sentinel.engine.reminders import ReminderStore

        self.reminders = ReminderStore(VIDEO_DIR)  # "remind me ..." -> a Telegram message at that time
        self._reminders_checked = 0.0
        self._reminder_failed: set = set()  # ids whose send failed (logged once; retried every check)
        from sentinel.engine.switches import SmartSwitches

        self.switches = SmartSwitches(VIDEO_DIR)  # Kasa smart plugs/switches on the home network
        from sentinel.engine.remotes import Remotes

        self.remotes = Remotes(VIDEO_DIR)  # IR/RF buttons taught to a BroadLink (heater, soundbar, fans)
        from sentinel.engine.heater import HeaterControl

        # A heater with only a remote, held at a temperature from the BroadLink's thermometer
        self.heater = HeaterControl(VIDEO_DIR, self.remotes, self.settings, notify=lambda text: [
            self.telegram.reply_to_chat(c, text) for c in self.telegram.unpaused_chats()])
        self.switches.extras = [self.heater, self.remotes]
        from sentinel.engine.wifi_motion import WifiMotion

        self.wifi_motion = WifiMotion(VIDEO_DIR)  # ESP32 boards that sense movement through the Wi-Fi signal
        from sentinel.engine.house import HouseLayout

        self.house = HouseLayout(VIDEO_DIR)  # the House page's 3D drawing and where each device sits on it
        self.presence = None  # PresenceTracker, when object detection + CLIP are available
        self.packages = None  # PackageWatch (parcels left at a door camera), likewise
        from sentinel.engine.camera_light import CameraLights

        self.camera_lights = CameraLights()  # white light for cars at night (cameras in camera_admin.json)
        self.telegram = TelegramNotifier(self.settings)
        self.weather = WeatherService(self.settings)
        from sentinel.engine.cars import Cars

        self.cars = Cars(  # the Vehicles page, "whose car is this?" / "Wrong car?" and the driveway's arrive/leave
            self.settings, self.household, self.telegram, self.recording_store, presence=lambda: self.presence,
            cameras=lambda: self.cameras,
            classifier=lambda: getattr(self.detection_worker, "vehicle_classifier", None),
            on_car=lambda kind, vehicle_id: self.routines.car(kind, vehicle_id))
        from sentinel.engine.family import Family

        self.family = Family(  # phones, pets' trackers and places (the Family and Timeline pages)
            self.settings, self.settings_manager, self.locations, self.household, self.telegram, self.weather,
            presence=lambda: self.presence, vehicle_statuses=self.cars.statuses,
            household_answer=self.cars.answer_question,
            on_home=lambda kind, person_id: self.routines.person(kind, person_id),
            speak=lambda text: self.audio.speak(text) if self.audio is not None else None)
        from sentinel.engine.mesh import MeshChat

        self.mesh = MeshChat(  # chat over the LoRa mesh, through the pets' base station (the Mesh page)
            VIDEO_DIR, self.settings, self.settings_manager, self.locations.authenticate, self.telegram,
            trackers=lambda: [p.tracker for p in self.locations.people() if p.tracker],
            speak=lambda text: self.audio.speak(text) if self.audio is not None else None)
        from sentinel.engine.news import HouseNews

        self.news = HouseNews(  # package news, the morning recap, routine and weather messages
            self.settings, self.telegram, self.recording_store, self.weather, packages=lambda: self.packages,
            weather_warnings=lambda: self.weather_alerts.active(), home_coords=self.family.home_coords,
            seen=lambda camera, group, jpeg: self.routines.seen(camera, group, jpeg))
        from sentinel.engine.routines import HOLDS_FILE, RoutineRunner, RoutineStore

        self._last_motion: Dict[str, float] = {}  # camera -> last frame with motion (routines' "until quiet")
        self.routines = RoutineRunner(RoutineStore(VIDEO_DIR), {
            "set_switch": self.switches.set,
            "switch_on": lambda sid: next((s.on for s in self.switches.switches() if s.id == sid and s.online), None),
            "telegram": self.news.routine_message,
            "say": lambda text: self.audio.speak(text) if self.audio is not None else None,
            "coords": self.family.home_coords,
            "nobody_home": self.family.nobody_home,
            "last_motion": lambda cam: (max(self._last_motion.values(), default=0.0) if cam is None
                                        else self._last_motion.get(cam, 0.0)),
            "last_sensor_motion": self.wifi_motion.last_motion,
        }, holds_path=os.path.join(VIDEO_DIR, HOLDS_FILE))
        self.wifi_motion.on_motion = self.routines.sensor
        self._routines_ticked = 0.0
        from sentinel.engine.recap import Recap

        self.recap = Recap(self.settings, self.recording_store, self.news.recap_extra, self.news.recap_text,
                           self.news.recap_video, os.path.join(VIDEO_DIR, "recap_sent.txt"))
        from sentinel.engine.weather_alerts import WeatherAlerts

        self.weather_alerts = WeatherAlerts(
            self.settings, self.family.home_coords, self.news.tell_everyone,
            lambda text: self.audio.speak(text) if self.audio is not None else None, VIDEO_DIR)
        self.assistant = Assistant(self.settings)
        from sentinel.engine.recipes import RecipeBox

        self.recipes = RecipeBox(  # the family recipe box (the Recipes page); Claude for swaps and twists
            VIDEO_DIR, self.assistant, speak=lambda text: self.audio.speak(text) if self.audio is not None else None)
        from sentinel.engine.backup import Backup

        self.backup = Backup(self.settings, VIDEO_DIR, os.getcwd(),  # nightly, to a USB drive (Settings → Backup)
                             warn=lambda text: [self.telegram.reply_to_chat(c, text) for c in self.telegram.chat_ids[:1]])
        self.spotify = SpotifyService(self.settings, lambda: self.settings_manager.save_settings(force=True))
        self.spotify.open_on_tv = lambda words: self.switches.open_app(words, "spotify", "spotify")
        from sentinel.audio.spotify_player import SpotifyPlayer  # (sentinel.audio loads the speech engines)

        from sentinel.audio import devices

        devices.use_cards(self.settings.get("audio_device"), self.settings.get("music_device", "auto"))  # line-out or USB
        self.spotify_player = SpotifyPlayer(self.settings)  # this PC as a Spotify speaker (spotifyd)
        self._command_offers: Dict[int, Tuple[str, str, float]] = {}  # "Do it" buttons under Claude's replies
        self._offer_ids = itertools.count(1)
        self.commands = self._build_commands()
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
            on_maybe_person=self._on_maybe_person,
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
        self.detection_worker.lights = self.camera_lights
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
        if classifier is not None:
            self.household.ready = False  # until the fingerprint model is chosen (and saved photos match it)
            threading.Thread(target=self.cars.use_fingerprints, args=(classifier, device), name="fingerprints",
                             daemon=True).start()
        if classifier is not None and self.settings.get("household_tracking_enabled", True):
            from sentinel.engine.household import PresenceTracker

            self.presence = PresenceTracker(
                self.household, detector, classifier,
                cameras=lambda: [p for p in self.cameras.values() if p.connected],
                on_change=self.cars.on_change,
                state_path=os.path.join(self.household.crops_dir, "presence.json"),
                on_unknown=self.cars.ask_whose_car,
                on_renamed=self.cars.on_renamed,
            )
            self.detection_worker.presence = self.presence
            self.presence.lights = self.camera_lights
        if classifier is not None:
            from sentinel.detection.package_check import PackageChecker
            from sentinel.engine.packages import PackageWatch

            self.packages = PackageWatch(
                self.settings, cameras=lambda: [p for p in self.cameras.values() if p.connected],
                detector=detector, checker=PackageChecker(classifier), notify=self.news.package_message,
                sunset=self.news.sunset_today, nobody_home=self.family.nobody_home, on_left=self.news.on_package,
                on_waiting=self.routines.package_waiting, on_gone=self.routines.package_gone,
                state_dir=os.path.join(VIDEO_DIR, ".packages"))
            self.detection_worker.packages = self.packages
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
        if self.packages is not None:
            self.packages.start()
        self.heater.start()
        self.camera_lights.start()
        self.switches.start()
        self.wifi_motion.start()

        self.telegram.question_filter = self.commands.claims
        self.telegram.location_handler = self.family.on_telegram_pin
        self.telegram.text_interceptor = self.family.name_pinned_place
        self.telegram.switch_filter = self.switches.understands
        self.telegram.assistant_available = self.assistant.enabled
        if self.assistant.enabled():
            threading.Thread(target=self._check_assistant, name="assistant-check", daemon=True).start()
        self._command_thread = threading.Thread(
            target=self._command_loop, name="telegram-commands", daemon=True
        )
        self._command_thread.start()
        self._start_voice()
        if self.settings.get("pc_volume") is not None:  # ALSA forgets it at reboot without `alsactl store` (sudo)
            from sentinel.audio.alert_system import set_pc_volume

            set_pc_volume(int(self.settings["pc_volume"]))
        self.spotify_player.start()
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
        if self.packages is not None:
            self.packages.stop()  # keeps the porch's quiet picture for the restart
        self.heater.stop()
        self.camera_lights.stop()
        self.switches.stop()
        self.wifi_motion.stop()
        self._stop_voice()
        self.spotify_player.stop()
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
        self.routines.seen(pipeline.host, group, jpeg)
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
            self.telegram.trigger_photo_alert(event.description, jpeg, self.cars.fix_button(pipeline, event))
            self._clip_for_alert[pipeline.host] = (event.timestamp, event.description)

    def _on_maybe_person(self, pipeline: CameraPipeline, track) -> None:
        """A moving person-shaped track too unsure to alert about (e.g. dim in infrared): enough for routines
        that only switch lights (runs on the detection thread)."""
        app_logger.log("info", "maybe_person", camera_display=pipeline.display_name, score=round(track.best_score, 2),
                       hits=track.hits, component="engine", action="on_maybe_person")
        self.routines.seen(pipeline.host, "person", sure=False)

    def _on_motion(self, pipeline: CameraPipeline) -> None:
        self._last_motion[pipeline.host] = time.time()
        self.routines.motion(pipeline.host)
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
                for chat_id, text in self.telegram.consume_reminder_commands():
                    self.telegram.reply_to_chat(chat_id, self.reminder_command(chat_id, text))
                if time.time() - self._reminders_checked >= 15:
                    self._reminders_checked = time.time()
                    self.send_due_reminders()
                for chat_id, text in self.telegram.consume_routine_commands():
                    self.telegram.reply_to_chat(chat_id, self.routine_command(text))
                if time.time() - self._routines_ticked >= 5:
                    self._routines_ticked = time.time()
                    self.routines.tick()
                    self.weather_alerts.tick()  # checks the NWS itself every 2 minutes, on its own thread
                    if self.recap.due(time.time()):
                        threading.Thread(target=self.recap.run, name="recap", daemon=True).start()
                    if self.backup.due(time.time()):
                        self.backup.start()
                for chat_id, text in self.telegram.consume_switch_commands():
                    threading.Thread(target=self._switch_reply, args=(chat_id, text), name="switch",
                                     daemon=True).start()  # a plug that doesn't answer takes seconds
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

    def _indoor_question(self, text: str):
        """Movement inside the house (the Wi-Fi sensors), unless a camera is named."""
        return self.wifi_motion.indoor_question(text, [p.display_name for p in self.cameras.values()])

    def _build_commands(self) -> Commands:
        """What both Telegram and voice understand, each once, in the order Telegram checks them."""
        from sentinel.audio.voice import to_speech
        from sentinel.engine.location import beep_command, lost_command, outing_command
        from sentinel.engine.mesh import read_command
        from sentinel.engine.packages import TAKEN_IN
        from sentinel.engine.questions import household_question
        from sentinel.engine.recap import RECAP
        from sentinel.engine.recipes import can_make_command, cooking_command, is_video_link, save_link_command

        def send_location_reply(chat_id: str, lq, _text: str) -> None:
            reply, pins = self.family.answer_location_question(lq)
            self.telegram.reply_to_chat(chat_id, reply)
            for _name, lat, lon in pins[:3]:
                self.telegram.send_location(chat_id, lat, lon)

        commands = Commands(lambda chat_id, text: self.telegram.reply_to_chat(chat_id, text))
        commands.add(Command(
            "recap", lambda t: RECAP.match(t.strip()), lambda _m, _t: "",
            telegram=lambda chat_id, _m, _t: self.recap.run(chat_id)))  # last night until now, to this chat
        commands.add(Command(
            "package taken in", lambda t: TAKEN_IN.match(t.strip()), lambda _m, _t: self.news.packages_taken_in(),
            speak=lambda said: said.replace("✅ ", ""),
            voice_if=lambda t: not t.strip().startswith(("got it", "ok")),
            claim_if=lambda: self.packages is not None and bool(self.packages.packages())))
        commands.add(Command(  # before "going out": "Xena got out" means she's loose
            "pet lost", lambda t: lost_command(t, self.locations.people()),
            lambda m, _t: self.family.set_lost(*m), speak=lambda said: said, prefix="🐕 "))
        commands.add(Command(
            "pets going out", lambda t: outing_command(t, self.locations.people()),
            lambda m, _t: self.family.start_outing(*m), speak=lambda said: said, prefix="🐕 "))
        commands.add(Command(
            "beep pets", lambda t: beep_command(t, self.locations.people()),
            lambda m, _t: self.family.beep_pets(m), speak=lambda said: said, prefix="🐕 "))
        commands.add(Command(
            "mesh messages", read_command, lambda _m, _t: self.mesh.read_unread(),
            speak=lambda said: to_speech(said, max_lines=20, max_chars=1200), prefix="📡 "))
        commands.add(Command(  # cooking mode is open somewhere: "next step", "how much garlic?", "start the timer"
            "cooking", cooking_command, lambda m, _t: self.recipes.answer_cooking(m, spoken=True),
            speak=lambda said: to_speech(said, max_lines=20, max_chars=1200), prefix="🍳 ",
            voice_if=lambda _t: self.recipes.cooking(), claim_if=self.recipes.cooking))
        commands.add(Command(
            "what can I make", can_make_command, lambda m, _t: self.recipes.answer_can_make(m),
            speak=lambda said: said, prefix="🍳 "))
        def save_recipe(chat_id: str, url: str, _text: str) -> None:
            if is_video_link(url):
                self.telegram.reply_to_chat(chat_id, "🎬 Watching the video for the recipe. "
                                                     "I'll send it here in a minute or two.")
            self.recipes.start_save(url, "Telegram", on_done=lambda _ok, message, _rid:
                                    self.telegram.reply_to_chat(chat_id, "🍳 " + message))

        commands.add(Command(  # Telegram only: a recipe link (or an Instagram reel) to keep
            "save recipe", save_link_command, lambda _u, _t: "", telegram=save_recipe))
        commands.add(Command(
            "location", self.family.location_question, lambda m, _t: self.family.answer_location_question(m)[0],
            speak=to_speech, voice_stage="late", telegram=send_location_reply,
            log_kind=lambda m: "location_" + m.kind))
        commands.add(Command(
            "household cars", lambda t: household_question(t, self.household.vehicles()),
            lambda m, _t: self.cars.answer_question(m), speak=to_speech, voice_stage="late",
            log_kind=lambda m: "household_" + m.kind))
        commands.add(Command(
            "movement inside", self._indoor_question, lambda _m, t: self.wifi_motion.answer(t),
            speak=to_speech, voice_stage="late", log_kind=lambda _m: "indoor"))
        return commands

    def answer_question(self, chat_id: str, text: str) -> None:
        """Reply to 'did anyone come by?'-style questions from the event history."""
        from sentinel.engine.questions import _fmt_clock

        if self.commands.telegram_reply(chat_id, text):
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
        # A parcel left at the door answers "any deliveries?" and "did anyone come by?" too (it headlines its visit).
        groups = sorted(q.groups | ({"package"} if "delivery" in q.groups else set())) or None
        store = self.recording_store
        events = store.events(q.start, q.end, groups, keys)
        others: Dict[str, int] = {}
        if groups:
            for e in store.events(q.start, q.end, None, keys):
                if e["grp"] not in groups:
                    others[e["grp"]] = others.get(e["grp"], 0) + 1
        last = store.last_event(q.start, groups, keys) if not events else None
        reply, photos = answer_question(q, events, last, others=others)
        if self.detection_worker is None:
            reply = ("Note: object recognition is off, so I only know about sightings from before it was "
                     "turned off.\n\n" + reply)
        app_logger.log("info", "question_answered", period=q.period, groups=groups, cameras=q.cameras,
                       matches=len(events), component="engine", action="answer_question")
        return reply, photos

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
            from sentinel.engine.routines import COMMAND as ROUTINE_JSON

            self.telegram.edit_text(chat_id, message_id, "✅ Saving the routine…" if ROUTINE_JSON.match(command)
                                    else f"✅ Sent: {command}")
            app_logger.log("info", "assistant_command_run", command=command, component="engine", action="button")
            self.telegram.route_text(chat_id, command, from_button=True)  # exactly as if typed
            return
        if data == "pkg:got":
            self.telegram.answer_button(callback_id, "Got it")
            self.telegram.reply_to_chat(chat_id, self.news.packages_taken_in())
            return
        if not self.cars.handle_button(chat_id, message_id, data, callback_id):
            self.telegram.answer_button(callback_id)

    # ------------------------------------------------------------ reminders
    def reminder_command(self, chat_id: Optional[str], text: str) -> str:
        """Run "Remind <date> <time> <what>", "Reminders" or "Cancel reminder N".

        ``chat_id`` None = said out loud: the reminder goes to every Sentinel chat and the reply is spoken."""
        from sentinel.engine.reminders import CANCEL, LIST, parse_remind, when_words

        text = (text or "").strip()
        now = time.time()
        if LIST.match(text):
            pending = self.reminders.pending(chat_id)
            if not pending:
                return "You have no reminders coming up."
            if chat_id is None:
                return "Your reminders: " + "; ".join(f"{when_words(r.due, now)}, {r.text}" for r in pending[:5]) + "."
            return "Coming up:\n" + "\n".join(f"{r.id}. {when_words(r.due, now)}: {r.text}" for r in pending) \
                + "\n\nCancel one with: Cancel reminder <number>"
        m = CANCEL.match(text)
        if m:
            ok = self.reminders.cancel(int(m.group(1)), chat_id)
            app_logger.log("info", "reminder_cancelled" if ok else "reminder_not_found", reminder_id=int(m.group(1)),
                           component="engine", action="reminder")
            return "Okay, that reminder is cancelled." if ok else "I couldn't find that reminder."
        parsed, why = parse_remind(text, now)
        if parsed is None:
            return why
        due, what = parsed
        r = self.reminders.add(due, what, chat_id or "")
        app_logger.log("info", "reminder_saved", reminder_id=r.id, due=time.strftime("%Y-%m-%d %H:%M", time.localtime(due)),
                       voice=chat_id is None, component="engine", action="reminder")
        where = "" if self.telegram.is_configured() else " (Telegram isn't set up yet, so it can't be sent)"
        return f"Okay, I'll remind you {when_words(due, now)}: {what}{where}."

    def send_due_reminders(self) -> None:
        from sentinel.engine.reminders import due_message

        now = time.time()
        for r in self.reminders.due(now):
            chats = [r.chat_id] if r.chat_id else list(self.telegram.chat_ids)
            sent = [c for c in chats if self.telegram.reply_to_chat(c, due_message(r, now))]
            if sent:
                self.reminders.mark_sent(r.id)
                self._reminder_failed.discard(r.id)
                app_logger.log("info", "reminder_sent", reminder_id=r.id, late_minutes=round((now - r.due) / 60),
                               component="engine", action="reminder")
            elif r.id not in self._reminder_failed:
                self._reminder_failed.add(r.id)
                app_logger.log("warning", "reminder_send_failed", reminder_id=r.id,
                               error_message="Telegram didn't accept it; retrying", component="engine",
                               action="reminder")

    def _check_assistant(self) -> None:
        ok, message = self.assistant.check_login()
        app_logger.log("info" if ok else "warning", "assistant_ready" if ok else "assistant_unavailable",
                       detail=message, component="assistant", action="check_login")

    def assistant_context(self) -> str:
        from sentinel.engine.questions import HouseholdQuestion

        now = time.time()
        events = self.recording_store.events(now - 48 * 3600, now + 1)
        cameras = [(p.display_name, bool(p.connected)) for p in self.cameras.values()]
        home = self.cars.answer_question(HouseholdQuestion("list")) if self.presence is not None else ""
        weather = self.weather.current() if self.settings.get("weather_location") else None
        from sentinel.engine.reminders import when_words

        reminders = "\n".join(f"{r.id}. {when_words(r.due, now)}: {r.text}" for r in self.reminders.pending()[:20])
        return build_context(now, events, cameras, home, self.household.vehicles(), weather,
                             self.telegram.commands_help_text(), reminders, self.switches.summary(),
                             self._routines_context(), self.wifi_motion.context(now))

    def _switch_reply(self, chat_id: str, text: str) -> None:
        said = self.switches.command(text, source="telegram")
        self.telegram.reply_to_chat(chat_id, "💡 " + (said or "I don't know that switch."))

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
            from sentinel.engine.routines import COMMAND as ROUTINE_JSON

            if command and ROUTINE_JSON.match(command):
                ok, said = self.routine_preview(command)
                if not ok:
                    self.telegram.reply_to_chat(chat_id, f"{text_part}\n\n(I couldn't make that into a routine: {said}.)".strip())
                    return
                offer = next(self._offer_ids)
                self._command_offers[offer] = (chat_id, command, time.time())
                self.telegram.send_text_with_buttons(chat_id, f"New routine:\n{said}\n\nSave it?",
                                                     [[("✅ Save routine", f"cmd:{offer}"), ("No thanks", f"cmd:{offer}:no")]])
                return
            if command and self.telegram.is_command(command):
                offer = next(self._offer_ids)
                self._command_offers[offer] = (chat_id, command, time.time())
                self.telegram.send_text_with_buttons(chat_id, text_part or reply, [[(f"✅ {command}", f"cmd:{offer}"),
                                                                                    ("No thanks", f"cmd:{offer}:no")]])
            else:
                self.telegram.reply_to_chat(chat_id, text_part or reply)
            return
        fallback = ("Sorry, I didn't catch that. " + QUESTION_HELP if text.rstrip().endswith("?")
                    else self.telegram.commands_help_text())
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
        self.voice = VoiceAssistant(self.settings, self.answer_voice, self.audio, names=self._voice_names,
                                    switches=lambda: [s.name for s in self.switches.switches()],
                                    apps=self.switches.tv_apps, artists=self.spotify.artists,
                                    tv_on=self.switches.samsung_tv_on,
                                    pets=lambda: [p.name for p in self.locations.people() if p.tracker])
        self.voice.start()

    def _voice_names(self) -> List[str]:
        """Everyone the family might ask about, so Whisper spells them right: names, then nicknames, then car
        owners (only the first few fit: nicknames first left Xena out, 2026-11-07)."""
        people = self.locations.people()
        names: List[str] = [p.name for p in people]
        for p in people:
            names += p.aliases
        for v in self.household.vehicles():
            names += ([v.owner] if v.owner else []) + v.aliases
        return names

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

    def change_volume(self, what: str, value: str) -> str:
        """By voice: the PC's Master volume (kept in pc_volume, set again at start) or the voice's own."""
        from sentinel.audio.alert_system import pc_volume, set_pc_volume

        if what == "pc":
            now = pc_volume()
            if now is None:
                return "I can't reach this PC's volume."
            new = max(0, min(100, now + (10 if value == "+" else -10))) if value in "+-" else int(value)
            if not set_pc_volume(new):
                return "I couldn't change this PC's volume."
            self.settings_manager.set("pc_volume", new)
            return f"PC volume {new} percent."
        now = float(self.settings.get("volume", 0.5) or 0.5)
        new = now + (0.1 if value == "+" else -0.1) if value in "+-" else int(value) / 100
        new = round(max(0.1, min(1.0, new)), 2)  # never 0: that turns the voice off altogether
        self.settings_manager.set("volume", new)
        if self.audio is not None:
            self.audio.update_settings({"volume": new})
        return f"Okay, my voice is at {round(new * 100)} percent."

    def _voice_toggle(self, changes: Dict[str, bool]):
        from sentinel.audio.voice import VoiceReply

        what = " and ".join(f"turn {'on' if on else 'off'} {self.VOICE_FEATURES.get(key, key)}"
                            for key, on in changes.items())

        def run() -> str:
            applied = self.apply_feature_changes(changes, source="voice")
            return "Done. " + ". ".join(applied) + "." if applied else "Nothing needed changing."

        return VoiceReply(f"Should I {what}? Say yes or no.", run)

    def answer_voice(self, text: str, say: Callable[[str], None], doubtful: bool = False):
        """What to say back to a spoken request: Sentinel's own answers first, then Claude (opt-in).
        doubtful (a follow-up that may be room talk): None instead of asking Claude."""
        from sentinel.audio.voice import VoiceReply, to_speech
        from sentinel.engine.assistant import split_command
        from sentinel.engine.questions import _fmt_clock, is_question

        plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s'?:-]", " ", text)).strip()
        low = plain.lower().rstrip("?")
        app_logger.log("info", "voice_request", text=text[:200], component="voice", action="answer")
        note = self._NOTE.match(plain)
        if note:
            said = note.group(1).strip(" .")
            if said:
                return VoiceReply(self.save_claude_note(said))
            return VoiceReply("Go ahead.", dictate=self.save_claude_note)
        volume = parse_volume_command(low)
        if volume:
            return VoiceReply(self.change_volume(*volume))
        from sentinel.engine.routines import REQUEST as NEW_ROUTINE

        said = self.commands.voice_reply(low, "early")
        if said:
            return VoiceReply(said)
        switched = None if NEW_ROUTINE.search(low) else self.switches.command(low, source="voice")
        if switched:
            return VoiceReply(switched)
        music = parse_music_command(low)
        if music and self.spotify.connected():
            said = self.spotify.command(*music, say=say)
            if music[0] == "pause" and said == "Nothing is playing.":
                return None  # "stop"/"pause" with no music: meant "never mind" (or was room talk): just the end chime
            if said.startswith(NO_SUCH_SPEAKER):
                return VoiceReply(said + " Do you want to hear the list of speakers?", confirm=self.spotify.speaker_list)
            return VoiceReply(said)
        if re.fullmatch(r"(never ?mind|cancel|stop|nothing|no|forget it|no thanks)", low):
            return VoiceReply("Okay.")
        if music and music[0] == "play":
            return VoiceReply("Spotify isn't connected yet. Connect it on the Settings page, under Spotify.")
        if re.search(r"\bwhat time is it\b|\bwhat'?s the time\b|\bwhat is the time\b", low):
            return VoiceReply(f"It's {_fmt_clock(time.time())}.")
        if re.search(r"\b(any|a) (packages?|parcels?|deliver(y|ies))\b|\bpackages? (here|outside|at the door)\b", low):
            waiting = self.packages.packages() if self.packages is not None else {}
            return VoiceReply("Yes: " + ", ".join(f"{n} at the {cam}" for cam, n in waiting.items()) + "." if waiting
                              else "I don't see any packages waiting at the door.")
        if re.search(r"\b(weather|storm) (alerts?|warnings?|watch(es)?)\b|\bany (weather )?(alerts|warnings)\b", low):
            return VoiceReply(self.weather_alerts.summary())
        if re.search(r"\b(weather|temperature|how (hot|cold|warm)|rain\w*|snow\w*)\b", low) \
                and self.settings.get("weather_location"):
            w = self.weather.current()
            if w.get("temp") is not None:
                return VoiceReply(f"It's {w['temp']} degrees and {w.get('words', '').lower()} in {w.get('place')}. "
                                  f"High of {w.get('high')}, low of {w.get('low')}, "
                                  f"and a {w.get('rain_today')} percent chance of rain today.")
        changes, _err = parse_feature_toggle(low)
        if changes:
            return self._voice_toggle(changes)
        from sentinel.engine.routines import COMMAND as ROUTINE_JSON, REQUEST as ROUTINE_REQUEST, is_routine_command

        if re.fullmatch(r"(?:what are |list |read )?(?:my |the |any )?routines|do i have any routines", low):
            return VoiceReply(self.routine_command("routines", spoken=True))
        if is_routine_command(low):
            if low.startswith(("delete", "remove")):
                return VoiceReply(f"Should I {low}? Say yes or no.", lambda: self.routine_command(low, spoken=True))
            return VoiceReply(self.routine_command(low, spoken=True))
        if re.fullmatch(r"(?:what are |list |read )?(?:my |the |any )?reminders|do i have any reminders", low):
            return VoiceReply(self.reminder_command(None, "reminders"))
        said = None if ROUTINE_REQUEST.search(low) else self.commands.voice_reply(low, "late")
        if said is not None:
            return VoiceReply(said)
        if is_question(low) and not ROUTINE_REQUEST.search(low):
            return VoiceReply(to_speech(self.event_question_reply(low)[0]))
        if doubtful:
            return None
        if not self.assistant.enabled():
            return VoiceReply("Sorry, I can't answer that one. Try asking who's home, or did anyone come by today.")
        reply = None
        if self.assistant.remaining_today() > 0:
            say("Let me think.")
            try:
                reply = self.assistant.ask(text, self.assistant_context(), voice=True,
                                           name=getattr(self.voice, "speaking_as", ""))
            except Exception as e:
                self.assistant.last_error = str(e)
        else:
            self.assistant.last_error = "today's limit of Claude questions is used up"
        if not reply:
            return VoiceReply(f"Sorry, I couldn't get an answer: {self.assistant.last_error}.")
        text_part, command = split_command(reply)
        spoken = to_speech(text_part or reply, max_lines=6, max_chars=600)
        changes = parse_feature_toggle(command)[0] if command else None
        if changes:
            return VoiceReply(spoken + " Should I do that? Say yes or no.", self._voice_toggle(changes).confirm)
        from sentinel.engine.reminders import is_reminder_command

        if command and is_reminder_command(command):
            return VoiceReply(spoken + " Should I do that? Say yes or no.",
                              lambda: self.reminder_command(None, command))
        if command and ROUTINE_JSON.match(command):
            ok, said = self.routine_preview(command)
            if not ok:
                return VoiceReply(f"Sorry, I couldn't make that into a routine: {said}.")
            return VoiceReply(to_speech(said, max_lines=6, max_chars=600) + " Should I save it? Say yes or no.",
                              lambda: self.routine_command(command, spoken=True))
        if command and is_routine_command(command):
            return VoiceReply(spoken + " Should I do that? Say yes or no.",
                              lambda: self.routine_command(command, spoken=True))
        if command and self.switches.understands(command):
            return VoiceReply(spoken + " Should I do that? Say yes or no.",
                              lambda: self.switches.command(command, source="voice") or "I couldn't do that.")
        return VoiceReply(spoken)

    # ---- routines (sentinel/engine/routines.py)
    def house_live(self) -> Dict[str, Any]:
        """What the House page shows moving or switched on, in one small poll (no score graphs)."""
        wm = self.wifi_motion.view()
        return {
            "now": wm["now"],
            "paths": [{k: p[k] for k in ("id", "name", "online", "motion", "last_motion")} for p in wm["sensors"]],
            "boards": [{k: b[k] for k in ("id", "name", "online")} for b in wm["boards"]],
            "switches": self.switches.view()["switches"],
        }

    def routine_catalog(self):
        from sentinel.engine.routines import Catalog

        return Catalog(cameras={p.host: p.display_name for p in self.cameras.values()},
                       switches={s.id: s.name for s in self.switches.switches()},
                       people={p.id: p.name for p in self.locations.people()},
                       cars={v.id: v.name for v in self.household.vehicles()},
                       sensors=self.wifi_motion.paths())

    @staticmethod
    def _routine_error(e: ValueError) -> str:
        return "it came out garbled" if isinstance(e, json.JSONDecodeError) else str(e)

    def routine_preview(self, command: str) -> Tuple[bool, str]:
        """``Routine {json}`` -> (True, what it would do in words) or (False, what's wrong)."""
        from sentinel.engine.routines import COMMAND, describe, normalize

        m = COMMAND.match(command.strip())
        cat = self.routine_catalog()
        try:
            spec = normalize(json.loads(m.group(1)) if m else None, cat)
        except ValueError as e:  # (json.JSONDecodeError is one too)
            return False, self._routine_error(e)
        return True, describe(spec, cat)

    def routine_command(self, text: str, spoken: bool = False) -> str:
        """Telegram / voice: ``Routines``, ``Routine 2 off``, ``Delete routine 2``, ``Routine {json}`` (save)."""
        from sentinel.engine.routines import COMMAND, LIST, TOGGLE, describe, normalize

        t = text.strip()
        cat = self.routine_catalog()
        store = self.routines.store
        m = COMMAND.match(t)
        if m:
            try:
                spec = normalize(json.loads(m.group(1)), cat)
            except ValueError as e:
                return f"I couldn't save that routine: {self._routine_error(e)}."
            rid = store.save(spec)
            self.routines.reload()
            app_logger.log("info", "routine_saved", routine=spec["name"], routine_id=rid, component="engine",
                           action="routine")
            return (f"Saved. {describe(spec, cat)}" if spoken
                    else f"✅ Saved routine {rid} ({spec['name']}):\n{describe(spec, cat)}\n\nSee them all on the Routines page.")
        if LIST.match(t):
            items = store.all()
            if not items:
                return "You don't have any routines yet." if spoken else (
                    "No routines yet. Make one on the Routines page, or say it in plain words, e.g. "
                    "“new routine: when the back yard camera sees a person after dark, turn on the patio lights "
                    "until there's no motion for 2 minutes”.")
            if spoken:
                return f"You have {len(items)} routine{'s' if len(items) != 1 else ''}. " + " ".join(
                    f"Number {r.id}, {r.name}{'' if r.enabled else ', turned off'}." for r in items)
            return "Routines:\n" + "\n".join(f"{r.id}. {'' if r.enabled else '⏸ (off) '}{r.name}: {describe(r.spec, cat)}"
                                             for r in items) + "\n\nRoutine 2 off / Routine 2 on / Delete routine 2"
        m = TOGGLE.match(t)
        if m and (m.group(1) or m.group(3) or m.group(5)):
            rid = int(m.group(2) or m.group(4))
            r = store.get(rid)
            if r is None:
                return f"There's no routine {rid}."
            if m.group(1):
                store.delete(rid)
                self.routines.reload()
                app_logger.log("info", "routine_deleted", routine=r.name, routine_id=rid, component="engine",
                               action="routine")
                return f"Deleted routine {rid}, {r.name}."
            on = (m.group(3) or m.group(5)).lower() == "on"
            store.set_enabled(rid, on)
            self.routines.reload()
            return f"Routine {rid}, {r.name}, is {'on' if on else 'off'}."
        return "Send Routines to see them, Routine 2 off to pause one, or Delete routine 2."

    def _routines_context(self) -> str:
        from sentinel.engine.routines import describe

        cat = self.routine_catalog()
        lines = [f"{r.id}. {r.name}{'' if r.enabled else ' (off)'}: {describe(r.spec, cat)}" for r in self.routines.store.all()]
        names = (f"Camera names: {', '.join(cat.cameras.values()) or 'none'}. Switch names: "
                 f"{', '.join(cat.switches.values()) or 'none'}. People with phones: "
                 f"{', '.join(cat.people.values()) or 'none'}. Family cars: {', '.join(cat.cars.values()) or 'none'}. "
                 f"Wi-Fi motion sensors: {', '.join(cat.sensors.values()) or 'none'}.")
        return names + "\n" + ("\n".join(lines) if lines else "(no routines yet)")

    def routines_view(self) -> Dict[str, Any]:
        from sentinel.engine.routines import describe, sun_times

        cat = self.routine_catalog()
        holding = self.routines.holding()
        coords = self.family.home_coords()
        sun = sun_times(coords[0], coords[1], time.time()) if coords else None
        return {
            "routines": [{"id": r.id, "name": r.name, "enabled": r.enabled, "spec": r.spec, "text": describe(r.spec, cat),
                          "last_run": r.last_run, "holding_until": holding.get(r.id)} for r in self.routines.store.all()],
            "catalog": {k: [{"id": i, "name": n} for i, n in getattr(cat, k).items()]
                        for k in ("cameras", "switches", "people", "cars", "sensors")},
            "dark": self.routines.is_dark(), "located": coords is not None,
            "sunrise": sun[0] if sun else None, "sunset": sun[1] if sun else None,
        }

    def routine_check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """The editor, as you change it: the routine in words (or what's still missing) and, for a Wi-Fi
        sensor routine, when it would have gone off over the last day."""
        from sentinel.engine.routines import REPLAY_HOURS, describe, normalize

        cat = self.routine_catalog()
        try:
            spec = normalize(body.get("spec"), cat)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        out: Dict[str, Any] = {"ok": True, "text": describe(spec, cat)}
        if spec["when"]["type"] == "sensor":
            now = time.time()
            start = now - REPLAY_HOURS * 3600
            times = self.routines.replay(spec, self.wifi_motion.moments(start - 60, now), self.wifi_motion.ratio_of,
                                         start, now)
            out["replay"] = {"hours": REPLAY_HOURS, "start": start, "end": now, "times": times,
                             "recorded_from": self.wifi_motion.first_moment()}
        return out

    def routine_save(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        from sentinel.engine.routines import normalize

        rid = body.get("id")
        if rid is not None and (not isinstance(rid, int) or self.routines.store.get(rid) is None):
            return False, "unknown routine"
        try:
            spec = normalize(body.get("spec"), self.routine_catalog())
        except ValueError as e:
            return False, str(e)
        rid = self.routines.store.save(spec, rid)
        self.routines.reload()
        app_logger.log("info", "routine_saved", routine=spec["name"], routine_id=rid, component="engine", action="web")
        return True, str(rid)

    def routine_action(self, rid: int, action: str, on: Any = None) -> Tuple[bool, str]:
        r = self.routines.store.get(rid)
        if r is None:
            return False, "unknown routine"
        if action == "enable":
            if not isinstance(on, bool):
                return False, "say on or off"
            self.routines.store.set_enabled(rid, on)
        elif action == "delete":
            self.routines.store.delete(rid)
            app_logger.log("info", "routine_deleted", routine=r.name, routine_id=rid, component="engine", action="web")
        elif action == "try":  # do its steps now, whatever the time (to check it does the right thing)
            threading.Thread(target=self.routines.run, args=(r,), name="routine", daemon=True).start()
        else:
            return False, "unknown action"
        self.routines.reload()
        return True, action

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
            "sound_cards": __import__("sentinel.audio.devices", fromlist=["sound_cards"]).sound_cards(),
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
        if {"audio_device", "music_device"} & cleaned.keys():  # another speaker: voice, chimes and Spotify move to it
            from sentinel.audio import devices

            devices.use_cards(self.settings.get("audio_device"), self.settings.get("music_device", "auto"))
            if self.audio is not None:
                self.audio.update_settings(self.settings)
            self.spotify_player.restart()
        if "spotify_speaker_name" in cleaned:
            self.spotify_player.settings_changed()
        if {"voice_enabled", "volume", "voice_device", "audio_device", "music_device"} & cleaned.keys() and self._started:
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
