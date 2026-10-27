"""
Settings management for Sentinel Home.
"""

import os
import json
import tempfile
import threading
import time
import copy
from typing import Any, Dict

from sentinel.constants import (
    SETTINGS_FILE,
    DEFAULT_SPEECH_SPEED,
    DEFAULT_VOLUME,
    DEFAULT_WINDOW_SIZE,
    DEFAULT_FEED_SIZE,
    DEFAULT_FEED_FPS,
    DEFAULT_DISPLAY_OVERLAY_MODE,
    SAVE_DEBOUNCE_INTERVAL,
    MOTION_COOLDOWN,
    MIN_CONTOUR_AREA,
    VAR_THRESHOLD,
    DEFAULT_ACTIVE_ZONES,
    DEFAULT_ENTRY_LINES,
    DEFAULT_ENTRY_DIRECTION,
    AUTO_RECONNECT_DEFAULT,
    RECONNECT_INTERVAL_SECONDS,
    TELEGRAM_COOLDOWN,
)

# Web viewer (MJPEG); merged into settings on load — see installation-guide.md
WEB_VIEWER_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "bind_host": "127.0.0.1",
    "port": 8088,
    "camera_subnet_block": "192.168.0.0/24",
    "jpeg_quality": 80,
    "max_fps": 10,
    "published_frame": "overlay",
    "show_zones": True,  # draw zones/entry lines on the live feed (display only)
    "max_concurrent_streams": 8,
    "max_concurrent_streams_per_session": 8,
    "auth": {
        "username": "",
        "password_hash": "",
        "session_secret": "",
        "session_idle_minutes": 60,
        "session_max_days": 7,
        "max_attempts": 5,
        "attempt_window_minutes": 5,
        "lockout_minutes": 15,
        "lockout_backoff_factor": 2,
        "lockout_max_minutes": 240,
        "bind_session_to_ip": True,
    },
}


class SettingsManager:
    """Handles loading and saving application settings."""

    def __init__(self, settings_file: str = SETTINGS_FILE):
        """
        Initialize the settings manager.
        
        Args:
            settings_file: Path to the settings JSON file
        """
        self.settings_file = settings_file
        self.last_save_time = 0
        self._save_lock = threading.RLock()
        self._deferred_save: threading.Timer = None
        self.default_settings = {
            "speech_speed": DEFAULT_SPEECH_SPEED,
            "volume": DEFAULT_VOLUME,
            "voice_id": "",  # Will be set after TTS initialization
            "cam_orientation": "horizontal",
            "camera_index": 0,
            "camera_source": "0",
            "camera_display": "",
            "rtsp_username": "",
            "rtsp_password": "",
            "camera_logins": {},
            "camera_profiles": {},
            "prefer_substream": True,
            "feed_active": False,
            "window_size": DEFAULT_WINDOW_SIZE,
            "feed_size": DEFAULT_FEED_SIZE,
            "feed_fps": DEFAULT_FEED_FPS,
            "display_overlay_mode": DEFAULT_DISPLAY_OVERLAY_MODE,
            # Add new recording settings
            "recording_enabled": True,  # Enable recording by default
            "motion_cooldown": MOTION_COOLDOWN,  # Use value from constants
            # Retention: delete clips older than N days; if free disk space drops
            # below the percentage, delete oldest clips (keeping the last 24h)
            "recording_retention_enabled": True,
            "recording_retention_days": 30,
            "recording_min_free_percent": 10,
            # Object detection (YOLOX on OpenVINO). alert_mode "objects" alerts only
            # when a person/vehicle/animal is recognised; "motion" alerts on any motion.
            "object_detection_enabled": True,
            "detector_model": "yolox_tiny",
            "alert_mode": "objects",
            "object_alert_groups": ["person", "vehicle", "delivery", "animal"],
            # Recognise delivery vans (UPS, FedEx, Amazon, USPS, DHL) and vehicle types with CLIP.
            "vehicle_types_enabled": True,
            # Where object detection runs: "auto" (Intel GPU if usable, else CPU), "GPU" or "CPU".
            "inference_device": "auto",
            # Ask in Telegram "whose car is this?" (with buttons) when a parked car isn't recognised.
            "household_ask_enabled": True,
            "household_ask_per_day": 3,
            "household_ask_start_hour": 8,
            "household_ask_end_hour": 21,
            "object_alert_cooldown": 60,
            # Weather tile in the live grid's spare cell: town or ZIP ("" = off).
            "weather_location": "",
            "weather_units": "fahrenheit",
            # Messages Sentinel doesn't understand go to Claude (the PC's signed-in subscription).
            "assistant_enabled": False,
            "assistant_daily_limit": 30,
            "assistant_model": "sonnet",
            # "Hey Jarvis" voice assistant (sentinel/audio/voice.py) and where sounds play
            "voice_enabled": False,
            # Spotify (sentinel/engine/spotify.py): public Client ID; the refresh token is stored here too
            "spotify_client_id": "",
            "spotify_device": "",  # preferred speaker when none is playing
            "spotify_refresh_token": "",
            "voice_follow_up": True,  # after an answer, listen ~5 s for another question without the wake word
            "voice_wake_threshold": 0.5,
            "voice_gain": 4.0,
            "audio_output": "speakers",  # or "remote": the Remote Desktop session, when one is open
            # Shown in the Help page and the Telegram "access" reply
            "remote_web_url": "",
            "remote_rdp_user": "",
            "min_contour_area": MIN_CONTOUR_AREA,
            "var_threshold": VAR_THRESHOLD,
            "active_zones": copy.deepcopy(DEFAULT_ACTIVE_ZONES),
            "entry_lines": copy.deepcopy(DEFAULT_ENTRY_LINES),
            "entry_direction": DEFAULT_ENTRY_DIRECTION,
            "auto_reconnect_enabled": AUTO_RECONNECT_DEFAULT,
            "reconnect_interval_seconds": RECONNECT_INTERVAL_SECONDS,
            "telegram_enabled": False,
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "telegram_chat_ids": "",
            "telegram_cooldown": TELEGRAM_COOLDOWN,
            "telegram_include_video": False,
            "telegram_motion_alerts": True,
            "night_bug_filter_enabled": True,
            "night_bug_start_hour": 20,
            "night_bug_end_hour": 6,
            "ui_expand_camera": True,
            "ui_expand_speech": True,
            "ui_expand_telegram": True,
            "ui_expand_orientation": True,
            "ui_expand_recording": True,
            "ui_expand_sensitivity": True,
        }
        self.settings = self.default_settings.copy()
        self.load_settings()

    def load_settings(self) -> Dict[str, Any]:
        """
        Load settings from the settings file.
        
        Returns:
            The loaded settings dictionary
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, "r") as f:
                    loaded_settings = json.load(f)
                    self.settings.update(loaded_settings)
                    self._migrate_camera_profiles()
                    print("Settings loaded successfully")
        except (json.JSONDecodeError, PermissionError, OSError) as e:
            print(f"Error loading settings: {e}")
            print("Using default settings")

        self.ensure_web_defaults()
        return self.settings

    def ensure_web_defaults(self) -> None:
        """Merge web viewer defaults; safe to call after manual edits."""
        base = copy.deepcopy(WEB_VIEWER_DEFAULTS)
        existing = self.settings.get("web")
        if not isinstance(existing, dict):
            self.settings["web"] = base
            return
        merged = base
        for k, v in existing.items():
            if k == "auth" and isinstance(v, dict):
                merged["auth"] = {**merged["auth"], **v}
            elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        self.settings["web"] = merged

    def _migrate_camera_profiles(self) -> None:
        """Backfill per-camera profiles from legacy single-camera settings."""
        profiles = self.settings.get("camera_profiles", {})
        if isinstance(profiles, dict) and profiles:
            return

        source = self.settings.get("camera_source", "")
        if source in (None, ""):
            self.settings["camera_profiles"] = {}
            return

        profile_id = str(source)
        self.settings["camera_profiles"] = {
            profile_id: {
                "source": str(source),
                "display": self.settings.get("camera_display", f"Camera {source}"),
                "active_zones": copy.deepcopy(self.settings.get("active_zones", [])),
                "entry_lines": copy.deepcopy(self.settings.get("entry_lines", [])),
                "entry_direction": self.settings.get("entry_direction", DEFAULT_ENTRY_DIRECTION),
                "min_contour_area": self.settings.get("min_contour_area", MIN_CONTOUR_AREA),
                "var_threshold": self.settings.get("var_threshold", VAR_THRESHOLD),
                "motion_cooldown": self.settings.get("motion_cooldown", MOTION_COOLDOWN),
                "recording_enabled": self.settings.get("recording_enabled", True),
                "night_bug_filter_enabled": self.settings.get("night_bug_filter_enabled", True),
                "night_bug_start_hour": self.settings.get("night_bug_start_hour", 20),
                "night_bug_end_hour": self.settings.get("night_bug_end_hour", 6),
            }
        }

    def save_settings(self, settings: Dict[str, Any] = None, force: bool = False) -> bool:
        """
        Save settings to the settings file with debouncing.
        
        Args:
            settings: The settings dictionary to save
            force: If True, skip debounce (e.g. set_password CLI)
            
        Returns:
            True if settings were saved, False if debounced
        """
        with self._save_lock:
            current_time = time.time()
            if settings:
                self.settings.update(settings)
            if not force and (current_time - self.last_save_time) < SAVE_DEBOUNCE_INTERVAL:
                # Too soon: save once the debounce window ends instead of dropping it.
                if self._deferred_save is None:
                    delay = SAVE_DEBOUNCE_INTERVAL - (current_time - self.last_save_time)
                    self._deferred_save = threading.Timer(delay, self._run_deferred_save)
                    self._deferred_save.daemon = True
                    self._deferred_save.start()
                return False

            temp_file = None
            try:
                print("Saving settings...")
                # Secrets live in this file (camera passwords, bot token): owner-only.
                directory = os.path.dirname(os.path.abspath(self.settings_file))
                fd, temp_file = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
                with os.fdopen(fd, "w") as f:
                    json.dump(copy.deepcopy(self.settings), f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_file, self.settings_file)
                print("Settings saved.")
                self.last_save_time = current_time
                return True
            except (OSError, TypeError, ValueError, RuntimeError) as e:
                print(f"Error saving settings: {e}")
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
                return False

    def _run_deferred_save(self) -> None:
        with self._save_lock:
            self._deferred_save = None
        self.save_settings(force=True)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a setting value.
        
        Args:
            key: Setting key
            default: Default value if key not found
            
        Returns:
            The setting value or default
        """
        return self.settings.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        """
        Set a setting value.
        
        Args:
            key: Setting key
            value: Setting value
            save: Whether to save settings after update
        """
        with self._save_lock:
            self.settings[key] = value
        if save:
            self.save_settings()
