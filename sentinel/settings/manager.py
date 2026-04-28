"""
Settings management for Sentinel Home.
"""

import os
import json
import time
import copy
from typing import Dict, Any

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

        return self.settings

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

    def save_settings(self, settings: Dict[str, Any] = None) -> bool:
        """
        Save settings to the settings file with debouncing.
        
        Args:
            settings: The settings dictionary to save
            
        Returns:
            True if settings were saved, False if debounced
        """
        current_time = time.time()
        if (current_time - self.last_save_time) < SAVE_DEBOUNCE_INTERVAL:
            return False  # Skip saving if called too soon

        if settings:
            self.settings.update(settings)

        try:
            print("Saving settings...")
            with open(self.settings_file, "w") as f:
                json.dump(self.settings, f)
            print("Settings saved.")
            self.last_save_time = current_time
            return True
        except (PermissionError, OSError) as e:
            print(f"Error saving settings: {e}")
            return False

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
        self.settings[key] = value
        if save:
            self.save_settings()
