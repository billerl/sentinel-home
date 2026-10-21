"""
Main application module for Sentinel Home.
"""

import os

import cv2

from sentinel.constants import CAPTURE_DIR, VIDEO_DIR
from sentinel.settings import SettingsManager
from sentinel.detection import MotionDetector
from sentinel.audio import AudioAlertSystem
from sentinel.notifications import TelegramNotifier
from sentinel.recordings import RecordingStore
from sentinel.ui import MainWindow


# OpenCV's default thread pool busy-spins between the small per-frame jobs we
# give it (blur, MOG2, dilate) and was the single largest CPU cost. Each camera
# already has its own worker thread, so run OpenCV single-threaded.
cv2.setNumThreads(1)


class MotionDetectionApp:
    """
    Main application class that initializes and connects all components.
    """
    
    def __init__(self):
        """Initialize the application."""
        # Ensure captured images directory exists
        if not os.path.exists(CAPTURE_DIR):
            os.makedirs(CAPTURE_DIR)
        
        # Initialize components
        self.settings_manager = SettingsManager()
        self.motion_detector = MotionDetector()
        self.audio_system = AudioAlertSystem(self.settings_manager.settings)
        self.telegram_notifier = TelegramNotifier(self.settings_manager.settings)

        # Recording index + hourly retention cleanup (runs in a background thread)
        self.recording_store = RecordingStore(VIDEO_DIR)
        self.recording_store.start_maintenance(lambda: self.settings_manager.settings)

        frame_broker = None
        web_cfg = self.settings_manager.settings.get("web")
        if isinstance(web_cfg, dict) and web_cfg.get("enabled"):
            auth_cfg = web_cfg.get("auth") if isinstance(web_cfg.get("auth"), dict) else {}
            if (auth_cfg.get("password_hash") or "").strip():
                try:
                    from sentinel.web.frame_broker import FrameBroker
                    from sentinel.web.server import WebServer
                except ImportError as e:
                    print(
                        "Web viewer: need Flask and bcrypt. "
                        "Run: pip install -r requirements.txt"
                    )
                    print(f"  ({e})")
                else:
                    frame_broker = FrameBroker()
                    web_server = WebServer(
                        self.settings_manager, frame_broker, self.recording_store
                    )
                    web_server.start()
            else:
                print(
                    "Web viewer: web.enabled is true but no password_hash. "
                    "Run: python -m sentinel.web.set_password"
                )

        self.main_window = MainWindow(
            self.settings_manager,
            self.motion_detector,
            self.audio_system,
            self.telegram_notifier,
            frame_broker=frame_broker,
            recording_store=self.recording_store,
        )
    
    def show(self):
        """Show the main window."""
        self.main_window.show()
