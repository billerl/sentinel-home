"""
Multi-camera grid view window.
"""

from __future__ import annotations

from typing import List, Tuple

import cv2
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QPushButton,
    QWidget,
    QSizePolicy,
)


class ClickableLabel(QLabel):
    """QLabel that emits a signal when clicked."""

    clicked = Signal(int)

    def __init__(self, index: int, text: str, parent=None):
        super().__init__(text, parent)
        self.index = index

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)


class MultiViewWindow(QDialog):
    """Shows a grid of camera streams."""

    def __init__(self, sources: List[Tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sentinel Home - MultiView")
        self.resize(1000, 700)
        self.sources = sources
        self.captures = []
        self.labels: List[QLabel] = []
        self.focused_index = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_streams)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.return_btn = QPushButton("Return to MultiView")
        self.return_btn.clicked.connect(self.return_to_grid)
        self.return_btn.hide()
        root.addWidget(self.return_btn)

        self.grid_widget = QWidget()
        self.grid_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        root.addWidget(self.grid_widget, stretch=1)

        self.focus_label = QLabel("No stream selected")
        self.focus_label.setAlignment(Qt.AlignCenter)
        self.focus_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.focus_label.setStyleSheet("background-color: black; color: #00FFFF;")
        self.focus_label.hide()
        root.addWidget(self.focus_label, stretch=1)

        columns = 2
        rows = (len(self.sources) + columns - 1) // columns if self.sources else 1
        for i, (name, source) in enumerate(self.sources):
            label = ClickableLabel(i, f"{name}\nConnecting...")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background-color: black; color: #00FFFF;")
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            label.setMinimumSize(120, 90)
            label.clicked.connect(self.focus_stream)
            self.grid.addWidget(label, i // columns, i % columns)
            self.labels.append(label)
            self.captures.append(cv2.VideoCapture(source))
        for r in range(rows):
            self.grid.setRowStretch(r, 1)
        for c in range(columns):
            self.grid.setColumnStretch(c, 1)

        self.timer.start(120)

    def focus_stream(self, index: int) -> None:
        """Switch from grid mode to focused single-stream mode."""
        self.focused_index = index
        self.grid_widget.hide()
        self.focus_label.show()
        self.return_btn.show()

    def return_to_grid(self) -> None:
        """Return to multi-camera grid mode."""
        self.focused_index = None
        self.focus_label.hide()
        self.return_btn.hide()
        self.grid_widget.show()

    def update_streams(self) -> None:
        for i, cap in enumerate(self.captures):
            if i >= len(self.labels):
                continue

            label = self.labels[i]
            if not cap or not cap.isOpened():
                label.setText("Camera Offline")
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                if self.focused_index == i:
                    self.focus_label.setText("No Frame")
                else:
                    label.setText("No Frame")
                continue

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            q_image = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(q_image)
            label_sz = (
                self.focus_label.size()
                if self.focused_index == i
                else label.size()
            )
            if label_sz.width() <= 0 or label_sz.height() <= 0:
                continue
            scaled = pixmap.scaled(
                label_sz,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            if self.focused_index == i:
                self.focus_label.setPixmap(scaled)
            elif self.focused_index is None:
                label.setPixmap(scaled)

    def closeEvent(self, event):
        self.timer.stop()
        for cap in self.captures:
            if cap:
                cap.release()
        super().closeEvent(event)
