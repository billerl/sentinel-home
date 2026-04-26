"""
Main application module for Sentinel Home.
"""

import os
from sentinel.constants import CAPTURE_DIR
from sentinel.settings import SettingsManager
from sentinel.detection import MotionDetector
from sentinel.audio import AudioAlertSystem
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
        
        # Create main window
        self.main_window = MainWindow(
            self.settings_manager,
            self.motion_detector,
            self.audio_system
        )
    
    def show(self):
        """Show the main window."""
        self.main_window.show()
