"""
Main application module for Sentinel Home.
"""

import os
from sentinel.constants import CAPTURE_DIR
from sentinel.settings import SettingsManager
from sentinel.detection import MotionDetector
from sentinel.audio import AudioAlertSystem
from sentinel.notifications import TelegramNotifier
from sentinel.ui import MainWindow


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
                    web_server = WebServer(self.settings_manager, frame_broker)
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
        )
    
    def show(self):
        """Show the main window."""
        self.main_window.show()
