"""
Main window for the Sentinel Home application.
"""

import sys
import os
import cv2
import numpy as np
import subprocess  # Add this for opening videos with system player
import time
import threading
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from sentinel.recordings import RecordingStore
    from sentinel.web.frame_broker import FrameBroker

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QComboBox,
    QPushButton,
    QSlider,
    QLabel,
    QGroupBox,
    QRadioButton,
    QFileDialog,
    QCheckBox,
    QScrollArea,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QInputDialog,
    QSizePolicy,
    QStackedWidget,
)
from PySide6.QtGui import (
    QImage,
    QPixmap,
    QIcon,
    QCloseEvent,
    QResizeEvent,
    QMouseEvent,
    QKeyEvent,
)
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QThread, QObject

from sentinel.constants import (
    CONTROL_PANEL_WIDTH,
    DEFAULT_FEED_FPS,
    FEED_FPS_MIN,
    FEED_FPS_MAX,
    CAPTURE_DIR,
    VIDEO_DIR,
    STYLESHEET,
    MOTION_COOLDOWN,
    VAR_THRESHOLD,
    MIN_CONTOUR_AREA,
    HISTORY_LENGTH,
    DETECT_SHADOWS,
    AUTO_RECONNECT_DEFAULT,
    RECONNECT_INTERVAL_SECONDS,
    DEFAULT_DISPLAY_OVERLAY_MODE,
)
from sentinel.detection import MotionDetector, NetworkCameraDiscovery, CameraEndpoint
from sentinel.audio import AudioAlertSystem
from sentinel.notifications import TelegramNotifier
from sentinel.settings import SettingsManager
from sentinel.logging_utils import app_logger


class CameraWorker(QObject):
    """Background worker that continuously pulls frames and runs detection."""
    
    finished = Signal()
    
    def __init__(self, detector: MotionDetector, source: object, fps: int):
        super().__init__()
        self.detector = detector
        self.source = source
        self.fps = fps
        self._running = False
        self._lock = threading.Lock()
        
        # Shared state protected by lock
        # Results: (annotated_frame, raw_frame, motion_rects, motion_detected, recording_status, error_message, is_active)
        self._latest_results: Tuple[Optional[np.ndarray], Optional[np.ndarray], List[Tuple[int, int, int, int]], bool, Optional[str], Optional[str], bool] = (
            None, None, [], False, None, None, True
        )

    def start_loop(self):
        """Main loop started when thread begins."""
        self._running = True
        interval = 1.0 / self.fps
        
        while self._running:
            loop_start = time.time()
            
            # Perform blocking network I/O and CPU-intensive detection
            # detector.detect_motion() now returns 5 values
            ann, raw, rects, mot, rec = self.detector.detect_motion()
            
            with self._lock:
                self._latest_results = (
                    ann, raw, rects, mot, rec, 
                    self.detector.last_error_message, 
                    self.detector.feed_active
                )
            
            # If the detector stopped itself (too many failures), we should exit the worker too
            if not self.detector.feed_active:
                break

            # Pace the loop to the target FPS
            elapsed = time.time() - loop_start
            wait_time = max(0, interval - elapsed)
            if wait_time > 0:
                time.sleep(wait_time)
        
        self._running = False
        self.finished.emit()

    def stop(self):
        """Signal the loop to stop."""
        self._running = False

    def get_latest_results(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], List[Tuple[int, int, int, int]], bool, Optional[str], Optional[str], bool]:
        """Thread-safe retrieval of latest camera data."""
        with self._lock:
            return self._latest_results


@dataclass
class CameraSession:
    camera_id: str
    display_name: str
    source: object
    detector: MotionDetector
    profile: dict
    host: str = ""
    tile_label: Optional[QLabel] = None
    last_frame: Optional[np.ndarray] = None
    pending_clip_request_chat_ids: List[str] = field(default_factory=list)
    thread: Optional[QThread] = None
    worker: Optional[CameraWorker] = None


class GridCameraTile(QLabel):
    """Grid cell: single-click selects active camera; double-click opens single view."""

    single_clicked = Signal(str)
    double_clicked = Signal(str)

    def __init__(self, camera_id: str, parent=None):
        super().__init__(parent)
        self._camera_id = camera_id
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._emit_single)

    def _emit_single(self) -> None:
        self.single_clicked.emit(self._camera_id)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            ms = QApplication.styleHints().mouseDoubleClickInterval() + 120
            self._single_click_timer.start(ms)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.stop()
            self.double_clicked.emit(self._camera_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    """
    Main window for the Sentinel Home motion detection application.
    """

    def __init__(
        self,
        settings_manager: SettingsManager,
        motion_detector: MotionDetector,
        audio_system: AudioAlertSystem,
        telegram_notifier: TelegramNotifier,
        frame_broker: Optional["FrameBroker"] = None,
        recording_store: Optional["RecordingStore"] = None,
    ):
        super().__init__()
        self.recording_store = recording_store

        # Apply sensitivity settings from stored settings
        motion_detector.min_contour_area = settings_manager.get("min_contour_area", MIN_CONTOUR_AREA)
        motion_detector.update_var_threshold(settings_manager.get("var_threshold", VAR_THRESHOLD))
        motion_detector.recording_enabled = settings_manager.get("recording_enabled", True)
        motion_detector.motion_cooldown = settings_manager.get("motion_cooldown", MOTION_COOLDOWN)
        motion_detector.set_active_zones(settings_manager.get("active_zones", []))
        motion_detector.set_entry_lines(settings_manager.get("entry_lines", []))
        motion_detector.set_entry_direction(
            settings_manager.get("entry_direction", "outside_to_inside")
        )
        motion_detector.set_night_bug_filter(
            settings_manager.get("night_bug_filter_enabled", True),
            settings_manager.get("night_bug_start_hour", 20),
            settings_manager.get("night_bug_end_hour", 6),
        )
        motion_detector.set_cam_orientation(
            settings_manager.get("cam_orientation", "horizontal")
        )

        # Store component references
        self.settings_manager = settings_manager
        self.motion_detector = motion_detector
        self.audio_system = audio_system
        self.telegram_notifier = telegram_notifier
        self.network_discovery = NetworkCameraDiscovery()
        self.camera_profiles = self._load_camera_profiles()
        self.camera_sessions: Dict[str, CameraSession] = {}
        self.focused_camera_id: Optional[str] = None
        self.view_mode = "focus"
        self.camera_sources = {}
        self.discovered_network_sources = []
        self.discovered_rtsp_hosts: List[str] = []
        self.prefer_substream = self.settings_manager.get("prefer_substream", True)
        self.camera_logins = self._load_camera_logins()
        self.current_frame_size: Tuple[int, int] = (640, 480)
        self.last_display_pixmap_size: Tuple[int, int] = (1, 1)
        self.last_display_offset: Tuple[int, int] = (0, 0)
        self.is_drawing_zone = False
        self.is_drawing_line = False
        self.pending_zone_points: List[List[float]] = []
        self.pending_line_points: List[List[float]] = []
        self.show_detection_size_preview = False
        self.detection_size_preview_until = 0.0
        self.detection_size_preview_value = 0
        self.auto_reconnect_enabled = self.settings_manager.get(
            "auto_reconnect_enabled", AUTO_RECONNECT_DEFAULT
        )
        self.reconnect_interval_seconds = self.settings_manager.get(
            "reconnect_interval_seconds", RECONNECT_INTERVAL_SECONDS
        )
        self.display_overlay_mode = self.settings_manager.get(
            "display_overlay_mode", DEFAULT_DISPLAY_OVERLAY_MODE
        )
        self.frame_broker = frame_broker
        _web = self.settings_manager.settings.get("web")
        self.web_enabled = bool(
            frame_broker is not None
            and isinstance(_web, dict)
            and _web.get("enabled")
        )
        self.web_published_frame = (
            str(_web.get("published_frame", "overlay"))
            if isinstance(_web, dict)
            else "overlay"
        )
        self.is_main_fullscreen = False
        raw_fps = int(self.settings_manager.get("feed_fps", DEFAULT_FEED_FPS))
        self.feed_fps = max(FEED_FPS_MIN, min(FEED_FPS_MAX, raw_fps))

        # Set up timer for camera feed updates
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_feed)
        self.reconnect_timer = QTimer()
        self.reconnect_timer.timeout.connect(self.try_reconnect)
        self.detection_size_release_timer = QTimer()
        self.detection_size_release_timer.setSingleShot(True)
        self.detection_size_release_timer.timeout.connect(
            self.start_detection_size_preview
        )
        self._grid_sessions_signature: Tuple[str, ...] = ()

        # Set up UI
        self.setup_ui()
        self.cooldown_slider.setEnabled(True)

        # Initial camera search and setup
        self.search_cameras()
        self.show_blank_frame()
        self.start_initial_camera_if_configured()

    def setup_ui(self) -> None:
        """Set up the user interface."""
        # Set window properties
        self.setWindowTitle("Sentinel Home - Motion Detection")
        icon = QIcon("sentinel.ico")
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setWindowIconText("Sentinel Home")

        # Set window size, clamped to 90% of the primary screen, then centered
        window_size = self.settings_manager.get("window_size", (1100, 700))
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 0.9)
        max_h = int(screen.height() * 0.9)
        safe_w = min(window_size[0], max_w)
        safe_h = min(window_size[1], max_h)
        x = screen.x() + (screen.width() - safe_w) // 2
        y = screen.y() + (screen.height() - safe_h) // 2
        self.setGeometry(x, y, safe_w, safe_h)

        # Create main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)

        # Create UI components
        self.setup_control_panel()
        self.setup_camera_feed()

        # Apply stylesheet
        self.setStyleSheet(STYLESHEET)

    def setup_control_panel(self):
        """Set up the left control panel with all controls inside a scroll area."""
        # Inner widget that holds all the controls
        self.control_widget = QWidget()
        self.control_layout = QVBoxLayout(self.control_widget)
        self.control_layout.setAlignment(Qt.AlignTop)

        # Add control groups
        self.setup_camera_controls()
        self.setup_speech_controls()
        self.setup_telegram_controls()
        self.setup_orientation_controls()
        self.setup_recording_controls()
        self.setup_motion_sensitivity_controls()

        # Add status label
        self.status_label = QLabel("Camera: Disconnected")
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumWidth(CONTROL_PANEL_WIDTH - 24)
        self.control_layout.addWidget(self.status_label)

        # Wrap in a scroll area so the panel is always fully accessible
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.control_widget)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFixedWidth(CONTROL_PANEL_WIDTH + 20)  # +20 for scrollbar
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: #2E2E2E; }
            QScrollBar:vertical {
                background: #3E3E3E;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #00FFFF;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self.main_layout.addWidget(self.scroll_area)

    def add_collapsible_section(
        self,
        title: str,
        content_widget: QWidget,
        settings_key: str,
        default_expanded: bool = True,
    ) -> None:
        """Wrap a section widget with a clickable collapse/expand header."""
        expanded = self.settings_manager.get(settings_key, default_expanded)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(4)

        header_btn = QPushButton()
        header_btn.setCheckable(True)
        header_btn.setChecked(expanded)

        def refresh_header_text(is_expanded: bool) -> None:
            icon = "▼" if is_expanded else "▶"
            header_btn.setText(f"{icon} {title}")

        refresh_header_text(expanded)
        content_widget.setVisible(expanded)

        def on_toggle(checked: bool) -> None:
            content_widget.setVisible(checked)
            refresh_header_text(checked)
            self.settings_manager.set(settings_key, checked)

        header_btn.toggled.connect(on_toggle)
        container_layout.addWidget(header_btn)
        container_layout.addWidget(content_widget)
        self.control_layout.addWidget(container)

    def setup_camera_controls(self) -> None:
        """Set up camera selection and control buttons."""
        # Create camera group
        self.camera_group = QGroupBox("Select Camera:")
        self.camera_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.camera_layout = QVBoxLayout()

        # Camera selection dropdown - DON'T initialize with any cameras yet
        self.camera_combo = QComboBox()
        self.camera_combo.currentTextChanged.connect(self.on_camera_select)
        self.camera_layout.addWidget(self.camera_combo)

        # Optional RTSP credentials for network camera discovery
        self.rtsp_user_input = QLineEdit()
        self.rtsp_user_input.setPlaceholderText("RTSP username (optional)")
        self.rtsp_user_input.setText(self.settings_manager.get("rtsp_username", ""))
        self.camera_layout.addWidget(self.rtsp_user_input)

        self.rtsp_pass_input = QLineEdit()
        self.rtsp_pass_input.setPlaceholderText("RTSP password (optional)")
        self.rtsp_pass_input.setEchoMode(QLineEdit.Password)
        self.rtsp_pass_input.setText(self.settings_manager.get("rtsp_password", ""))
        self.camera_layout.addWidget(self.rtsp_pass_input)

        self.prefer_substream_cb = QCheckBox("Prefer sub-stream for network cameras")
        self.prefer_substream_cb.setChecked(self.prefer_substream)
        self.prefer_substream_cb.toggled.connect(self.on_prefer_substream_toggled)
        self.camera_layout.addWidget(self.prefer_substream_cb)

        # Camera control buttons
        self.search_btn = QPushButton("Search Cameras")
        self.search_btn.clicked.connect(self.search_cameras)
        self.camera_layout.addWidget(self.search_btn)

        self.add_camera_btn = QPushButton("Add Camera")
        self.add_camera_btn.clicked.connect(self.add_camera)
        self.camera_layout.addWidget(self.add_camera_btn)

        self.camera_login_btn = QPushButton("Add Camera Login")
        self.camera_login_btn.clicked.connect(self.add_camera_login)
        self.camera_layout.addWidget(self.camera_login_btn)

        self.manage_camera_logins_btn = QPushButton("Manage Camera Logins")
        self.manage_camera_logins_btn.clicked.connect(self.manage_camera_logins)
        self.camera_layout.addWidget(self.manage_camera_logins_btn)

        self.multiview_btn = QPushButton("Open MultiView")
        self.multiview_btn.clicked.connect(self.open_multiview)
        self.camera_layout.addWidget(self.multiview_btn)

        self.fullscreen_btn = QPushButton("Enter Full Screen")
        self.fullscreen_btn.clicked.connect(self.toggle_main_fullscreen)
        self.camera_layout.addWidget(self.fullscreen_btn)

        self.toggle_btn = QPushButton("Camera Offline")
        self.toggle_btn.clicked.connect(self.toggle_feed)
        self.camera_layout.addWidget(self.toggle_btn)

        self.capture_btn = QPushButton("Capture Image")
        self.capture_btn.clicked.connect(self.capture_image)
        self.camera_layout.addWidget(self.capture_btn)

        self.recall_btn = QPushButton("Recall Image")
        self.recall_btn.clicked.connect(self.recall_image)
        self.camera_layout.addWidget(self.recall_btn)

        self.setup_zone_line_controls()
        self.setup_reconnect_controls()

        # Add to control panel
        self.camera_group.setLayout(self.camera_layout)
        self.add_collapsible_section(
            "Camera",
            self.camera_group,
            "ui_expand_camera",
            default_expanded=True,
        )

    def setup_zone_line_controls(self) -> None:
        """Set up active-zone and entry-line controls."""
        self.draw_zone_btn = QPushButton("Draw Active Zone")
        self.draw_zone_btn.setCheckable(True)
        self.draw_zone_btn.toggled.connect(self.on_draw_zone_toggled)
        self.camera_layout.addWidget(self.draw_zone_btn)

        self.clear_zones_btn = QPushButton("Clear Zones")
        self.clear_zones_btn.clicked.connect(self.clear_active_zones)
        self.camera_layout.addWidget(self.clear_zones_btn)

        self.draw_line_btn = QPushButton("Draw Entry Line")
        self.draw_line_btn.setCheckable(True)
        self.draw_line_btn.toggled.connect(self.on_draw_line_toggled)
        self.camera_layout.addWidget(self.draw_line_btn)

        self.clear_lines_btn = QPushButton("Clear Lines")
        self.clear_lines_btn.clicked.connect(self.clear_entry_lines)
        self.camera_layout.addWidget(self.clear_lines_btn)

        self.entry_direction_combo = QComboBox()
        self.entry_direction_combo.addItems(
            ["outside_to_inside", "inside_to_outside", "both"]
        )
        saved_direction = self.settings_manager.get("entry_direction", "outside_to_inside")
        self.entry_direction_combo.setCurrentText(saved_direction)
        self.entry_direction_combo.currentTextChanged.connect(self.on_entry_direction_change)
        self.camera_layout.addWidget(self.entry_direction_combo)

        self.display_mode_btn = QPushButton()
        self.display_mode_btn.clicked.connect(self.toggle_display_mode)
        self.update_display_mode_button_text()
        self.camera_layout.addWidget(self.display_mode_btn)

    def setup_reconnect_controls(self) -> None:
        """Set up reconnect controls for dropped streams."""
        self.auto_reconnect_cb = QCheckBox("Auto reconnect")
        self.auto_reconnect_cb.setChecked(self.auto_reconnect_enabled)
        self.auto_reconnect_cb.toggled.connect(self.on_auto_reconnect_toggled)
        self.camera_layout.addWidget(self.auto_reconnect_cb)

        reconnect_layout = QHBoxLayout()
        reconnect_layout.addWidget(QLabel("Retry (sec):"))
        self.reconnect_interval_spin = QSpinBox()
        self.reconnect_interval_spin.setRange(2, 30)
        self.reconnect_interval_spin.setValue(int(self.reconnect_interval_seconds))
        self.reconnect_interval_spin.valueChanged.connect(self.on_reconnect_interval_change)
        reconnect_layout.addWidget(self.reconnect_interval_spin)
        self.camera_layout.addLayout(reconnect_layout)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Feed FPS (lower = less RAM/CPU):"))
        self.feed_fps_spin = QSpinBox()
        self.feed_fps_spin.setRange(FEED_FPS_MIN, FEED_FPS_MAX)
        self.feed_fps_spin.setValue(self.feed_fps)
        self.feed_fps_spin.valueChanged.connect(self.on_feed_fps_changed)
        fps_row.addWidget(self.feed_fps_spin)
        self.camera_layout.addLayout(fps_row)

    def setup_speech_controls(self) -> None:
        """Set up speech and audio controls."""
        # Speech speed control
        self.speech_group = QGroupBox("Speech Speed:")
        self.speech_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.speech_layout = QVBoxLayout()

        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(50, 300)
        self.speed_slider.setValue(self.settings_manager.get("speech_speed", 150))
        self.speed_slider.valueChanged.connect(self.on_speed_change)
        self.speech_layout.addWidget(self.speed_slider)

        self.speech_group.setLayout(self.speech_layout)
        self.control_layout.addWidget(self.speech_group)

        # Volume control
        self.volume_group = QGroupBox("Volume:")
        self.volume_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.volume_layout = QVBoxLayout()

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 10)
        self.volume_slider.setValue(int(self.settings_manager.get("volume", 1.0) * 10))
        self.volume_slider.valueChanged.connect(self.on_volume_change)
        self.volume_layout.addWidget(self.volume_slider)

        self.volume_group.setLayout(self.volume_layout)
        self.control_layout.addWidget(self.volume_group)

        # Voice selection
        self.voice_group = QGroupBox("Voice:")
        self.voice_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.voice_layout = QVBoxLayout()

        self.voice_buttons = []
        selected_voice = self.settings_manager.get(
            "voice_id",
            self.audio_system.voice_ids[0] if self.audio_system.voice_ids else None,
        )

        all_voices = list(enumerate(self.audio_system.voices))

        # If Piper is active, all models in models/ are already English — show all.
        # If pyttsx3 fallback is active, filter to English only.
        if self.audio_system.piper_voice is not None:
            voices_to_show = all_voices
        else:
            english_voices = [
                (i, v) for i, v in all_voices
                if "english" in getattr(v, "name", "").lower()
                or str(v.id).lower().startswith("en")
                or "/en" in str(v.id).lower()
            ]
            voices_to_show = english_voices if english_voices else all_voices

        for i, voice in voices_to_show:
            voice_name = getattr(voice, "name", str(voice.id)) or str(voice.id)
            btn = QRadioButton(voice_name)
            btn.setStyleSheet("font-size: 10px;")
            btn.toggled.connect(
                lambda checked, idx=i: self.on_voice_change(idx, checked)
            )
            if voice.id == selected_voice:
                btn.setChecked(True)
            self.voice_buttons.append(btn)
            self.voice_layout.addWidget(btn)

        self.voice_group.setLayout(self.voice_layout)
        self.add_collapsible_section(
            "Speech",
            self.voice_group,
            "ui_expand_speech",
            default_expanded=True,
        )

    def setup_telegram_controls(self) -> None:
        """Set up Telegram alert controls."""
        self.telegram_group = QGroupBox("Telegram Alerts:")
        self.telegram_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.telegram_layout = QVBoxLayout()

        self.telegram_enabled_cb = QCheckBox("Enable Telegram notifications")
        self.telegram_enabled_cb.setChecked(
            self.settings_manager.get("telegram_enabled", False)
        )
        self.telegram_enabled_cb.toggled.connect(self.on_telegram_enabled_toggled)
        self.telegram_layout.addWidget(self.telegram_enabled_cb)

        self.telegram_motion_alerts_cb = QCheckBox("Telegram motion alerts (text/video on detection)")
        self.telegram_motion_alerts_cb.setChecked(
            self.settings_manager.get("telegram_motion_alerts", True)
        )
        self.telegram_motion_alerts_cb.toggled.connect(self.on_telegram_motion_alerts_toggled)
        self.telegram_layout.addWidget(self.telegram_motion_alerts_cb)

        self.telegram_token_input = QLineEdit()
        self.telegram_token_input.setPlaceholderText("Bot token")
        self.telegram_token_input.setEchoMode(QLineEdit.Password)
        self.telegram_token_input.setText(self.settings_manager.get("telegram_bot_token", ""))
        self.telegram_token_input.editingFinished.connect(self.on_telegram_config_changed)
        self.telegram_layout.addWidget(self.telegram_token_input)

        self.show_telegram_token_cb = QCheckBox("Show token")
        self.show_telegram_token_cb.toggled.connect(self.on_show_telegram_token_toggled)
        self.telegram_layout.addWidget(self.show_telegram_token_cb)

        self.telegram_chat_id_input = QLineEdit()
        self.telegram_chat_id_input.setPlaceholderText("Chat IDs (comma separated)")
        saved_chat_ids = self.settings_manager.get("telegram_chat_ids", "")
        if not saved_chat_ids:
            saved_chat_ids = self.settings_manager.get("telegram_chat_id", "")
        self.telegram_chat_id_input.setText(saved_chat_ids)
        self.telegram_chat_id_input.editingFinished.connect(self.on_telegram_config_changed)
        self.telegram_layout.addWidget(self.telegram_chat_id_input)

        self.telegram_include_video_cb = QCheckBox("Include recording video clip")
        self.telegram_include_video_cb.setChecked(
            self.settings_manager.get("telegram_include_video", False)
        )
        self.telegram_include_video_cb.toggled.connect(self.on_telegram_include_video_toggled)
        self.telegram_layout.addWidget(self.telegram_include_video_cb)

        cooldown_row = QHBoxLayout()
        cooldown_row.addWidget(QLabel("Cooldown (sec):"))
        self.telegram_cooldown_spin = QSpinBox()
        self.telegram_cooldown_spin.setRange(0, 300)
        self.telegram_cooldown_spin.setValue(
            int(self.settings_manager.get("telegram_cooldown", 10.0))
        )
        self.telegram_cooldown_spin.valueChanged.connect(self.on_telegram_cooldown_changed)
        cooldown_row.addWidget(self.telegram_cooldown_spin)
        self.telegram_layout.addLayout(cooldown_row)

        self.telegram_test_btn = QPushButton("Send Telegram Test")
        self.telegram_test_btn.clicked.connect(self.on_send_telegram_test)
        self.telegram_layout.addWidget(self.telegram_test_btn)

        self.telegram_group.setLayout(self.telegram_layout)
        self.add_collapsible_section(
            "Telegram",
            self.telegram_group,
            "ui_expand_telegram",
            default_expanded=True,
        )

    def setup_orientation_controls(self) -> None:
        """Set up camera orientation controls."""
        self.orient_group = QGroupBox("Camera View:")
        self.orient_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.orient_layout = QHBoxLayout()

        self.horiz_btn = QRadioButton("Horizontal")
        self.horiz_btn.toggled.connect(self.on_cam_orient_change)

        self.vert_btn = QRadioButton("Vertical")
        self.vert_btn.toggled.connect(self.on_cam_orient_change)

        self.orient_layout.addWidget(self.horiz_btn)
        self.orient_layout.addWidget(self.vert_btn)

        # Set initial state from settings
        if self.settings_manager.get("cam_orientation", "horizontal") == "horizontal":
            self.horiz_btn.setChecked(True)
        else:
            self.vert_btn.setChecked(True)

        self.orient_group.setLayout(self.orient_layout)
        self.add_collapsible_section(
            "Orientation",
            self.orient_group,
            "ui_expand_orientation",
            default_expanded=True,
        )

    def setup_camera_feed(self) -> None:
        """Set up the camera feed display area."""
        self.feed_container = QWidget()
        self.feed_layout = QVBoxLayout(self.feed_container)
        self.feed_layout.setContentsMargins(0, 0, 0, 0)

        self.return_to_grid_btn = QPushButton("Return to Grid")
        self.return_to_grid_btn.clicked.connect(self.show_grid_view)
        self.return_to_grid_btn.hide()
        self.feed_layout.addWidget(self.return_to_grid_btn)

        self.feed_mode_container = QWidget()
        self.feed_mode_layout = QVBoxLayout(self.feed_mode_container)
        self.feed_mode_layout.setContentsMargins(0, 0, 0, 0)
        self.feed_layout.addWidget(self.feed_mode_container, stretch=1)

        self.camera_feed = QLabel()
        self.camera_feed.setAlignment(Qt.AlignCenter)
        self.camera_feed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.camera_feed.setStyleSheet("background-color: black;")
        self.camera_feed.setMouseTracking(True)

        # Set size from settings
        feed_size = self.settings_manager.get("feed_size", (640, 480))
        self.camera_feed.setMinimumSize(320, 240)  # Minimum size
        self.camera_feed.resize(feed_size[0], feed_size[1])

        self.grid_widget = QWidget()
        self.grid_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(4)

        # Stack ensures grid gets the full feed area; hiding the single-view label
        # in a shared QVBoxLayout can still squeeze the grid geometry.
        self.feed_stack = QStackedWidget()
        self.feed_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.feed_stack.addWidget(self.camera_feed)
        self.feed_stack.addWidget(self.grid_widget)
        self.feed_mode_layout.addWidget(self.feed_stack, stretch=1)

        self.main_layout.addWidget(self.feed_container, stretch=1)

    def search_cameras(self) -> None:
        """Search for available cameras and update the dropdown."""
        camera_list = []
        self.camera_sources = {}
        connected_session_ids = set(self.camera_sessions.keys())

        for i in range(10):  # Check the first 10 camera indices
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
                if cap.isOpened() and cap.grab():
                    name = f"Camera {i}"
                    camera_list.append(name)
                    self.camera_sources[name] = i
                cap.release()
            except Exception as e:
                print(f"Error checking camera {i}: {e}")

        username = self.rtsp_user_input.text().strip()
        password = self.rtsp_pass_input.text()
        self.settings_manager.set("rtsp_username", username)
        self.settings_manager.set("rtsp_password", password)

        self.discovered_network_sources = self.network_discovery.discover(
            username=username,
            password=password,
            prefer_substream=self.prefer_substream,
            host_credentials=self.camera_logins,
        )
        self.discovered_rtsp_hosts = self.network_discovery.discover_hosts()
        endpoint_hosts = set()
        for endpoint in self.discovered_network_sources:
            camera_list.append(endpoint.label)
            self.camera_sources[endpoint.label] = endpoint.source
            endpoint_hosts.add(endpoint.host)

        for host in self.discovered_rtsp_hosts:
            if host in endpoint_hosts:
                continue
            pending_label = f"Network {host} (Login Required)"
            camera_list.append(pending_label)
            self.camera_sources[pending_label] = f"pending://{host}"

        # Keep currently connected sessions selectable even when discovery
        # temporarily reports no cameras or host probes time out.
        for session in self.camera_sessions.values():
            if session.display_name not in self.camera_sources:
                camera_list.append(session.display_name)
                self.camera_sources[session.display_name] = session.source

        # Restore whichever source was selected last
        saved_source = self.settings_manager.get("camera_source", "")
        saved_display = self.settings_manager.get("camera_display", "")

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        if not camera_list:
            self.camera_combo.addItems(["No Cameras Found"])
            self.camera_combo.blockSignals(False)
            self.status_label.setText("Camera: None Detected")
            return

        self.camera_combo.addItems(camera_list)
        self.camera_combo.blockSignals(False)

        if saved_display in self.camera_sources:
            self.camera_combo.setCurrentText(saved_display)
        else:
            matched_display = None
            for display, source in self.camera_sources.items():
                if str(source) == str(saved_source):
                    matched_display = display
                    break
            self.camera_combo.setCurrentText(matched_display or camera_list[0])

        selected_display = self.camera_combo.currentText()
        selected_source = self.camera_sources.get(selected_display)
        selected_id = str(selected_source) if selected_source is not None else ""
        if selected_id in connected_session_ids:
            self.status_label.setText(f"Camera: {selected_display} (Connected)")
        else:
            self.status_label.setText(f"Camera: {selected_display} (Disconnected)")

    def _load_camera_logins(self) -> dict:
        """Load per-host camera credentials from settings."""
        saved = self.settings_manager.get("camera_logins", {})
        if isinstance(saved, dict):
            return saved
        return {}

    def _load_camera_profiles(self) -> dict:
        """Load per-camera profiles keyed by camera source id."""
        saved = self.settings_manager.get("camera_profiles", {})
        if isinstance(saved, dict):
            return saved
        return {}

    def _default_profile_for_source(self, source, display_name: str) -> dict:
        return {
            "source": str(source),
            "display": display_name,
            "active_zones": [],
            "entry_lines": [],
            "entry_direction": self.settings_manager.get("entry_direction", "outside_to_inside"),
            "min_contour_area": self.settings_manager.get("min_contour_area", MIN_CONTOUR_AREA),
            "var_threshold": self.settings_manager.get("var_threshold", VAR_THRESHOLD),
            "motion_cooldown": self.settings_manager.get("motion_cooldown", MOTION_COOLDOWN),
            "recording_enabled": self.settings_manager.get("recording_enabled", True),
            "night_bug_filter_enabled": self.settings_manager.get("night_bug_filter_enabled", True),
            "night_bug_start_hour": self.settings_manager.get("night_bug_start_hour", 20),
            "night_bug_end_hour": self.settings_manager.get("night_bug_end_hour", 6),
        }

    def _get_or_create_profile(self, source, display_name: str) -> Tuple[str, dict]:
        camera_id = str(source)
        profile = self.camera_profiles.get(camera_id)
        if not isinstance(profile, dict):
            profile = self._default_profile_for_source(source, display_name)
            self.camera_profiles[camera_id] = profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)
        else:
            profile.setdefault("display", display_name)
            profile.setdefault("source", str(source))
            profile.setdefault("active_zones", [])
            profile.setdefault("entry_lines", [])
            profile.setdefault("entry_direction", "outside_to_inside")
        return camera_id, profile

    def add_camera_login(self) -> None:
        """Prompt for and save per-camera login credentials."""
        host, ok = QInputDialog.getText(
            self,
            "Add Camera Login",
            "Camera IP/Host (example: 192.168.1.10):",
        )
        if not ok:
            return
        host = host.strip()
        if not host:
            return

        current = self.camera_logins.get(host, {})
        current_username = current.get("username", "")
        current_password = current.get("password", "")

        username, ok = QInputDialog.getText(
            self,
            "Add Camera Login",
            f"Username for {host} (leave blank to remove saved login):",
            text=current_username,
        )
        if not ok:
            return
        username = username.strip()

        if not username:
            if host in self.camera_logins:
                del self.camera_logins[host]
                self.settings_manager.set("camera_logins", self.camera_logins)
                self.status_label.setText(f"Saved login removed for {host}")
            return

        password, ok = QInputDialog.getText(
            self,
            "Add Camera Login",
            f"Password for {host}:",
            QLineEdit.Password,
            current_password,
        )
        if not ok:
            return

        self.camera_logins[host] = {
            "username": username,
            "password": password,
        }
        self.settings_manager.set("camera_logins", self.camera_logins)
        self.status_label.setText(f"Saved login for {host}")

    def manage_camera_logins(self) -> None:
        """View/edit/remove existing saved per-camera logins."""
        if not self.camera_logins:
            QMessageBox.information(
                self,
                "Manage Camera Logins",
                "No saved camera logins yet.",
            )
            return

        hosts = sorted(self.camera_logins.keys())
        host, ok = QInputDialog.getItem(
            self,
            "Manage Camera Logins",
            "Select camera host:",
            hosts,
            0,
            False,
        )
        if not ok:
            return

        action, ok = QInputDialog.getItem(
            self,
            "Manage Camera Logins",
            f"Action for {host}:",
            ["Edit login", "Remove login"],
            0,
            False,
        )
        if not ok:
            return

        if action == "Remove login":
            del self.camera_logins[host]
            self.settings_manager.set("camera_logins", self.camera_logins)
            self.status_label.setText(f"Saved login removed for {host}")
            return

        current = self.camera_logins.get(host, {})
        current_username = current.get("username", "")
        current_password = current.get("password", "")

        username, ok = QInputDialog.getText(
            self,
            "Manage Camera Logins",
            f"Username for {host} (leave blank to remove):",
            text=current_username,
        )
        if not ok:
            return
        username = username.strip()

        if not username:
            if host in self.camera_logins:
                del self.camera_logins[host]
                self.settings_manager.set("camera_logins", self.camera_logins)
                self.status_label.setText(f"Saved login removed for {host}")
            return

        password, ok = QInputDialog.getText(
            self,
            "Manage Camera Logins",
            f"Password for {host}:",
            QLineEdit.Password,
            current_password,
        )
        if not ok:
            return

        self.camera_logins[host] = {
            "username": username,
            "password": password,
        }
        self.settings_manager.set("camera_logins", self.camera_logins)
        self.status_label.setText(f"Saved login updated for {host}")

    def add_camera(self) -> None:
        """Add/resolve a network camera from discovered hosts or manual entry."""
        host_choices = list(self.discovered_rtsp_hosts)
        host_choices.append("Manual entry...")
        selected_host, ok = QInputDialog.getItem(
            self,
            "Add Camera",
            "Choose discovered camera host or manual entry:",
            host_choices,
            0,
            False,
        )
        if not ok:
            return

        host = selected_host
        if selected_host == "Manual entry...":
            host, ok = QInputDialog.getText(
                self,
                "Add Camera",
                "Camera IP/Host (example: 192.168.1.10):",
            )
            if not ok:
                return
            host = host.strip()
            if not host:
                return

        resolved = self._resolve_host_with_prompt(host)
        if not resolved:
            return
        self._upsert_discovered_endpoint(resolved)
        self.camera_combo.setCurrentText(resolved.label)
        self.status_label.setText(f"{resolved.label}: Added")

    def _upsert_discovered_endpoint(self, endpoint: CameraEndpoint) -> None:
        """Ensure a resolved endpoint is available in camera sources and multiview."""
        self.camera_sources[endpoint.label] = endpoint.source
        existing_hosts = {entry.host for entry in self.discovered_network_sources}
        if endpoint.host not in existing_hosts:
            self.discovered_network_sources.append(endpoint)
        if self.camera_combo.findText(endpoint.label) < 0:
            self.camera_combo.addItem(endpoint.label)

    def _resolve_host_with_prompt(self, host: str) -> Optional[CameraEndpoint]:
        """Prompt for per-host login and resolve to a readable stream endpoint."""
        current = self.camera_logins.get(host, {})
        current_username = current.get("username", self.rtsp_user_input.text().strip())
        current_password = current.get("password", self.rtsp_pass_input.text())

        username, ok = QInputDialog.getText(
            self,
            "Camera Login",
            f"Username for {host}:",
            text=current_username,
        )
        if not ok:
            return None
        username = username.strip()

        password, ok = QInputDialog.getText(
            self,
            "Camera Login",
            f"Password for {host}:",
            QLineEdit.Password,
            current_password,
        )
        if not ok:
            return None

        endpoint = self.network_discovery.resolve_host_endpoint(
            host=host,
            username=username,
            password=password,
            prefer_substream=self.prefer_substream,
        )
        if not endpoint:
            QMessageBox.warning(
                self,
                "Camera Not Added",
                f"Could not open a stream for {host} using the provided credentials.",
            )
            return None

        self.camera_logins[host] = {
            "username": username,
            "password": password,
        }
        self.settings_manager.set("camera_logins", self.camera_logins)
        return endpoint

    def toggle_feed(self) -> None:
        """Toggle selected camera connection on/off without affecting others."""
        if self.camera_combo.currentText() == "No Cameras Found":
            self.status_label.setText("Camera: None Available")
            return
        selected_display = self.camera_combo.currentText()
        selected_source = self.camera_sources.get(selected_display)
        if selected_source is None:
            self.status_label.setText("Camera: Invalid Source")
            return
        session_id = str(selected_source)
        if session_id in self.camera_sessions:
            self.disconnect_camera(session_id=session_id)
            return
        if not self.connect_camera_source(selected_source, selected_display):
            self.status_label.setText(f"{selected_display}: Failed to Connect")
            self.start_reconnect_timer()

    def connect_camera_source(self, source, display_name: str) -> bool:
        """Connect selected camera and update UI/settings."""
        if isinstance(source, str) and source.startswith("pending://"):
            host = source.split("pending://", 1)[1]
            resolved = self._resolve_host_with_prompt(host)
            if not resolved:
                return False
            self._upsert_discovered_endpoint(resolved)
            source = resolved.source
            display_name = resolved.label

        camera_id, profile = self._get_or_create_profile(source, display_name)
        detector = MotionDetector()
        session = CameraSession(
            camera_id=camera_id,
            display_name=display_name,
            source=source,
            detector=detector,
            profile=profile,
            host=self._extract_host(source),
        )
        detector.set_recording_identity(
            self.recording_store, session.host or display_name, display_name
        )
        self._apply_profile_to_detector(session)
        if detector.start_camera(source, target_fps=self.feed_fps):
            # Set up background thread for this camera
            thread = QThread()
            worker = CameraWorker(detector, source, self.feed_fps)
            worker.moveToThread(thread)
            
            # Connect signals
            thread.started.connect(worker.start_loop)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            
            session.thread = thread
            session.worker = worker
            
            self.camera_sessions[camera_id] = session
            thread.start()
            
            self._ensure_camera_selectable(display_name, source)
            self.focused_camera_id = camera_id
            self.timer.start(self._feed_timer_interval_ms())
            self.reconnect_timer.stop()
            self.toggle_btn.setText("Disconnect Selected")
            self.status_label.setText(f"{display_name}: Connected")
            self.search_btn.setEnabled(True)
            self.search_btn.setProperty("inactive", False)
            self.search_btn.setStyleSheet("")
            self.recall_btn.setEnabled(True)
            self.recall_btn.setProperty("inactive", False)
            self.recall_btn.setStyleSheet("")
            self._apply_profile_to_controls(profile)
            self.show_focus_view()
            self._refresh_grid_tiles()
            self.settings_manager.set("camera_index", source if isinstance(source, int) else 0)
            self.settings_manager.set("camera_source", str(source))
            self.settings_manager.set("camera_display", display_name)
            self.settings_manager.set("feed_active", True)
            app_logger.log(
                "info",
                "camera_connected",
                camera_id=camera_id,
                camera_ip=session.host or "unknown",
                camera_display=display_name,
                component="main_window",
                action="connect_camera_source",
            )
            return True
        app_logger.log(
            "error",
            "camera_connect_failed",
            camera_id=camera_id,
            camera_ip=session.host or "unknown",
            camera_display=display_name,
            component="main_window",
            action="connect_camera_source",
            error_message=detector.last_error_message or "connect_failed",
        )
        return False

    def _ensure_camera_selectable(self, display_name: str, source: object) -> None:
        """Ensure connected camera appears in combo even after empty discovery."""
        self.camera_sources[display_name] = source
        self.camera_combo.blockSignals(True)
        if self.camera_combo.count() == 1 and self.camera_combo.itemText(0) == "No Cameras Found":
            self.camera_combo.clear()
        if self.camera_combo.findText(display_name) < 0:
            self.camera_combo.addItem(display_name)
        self.camera_combo.setCurrentText(display_name)
        self.camera_combo.blockSignals(False)

    def disconnect_camera(self, user_requested: bool = True, session_id: Optional[str] = None) -> None:
        """Disconnect one camera session and restore idle state if none remain."""
        target_id = session_id or self.focused_camera_id
        if not target_id:
            return
        session = self.camera_sessions.pop(target_id, None)
        if not session:
            return
        
        if session.worker:
            session.worker.stop()
        if session.thread:
            session.thread.quit()
            # Do NOT call .wait() here; it can hang the UI for 30s if OpenCV is stuck in a socket read.
            # The thread will eventually exit its loop and be deleted by the deleteLater connections.

        if self.frame_broker is not None and self.web_enabled:
            self.frame_broker.unpublish(target_id)
        session.detector.stop_camera()
        if user_requested:
            self.reconnect_timer.stop()
        if not self.camera_sessions:
            self.timer.stop()
            self.settings_manager.set("feed_active", False)
            self.toggle_btn.setText("Camera Offline")
            self.show_blank_frame()
        else:
            self.focused_camera_id = next(iter(self.camera_sessions.keys()))
            self.toggle_btn.setText("Disconnect Selected")
        self._refresh_grid_tiles()
        self.status_label.setText(f"{session.display_name}: Disconnected")

    def start_reconnect_timer(self) -> None:
        """Start retry timer when reconnect is enabled."""
        if not self.auto_reconnect_enabled:
            return
        if not self.reconnect_timer.isActive():
            self.reconnect_timer.start(int(self.reconnect_interval_seconds * 1000))

    def try_reconnect(self) -> None:
        """Reconnect every saved camera that is not currently connected."""
        profiles = self.settings_manager.get("camera_profiles", {})
        targets = []
        if isinstance(profiles, dict) and profiles:
            for camera_id, profile in profiles.items():
                if camera_id in self.camera_sessions:
                    continue
                source = self._coerce_source(profile.get("source", camera_id))
                targets.append((source, profile.get("display", f"Camera {camera_id}")))
        elif not self.camera_sessions:
            saved_source = self.settings_manager.get("camera_source", "")
            if saved_source:
                source = self._coerce_source(saved_source)
                targets.append(
                    (source, self.settings_manager.get("camera_display", f"Camera {saved_source}"))
                )
        if not targets:
            self.reconnect_timer.stop()
            return

        focused_before = self.focused_camera_id
        still_down = 0
        for source, display in targets:
            if self.connect_camera_source(source, display):
                self.status_label.setText(f"{display}: Reconnected")
                app_logger.log(
                    "info",
                    "camera_reconnect_success",
                    camera_id=str(source),
                    camera_ip=self._extract_host(source) or "unknown",
                    camera_display=display,
                    component="main_window",
                    action="try_reconnect",
                    retry_outcome="recovered",
                )
                continue
            still_down += 1
            self.status_label.setText(f"{display}: Reconnecting...")
            app_logger.log(
                "error",
                "camera_reconnect_failed",
                camera_id=str(source),
                camera_ip=self._extract_host(source) or "unknown",
                camera_display=display,
                component="main_window",
                action="try_reconnect",
                retry_outcome="will_retry",
            )
        # connect_camera_source focuses the new camera; keep the user's focus.
        if focused_before in self.camera_sessions:
            self.focused_camera_id = focused_before
        if not still_down:
            self.reconnect_timer.stop()

    def _coerce_source(self, source_value: str):
        """Convert saved source into int index when possible."""
        if isinstance(source_value, int):
            return source_value
        if isinstance(source_value, str) and source_value.isdigit():
            return int(source_value)
        return source_value

    def start_initial_camera_if_configured(self) -> None:
        """Attempt startup connect and begin retries if needed."""
        profiles = self.settings_manager.get("camera_profiles", {})
        if not isinstance(profiles, dict) or not profiles:
            if not self.settings_manager.get("feed_active", False):
                return
            saved_source = self.settings_manager.get("camera_source", "")
            if not saved_source:
                return
            source = self._coerce_source(saved_source)
            display = self.settings_manager.get("camera_display", f"Camera {saved_source}")
            if not self.connect_camera_source(source, display):
                self.status_label.setText(f"{display}: Initial connect failed, retrying...")
                self.start_reconnect_timer()
            return
        for camera_id, profile in profiles.items():
            source = self._coerce_source(profile.get("source", camera_id))
            display = profile.get("display", f"Camera {camera_id}")
            if not self.connect_camera_source(source, display):
                self.start_reconnect_timer()

    def display_frame(self, frame: np.ndarray) -> None:
        """
        Display a frame in the camera feed.
        
        Args:
            frame: The OpenCV frame to display
        """
        # Convert BGR to RGB for Qt
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Create QImage and QPixmap
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        q_image = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(q_image)

        # Display with aspect ratio preservation
        scaled = pixmap.scaled(
            self.camera_feed.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.last_display_pixmap_size = (scaled.width(), scaled.height())
        self.last_display_offset = (
            max(0, (self.camera_feed.width() - scaled.width()) // 2),
            max(0, (self.camera_feed.height() - scaled.height()) // 2),
        )
        self.camera_feed.setPixmap(scaled)

    def _display_frame_in_label(self, frame: np.ndarray, target: QLabel) -> None:
        # Contiguous RGB + copy so Qt never reads stale numpy backing after this returns.
        frame_rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        q_image = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(q_image.copy())
        size = target.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        # Fit inside the tile without cropping (correct per-camera aspect; small letterbox).
        scaled = pixmap.scaled(
            size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        target.setPixmap(scaled)

    def _refresh_grid_tiles(self) -> None:
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        sessions = list(self.camera_sessions.values())
        if not sessions:
            self._grid_sessions_signature = ()
            return

        columns = 2 if len(sessions) > 1 else 1
        rows = (len(sessions) + columns - 1) // columns
        for idx, session in enumerate(sessions):
            tile = GridCameraTile(session.camera_id)
            tile.setAlignment(Qt.AlignCenter)
            tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            tile.setMinimumSize(120, 90)
            tile.setScaledContents(False)
            tile.setText(session.display_name)
            tile.single_clicked.connect(self.activate_camera_for_grid)
            tile.double_clicked.connect(self.focus_camera)
            session.tile_label = tile
            row = idx // columns
            col = idx % columns
            self.grid_layout.addWidget(tile, row, col)
        for r in range(rows):
            self.grid_layout.setRowStretch(r, 1)
        for c in range(columns):
            self.grid_layout.setColumnStretch(c, 1)
        self._grid_sessions_signature = tuple(sorted(self.camera_sessions.keys()))
        self._style_grid_tile_borders()

    def _ensure_grid_tiles(self) -> None:
        """Rebuild grid tiles only when session set changes (not every frame)."""
        sig = tuple(sorted(self.camera_sessions.keys()))
        if sig != self._grid_sessions_signature or self.grid_layout.count() == 0:
            self._refresh_grid_tiles()
        else:
            self._style_grid_tile_borders()

    def _style_grid_tile_borders(self) -> None:
        """Highlight the active camera tile in grid mode."""
        for session in self.camera_sessions.values():
            tile = session.tile_label
            if tile is None:
                continue
            if session.camera_id == self.focused_camera_id:
                tile.setStyleSheet(
                    "background-color: black; border: 3px solid #00FFFF;"
                )
            else:
                tile.setStyleSheet(
                    "background-color: black; border: 1px solid #448888;"
                )

    def show_grid_view(self) -> None:
        self.view_mode = "grid"
        self.feed_stack.setCurrentWidget(self.grid_widget)
        self.return_to_grid_btn.setText("Return to Single View")
        self.return_to_grid_btn.show()
        self.multiview_btn.setText("Single View")
        self._refresh_grid_tiles()

    def show_focus_view(self) -> None:
        self.view_mode = "focus"
        self.feed_stack.setCurrentWidget(self.camera_feed)
        self.return_to_grid_btn.setText("Return to Grid")
        self.return_to_grid_btn.setVisible(bool(self.camera_sessions))
        self.multiview_btn.setText("Open MultiView")

    def activate_camera_for_grid(self, camera_id: str) -> None:
        """Grid mode: select which camera drives the control panel (stay in grid)."""
        if camera_id not in self.camera_sessions:
            return
        self.focused_camera_id = camera_id
        session = self.camera_sessions[camera_id]
        self._apply_profile_to_controls(session.profile)
        self._style_grid_tile_borders()
        self.status_label.setText(
            f"Grid — active: {session.display_name} (double-click tile for single view)"
        )

    def focus_camera(self, camera_id: str) -> None:
        """Open single-camera view for this feed."""
        if camera_id not in self.camera_sessions:
            return
        self.focused_camera_id = camera_id
        session = self.camera_sessions[camera_id]
        self._apply_profile_to_controls(session.profile)
        self.show_focus_view()

    def show_blank_frame(self) -> None:
        """Display a blank frame with 'Camera Offline' text."""
        # Get current feed dimensions
        feed_width = max(self.camera_feed.width(), 640)
        feed_height = max(self.camera_feed.height(), 480)

        # Create blank frame
        frame = np.zeros((feed_height, feed_width, 3), dtype=np.uint8)

        # Add text
        text = "Camera Offline"
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 1
        thickness = 2

        # Center the text
        text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]
        text_x = (feed_width - text_size[0]) // 2
        text_y = (feed_height + text_size[1]) // 2

        cv2.putText(
            frame, text, (text_x, text_y), font, font_scale, (255, 255, 255), thickness
        )

        # Display the frame
        self.display_frame(frame)

    def _draw_profile_zones_lines(self, frame: np.ndarray, profile: dict) -> np.ndarray:
        """Draw saved zones/lines from a camera profile onto frame."""
        draw = frame.copy()
        height, width = draw.shape[:2]

        for zone in profile.get("active_zones", []):
            points = np.array(
                [[int(p[0] * width), int(p[1] * height)] for p in zone],
                dtype=np.int32,
            )
            if len(points) >= 3:
                cv2.polylines(draw, [points], True, (255, 255, 0), 2)

        for line in profile.get("entry_lines", []):
            if len(line) != 2:
                continue
            p1 = (int(line[0][0] * width), int(line[0][1] * height))
            p2 = (int(line[1][0] * width), int(line[1][1] * height))
            cv2.line(draw, p1, p2, (255, 0, 255), 2)

        return draw

    def _draw_pending_zone_line_guides(self, frame: np.ndarray) -> np.ndarray:
        """Draw in-progress polygon/line while drawing on the focused camera."""
        draw = frame.copy()
        height, width = draw.shape[:2]

        if self.pending_zone_points:
            pending = np.array(
                [[int(p[0] * width), int(p[1] * height)] for p in self.pending_zone_points],
                dtype=np.int32,
            )
            if len(pending) >= 2:
                cv2.polylines(draw, [pending], False, (0, 255, 255), 1)

        if len(self.pending_line_points) == 2:
            p1 = (
                int(self.pending_line_points[0][0] * width),
                int(self.pending_line_points[0][1] * height),
            )
            p2 = (
                int(self.pending_line_points[1][0] * width),
                int(self.pending_line_points[1][1] * height),
            )
            cv2.line(draw, p1, p2, (0, 255, 255), 1)
        return draw

    def draw_zone_line_overlays(self, frame: np.ndarray) -> np.ndarray:
        """Draw configured zones/lines and in-progress drawing guides (single-view focused camera)."""
        session = self._focused_session()
        profile = session.profile if session else {}
        frame = self._draw_profile_zones_lines(frame, profile)
        frame = self._draw_pending_zone_line_guides(frame)
        return frame

    def _label_point_to_normalized(self, pos: QPoint) -> Optional[List[float]]:
        """Map mouse click in QLabel to normalized frame coordinates."""
        pix_w, pix_h = self.last_display_pixmap_size
        off_x, off_y = self.last_display_offset
        if pix_w <= 0 or pix_h <= 0:
            return None
        x = pos.x() - off_x
        y = pos.y() - off_y
        if x < 0 or y < 0 or x >= pix_w or y >= pix_h:
            return None
        nx = max(0.0, min(1.0, x / pix_w))
        ny = max(0.0, min(1.0, y / pix_h))
        return [nx, ny]

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Capture drawing clicks while in zone/line mode."""
        feed_pos = self.camera_feed.mapFromGlobal(event.globalPosition().toPoint())
        in_feed = self.camera_feed.rect().contains(feed_pos)
        if in_feed and (self.is_drawing_zone or self.is_drawing_line):
            point = self._label_point_to_normalized(feed_pos)
            if point is None:
                super().mousePressEvent(event)
                return
            if self.is_drawing_zone:
                if event.button() == Qt.LeftButton:
                    self.pending_zone_points.append(point)
                elif event.button() == Qt.RightButton:
                    self.commit_pending_zone()
            elif self.is_drawing_line and event.button() == Qt.LeftButton:
                self.pending_line_points.append(point)
                if len(self.pending_line_points) == 2:
                    self.commit_pending_line()
            return
        super().mousePressEvent(event)

    def setup_motion_sensitivity_controls(self):
        """Set up controls for motion detection sensitivity."""
        self.sensitivity_group = QGroupBox("Motion Sensitivity:")
        self.sensitivity_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.sensitivity_layout = QVBoxLayout()

        # Minimum contour area slider
        contour_layout = QHBoxLayout()
        contour_label = QLabel("Detection Size:")
        self.contour_value_label = QLabel(
            f"{self.settings_manager.get('min_contour_area', MIN_CONTOUR_AREA)}"
        )
        contour_layout.addWidget(contour_label)
        contour_layout.addWidget(self.contour_value_label)
        self.sensitivity_layout.addLayout(contour_layout)

        self.contour_slider = QSlider(Qt.Horizontal)
        self.contour_slider.setRange(100, 2000)  # Range for minimum contour area
        self.contour_slider.setValue(
            self.settings_manager.get("min_contour_area", MIN_CONTOUR_AREA)
        )
        self.contour_slider.valueChanged.connect(self.on_contour_size_change)
        self.contour_slider.sliderPressed.connect(self.on_contour_slider_pressed)
        self.contour_slider.sliderReleased.connect(self.on_contour_slider_released)
        self.contour_slider.setToolTip(
            "Adjust how large movement needs to be to trigger detection"
        )
        self.sensitivity_layout.addWidget(self.contour_slider)

        # Variance threshold slider
        variance_layout = QHBoxLayout()
        variance_label = QLabel("Sensitivity:")
        self.variance_value_label = QLabel(
            f"{self.settings_manager.get('var_threshold', VAR_THRESHOLD)}"
        )
        variance_layout.addWidget(variance_label)
        variance_layout.addWidget(self.variance_value_label)
        self.sensitivity_layout.addLayout(variance_layout)

        self.variance_slider = QSlider(Qt.Horizontal)
        self.variance_slider.setRange(10, 100)  # Range for variance threshold
        self.variance_slider.setValue(
            self.settings_manager.get("var_threshold", VAR_THRESHOLD)
        )
        self.variance_slider.valueChanged.connect(self.on_variance_change)
        self.variance_slider.setToolTip(
            "Adjust how sensitive the detector is to changes (lower = more sensitive)"
        )
        self.sensitivity_layout.addWidget(self.variance_slider)

        # Night bug filter controls
        self.night_bug_filter_cb = QCheckBox("Night bug filter")
        self.night_bug_filter_cb.setChecked(
            self.settings_manager.get("night_bug_filter_enabled", True)
        )
        self.night_bug_filter_cb.toggled.connect(self.on_night_bug_filter_toggled)
        self.sensitivity_layout.addWidget(self.night_bug_filter_cb)

        night_hours_layout = QHBoxLayout()
        night_hours_layout.addWidget(QLabel("Night hours:"))
        self.night_bug_start_spin = QSpinBox()
        self.night_bug_start_spin.setRange(0, 23)
        self.night_bug_start_spin.setValue(
            int(self.settings_manager.get("night_bug_start_hour", 20))
        )
        self.night_bug_start_spin.valueChanged.connect(self.on_night_bug_schedule_changed)
        night_hours_layout.addWidget(self.night_bug_start_spin)
        night_hours_layout.addWidget(QLabel("to"))
        self.night_bug_end_spin = QSpinBox()
        self.night_bug_end_spin.setRange(0, 23)
        self.night_bug_end_spin.setValue(
            int(self.settings_manager.get("night_bug_end_hour", 6))
        )
        self.night_bug_end_spin.valueChanged.connect(self.on_night_bug_schedule_changed)
        night_hours_layout.addWidget(self.night_bug_end_spin)
        self.sensitivity_layout.addLayout(night_hours_layout)
        self.sensitivity_group.setLayout(self.sensitivity_layout)
        self.add_collapsible_section(
            "Motion Sensitivity",
            self.sensitivity_group,
            "ui_expand_sensitivity",
            default_expanded=True,
        )

    def setup_recording_controls(self):
        """Set up controls for video recording settings."""
        self.recording_group = QGroupBox("Recording Settings:")
        self.recording_group.setStyleSheet("QGroupBox::title { color: #00FFFF; }")
        self.recording_layout = QVBoxLayout()

        # Enable/disable recording checkbox
        self.recording_enabled_cb = QCheckBox("Enable Video Recording")
        self.recording_enabled_cb.setChecked(self.settings_manager.get("recording_enabled", True))
        self.recording_enabled_cb.toggled.connect(self.on_recording_enabled_change)
        self.recording_layout.addWidget(self.recording_enabled_cb)
        # Add tooltip to recording checkbox
        self.recording_enabled_cb.setToolTip("Enable or disable automatic recording when motion is detected")

        # Cooldown slider and label
        cooldown_layout = QHBoxLayout()
        cooldown_label = QLabel("Motion Cooldown:")
        self.cooldown_value_label = QLabel(f"{self.settings_manager.get('motion_cooldown', MOTION_COOLDOWN):.1f}s")
        cooldown_layout.addWidget(cooldown_label)
        cooldown_layout.addWidget(self.cooldown_value_label)
        self.recording_layout.addLayout(cooldown_layout)

        self.cooldown_slider = QSlider(Qt.Horizontal)
        self.cooldown_slider.setRange(1, 100)  # 0.1s to 10.0s
        self.cooldown_slider.setValue(int(self.settings_manager.get("motion_cooldown", MOTION_COOLDOWN) * 10))
        self.cooldown_slider.valueChanged.connect(self.on_cooldown_change)
        self.recording_layout.addWidget(self.cooldown_slider)
        # Add tooltip to cooldown slider
        self.cooldown_slider.setToolTip(
            "Set the minimum time between recordings (in seconds)"
        )

        # Add to control panel
        self.recording_group.setLayout(self.recording_layout)
        self.add_collapsible_section(
            "Recording",
            self.recording_group,
            "ui_expand_recording",
            default_expanded=True,
        )

    def on_recording_enabled_change(self, enabled):
        """Handle recording enabled/disabled toggle."""
        self.cooldown_slider.setEnabled(enabled)
        self.settings_manager.set("recording_enabled", bool(enabled))
        for session in self.camera_sessions.values():
            session.profile["recording_enabled"] = bool(enabled)
            session.detector.set_recording_enabled(enabled)
            self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)
        status = "enabled" if enabled else "disabled"
        self.status_label.setText(f"Recording {status}")
        app_logger.log(
            "info",
            "recording_toggle_changed",
            component="main_window",
            action="on_recording_enabled_change",
            recording_enabled=bool(enabled),
        )

    def on_cooldown_change(self, value):
        """Handle motion cooldown slider change."""
        cooldown = value / 10.0  # Convert to seconds (0.1 to 10.0)
        self.cooldown_value_label.setText(f"{cooldown:.1f}s")
        session = self._focused_session()
        if session:
            session.profile["motion_cooldown"] = cooldown
            session.detector.set_motion_cooldown(cooldown)
            self.camera_profiles[session.camera_id] = session.profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)

    def on_draw_zone_toggled(self, checked: bool) -> None:
        self.is_drawing_zone = checked
        if checked:
            self.is_drawing_line = False
            self.draw_line_btn.setChecked(False)
            self.pending_zone_points = []
            self.status_label.setText(
                "Zone draw mode: left-click points, right-click to finish polygon"
            )
        else:
            self.pending_zone_points = []

    def on_draw_line_toggled(self, checked: bool) -> None:
        self.is_drawing_line = checked
        if checked:
            self.is_drawing_zone = False
            self.draw_zone_btn.setChecked(False)
            self.pending_line_points = []
            self.status_label.setText("Line draw mode: click two points to set entry line")
        else:
            self.pending_line_points = []

    def commit_pending_zone(self) -> None:
        if len(self.pending_zone_points) < 3:
            self.pending_zone_points = []
            return
        session = self._focused_session()
        if not session:
            self.pending_zone_points = []
            return
        zones = session.profile.get("active_zones", [])
        zones.append(self.pending_zone_points.copy())
        session.profile["active_zones"] = zones
        session.detector.set_active_zones(zones)
        self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)
        self.pending_zone_points = []
        self.status_label.setText(f"Active zones: {len(zones)}")

    def commit_pending_line(self) -> None:
        if len(self.pending_line_points) != 2:
            self.pending_line_points = []
            return
        session = self._focused_session()
        if not session:
            self.pending_line_points = []
            return
        lines = session.profile.get("entry_lines", [])
        lines.append(self.pending_line_points.copy())
        session.profile["entry_lines"] = lines
        session.detector.set_entry_lines(lines)
        self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)
        self.pending_line_points = []
        self.status_label.setText(f"Entry lines: {len(lines)}")

    def clear_active_zones(self) -> None:
        session = self._focused_session()
        if not session:
            return
        session.profile["active_zones"] = []
        session.detector.set_active_zones([])
        self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)
        self.pending_zone_points = []
        self.status_label.setText("Active zones cleared")

    def clear_entry_lines(self) -> None:
        session = self._focused_session()
        if not session:
            return
        session.profile["entry_lines"] = []
        session.detector.set_entry_lines([])
        self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)
        self.pending_line_points = []
        self.status_label.setText("Entry lines cleared")

    def on_entry_direction_change(self, value: str) -> None:
        session = self._focused_session()
        if session:
            session.profile["entry_direction"] = value
            session.detector.set_entry_direction(value)
            self.camera_profiles[session.camera_id] = session.profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)
        self.status_label.setText(f"Entry direction: {value}")

    def _focused_session(self) -> Optional[CameraSession]:
        if self.focused_camera_id and self.focused_camera_id in self.camera_sessions:
            return self.camera_sessions[self.focused_camera_id]
        return None

    def _apply_profile_to_controls(self, profile: dict) -> None:
        self.contour_slider.blockSignals(True)
        self.variance_slider.blockSignals(True)
        self.cooldown_slider.blockSignals(True)
        self.recording_enabled_cb.blockSignals(True)
        self.entry_direction_combo.blockSignals(True)
        self.night_bug_filter_cb.blockSignals(True)
        self.night_bug_start_spin.blockSignals(True)
        self.night_bug_end_spin.blockSignals(True)

        self.contour_slider.setValue(int(profile.get("min_contour_area", MIN_CONTOUR_AREA)))
        self.variance_slider.setValue(int(profile.get("var_threshold", VAR_THRESHOLD)))
        self.cooldown_slider.setValue(int(float(profile.get("motion_cooldown", MOTION_COOLDOWN)) * 10))
        self.recording_enabled_cb.setChecked(bool(profile.get("recording_enabled", True)))
        self.entry_direction_combo.setCurrentText(profile.get("entry_direction", "outside_to_inside"))
        self.night_bug_filter_cb.setChecked(bool(profile.get("night_bug_filter_enabled", True)))
        self.night_bug_start_spin.setValue(int(profile.get("night_bug_start_hour", 20)))
        self.night_bug_end_spin.setValue(int(profile.get("night_bug_end_hour", 6)))

        self.contour_slider.blockSignals(False)
        self.variance_slider.blockSignals(False)
        self.cooldown_slider.blockSignals(False)
        self.recording_enabled_cb.blockSignals(False)
        self.entry_direction_combo.blockSignals(False)
        self.night_bug_filter_cb.blockSignals(False)
        self.night_bug_start_spin.blockSignals(False)
        self.night_bug_end_spin.blockSignals(False)

        self.contour_value_label.setText(str(self.contour_slider.value()))
        self.variance_value_label.setText(str(self.variance_slider.value()))
        self.cooldown_value_label.setText(f"{self.cooldown_slider.value() / 10.0:.1f}s")

    def _apply_profile_to_detector(self, session: CameraSession) -> None:
        profile = session.profile
        detector = session.detector
        detector.set_cam_orientation(
            self.settings_manager.get("cam_orientation", "horizontal")
        )
        detector.min_contour_area = int(profile.get("min_contour_area", MIN_CONTOUR_AREA))
        detector.update_var_threshold(int(profile.get("var_threshold", VAR_THRESHOLD)))
        detector.recording_enabled = bool(profile.get("recording_enabled", True))
        detector.motion_cooldown = float(profile.get("motion_cooldown", MOTION_COOLDOWN))
        detector.set_active_zones(profile.get("active_zones", []))
        detector.set_entry_lines(profile.get("entry_lines", []))
        detector.set_entry_direction(profile.get("entry_direction", "outside_to_inside"))
        detector.set_night_bug_filter(
            bool(profile.get("night_bug_filter_enabled", True)),
            int(profile.get("night_bug_start_hour", 20)),
            int(profile.get("night_bug_end_hour", 6)),
        )

    def update_display_mode_button_text(self) -> None:
        """Refresh display mode toggle button text."""
        if self.display_overlay_mode == "raw":
            self.display_mode_btn.setText("Raw View")
        else:
            self.display_mode_btn.setText("Overlay View")

    def toggle_display_mode(self) -> None:
        """Toggle between overlay and raw display modes."""
        value = "raw" if self.display_overlay_mode == "overlay" else "overlay"
        self.display_overlay_mode = value
        self.settings_manager.set("display_overlay_mode", value)
        self.update_display_mode_button_text()
        if value == "raw":
            self.status_label.setText("Raw view enabled (detection still active)")
        else:
            self.status_label.setText("Overlay view enabled")

    def on_auto_reconnect_toggled(self, enabled: bool) -> None:
        self.auto_reconnect_enabled = enabled
        self.settings_manager.set("auto_reconnect_enabled", enabled)
        if not enabled:
            self.reconnect_timer.stop()

    def on_reconnect_interval_change(self, value: int) -> None:
        self.reconnect_interval_seconds = value
        self.settings_manager.set("reconnect_interval_seconds", value)
        if self.reconnect_timer.isActive():
            self.reconnect_timer.start(int(self.reconnect_interval_seconds * 1000))

    def _feed_timer_interval_ms(self) -> int:
        return max(1, int(1000 / max(1, self.feed_fps)))

    def on_feed_fps_changed(self, value: int) -> None:
        self.feed_fps = max(FEED_FPS_MIN, min(FEED_FPS_MAX, int(value)))
        self.settings_manager.set("feed_fps", self.feed_fps)
        if self.timer.isActive():
            self.timer.setInterval(self._feed_timer_interval_ms())

    def _build_telegram_status_message(self, chat_id: str) -> str:
        remaining = self.telegram_notifier.remaining_pause_minutes(chat_id)
        lines = ["Sentinel status"]
        if remaining > 0:
            lines.append(f"Pause: {remaining} minute(s) left for this chat")
        else:
            lines.append("Pause: not paused")
        lines.append(
            f"Video recording (motion): {'on' if self.recording_enabled_cb.isChecked() else 'off'}"
        )
        lines.append(
            f"Telegram motion alerts: {'on' if self.telegram_motion_alerts_cb.isChecked() else 'off'}"
        )
        lines.append(
            f"Motion clip uploads (Telegram): {'on' if self.telegram_include_video_cb.isChecked() else 'off'}"
        )
        lines.append(f"Night bug filter: {'on' if self.night_bug_filter_cb.isChecked() else 'off'}")
        lines.append(f"Auto reconnect: {'on' if self.auto_reconnect_cb.isChecked() else 'off'}")
        lines.append(f"Alert cooldown: {self.telegram_cooldown_spin.value()}s")
        return "\n".join(lines)

    def _apply_night_bug_enabled_all_sessions(self, enabled: bool) -> None:
        start_hour = int(self.night_bug_start_spin.value())
        end_hour = int(self.night_bug_end_spin.value())
        self.settings_manager.set("night_bug_filter_enabled", enabled)
        self.night_bug_filter_cb.blockSignals(True)
        self.night_bug_filter_cb.setChecked(enabled)
        self.night_bug_filter_cb.blockSignals(False)
        for session in self.camera_sessions.values():
            session.profile["night_bug_filter_enabled"] = enabled
            session.profile["night_bug_start_hour"] = start_hour
            session.profile["night_bug_end_hour"] = end_hour
            session.detector.set_night_bug_filter(enabled, start_hour, end_hour)
            self.camera_profiles[session.camera_id] = session.profile
        self.settings_manager.set("camera_profiles", self.camera_profiles)

    def _apply_telegram_remote_features(self, chat_id: str, changes: Dict[str, bool]) -> None:
        lines_applied: List[str] = []
        for key, val in changes.items():
            if key == "recording":
                self.recording_enabled_cb.blockSignals(True)
                self.recording_enabled_cb.setChecked(val)
                self.recording_enabled_cb.blockSignals(False)
                self.on_recording_enabled_change(val)
                lines_applied.append(f"Recording: {'on' if val else 'off'}")
            elif key == "messages":
                self.telegram_motion_alerts_cb.blockSignals(True)
                self.telegram_motion_alerts_cb.setChecked(val)
                self.telegram_motion_alerts_cb.blockSignals(False)
                self.settings_manager.set("telegram_motion_alerts", val)
                lines_applied.append(f"Telegram motion alerts: {'on' if val else 'off'}")
            elif key == "clips":
                self.telegram_include_video_cb.blockSignals(True)
                self.telegram_include_video_cb.setChecked(val)
                self.telegram_include_video_cb.blockSignals(False)
                self.on_telegram_include_video_toggled(val)
                lines_applied.append(f"Motion clip uploads: {'on' if val else 'off'}")
            elif key == "night_bug":
                self._apply_night_bug_enabled_all_sessions(val)
                lines_applied.append(f"Night bug filter: {'on' if val else 'off'}")
            elif key == "reconnect":
                self.auto_reconnect_cb.blockSignals(True)
                self.auto_reconnect_cb.setChecked(val)
                self.auto_reconnect_cb.blockSignals(False)
                self.on_auto_reconnect_toggled(val)
                lines_applied.append(f"Auto reconnect: {'on' if val else 'off'}")
        summary = (
            "Applied:\n" + "\n".join(lines_applied)
            if lines_applied
            else "No changes applied."
        )
        self.telegram_notifier.reply_to_chat(chat_id, summary)

    def update_feed(self):
        """Update all active camera feeds and render focused/grid views."""
        for chat_id in self.telegram_notifier.consume_detailed_status_requests():
            self.telegram_notifier.reply_to_chat(
                chat_id, self._build_telegram_status_message(chat_id)
            )
        for chat_id, changes in self.telegram_notifier.consume_remote_feature_commands():
            self._apply_telegram_remote_features(chat_id, changes)

        requested_clip_chat_ids = self.telegram_notifier.consume_clip_requests()

        if not self.camera_sessions:
            for cid in dict.fromkeys(requested_clip_chat_ids):
                self.telegram_notifier.reply_to_chat(
                    cid,
                    "Sentinel: no camera connected; clip not recorded.",
                )
            return
        alive_sessions: Dict[str, CameraSession] = {}
        for camera_id, session in list(self.camera_sessions.items()):
            if session.worker:
                # Thread-safe retrieval of ALL state
                (
                    frame,
                    raw_frame,
                    motion_rects,
                    motion_detected,
                    recording_status,
                    error_msg,
                    is_active,
                ) = session.worker.get_latest_results()
            else:
                # Fallback for synchronous startup (rare)
                frame, raw_frame, motion_rects, motion_detected, recording_status = session.detector.detect_motion()
                error_msg = session.detector.last_error_message
                is_active = session.detector.feed_active
            
            if frame is None:
                if is_active:
                    # Connection is alive but stuttering; dim the last frame to show it's "frozen/stale"
                    if session.last_frame is not None:
                        stale_frame = session.last_frame.copy()
                        # Dim the frame (multiply by 0.5)
                        stale_frame = (stale_frame.astype(np.float32) * 0.5).astype(np.uint8)
                        cv2.putText(
                            stale_frame,
                            "SIGNAL STUTTER / CONNECTING...",
                            (20, 50),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.8,
                            (0, 255, 255),
                            2,
                        )
                        session.last_frame = stale_frame
                    
                    alive_sessions[camera_id] = session
                    continue
                
                # Camera has dropped and stopped itself
                self.status_label.setText(f"{session.display_name}: stream dropped")
                app_logger.log(
                    "error",
                    "camera_stream_dropped",
                    camera_id=session.camera_id,
                    camera_ip=session.host or self._extract_host(session.source) or "unknown",
                    camera_display=session.display_name,
                    component="main_window",
                    action="update_feed",
                    error_message=error_msg or "stream_dropped",
                )
                # Properly clean up the leaked thread, then retry just this camera
                self.disconnect_camera(user_requested=False, session_id=camera_id)
                self.start_reconnect_timer()
                continue

            display_frame = frame
            if self.display_overlay_mode == "raw" and raw_frame is not None:
                display_frame = raw_frame

            if self.display_overlay_mode == "overlay":
                if self.view_mode == "grid":
                    display_frame = self._draw_profile_zones_lines(
                        display_frame, session.profile
                    )
                    if camera_id == self.focused_camera_id:
                        display_frame = self._draw_pending_zone_line_guides(display_frame)
                elif self.focused_camera_id == camera_id:
                    display_frame = self.draw_zone_line_overlays(display_frame)
            if self.focused_camera_id == camera_id and self.show_detection_size_preview:
                if time.time() <= self.detection_size_preview_until:
                    display_frame = self.draw_detection_size_preview(display_frame)
                else:
                    self.show_detection_size_preview = False

            session.last_frame = display_frame
            alive_sessions[camera_id] = session

            if self.frame_broker is not None and self.web_enabled:
                pub = (
                    raw_frame
                    if self.web_published_frame == "raw" and raw_frame is not None
                    else display_frame
                )
                wcfg = self.settings_manager.settings.get("web", {})
                q = int(wcfg.get("jpeg_quality", 80))
                self.frame_broker.publish(camera_id, session.display_name, pub, jpeg_quality=q)

            if recording_status == "started" and self.focused_camera_id == camera_id:
                self.status_label.setText(f"{session.display_name}: recording started")

            if (
                requested_clip_chat_ids
                and not session.detector.recording
                and session.detector.recording_enabled
            ):
                h, w = frame.shape[:2]
                session.pending_clip_request_chat_ids.extend(requested_clip_chat_ids)
                session.pending_clip_request_chat_ids = list(
                    dict.fromkeys(session.pending_clip_request_chat_ids)
                )
                session.detector.start_recording((w, h), reason="telegram_clip_request")
            elif requested_clip_chat_ids and not session.detector.recording_enabled:
                app_logger.log(
                    "info",
                    "recording_skip_telegram_clip_request",
                    camera_id=session.camera_id,
                    camera_ip=session.host or self._extract_host(session.source) or "unknown",
                    camera_display=session.display_name,
                    component="main_window",
                    action="update_feed",
                    recording_enabled=False,
                )

            completed_video_path, completed_video_reason = (
                session.detector.consume_last_completed_recording()
            )

            if motion_detected:
                self.audio_system.trigger_alert()

            if completed_video_path:
                target_chat_ids = None
                message = f"Sentinel: motion detected on {session.display_name}."
                force_video = False
                bypass_cooldown = False
                motion_alerts_on = self.telegram_motion_alerts_cb.isChecked()
                if completed_video_reason == "telegram_clip_request":
                    message = f"Sentinel: requested 10-second clip from {session.display_name}."
                    target_chat_ids = session.pending_clip_request_chat_ids or None
                    force_video = True
                    bypass_cooldown = True
                    session.pending_clip_request_chat_ids = []
                elif not motion_alerts_on:
                    message = ""
                elif not self.telegram_notifier.include_video:
                    message = ""

                if message:
                    self.telegram_notifier.trigger_alert(
                        message=message,
                        video_path=completed_video_path,
                        target_chat_ids=target_chat_ids,
                        force_video=force_video,
                        bypass_cooldown=bypass_cooldown,
                    )
            elif (
                self.telegram_motion_alerts_cb.isChecked()
                and not self.telegram_notifier.include_video
                and motion_detected
            ):
                self.telegram_notifier.trigger_alert(
                    message=f"Sentinel: motion detected on {session.display_name}."
                )

        self.camera_sessions = alive_sessions
        if not self.camera_sessions:
            if self.frame_broker is not None and self.web_enabled:
                self.frame_broker.clear()
            self.timer.stop()
            self.show_blank_frame()
            self.toggle_btn.setText("Camera Offline")
            self.start_reconnect_timer()
            return

        if not self.focused_camera_id or self.focused_camera_id not in self.camera_sessions:
            self.focused_camera_id = next(iter(self.camera_sessions.keys()))

        focused = self.camera_sessions.get(self.focused_camera_id)
        if focused and focused.last_frame is not None and self.view_mode == "focus":
            self.current_frame_size = (focused.last_frame.shape[1], focused.last_frame.shape[0])
            self.display_frame(focused.last_frame)

        if self.view_mode == "grid":
            self._ensure_grid_tiles()
            for session in self.camera_sessions.values():
                if session.tile_label and session.last_frame is not None:
                    self._display_frame_in_label(session.last_frame, session.tile_label)

    def capture_image(self) -> None:
        """Capture and save an image from the camera."""
        session = self._focused_session()
        if not session:
            self.status_label.setText("Capture Failed: No focused camera")
            return
        result = session.detector.capture_image(
            orientation=self.settings_manager.get("cam_orientation", "horizontal")
        )

        if not result:
            self.status_label.setText("Capture Failed: Camera Offline or Error")
            return

        frame, filename = result

        # Save the image
        try:
            cv2.imwrite(filename, frame)
            self.status_label.setText(f"Image Saved: {os.path.basename(filename)}")

            # Display the captured frame
            self.display_frame(frame)
        except Exception as e:
            self.status_label.setText(f"Error Saving Image: {str(e)}")

    def recall_image(self) -> None:
        """Open a file dialog to select and display a captured image."""
        try:
            # Create the captures directory if it doesn't exist
            if not os.path.exists(CAPTURE_DIR):
                os.makedirs(CAPTURE_DIR)
                self.status_label.setText("No images found. Capture an image first.")
                return

            # Check if the directory has any images
            image_files = [f for f in os.listdir(CAPTURE_DIR) if f.endswith((".png", ".jpg", ".jpeg"))]
            if not image_files:
                self.status_label.setText("No images found. Capture an image first.")
                return

            # Open file dialog in the captures directory
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Image to Recall",
                CAPTURE_DIR,
                "Images (*.png *.jpg *.jpeg)"
            )

            if not file_path:  # User canceled the dialog
                return

            # Load and display the selected image
            frame = cv2.imread(file_path)

            if frame is None:
                self.status_label.setText(f"Error Loading Image: {os.path.basename(file_path)}")
                return

            self.display_frame(frame)
            self.status_label.setText(f"Recalled: {os.path.basename(file_path)}")
        except Exception as e:
            self.status_label.setText(f"Error Recalling Image: {str(e)}")

    def browse_recordings(self):
        """Open file dialog to browse and open recorded videos."""
        try:
            # Create the videos directory if it doesn't exist
            if not os.path.exists(VIDEO_DIR):
                os.makedirs(VIDEO_DIR)
                self.status_label.setText("No videos found.")
                return

            # Check if the directory (or its per-day folders) has any videos
            has_videos = any(
                name.endswith((".avi", ".mp4"))
                for _, _, names in os.walk(VIDEO_DIR)
                for name in names
            )
            if not has_videos:
                self.status_label.setText("No videos found.")
                return

            # Open file dialog in the videos directory
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Select Video to Open", VIDEO_DIR, "Videos (*.avi *.mp4)"
            )

            if not file_path:  # User canceled the dialog
                return

            # Open the video with the default system video player
            if sys.platform.startswith("win"):
                os.startfile(file_path)
            elif sys.platform.startswith("darwin"):  # macOS
                subprocess.call(["open", file_path])
            else:  # Linux
                subprocess.call(["xdg-open", file_path])

            self.status_label.setText(f"Opened: {os.path.basename(file_path)}")
        except Exception as e:
            self.status_label.setText(f"Error opening video: {str(e)}")

    def open_multiview(self) -> None:
        """Toggle embedded grid mode on/off."""
        if not self.camera_sessions:
            QMessageBox.information(
                self,
                "No Cameras Connected",
                "Connect at least one camera first.",
            )
            return
        if self.view_mode == "grid":
            self.show_focus_view()
            return
        self.show_grid_view()

    def on_contour_size_change(self, value):
        """Handle minimum contour area slider change."""
        self.contour_value_label.setText(f"{value}")
        session = self._focused_session()
        if session:
            session.profile["min_contour_area"] = value
            session.detector.set_min_contour_area(value)
            self.camera_profiles[session.camera_id] = session.profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)

    def on_contour_slider_pressed(self) -> None:
        """Hide preview while slider is actively dragged."""
        self.detection_size_release_timer.stop()
        self.show_detection_size_preview = False

    def on_contour_slider_released(self) -> None:
        """After 1s dwell, show detection-size preview for 5 seconds."""
        self.detection_size_release_timer.start(1000)

    def start_detection_size_preview(self) -> None:
        """Enable minimum detection size overlay preview."""
        self.detection_size_preview_value = int(self.contour_slider.value())
        self.show_detection_size_preview = True
        self.detection_size_preview_until = time.time() + 5.0

    def draw_detection_size_preview(self, frame: np.ndarray) -> np.ndarray:
        """Draw centered box representing current minimum contour area."""
        draw = frame.copy()
        frame_h, frame_w = draw.shape[:2]
        area = max(100, int(self.detection_size_preview_value))
        side = int(np.sqrt(area))
        side = max(12, min(side, min(frame_w, frame_h) // 2))
        center_x, center_y = frame_w // 2, frame_h // 2
        x1 = center_x - side // 2
        y1 = center_y - side // 2
        x2 = center_x + side // 2
        y2 = center_y + side // 2

        cv2.rectangle(draw, (x1, y1), (x2, y2), (0, 255, 255), 2)
        cv2.putText(
            draw,
            f"Min detection size: {self.detection_size_preview_value}",
            (max(10, x1 - 30), max(30, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 255),
            2,
        )
        return draw

    def on_variance_change(self, value):
        """Handle variance threshold slider change."""
        self.variance_value_label.setText(f"{value}")
        session = self._focused_session()
        if session:
            session.profile["var_threshold"] = value
            session.detector.fgbg = cv2.createBackgroundSubtractorMOG2(
                history=HISTORY_LENGTH, varThreshold=value, detectShadows=DETECT_SHADOWS
            )
            self.camera_profiles[session.camera_id] = session.profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)

    def _apply_night_bug_filter_settings(self) -> None:
        """Apply night bug filter settings to detector."""
        enabled = self.night_bug_filter_cb.isChecked()
        start_hour = int(self.night_bug_start_spin.value())
        end_hour = int(self.night_bug_end_spin.value())
        session = self._focused_session()
        if session:
            session.profile["night_bug_filter_enabled"] = enabled
            session.profile["night_bug_start_hour"] = start_hour
            session.profile["night_bug_end_hour"] = end_hour
            session.detector.set_night_bug_filter(enabled, start_hour, end_hour)
            self.camera_profiles[session.camera_id] = session.profile
            self.settings_manager.set("camera_profiles", self.camera_profiles)

    def on_night_bug_filter_toggled(self, enabled: bool) -> None:
        self._apply_night_bug_filter_settings()
        status = "enabled" if enabled else "disabled"
        self.status_label.setText(f"Night bug filter {status}")

    def on_night_bug_schedule_changed(self) -> None:
        start_hour = int(self.night_bug_start_spin.value())
        end_hour = int(self.night_bug_end_spin.value())
        self._apply_night_bug_filter_settings()
        self.status_label.setText(f"Night bug filter schedule: {start_hour:02d}:00-{end_hour:02d}:00")

    def on_camera_select(self, value: str) -> None:
        """
        Handle camera selection change.

        Args:
            value: The selected camera text
        """
        if value != "No Cameras Found" and value in self.camera_sources:
            source = self.camera_sources[value]
            if isinstance(source, str) and source.startswith("pending://"):
                self.settings_manager.set("camera_display", value)
                return
            camera_id = str(source)
            if camera_id in self.camera_sessions:
                if self.view_mode == "grid":
                    self.activate_camera_for_grid(camera_id)
                else:
                    self.focus_camera(camera_id)
                self.toggle_btn.setText("Disconnect Selected")
            else:
                self.toggle_btn.setText("Connect Selected")
            if isinstance(source, int):
                self.settings_manager.set("camera_index", source)
            self.settings_manager.set("camera_source", str(source))
            self.settings_manager.set("camera_display", value)

    def on_prefer_substream_toggled(self, enabled: bool) -> None:
        self.prefer_substream = enabled
        self.settings_manager.set("prefer_substream", enabled)
        mode = "sub-stream" if enabled else "main stream"
        self.status_label.setText(f"Network discovery now prefers {mode}")

    def toggle_main_fullscreen(self) -> None:
        """Toggle full-screen mode for the primary camera view."""
        if not self.is_main_fullscreen:
            self.scroll_area.hide()
            self.showFullScreen()
            self.is_main_fullscreen = True
            self.fullscreen_btn.setText("Exit Full Screen")
            return

        self.showNormal()
        self.scroll_area.show()
        self.is_main_fullscreen = False
        self.fullscreen_btn.setText("Enter Full Screen")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle fullscreen shortcuts."""
        if event.key() == Qt.Key_F11:
            self.toggle_main_fullscreen()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self.is_main_fullscreen:
            self.toggle_main_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def on_speed_change(self, value: int) -> None:
        """
        Handle speech speed slider change.

        Args:
            value: The new speech speed value
        """
        self.settings_manager.set("speech_speed", value)
        self.audio_system.update_settings({"speech_speed": value})

    def on_volume_change(self, value: int) -> None:
        """
        Handle volume slider change.

        Args:
            value: The new volume value (0-10)
        """
        volume = value / 10.0
        self.settings_manager.set("volume", volume)
        self.audio_system.update_settings({"volume": volume})
        self.status_label.setText(f"Volume: {int(volume * 100)}%")
        app_logger.log(
            "info",
            "volume_changed",
            component="main_window",
            action="on_volume_change",
            volume=volume,
        )

    def _extract_host(self, source: object) -> str:
        """Extract camera host/IP from integer index or URL-like source."""
        if isinstance(source, int):
            return f"local-{source}"
        if not isinstance(source, str):
            return ""
        if source.startswith("pending://"):
            return source.split("pending://", 1)[1]
        if "://" in source:
            parsed = urlparse(source)
            return parsed.hostname or ""
        return ""

    def _current_telegram_settings(self) -> dict:
        """Build Telegram settings payload from current UI values."""
        token = self.telegram_token_input.text().strip()
        chat_ids = self.telegram_chat_id_input.text().strip()
        return {
            "telegram_enabled": self.telegram_enabled_cb.isChecked(),
            "telegram_bot_token": token,
            "telegram_chat_id": chat_ids,
            "telegram_chat_ids": chat_ids,
            "telegram_cooldown": float(self.telegram_cooldown_spin.value()),
            "telegram_motion_alerts": self.telegram_motion_alerts_cb.isChecked(),
            "telegram_include_video": self.telegram_include_video_cb.isChecked(),
        }

    def on_telegram_enabled_toggled(self, enabled: bool) -> None:
        self.settings_manager.set("telegram_enabled", enabled)
        self.telegram_notifier.update_settings({"telegram_enabled": enabled})
        if enabled and not self.telegram_notifier.is_configured():
            self.status_label.setText("Telegram enabled: add token + chat ID(s)")
        elif enabled:
            self.status_label.setText("Telegram notifications enabled")
        else:
            self.status_label.setText("Telegram notifications disabled")

    def on_telegram_config_changed(self) -> None:
        payload = self._current_telegram_settings()
        self.settings_manager.set("telegram_bot_token", payload["telegram_bot_token"])
        self.settings_manager.set("telegram_chat_id", payload["telegram_chat_id"])
        self.settings_manager.set("telegram_chat_ids", payload["telegram_chat_ids"])
        self.settings_manager.set(
            "telegram_motion_alerts", bool(payload["telegram_motion_alerts"])
        )
        self.telegram_notifier.update_settings(payload)

    def on_telegram_cooldown_changed(self, value: int) -> None:
        self.settings_manager.set("telegram_cooldown", float(value))
        self.telegram_notifier.update_settings({"telegram_cooldown": float(value)})

    def on_show_telegram_token_toggled(self, checked: bool) -> None:
        if checked:
            self.telegram_token_input.setEchoMode(QLineEdit.Normal)
        else:
            self.telegram_token_input.setEchoMode(QLineEdit.Password)

    def on_telegram_motion_alerts_toggled(self, enabled: bool) -> None:
        self.settings_manager.set("telegram_motion_alerts", bool(enabled))

    def on_telegram_include_video_toggled(self, enabled: bool) -> None:
        self.settings_manager.set("telegram_include_video", enabled)
        self.telegram_notifier.update_settings({"telegram_include_video": enabled})

    def on_send_telegram_test(self) -> None:
        self.on_telegram_config_changed()
        if not self.telegram_enabled_cb.isChecked():
            self.status_label.setText("Enable Telegram notifications first")
            return
        if not self.telegram_notifier.is_configured():
            self.status_label.setText("Telegram test failed: token/chat ID missing")
            return
        if self.telegram_notifier.send_test_message():
            self.status_label.setText("Telegram test sent")
        else:
            self.status_label.setText("Telegram test failed")

    def on_voice_change(self, idx: int, checked: bool) -> None:
        """
        Handle voice selection change.

        Args:
            idx: Index of the selected voice
            checked: Whether the button is checked
        """
        if checked and 0 <= idx < len(self.audio_system.voice_ids):
            voice_id = self.audio_system.voice_ids[idx]
            self.settings_manager.set("voice_id", voice_id)
            self.audio_system.update_settings({"voice_id": voice_id})

    def on_cam_orient_change(self) -> None:
        """Handle camera orientation change."""
        orientation = "horizontal" if self.horiz_btn.isChecked() else "vertical"
        self.settings_manager.set("cam_orientation", orientation)
        for session in self.camera_sessions.values():
            session.detector.set_cam_orientation(orientation)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """
        Handle window resize events.
        We intentionally do NOT save dimensions here — resizeEvent fires during
        window creation with unreliable values (e.g. virtual desktop height).
        Dimensions are saved once in closeEvent instead.

        Args:
            event: The resize event
        """
        super().resizeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        """
        Handle window close events for cleanup.

        Args:
            event: The close event
        """
        # Clean up resources
        self.reconnect_timer.stop()
        self.telegram_notifier.cleanup()
        self.timer.stop()
        for session in list(self.camera_sessions.values()):
            if session.worker:
                session.worker.stop()
            if session.thread:
                session.thread.quit()
                # Again, no .wait() to ensure immediate UI exit
            session.detector.cleanup()

        # Save window dimensions here (not in resizeEvent) to get reliable values
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 0.9)
        max_h = int(screen.height() * 0.9)
        self.settings_manager.set("window_size", [
            min(self.width(), max_w),
            min(self.height(), max_h),
        ])
        self.settings_manager.set(
            "feed_size", [self.feed_container.width(), self.feed_container.height()]
        )
        self.settings_manager.set("feed_active", False)

        # Save final settings
        self.settings_manager.save_settings()

        event.accept()
