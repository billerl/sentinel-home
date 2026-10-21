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
        self._jpeg_ts: Dict[str, float] = {}

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
        """Store the latest frame. JPEG encoding happens lazily, only when a web
        client asks for it, so idle cameras cost no encoding work."""
        host = urlparse(camera_id).hostname or display_name
        camera_id = self.web_id(camera_id)
        with self._lock:
            self._hosts[camera_id] = host
            self._frames[camera_id] = np.ascontiguousarray(frame_bgr).copy()
            self._names[camera_id] = display_name
            self._ts[camera_id] = time.monotonic()
            self._last_jpeg_quality[camera_id] = int(jpeg_quality or 80)

    def unpublish(self, camera_id: str) -> None:
        camera_id = self.web_id(camera_id)
        with self._lock:
            self._hosts.pop(camera_id, None)
            self._frames.pop(camera_id, None)
            self._jpegs.pop(camera_id, None)
            self._names.pop(camera_id, None)
            self._ts.pop(camera_id, None)
            self._last_jpeg_quality.pop(camera_id, None)
            self._jpeg_ts.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._hosts.clear()
            self._jpegs.clear()
            self._names.clear()
            self._ts.clear()
            self._last_jpeg_quality.clear()
            self._jpeg_ts.clear()

    def get_latest(self, camera_id: str) -> Tuple[Optional[np.ndarray], float]:
        """Return (frame, monotonic_ts) or (None, 0.0) for a ``web_id``."""
        with self._lock:
            frame = self._frames.get(camera_id)
            ts = self._ts.get(camera_id, 0.0)
            return frame, ts

    def get_latest_jpeg(self, camera_id: str) -> Tuple[Optional[bytes], float]:
        """Return (jpeg_bytes, monotonic_ts) or (None, 0.0) for a ``web_id``.

        Encodes the latest frame on first request and caches it, so any number
        of viewers share one encode per frame."""
        with self._lock:
            frame = self._frames.get(camera_id)
            if frame is None:
                return None, 0.0
            ts = self._ts.get(camera_id, 0.0)
            if self._jpeg_ts.get(camera_id) != ts:
                ok, buf = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self._last_jpeg_quality.get(camera_id, 80)],
                )
                if not ok:
                    return None, 0.0
                self._jpegs[camera_id] = buf.tobytes()
                self._jpeg_ts[camera_id] = ts
            return self._jpegs[camera_id], ts

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
