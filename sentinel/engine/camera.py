"""
One camera's pipeline: capture -> motion analysis -> recording, in its own thread.

The pipeline owns reconnecting its camera, so one camera dropping never affects
the others. It reports what happens (motion, finished clips, up/down) to the
engine through callbacks; it never touches Telegram, audio or the web directly.
"""

import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import cv2
import numpy as np

from sentinel.constants import MIN_CONTOUR_AREA, MOTION_COOLDOWN, VAR_THRESHOLD
from sentinel.detection import MotionDetector
from sentinel.logging_utils import app_logger, redact
from sentinel.recordings.hd_recorder import HDRecorder, main_stream_url

if TYPE_CHECKING:
    from sentinel.recordings import RecordingStore
    from sentinel.web.frame_broker import FrameBroker

CLIP_REQUEST_REASON = "telegram_clip_request"
RECONNECT_BACKOFF_MAX = 120.0


def camera_host(source: object, display_name: str = "") -> str:
    """Stable, credential-free key for a camera (its IP/hostname)."""
    if isinstance(source, str):
        host = urlparse(source).hostname
        if host:
            return host
    return display_name or str(source)


def draw_zones_and_lines(frame: np.ndarray, profile: Dict[str, Any]) -> np.ndarray:
    """Draw a profile's zones (cyan) and entry lines (magenta) onto ``frame`` in place."""
    height, width = frame.shape[:2]
    for zone in profile.get("active_zones", []):
        points = np.array(
            [[int(p[0] * width), int(p[1] * height)] for p in zone if len(p) == 2],
            dtype=np.int32,
        )
        if len(points) >= 3:
            cv2.polylines(frame, [points], True, (255, 255, 0), 2)
    for line in profile.get("entry_lines", []):
        if len(line) != 2:
            continue
        p1 = (int(line[0][0] * width), int(line[0][1] * height))
        p2 = (int(line[1][0] * width), int(line[1][1] * height))
        cv2.line(frame, p1, p2, (255, 0, 255), 2)
    return frame


class CameraPipeline:
    """Runs one camera in a background thread."""

    def __init__(
        self,
        camera_id: str,
        source: object,
        display_name: str,
        profile: Dict[str, Any],
        *,
        recording_store: Optional["RecordingStore"],
        frame_broker: Optional["FrameBroker"],
        settings: Callable[[], Dict[str, Any]],
        on_motion: Callable[["CameraPipeline"], None],
        on_clip: Callable[["CameraPipeline", str, str, List[str]], None],
        on_status: Callable[["CameraPipeline", bool, Optional[str]], None],
    ) -> None:
        self.camera_id = camera_id
        self.source = source
        self.display_name = display_name
        self.host = camera_host(source, display_name)
        self.profile = profile
        self.detector = MotionDetector()
        self.detector.set_recording_identity(recording_store, self.host, display_name)
        self._store = recording_store
        self.hd: Optional[HDRecorder] = None
        self._broker = frame_broker
        self._settings = settings
        self._on_motion = on_motion
        self._on_clip = on_clip
        self._on_status = on_status

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending_clip_chats: List[str] = []
        self._clip_chats: List[str] = []
        self._last_publish = 0.0
        self.connected = False
        self._status_known = False
        self.last_frame_time = 0.0
        self.apply_profile(profile)

    # ------------------------------------------------------------ configuration
    def apply_profile(self, profile: Dict[str, Any]) -> None:
        """Push a camera profile's settings into the detector."""
        self.profile = profile
        d = self.detector
        d.set_cam_orientation(str(self._settings().get("cam_orientation", "horizontal")))
        d.set_min_contour_area(int(profile.get("min_contour_area", MIN_CONTOUR_AREA)))
        new_var = int(profile.get("var_threshold", VAR_THRESHOLD))
        if new_var != d.var_threshold:
            d.update_var_threshold(new_var)
        d.set_recording_enabled(bool(profile.get("recording_enabled", True)))
        d.set_motion_cooldown(float(profile.get("motion_cooldown", MOTION_COOLDOWN)))
        d.set_active_zones(profile.get("active_zones", []))
        d.set_entry_lines(profile.get("entry_lines", []))
        d.set_entry_direction(profile.get("entry_direction", "outside_to_inside"))
        d.set_night_bug_filter(
            bool(profile.get("night_bug_filter_enabled", True)),
            int(profile.get("night_bug_start_hour", 20)),
            int(profile.get("night_bug_end_hour", 6)),
        )

    def request_clip(self, chat_ids: List[str]) -> None:
        """Record a clip as soon as possible and deliver it to ``chat_ids``."""
        with self._lock:
            self._pending_clip_chats.extend(c for c in chat_ids if c not in self._pending_clip_chats)

    def snapshot(self) -> Optional[np.ndarray]:
        """Latest clean (un-annotated) frame."""
        return self.detector.get_last_raw_frame()

    # --------------------------------------------------------------- lifecycle
    def hd_source(self) -> Optional[str]:
        """Main-stream URL for high-res recording, or None when disabled/unknown."""
        if not self.profile.get("hd_recording", True) or not isinstance(self.source, str):
            return None
        return self.profile.get("record_source") or main_stream_url(self.source)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        url = self.hd_source()
        if url and self._store is not None:
            self.hd = HDRecorder(url, self.host)
            self.hd.start()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-{self.host}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread and finalize any clip in progress."""
        self._stop.set()
        self.detector.stop_camera()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._deliver_completed_clip()  # clip finalized by stop_camera
        if self.hd is not None:
            self.hd.stop()
            self.hd = None
        if self._broker is not None:
            self._broker.unpublish(self.camera_id)

    # ------------------------------------------------------------------- loop
    def _feed_fps(self) -> int:
        try:
            return max(5, min(30, int(self._settings().get("feed_fps", 15))))
        except (TypeError, ValueError):
            return 15

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            if not self._connect():
                settings = self._settings()
                if not settings.get("auto_reconnect_enabled", True):
                    return
                base = float(settings.get("reconnect_interval_seconds", 15))
                backoff = min(RECONNECT_BACKOFF_MAX, max(base, backoff * 2 or base))
                self._stop.wait(backoff)
                continue
            backoff = 0.0
            self._process_frames()
            if self._stop.is_set():
                break
            self._set_connected(False, self.detector.last_error_message or "stream dropped")
            if self._broker is not None:
                self._broker.unpublish(self.camera_id)
            if not self._settings().get("auto_reconnect_enabled", True):
                return
            self._stop.wait(float(self._settings().get("reconnect_interval_seconds", 15)))

    def _connect(self) -> bool:
        ok = self.detector.start_camera(self.source, target_fps=self._feed_fps())
        if ok:
            self._set_connected(True, None)
        else:
            self._set_connected(False, self.detector.last_error_message or "connect failed")
        return ok

    def _set_connected(self, connected: bool, error: Optional[str]) -> None:
        if self._status_known and connected == self.connected:
            return
        self._status_known = True
        self.connected = connected
        app_logger.log(
            "info" if connected else "error",
            "camera_connected" if connected else "camera_down",
            camera_ip=self.host,
            camera_display=self.display_name,
            error_message=redact(error) if error else None,
            component="camera_pipeline",
            action="connect",
        )
        self._on_status(self, connected, error)

    def _process_frames(self) -> None:
        interval = 1.0 / self._feed_fps()
        while not self._stop.is_set():
            loop_start = time.time()
            annotated, raw, _rects, motion, _rec_status = self.detector.detect_motion()
            if not self.detector.feed_active:
                return
            if annotated is None:
                continue  # transient read error; detector counts failures
            self.last_frame_time = loop_start

            self._start_requested_clip(raw)
            self._publish(annotated, raw)
            if motion:
                self._on_motion(self)
            self._deliver_completed_clip()

            wait = interval - (time.time() - loop_start)
            if wait > 0:
                self._stop.wait(wait)

    def _deliver_completed_clip(self) -> None:
        """Report a finished clip, upgraded to the high-res version with pre-roll when available."""
        path, reason = self.detector.consume_last_completed_recording()
        if not path:
            return
        chats: List[str] = []
        if reason == CLIP_REQUEST_REASON:
            chats, self._clip_chats = self._clip_chats, []
        span = self.detector.last_completed_span
        hd = self.hd
        if hd is not None and hd.running and span and span[2] is not None:
            start_ts, end_ts, record_id = span
            hd_out = os.path.splitext(path)[0] + "_hd.mp4"

            def _done(hd_path: Optional[str]) -> None:
                if hd_path and self._store is not None:
                    self._store.attach_hd(record_id, hd_path)
                self._on_clip(self, hd_path or path, reason, chats)

            if hd.request_clip(start_ts, end_ts, hd_out, _done):
                return
        self._on_clip(self, path, reason, chats)

    def _start_requested_clip(self, frame: Optional[np.ndarray]) -> None:
        with self._lock:
            if not self._pending_clip_chats or frame is None:
                return
            if self.detector.recording or not self.detector.recording_enabled:
                return  # wait for the current clip to finish (or recording to be enabled)
            self._clip_chats = self._pending_clip_chats
            self._pending_clip_chats = []
        h, w = frame.shape[:2]
        with self.detector._lock:
            self.detector.start_recording((w, h), reason=CLIP_REQUEST_REASON)

    def has_pending_clip_request(self) -> bool:
        with self._lock:
            return bool(self._pending_clip_chats)

    def _publish(self, annotated: np.ndarray, raw: Optional[np.ndarray]) -> None:
        if self._broker is None:
            return
        web = self._settings().get("web", {})
        max_fps = max(1, int(web.get("max_fps", 10)))
        now = time.time()
        if now - self._last_publish < 1.0 / max_fps:
            return
        self._last_publish = now
        if web.get("published_frame", "overlay") == "raw" and raw is not None:
            frame = raw
        else:
            frame = draw_zones_and_lines(annotated, self.profile)
        self._broker.publish(
            self.camera_id,
            self.display_name,
            frame,
            jpeg_quality=int(web.get("jpeg_quality", 80)),
        )
