"""
Sentinel engine: wires cameras, recordings, Telegram, audio and the web viewer.

Runs headless (no GUI). The web viewer is the user interface; Telegram is the
remote control. Replaces the old Qt MainWindow, which ran invisibly under Xvfb.
"""

import copy
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import cv2
import numpy as np

from sentinel.constants import CAPTURE_DIR, VIDEO_DIR
from sentinel.engine import admin
from sentinel.engine.camera import CLIP_REQUEST_REASON, CameraPipeline, camera_host
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
    return [re.sub(r"\s+", " ", str(n)).strip()[:30] for n in names][:16]


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
        self.telegram = TelegramNotifier(self.settings)
        self.audio = self._init_audio()
        self.frame_broker = None
        self.web_server = None
        self.cameras: Dict[str, CameraPipeline] = {}
        self._command_thread: Optional[threading.Thread] = None
        self.started_at = time.time()
        self._cpu = admin.CpuMeter()
        self.detection_worker = None
        self.detector_error: Optional[str] = None
        self._object_alert_times: Dict[Tuple[str, str], float] = {}
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
        detector = ObjectDetector(model)
        if not detector.available:
            self.detector_error = detector.error
            app_logger.log("error", "object_detector_unavailable", error_message=detector.error,
                           component="engine", action="init_objects")
            return
        self.detection_worker = DetectionWorker(detector)
        self.detection_worker.start()
        app_logger.log("info", "object_detector_ready", model=os.path.basename(model),
                       load_ms=int((time.time() - started) * 1000), component="engine", action="init_objects")

    def objects_active(self) -> bool:
        return self.detection_worker is not None and self.settings.get("alert_mode", "objects") == "objects"

    def _build_cameras(self) -> None:
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
        if not self.cameras:
            print("No cameras configured (settings.json camera_profiles is empty).")
        for pipeline in self.cameras.values():
            if pipeline.profile.get("enabled", True):
                pipeline.start()
        self._command_thread = threading.Thread(
            target=self._command_loop, name="telegram-commands", daemon=True
        )
        self._command_thread.start()
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
        group = event.track.group
        self._last_description[pipeline.host] = (event.timestamp, event.description)
        app_logger.log("info", "object_detected", camera_display=pipeline.display_name,
                       label=event.track.label, zone=event.zone_name, score=round(event.track.best_score, 2),
                       description=event.description, component="engine", action="on_object")
        if not self.objects_active() or group not in self.settings.get("object_alert_groups", []):
            return
        key = (pipeline.host, group)
        cooldown = float(self.settings.get("object_alert_cooldown", 60))
        if event.timestamp - self._object_alert_times.get(key, 0.0) < cooldown:
            return
        self._object_alert_times[key] = event.timestamp
        if self.audio is not None:
            self.audio.trigger_alert(event.description.split(" ", 1)[1])  # drop the emoji
        if self.settings.get("telegram_motion_alerts", True):
            ok, jpeg = cv2.imencode(".jpg", event.snapshot, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                self.telegram.trigger_photo_alert(event.description, jpeg.tobytes())

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
            groups = set(self.settings.get("object_alert_groups", []))
            from sentinel.detection.objects import CLASS_GROUPS

            if not any(CLASS_GROUPS.get(label) in groups for label in pipeline.last_clip_labels):
                return  # nothing worth a clip (no recognised object)
            _, text = self._last_description.get(pipeline.host, (0.0, f"Activity at the {pipeline.display_name}."))
            self.telegram.trigger_alert(message=f"{text} (clip)", video_path=path, bypass_cooldown=True)
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
                clip_chats = self.telegram.consume_clip_requests()
                if clip_chats:
                    self._request_clips(clip_chats)
            except Exception as e:
                app_logger.log(
                    "error", "telegram_command_failed", error_message=str(e),
                    component="engine", action="command_loop",
                )

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
        app_logger.log("info", "remote_feature_toggle", changes=changes, component="engine", action="telegram")
        self.telegram.reply_to_chat(
            chat_id, "Applied:\n" + "\n".join(applied) if applied else "No changes applied."
        )

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

    def add_camera(self, spec: Dict[str, Any]) -> Tuple[bool, str]:
        """Add an RTSP camera from {name, host, port, path, username, password}."""
        host = str(spec.get("host", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
            return False, "enter the camera's IP address or hostname"
        if host in self.cameras:
            return False, "that camera is already added"
        try:
            port = int(spec.get("port") or 554)
        except (TypeError, ValueError):
            return False, "port must be a number"
        if not 1 <= port <= 65535:
            return False, "port must be 1-65535"
        path = str(spec.get("path") or "/").strip()
        if not path.startswith("/") or re.search(r"\s", path):
            return False, "stream path must start with /"
        name = _display_name(spec.get("name") or "") or f"Camera {host}"
        user = str(spec.get("username") or "")
        password = str(spec.get("password") or "")
        auth = ""
        if user:
            auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
        source = f"rtsp://{auth}{host}:{port}{path}"
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
        if {"volume", "speech_speed", "voice_id"} & cleaned.keys():
            if self.audio is None:
                self.audio = self._init_audio()
            else:
                self.audio.update_settings(self.settings)

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
                "inference_ms": round(self.detection_worker.inference_ms) if self.detection_worker else None,
                "error": self.detector_error,
                "recent": admin.recent_objects(),
            },
        }
