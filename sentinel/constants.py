"""
Configuration constants for the Sentinel Home application.
"""

import os

# File and directory paths
SETTINGS_FILE = "settings.json"
CAPTURE_DIR = "captured_images"

# Camera and motion detection settings
HISTORY_LENGTH = 500  # History length for background subtractor
VAR_THRESHOLD = 50    # Variance threshold for background subtractor
DETECT_SHADOWS = True # Whether to detect shadows
MIN_CONTOUR_AREA = 700  # Minimum contour area to consider as motion
# Live feed: lower FPS = fewer frames decoded/copied per second (less RAM + CPU pressure).
DEFAULT_FEED_FPS = 15
FEED_FPS_MIN = 5
FEED_FPS_MAX = 30
FPS = DEFAULT_FEED_FPS  # legacy alias
FRAME_INTERVAL = max(1, int(1000 / DEFAULT_FEED_FPS))  # ms between UI timer ticks

# Motion alert settings
MOTION_TIMEOUT = 10.0  # Seconds between motion alerts
TELEGRAM_COOLDOWN = 10.0  # Seconds between Telegram alerts

# Settings management
SAVE_DEBOUNCE_INTERVAL = 1.0  # Seconds between settings save operations

# UI settings
CONTROL_PANEL_WIDTH = 300  # Width of the control panel in pixels
DEFAULT_WINDOW_SIZE = (800, 600)  # Default window size (width, height)
DEFAULT_FEED_SIZE = (640, 480)    # Default camera feed size (width, height)
DEFAULT_DISPLAY_OVERLAY_MODE = "overlay"  # overlay | raw

# Audio settings
DEFAULT_SPEECH_SPEED = 150  # Default speech rate
DEFAULT_VOLUME = 1.0       # Default volume (0.0 to 1.0)

# Video recording settings
VIDEO_DIR = "recorded_videos"  # Directory to store video recordings
RECORDING_DURATION = 10.0  # Duration of recordings in seconds
RECORDING_FPS = 15  # Frames per second for recordings (lower = smaller files, less load)
VIDEO_CODEC = "XVID"  # Codec to use for recordings (XVID is widely supported)
MOTION_COOLDOWN = 30.0  # Seconds to wait before starting a new recording after motion

# Zone/line detection defaults
DEFAULT_ACTIVE_ZONES = []  # Normalized polygons [[x,y], ...]
DEFAULT_ENTRY_LINES = []  # Normalized lines [[x1,y1], [x2,y2]]
DEFAULT_ENTRY_DIRECTION = "outside_to_inside"

# Camera reconnect defaults
AUTO_RECONNECT_DEFAULT = True
RECONNECT_INTERVAL_SECONDS = 5
MAX_CONSECUTIVE_READ_FAILURES = 3

# Application stylesheet - Dark theme with cyan accents
STYLESHEET = """
QMainWindow, QWidget {
    background-color: #2E2E2E;
    color: #00FFFF;
}
QGroupBox {
    border: 1px solid #00FFFF;
    border-radius: 5px;
    margin-top: 10px;
    color: #00FFFF;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 3px;
    color: #00FFFF;
}
QPushButton, QRadioButton {
    background-color: #00FFFF;
    color: #2E2E2E;
    border: none;
    padding: 5px;
    border-radius: 3px;
}
QPushButton:hover, QRadioButton:hover {
    background-color: #00CCCC;
}
QComboBox, QSlider, QLabel {
    color: #00FFFF;
}
QComboBox {
    background-color: #3E3E3E;
    border: 1px solid #00FFFF;
    padding: 2px;
}
QComboBox::drop-down {
    border: none;
}
QSlider::groove:horizontal {
    background: #3E3E3E;
    height: 8px;
    border-radius: 4px;
}
QSlider::handle:horizontal {
    background: #00FFFF;
    width: 16px;
    height: 16px;
    border-radius: 8px;
    margin: -4px 0;
}
"""
