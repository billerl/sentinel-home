"""
Thread-safe latest-frame store for the web MJPEG layer.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


class FrameBroker:
    """Holds the most recent BGR frame and pre-encoded JPEG per camera."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._frames: Dict[str, np.ndarray] = {}
        self._jpegs: Dict[str, bytes] = {}
        self._names: Dict[str, str] = {}
        self._ts: Dict[str, float] = {}
        self._last_jpeg_quality: Dict[str, int] = {}

    def publish(
        self,
        camera_id: str,
        display_name: str,
        frame_bgr: np.ndarray,
        jpeg_quality: Optional[int] = None,
    ) -> None:
        """Store latest frame and optionally pre-encode to JPEG for web clients."""
        with self._lock:
            self._frames[camera_id] = np.ascontiguousarray(frame_bgr).copy()
            self._names[camera_id] = display_name
            self._ts[camera_id] = time.monotonic()

            if jpeg_quality is not None:
                ok, buf = cv2.imencode(
                    ".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
                )
                if ok:
                    self._jpegs[camera_id] = buf.tobytes()
                    self._last_jpeg_quality[camera_id] = jpeg_quality

    def unpublish(self, camera_id: str) -> None:
        with self._lock:
            self._frames.pop(camera_id, None)
            self._jpegs.pop(camera_id, None)
            self._names.pop(camera_id, None)
            self._ts.pop(camera_id, None)
            self._last_jpeg_quality.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._jpegs.clear()
            self._names.clear()
            self._ts.clear()
            self._last_jpeg_quality.clear()

    def get_latest(self, camera_id: str) -> Tuple[Optional[np.ndarray], float]:
        """Return (frame, monotonic_ts) or (None, 0.0)."""
        with self._lock:
            frame = self._frames.get(camera_id)
            ts = self._ts.get(camera_id, 0.0)
            return frame, ts

    def get_latest_jpeg(self, camera_id: str) -> Tuple[Optional[bytes], float]:
        """Return (jpeg_bytes, monotonic_ts) or (None, 0.0)."""
        with self._lock:
            jpeg = self._jpegs.get(camera_id)
            ts = self._ts.get(camera_id, 0.0)
            return jpeg, ts

    def list_cameras(self) -> List[Tuple[str, str]]:
        """Stable list of (camera_id, display_name)."""
        with self._lock:
            return sorted((cid, self._names.get(cid, cid)) for cid in self._frames)
