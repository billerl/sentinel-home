"""
Thread-safe latest-frame store for the web MJPEG layer.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from threading import Lock
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import cv2
import numpy as np


class FrameBroker:
    """Holds the most recent BGR frame and pre-encoded JPEG per camera.

    Publishers use the internal camera id (an RTSP URL that may contain
    credentials). Web clients only ever see an opaque ``web_id`` derived from it
    with a per-process key, so camera passwords never reach the browser.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._key = secrets.token_bytes(16)
        self._hosts: Dict[str, str] = {}
        self._frames: Dict[str, np.ndarray] = {}
        self._jpegs: Dict[str, bytes] = {}
        self._names: Dict[str, str] = {}
        self._ts: Dict[str, float] = {}
        self._last_jpeg_quality: Dict[str, int] = {}

    def web_id(self, camera_id: str) -> str:
        """Opaque, stable-per-process id for a camera, safe to expose to browsers."""
        return hmac.new(self._key, camera_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def publish(
        self,
        camera_id: str,
        display_name: str,
        frame_bgr: np.ndarray,
        jpeg_quality: Optional[int] = None,
    ) -> None:
        """Store latest frame and optionally pre-encode to JPEG for web clients."""
        host = urlparse(camera_id).hostname or display_name
        camera_id = self.web_id(camera_id)
        with self._lock:
            self._hosts[camera_id] = host
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
        camera_id = self.web_id(camera_id)
        with self._lock:
            self._hosts.pop(camera_id, None)
            self._frames.pop(camera_id, None)
            self._jpegs.pop(camera_id, None)
            self._names.pop(camera_id, None)
            self._ts.pop(camera_id, None)
            self._last_jpeg_quality.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._hosts.clear()
            self._jpegs.clear()
            self._names.clear()
            self._ts.clear()
            self._last_jpeg_quality.clear()

    def get_latest(self, camera_id: str) -> Tuple[Optional[np.ndarray], float]:
        """Return (frame, monotonic_ts) or (None, 0.0) for a ``web_id``."""
        with self._lock:
            frame = self._frames.get(camera_id)
            ts = self._ts.get(camera_id, 0.0)
            return frame, ts

    def get_latest_jpeg(self, camera_id: str) -> Tuple[Optional[bytes], float]:
        """Return (jpeg_bytes, monotonic_ts) or (None, 0.0) for a ``web_id``."""
        with self._lock:
            jpeg = self._jpegs.get(camera_id)
            ts = self._ts.get(camera_id, 0.0)
            return jpeg, ts

    def list_cameras(self) -> List[Tuple[str, str]]:
        """Stable list of (web_id, display_name), ordered by display name."""
        with self._lock:
            return sorted(
                ((cid, self._names.get(cid, cid)) for cid in self._frames),
                key=lambda item: item[1],
            )

    def list_camera_hosts(self) -> List[Tuple[str, str]]:
        """(host, display_name) per live camera, matching list_cameras order."""
        with self._lock:
            return sorted(
                ((self._hosts.get(cid, ""), self._names.get(cid, cid)) for cid in self._frames),
                key=lambda item: item[1],
            )
