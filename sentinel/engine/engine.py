"""
Sentinel engine: wires cameras, recordings, Telegram, audio and the web viewer.

Runs headless (no GUI). The web viewer is the user interface; Telegram is the
remote control. Replaces the old Qt MainWindow, which ran invisibly under Xvfb.
"""

import copy
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.constants import CAPTURE_DIR, VIDEO_DIR
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


EDITABLE_PROFILE_FIELDS = {
    "active_zones": _clean_zones,
    "entry_lines": _clean_lines,
    "entry_direction": lambda v: v if v in _ENTRY_DIRECTIONS else None,
    "min_contour_area": lambda v: max(100, min(50000, int(v))),
    "motion_cooldown": lambda v: max(0.0, min(300.0, float(v))),
    "recording_enabled": bool,
    "night_bug_filter_enabled": bool,
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

    def _build_cameras(self) -> None:
        for camera_id, profile in self._camera_profiles().items():
            if profile.get("enabled", True) is False:
                continue
            source = profile.get("source", camera_id)
            if isinstance(source, str) and source.isdigit():
                source = int(source)
            pipeline = CameraPipeline(
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
            )
            self.cameras[pipeline.host] = pipeline

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.recording_store.start_maintenance(lambda: self.settings)
        self._init_web()
        self._build_cameras()
        if not self.cameras:
            print("No cameras configured (settings.json camera_profiles is empty).")
        for pipeline in self.cameras.values():
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

    def _on_motion(self, pipeline: CameraPipeline) -> None:
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
        if self.settings.get("telegram_motion_alerts", True) and self.telegram.include_video:
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
                for chat_id, changes in self.telegram.consume_remote_feature_commands():
                    self.apply_feature_toggles(chat_id, changes)
                clip_chats = self.telegram.consume_clip_requests()
                if clip_chats:
                    self._request_clips(clip_chats)
            except Exception as e:
                app_logger.log(
                    "error", "telegram_command_failed", error_message=str(e),
                    component="engine", action="command_loop",
                )

    def _request_clips(self, chat_ids: List[str]) -> None:
        live = [p for p in self.cameras.values() if p.connected]
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
            out.append(
                {
                    "key": key,
                    "name": pipeline.display_name,
                    "online": pipeline.connected,
                    **{field: copy.deepcopy(p.get(field)) for field in EDITABLE_PROFILE_FIELDS},
                }
            )
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
            self.settings_manager.save_settings(force=True)
        app_logger.log(
            "info", "camera_config_updated", camera_ip=key, fields=sorted(cleaned),
            component="engine", action="update_camera_config",
        )
        return True, "saved"

    def camera_snapshot(self, key: str) -> Optional[np.ndarray]:
        pipeline = self.cameras.get(key)
        return pipeline.snapshot() if pipeline else None
