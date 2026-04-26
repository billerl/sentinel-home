"""
Main window for the Sentinel Home application.
"""

import sys
import os
import cv2
import numpy as np
import subprocess  # Add this for opening videos with system player
from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
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
from PySide6.QtCore import Qt, QTimer, QPoint

from sentinel.constants import (
    CONTROL_PANEL_WIDTH,
    FRAME_INTERVAL,
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
from sentinel.detection import MotionDetector, NetworkCameraDiscovery
from sentinel.audio import AudioAlertSystem
from sentinel.notifications import TelegramNotifier
from sentinel.settings import SettingsManager
from sentinel.ui.multiview_window import MultiViewWindow


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
    ):
        super().__init__()

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

        # Store component references
        self.settings_manager = settings_manager
        self.motion_detector = motion_detector
        self.audio_system = audio_system
        self.telegram_notifier = telegram_notifier
        self.network_discovery = NetworkCameraDiscovery()
        self.camera_sources = {}
        self.discovered_network_sources = []
        self.multiview_window = None
        self.pending_clip_request_chat_ids: List[str] = []
        self.prefer_substream = self.settings_manager.get("prefer_substream", True)
        self.current_frame_size: Tuple[int, int] = (640, 480)
        self.last_display_pixmap_size: Tuple[int, int] = (1, 1)
        self.last_display_offset: Tuple[int, int] = (0, 0)
        self.is_drawing_zone = False
        self.is_drawing_line = False
        self.pending_zone_points: List[List[float]] = []
        self.pending_line_points: List[List[float]] = []
        self.auto_reconnect_enabled = self.settings_manager.get(
            "auto_reconnect_enabled", AUTO_RECONNECT_DEFAULT
        )
        self.reconnect_interval_seconds = self.settings_manager.get(
            "reconnect_interval_seconds", RECONNECT_INTERVAL_SECONDS
        )
        self.display_overlay_mode = self.settings_manager.get(
            "display_overlay_mode", DEFAULT_DISPLAY_OVERLAY_MODE
        )
        self.is_main_fullscreen = False

        # Set up timer for camera feed updates
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_feed)
        self.reconnect_timer = QTimer()
        self.reconnect_timer.timeout.connect(self.try_reconnect)

        # Set up UI
        self.setup_ui()
        self.cooldown_slider.setEnabled(motion_detector.recording_enabled)

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
        from PySide6.QtWidgets import QSizePolicy
        self.camera_feed = QLabel()
        self.camera_feed.setAlignment(Qt.AlignCenter)
        self.camera_feed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.camera_feed.setStyleSheet("background-color: black;")
        self.camera_feed.setMouseTracking(True)

        # Set size from settings
        feed_size = self.settings_manager.get("feed_size", (640, 480))
        self.camera_feed.setMinimumSize(320, 240)  # Minimum size
        self.camera_feed.resize(feed_size[0], feed_size[1])

        self.main_layout.addWidget(self.camera_feed, stretch=1)

    def search_cameras(self) -> None:
        """Search for available cameras and update the dropdown."""
        camera_list = []
        self.camera_sources = {}

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
        )
        for endpoint in self.discovered_network_sources:
            camera_list.append(endpoint.label)
            self.camera_sources[endpoint.label] = endpoint.source

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

        self.status_label.setText(f"Camera: {self.camera_combo.currentText()} (Disconnected)")

    def toggle_feed(self) -> None:
        """Toggle the camera feed on/off."""
        if not self.motion_detector.feed_active:
            if self.camera_combo.currentText() == "No Cameras Found":
                self.status_label.setText("Camera: None Available")
                return
            selected_display = self.camera_combo.currentText()
            selected_source = self.camera_sources.get(selected_display)
            if selected_source is None:
                self.status_label.setText("Camera: Invalid Source")
                return
            if not self.connect_camera_source(selected_source, selected_display):
                self.status_label.setText(f"{selected_display}: Failed to Connect")
                self.start_reconnect_timer()
        else:
            self.disconnect_camera()
            self.show_blank_frame()

    def connect_camera_source(self, source, display_name: str) -> bool:
        """Connect selected camera and update UI/settings."""
        if self.motion_detector.start_camera(source):
            self.timer.start(FRAME_INTERVAL)
            self.reconnect_timer.stop()
            self.toggle_btn.setText("Camera Online")
            self.status_label.setText(f"{display_name}: Connected")
            self.search_btn.setEnabled(False)
            self.search_btn.setProperty("inactive", True)
            self.search_btn.setStyleSheet(
                "background-color: #555555; color: #999999; border: 1px solid #444444;"
            )
            self.recall_btn.setStyleSheet(
                "background-color: #555555; color: #999999; border: 1px solid #444444;"
            )
            self.recall_btn.setEnabled(False)
            self.recall_btn.setProperty("inactive", True)
            self.settings_manager.set("camera_index", source if isinstance(source, int) else 0)
            self.settings_manager.set("camera_source", str(source))
            self.settings_manager.set("camera_display", display_name)
            self.settings_manager.set("feed_active", True)
            return True
        return False

    def disconnect_camera(self, user_requested: bool = True) -> None:
        """Disconnect camera and restore idle UI state."""
        self.motion_detector.stop_camera()
        self.timer.stop()
        self.toggle_btn.setText("Camera Offline")
        if user_requested:
            self.reconnect_timer.stop()
            self.settings_manager.set("feed_active", False)
        self.status_label.setText(
            f"{self.settings_manager.get('camera_display', 'Camera')}: Disconnected"
        )
        self.search_btn.setEnabled(True)
        self.search_btn.setProperty("inactive", False)
        self.search_btn.setStyleSheet("")
        self.recall_btn.setEnabled(True)
        self.recall_btn.setProperty("inactive", False)
        self.recall_btn.setStyleSheet("")

    def start_reconnect_timer(self) -> None:
        """Start retry timer when reconnect is enabled."""
        if not self.auto_reconnect_enabled:
            return
        if not self.reconnect_timer.isActive():
            self.reconnect_timer.start(int(self.reconnect_interval_seconds * 1000))

    def try_reconnect(self) -> None:
        """Attempt reconnect to saved camera source."""
        saved_source = self.settings_manager.get("camera_source", "")
        if not saved_source:
            self.reconnect_timer.stop()
            return
        source = self._coerce_source(saved_source)
        display = self.settings_manager.get("camera_display", f"Camera {saved_source}")
        if self.connect_camera_source(source, display):
            self.status_label.setText(f"{display}: Reconnected")
            return
        self.status_label.setText(f"{display}: Reconnecting...")

    def _coerce_source(self, source_value: str):
        """Convert saved source into int index when possible."""
        if isinstance(source_value, int):
            return source_value
        if isinstance(source_value, str) and source_value.isdigit():
            return int(source_value)
        return source_value

    def start_initial_camera_if_configured(self) -> None:
        """Attempt startup connect and begin retries if needed."""
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

    def draw_zone_line_overlays(self, frame: np.ndarray) -> np.ndarray:
        """Draw configured zones/lines and in-progress drawing guides."""
        draw = frame.copy()
        height, width = draw.shape[:2]

        for zone in self.settings_manager.get("active_zones", []):
            points = np.array(
                [[int(p[0] * width), int(p[1] * height)] for p in zone],
                dtype=np.int32,
            )
            if len(points) >= 3:
                cv2.polylines(draw, [points], True, (255, 255, 0), 2)

        for line in self.settings_manager.get("entry_lines", []):
            if len(line) != 2:
                continue
            p1 = (int(line[0][0] * width), int(line[0][1] * height))
            p2 = (int(line[1][0] * width), int(line[1][1] * height))
            cv2.line(draw, p1, p2, (255, 0, 255), 2)

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
        feed_pos = self.camera_feed.mapFrom(self, event.position().toPoint())
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
        self.settings_manager.set("recording_enabled", enabled)

        # Update cooldown slider enabled state
        self.cooldown_slider.setEnabled(enabled)

        # Update motion detector
        self.motion_detector.recording_enabled = enabled

        status = "enabled" if enabled else "disabled"
        self.status_label.setText(f"Recording {status}")

    def on_cooldown_change(self, value):
        """Handle motion cooldown slider change."""
        cooldown = value / 10.0  # Convert to seconds (0.1 to 10.0)
        self.settings_manager.set("motion_cooldown", cooldown)

        # Update the display label
        self.cooldown_value_label.setText(f"{cooldown:.1f}s")

        # Update motion detector
        self.motion_detector.motion_cooldown = cooldown

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
        zones = self.settings_manager.get("active_zones", [])
        zones.append(self.pending_zone_points.copy())
        self.settings_manager.set("active_zones", zones)
        self.motion_detector.set_active_zones(zones)
        self.pending_zone_points = []
        self.status_label.setText(f"Active zones: {len(zones)}")

    def commit_pending_line(self) -> None:
        if len(self.pending_line_points) != 2:
            self.pending_line_points = []
            return
        lines = self.settings_manager.get("entry_lines", [])
        lines.append(self.pending_line_points.copy())
        self.settings_manager.set("entry_lines", lines)
        self.motion_detector.set_entry_lines(lines)
        self.pending_line_points = []
        self.status_label.setText(f"Entry lines: {len(lines)}")

    def clear_active_zones(self) -> None:
        self.settings_manager.set("active_zones", [])
        self.motion_detector.set_active_zones([])
        self.pending_zone_points = []
        self.status_label.setText("Active zones cleared")

    def clear_entry_lines(self) -> None:
        self.settings_manager.set("entry_lines", [])
        self.motion_detector.set_entry_lines([])
        self.pending_line_points = []
        self.status_label.setText("Entry lines cleared")

    def on_entry_direction_change(self, value: str) -> None:
        self.settings_manager.set("entry_direction", value)
        self.motion_detector.set_entry_direction(value)
        self.status_label.setText(f"Entry direction: {value}")

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

    def update_feed(self):
        """Update the camera feed with motion detection."""
        # Get frame with motion detection and recording status
        frame, _, motion_detected, recording_status = self.motion_detector.detect_motion()

        if frame is None:
            # Treat occasional read/decode errors as transient. Only reconnect after
            # MotionDetector has already decided to stop the camera.
            if self.motion_detector.feed_active:
                self.status_label.setText("Camera frame read error. Waiting for stream recovery...")
                return

            self.status_label.setText("Camera stream dropped. Attempting reconnect...")
            self.disconnect_camera(user_requested=False)
            self.start_reconnect_timer()
            return

        raw_frame = self.motion_detector.get_last_raw_frame()
        display_frame = frame
        if self.display_overlay_mode == "raw" and raw_frame is not None:
            display_frame = raw_frame

        # Handle camera orientation
        if self.settings_manager.get("cam_orientation", "horizontal") == "vertical":
            display_frame = cv2.rotate(display_frame, cv2.ROTATE_90_CLOCKWISE)

        self.current_frame_size = (display_frame.shape[1], display_frame.shape[0])
        if self.display_overlay_mode == "overlay":
            display_frame = self.draw_zone_line_overlays(display_frame)

        # Update status based on recording state
        if recording_status == "started":
            self.status_label.setText("Recording started (10 seconds)")
        elif recording_status == "recording":
            # Don't update status during recording to avoid flickering
            pass

        # Handle on-demand Telegram clip requests.
        requested_clip_chat_ids = self.telegram_notifier.consume_clip_requests()
        if requested_clip_chat_ids:
            self.pending_clip_request_chat_ids.extend(requested_clip_chat_ids)
            # Preserve order, remove duplicates.
            self.pending_clip_request_chat_ids = list(
                dict.fromkeys(self.pending_clip_request_chat_ids)
            )
        if requested_clip_chat_ids and not self.motion_detector.recording:
            h, w = frame.shape[:2]
            self.motion_detector.start_recording((w, h), reason="telegram_clip_request")

        completed_video_path, completed_video_reason = (
            self.motion_detector.consume_last_completed_recording()
        )

        # Trigger audio alert immediately on motion.
        if motion_detected:
            self.audio_system.trigger_alert()

        # Telegram behavior:
        # - include_video ON: send one combined message when clip is complete.
        # - include_video OFF: send immediate motion text message.
        if completed_video_path:
            target_chat_ids = None
            message = "Sentinel: motion detected."
            force_video = False
            bypass_cooldown = False
            if completed_video_reason == "telegram_clip_request":
                message = "Sentinel: requested 10-second clip."
                target_chat_ids = self.pending_clip_request_chat_ids or None
                force_video = True
                bypass_cooldown = True
                self.pending_clip_request_chat_ids = []
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
        elif not self.telegram_notifier.include_video and motion_detected:
            self.telegram_notifier.trigger_alert(message="Sentinel: motion detected.")

        # Display the frame
        self.display_frame(display_frame)

    def capture_image(self) -> None:
        """Capture and save an image from the camera."""
        result = self.motion_detector.capture_image(
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

            # Check if the directory has any videos
            video_files = [f for f in os.listdir(VIDEO_DIR) if f.endswith((".avi", ".mp4"))]
            if not video_files:
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
        """Open a grid view for discovered network cameras."""
        if not self.discovered_network_sources:
            QMessageBox.information(
                self,
                "No Network Cameras",
                "No network streams are available yet. Run Search Cameras first.",
            )
            return

        sources = [(endpoint.label, endpoint.source) for endpoint in self.discovered_network_sources]
        self.multiview_window = MultiViewWindow(sources=sources, parent=self)
        self.multiview_window.show()

    def on_contour_size_change(self, value):
        """Handle minimum contour area slider change."""
        self.settings_manager.set("min_contour_area", value)
        self.contour_value_label.setText(f"{value}")
        self.motion_detector.min_contour_area = value

    def on_variance_change(self, value):
        """Handle variance threshold slider change."""
        self.settings_manager.set("var_threshold", value)
        self.variance_value_label.setText(f"{value}")

        # This requires recreating the background subtractor
        self.motion_detector.fgbg = cv2.createBackgroundSubtractorMOG2(
            history=HISTORY_LENGTH, varThreshold=value, detectShadows=DETECT_SHADOWS
        )

    def _apply_night_bug_filter_settings(self) -> None:
        """Apply night bug filter settings to detector."""
        enabled = self.night_bug_filter_cb.isChecked()
        start_hour = int(self.night_bug_start_spin.value())
        end_hour = int(self.night_bug_end_spin.value())
        self.motion_detector.set_night_bug_filter(enabled, start_hour, end_hour)

    def on_night_bug_filter_toggled(self, enabled: bool) -> None:
        self.settings_manager.set("night_bug_filter_enabled", enabled)
        self._apply_night_bug_filter_settings()
        status = "enabled" if enabled else "disabled"
        self.status_label.setText(f"Night bug filter {status}")

    def on_night_bug_schedule_changed(self) -> None:
        start_hour = int(self.night_bug_start_spin.value())
        end_hour = int(self.night_bug_end_spin.value())
        self.settings_manager.set("night_bug_start_hour", start_hour)
        self.settings_manager.set("night_bug_end_hour", end_hour)
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
        self.telegram_notifier.update_settings(payload)

    def on_telegram_cooldown_changed(self, value: int) -> None:
        self.settings_manager.set("telegram_cooldown", float(value))
        self.telegram_notifier.update_settings({"telegram_cooldown": float(value)})

    def on_show_telegram_token_toggled(self, checked: bool) -> None:
        if checked:
            self.telegram_token_input.setEchoMode(QLineEdit.Normal)
        else:
            self.telegram_token_input.setEchoMode(QLineEdit.Password)

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
        if self.motion_detector.feed_active:
            self.timer.stop()
            self.motion_detector.cleanup()

        # Save window dimensions here (not in resizeEvent) to get reliable values
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 0.9)
        max_h = int(screen.height() * 0.9)
        self.settings_manager.set("window_size", [
            min(self.width(), max_w),
            min(self.height(), max_h),
        ])
        self.settings_manager.set(
            "feed_size", [self.camera_feed.width(), self.camera_feed.height()]
        )
        self.settings_manager.set("feed_active", False)

        # Save final settings
        self.settings_manager.save_settings()

        event.accept()
