"""
Multi-camera grid view window.
"""

from __future__ import annotations

from typing import List, Tuple

import cv2
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QGridLayout, QLabel, QVBoxLayout


class MultiViewWindow(QDialog):
    """Shows a grid of camera streams."""

    def __init__(self, sources: List[Tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sentinel Home - MultiView")
        self.resize(1000, 700)
        self.sources = sources
        self.captures = []
        self.labels: List[QLabel] = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_streams)

        root = QVBoxLayout(self)
        grid = QGridLayout()
        root.addLayout(grid)

        columns = 2
        for i, (name, source) in enumerate(self.sources):
            label = QLabel(f"{name}\nConnecting...")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background-color: black; color: #00FFFF;")
            label.setMinimumSize(320, 240)
            grid.addWidget(label, i // columns, i % columns)
            self.labels.append(label)
            self.captures.append(cv2.VideoCapture(source))

        self.timer.start(120)

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
                label.setText("No Frame")
                continue

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            q_image = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(q_image)
            label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def closeEvent(self, event):
        self.timer.stop()
        for cap in self.captures:
            if cap:
                cap.release()
        super().closeEvent(event)
