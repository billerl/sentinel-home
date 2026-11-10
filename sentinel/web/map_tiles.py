"""
Map pictures (tiles) for the location map, fetched by Sentinel and kept on disk, so the browser only ever talks
to Sentinel. The Samsung TV's old browser got "Access blocked" squares from OpenStreetMap's servers (they turn
away requests that don't say who's asking), so its map stayed blank (2026-11-10). One home's map is a few hundred
small pictures, kept for a week as OpenStreetMap's tile policy asks.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.request
from typing import Optional, Tuple

SOURCES = {
    # name -> (address, maximum zoom, picture type)
    "street": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19, "image/png"),
    # the street map darkened here, for a TV in a dark room: the pages did it with a CSS filter (invert + hue-rotate),
    # and the Samsung's old browser drew those tiles solid black (2026-11-10)
    "dark": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19, "image/jpeg"),
    "aerial": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
               19, "image/jpeg"),
}
USER_AGENT = "SentinelHome/1.0 (self-hosted home security; https://github.com/billerl/sentinel-home)"
KEEP_SECONDS = 7 * 86400
MAX_FILES = 20000  # ~300 MB at most; the oldest go first


def darken(png: bytes) -> bytes:
    """A street map picture turned dark the way the pages' CSS did it: colours inverted, hues turned back round (so
    water stays blue-ish and parks green-ish), a little dimmer and flatter. JPEG: smaller for the TV."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a picture")
    hsv = cv2.cvtColor(255 - img, cv2.COLOR_BGR2HSV)
    hsv[..., 0] = (hsv[..., 0].astype(np.int16) + 90) % 180  # (OpenCV hue runs 0-179: +90 = half way round)
    out = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.float32)
    out = ((out - 128.0) * 0.9 + 128.0) * 0.82
    ok, jpeg = cv2.imencode(".jpg", np.clip(out, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("couldn't save the picture")
    return jpeg.tobytes()


class MapTiles:
    def __init__(self, folder: str, timeout: float = 8.0) -> None:
        self.folder = folder
        self.timeout = timeout
        self._lock = threading.Lock()
        self._writes = 0

    @staticmethod
    def valid(source: str, z: int, x: int, y: int) -> bool:
        if source not in SOURCES or not 0 <= z <= SOURCES[source][1]:
            return False
        return 0 <= x < 2 ** z and 0 <= y < 2 ** z

    def get(self, source: str, z: int, x: int, y: int) -> Optional[Tuple[bytes, str]]:
        """(picture, type), from disk when it's less than a week old, else fetched; None if neither works."""
        if not self.valid(source, z, x, y):
            return None
        url, _, kind = SOURCES[source]
        path = os.path.join(self.folder, source, str(z), str(x), f"{y}.tile")
        cached = None
        try:
            with open(path, "rb") as f:
                cached = f.read()
            if time.time() - os.path.getmtime(path) < KEEP_SECONDS:
                return cached, kind
        except OSError:
            pass
        try:
            data = self._fetch(url.format(z=z, x=x, y=y))
            if source == "dark":
                data = darken(data)
        except (OSError, ValueError):
            return (cached, kind) if cached else None  # offline: an old picture beats none
        self._save(path, data)
        return data, kind

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read(2 << 20)
        if not data:
            raise OSError("empty map picture")
        return data

    def _save(self, path: str, data: bytes) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            return
        with self._lock:
            self._writes += 1
            check = self._writes % 500 == 0
        if check:
            self._trim()

    def _trim(self) -> None:
        files = []
        for root, _, names in os.walk(self.folder):
            for name in names:
                p = os.path.join(root, name)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
        files.sort()
        for _, p in files[:max(0, len(files) - MAX_FILES)]:
            try:
                os.remove(p)
            except OSError:
                pass
