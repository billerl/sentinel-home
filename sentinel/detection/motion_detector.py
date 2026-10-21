"""
Motion detection functionality for Sentinel Home.
"""

import os
import cv2
import numpy as np
import time
import threading
from typing import Tuple, List, Optional, Union, Dict, Any
from sentinel.logging_utils import app_logger

# Import the constants
from sentinel.constants import (
    HISTORY_LENGTH,
    VAR_THRESHOLD,
    DETECT_SHADOWS,
    MIN_CONTOUR_AREA,
    VIDEO_DIR,
    RECORDING_DURATION,
    RECORDING_FPS,
    VIDEO_CODEC,
    MOTION_COOLDOWN,
    CAPTURE_DIR,
    DEFAULT_ACTIVE_ZONES,
    DEFAULT_ENTRY_LINES,
    DEFAULT_ENTRY_DIRECTION,
    MAX_CONSECUTIVE_READ_FAILURES,
)


class MotionDetector:
    """Handles camera access and motion detection."""

    def __init__(self):
        """Initialize the motion detector."""
        self._lock = threading.Lock()
        self.cap = None
        self.camera_index: Union[int, str] = 0
        self.feed_active = False
        self.min_contour_area = MIN_CONTOUR_AREA
        self.var_threshold = VAR_THRESHOLD
        self.fgbg = cv2.createBackgroundSubtractorMOG2(
            history=HISTORY_LENGTH,
            varThreshold=VAR_THRESHOLD,
            detectShadows=DETECT_SHADOWS
        )
        # Video recording properties
        self.video_writer = None
        self.recording = False
        self.recording_enabled = True  # Add this line
        self.recording_start_time = 0
        self.last_motion_time = 0
        self.frames_recorded = 0
        self.recording_target_frames = int(RECORDING_DURATION * RECORDING_FPS)
        self.current_video_path: Optional[str] = None
        self.last_completed_video_path: Optional[str] = None
        self.current_recording_reason: str = "motion"
        self.last_completed_video_reason: str = "motion"
        self.motion_cooldown = MOTION_COOLDOWN

        # Zone and line based alert gating
        self.active_zones = DEFAULT_ACTIVE_ZONES.copy()
        self.entry_lines = DEFAULT_ENTRY_LINES.copy()
        self.entry_direction = DEFAULT_ENTRY_DIRECTION
        self.night_bug_filter_enabled = True
        self.night_bug_start_hour = 20
        self.night_bug_end_hour = 6

        # Lightweight centroid tracking for line crossing
        self.tracks: Dict[int, Dict[str, Any]] = {}
        self.next_track_id = 1
        self.track_ttl_seconds = 1.2
        self.track_match_distance_px = 80.0

        # Reconnect/read-failure support
        self.consecutive_read_failures = 0
        self.max_consecutive_read_failures = MAX_CONSECUTIVE_READ_FAILURES
        self.last_raw_frame = None
        self.last_error_message: Optional[str] = None
        # Match UI "Camera View" so zone/line clicks align with motion gating.
        self.cam_orientation: str = "horizontal"

        # Create video directory if it doesn't exist
        if not os.path.exists(VIDEO_DIR):
            os.makedirs(VIDEO_DIR)

    def set_recording_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.recording_enabled = bool(enabled)

    def set_min_contour_area(self, area: int) -> None:
        with self._lock:
            self.min_contour_area = int(area)

    def set_motion_cooldown(self, cooldown: float) -> None:
        with self._lock:
            self.motion_cooldown = float(cooldown)

    def set_active_zones(self, zones: List[List[List[float]]]) -> None:
        with self._lock:
            self.active_zones = zones or []

    def set_entry_lines(self, lines: List[List[List[float]]]) -> None:
        with self._lock:
            self.entry_lines = lines or []

    def set_entry_direction(self, direction: str) -> None:
        with self._lock:
            if direction in ("outside_to_inside", "inside_to_outside", "both"):
                self.entry_direction = direction

    def set_night_bug_filter(self, enabled: bool, start_hour: int, end_hour: int) -> None:
        """Update night bug filter schedule settings."""
        with self._lock:
            self.night_bug_filter_enabled = bool(enabled)
            self.night_bug_start_hour = max(0, min(23, int(start_hour)))
            self.night_bug_end_hour = max(0, min(23, int(end_hour)))

    def set_cam_orientation(self, orientation: str) -> None:
        """horizontal | vertical — vertical matches UI rotate before zones/lines."""
        orient = "vertical" if orientation == "vertical" else "horizontal"
        with self._lock:
            if orient == getattr(self, "cam_orientation", "horizontal"):
                return
            self.cam_orientation = orient
            # Frame size changes; reset background model so motion mask stays sane.
            self.fgbg = cv2.createBackgroundSubtractorMOG2(
                history=HISTORY_LENGTH,
                varThreshold=self.var_threshold,
                detectShadows=DETECT_SHADOWS,
            )

    def get_last_raw_frame(self) -> Optional[np.ndarray]:
        """Return the latest unannotated frame."""
        if self.last_raw_frame is None:
            return None
        return self.last_raw_frame.copy()

    def consume_last_completed_video_path(self) -> Optional[str]:
        """Return and clear the most recently completed recording path."""
        path = self.last_completed_video_path
        self.last_completed_video_path = None
        return path

    def consume_last_completed_recording(self) -> Tuple[Optional[str], str]:
        """Return and clear completed recording path plus reason."""
        path = self.last_completed_video_path
        reason = self.last_completed_video_reason
        self.last_completed_video_path = None
        self.last_completed_video_reason = "motion"
        return path, reason

    def start_recording(self, frame_size, reason: str = "motion"):
        """
        Start recording a video clip.

        Args:
            frame_size: Tuple of (width, height) for the video frames
        """
        # Only start if not already recording
        if self.recording:
            return

        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            video_path = os.path.join(VIDEO_DIR, f"motion_{timestamp}.avi")

            # Get the four-character code for the video codec
            fourcc = cv2.VideoWriter_fourcc(*VIDEO_CODEC)

            # Create video writer
            self.video_writer = cv2.VideoWriter(
                video_path, fourcc, RECORDING_FPS, frame_size, True  # isColor
            )

            self.recording = True
            self.recording_start_time = time.time()
            self.frames_recorded = 0
            self.current_video_path = video_path
            self.current_recording_reason = reason

            print(f"Started recording: {video_path}")
            return video_path
        except Exception as e:
            print(f"Error starting recording: {e}")
            self.recording = False
            self.video_writer = None
            return None

    def stop_recording(self):
        """Stop the current video recording."""
        if not self.recording or self.video_writer is None:
            return

        try:
            # Release the video writer
            self.video_writer.release()
            self.video_writer = None
            self.recording = False
            if self.current_video_path:
                self.last_completed_video_path = self.current_video_path
                self.last_completed_video_reason = self.current_recording_reason
            self.current_video_path = None
            self.current_recording_reason = "motion"

            duration = time.time() - self.recording_start_time
            print(
                f"Stopped recording. Duration: {duration:.2f}s, Frames: {self.frames_recorded}"
            )

            return True
        except Exception as e:
            print(f"Error stopping recording: {e}")
            self.video_writer = None
            self.recording = False
            self.current_video_path = None
            self.current_recording_reason = "motion"
            return False

    def write_frame(self, frame):
        """
        Write a frame to the current video recording.

        Args:
            frame: The frame to write to the video

        Returns:
            True if the frame was written, False otherwise
        """
        if not self.recording or self.video_writer is None:
            return False

        try:
            self.video_writer.write(frame)
            self.frames_recorded += 1

            # Stop based on target frame count so output clip duration is stable.
            if self.frames_recorded >= self.recording_target_frames:
                self.stop_recording()

            return True
        except Exception as e:
            print(f"Error writing frame: {e}")
            return False

    def check_recording_status(self, motion_detected, frame):
        """
        Check if recording should start, continue, or stop based on motion detection.
        
        Args:
            motion_detected: Whether motion was detected in the current frame
            frame: The current frame
            
        Returns:
            Tuple of (recording_status, video_path)
            recording_status can be: "started", "recording", "stopped", or None
        """
        current_time = time.time()
        
        # If recording is disabled, stop any ongoing recording and return
        if not self.recording_enabled and self.recording:
            self.stop_recording()
            return "stopped", None
        elif not self.recording_enabled:
            return None, None
        
        # If motion detected and not recording, start recording
        if motion_detected and not self.recording:
            # Check cooldown period
            if (current_time - self.last_motion_time) >= self.motion_cooldown:
                self.last_motion_time = current_time
                
                # Get frame dimensions
                height, width = frame.shape[:2]
                
                # Start recording
                video_path = self.start_recording((width, height))
                if video_path:
                    return "started", video_path
        
        # If recording, write the frame
        if self.recording:
            self.write_frame(frame)
            
            # If motion detected, update the last motion time
            if motion_detected:
                self.last_motion_time = current_time
                
            return "recording", None
            
        return None, None

    def start_camera(
        self,
        camera_index: Union[int, str],
        target_fps: Optional[int] = None,
    ) -> bool:
        """
        Start the camera at the specified index.

        Args:
            camera_index: Index of the camera to use
            target_fps: Requested capture rate (best-effort; reduces work when honored)

        Returns:
            True if camera started successfully, False otherwise
        """
        with self._lock:
            if self.feed_active:
                if self.cap:
                    self.cap.release()
                    self.cap = None

            try:
                self.camera_index = camera_index
                print(f"[Connect] Attempting camera source: {camera_index}")
                app_logger.log(
                    "info",
                    "camera_connect_attempt",
                    camera_id=str(camera_index),
                    camera_source=str(camera_index),
                    component="motion_detector",
                    action="start_camera",
                )
                if isinstance(camera_index, int):
                    self.cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
                else:
                    self.cap = cv2.VideoCapture(camera_index)
                if not self.cap.isOpened():
                    raise RuntimeError(f"Failed to open camera {camera_index}")

                # Increase buffer size to handle network jitters (especially for high-FPS or 4K)
                try:
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 5)
                except Exception:
                    pass

                if target_fps is not None and target_fps > 0:
                    try:
                        self.cap.set(cv2.CAP_PROP_FPS, float(target_fps))
                    except Exception:
                        pass

                # Verify we can actually read frames (isOpened alone is not enough)
                if not self.cap.grab():
                    raise RuntimeError(f"Camera {camera_index} opened but cannot grab frames")

                self.feed_active = True
                self.consecutive_read_failures = 0
                self.last_error_message = None
                print(f"[Connect] Camera connected successfully: {camera_index}")
                app_logger.log(
                    "info",
                    "camera_connect_success",
                    camera_id=str(camera_index),
                    camera_source=str(camera_index),
                    component="motion_detector",
                    action="start_camera",
                )
                return True
            except Exception as e:
                print(f"Error starting camera: {e}")
                self.last_error_message = str(e)
                app_logger.log(
                    "error",
                    "camera_connect_error",
                    camera_id=str(camera_index),
                    camera_source=str(camera_index),
                    component="motion_detector",
                    action="start_camera",
                    error_message=str(e),
                )
                if self.cap:
                    self.cap.release()
                    self.cap = None
                self.feed_active = False
                return False

    def stop_camera(self) -> None:
        """Stop the currently active camera."""
        with self._lock:
            self.feed_active = False
            if self.cap:
                self.cap.release()
                self.cap = None
            self.consecutive_read_failures = 0
            self.tracks = {}

    def update_var_threshold(self, value):
        """Update the variance threshold and recreate the background subtractor."""
        with self._lock:
            self.var_threshold = value
            self.fgbg = cv2.createBackgroundSubtractorMOG2(
                history=HISTORY_LENGTH,
                varThreshold=self.var_threshold,
                detectShadows=DETECT_SHADOWS
            )

    def _denormalize_points(
        self,
        points: List[List[float]],
        width: int,
        height: int,
    ) -> np.ndarray:
        pts = []
        for p in points:
            if len(p) != 2:
                continue
            x = int(float(p[0]) * width)
            y = int(float(p[1]) * height)
            pts.append([x, y])
        return np.array(pts, dtype=np.int32) if pts else np.empty((0, 2), dtype=np.int32)

    def _point_in_active_zone(self, point: Tuple[int, int], width: int, height: int) -> bool:
        if not self.active_zones:
            return True
        for zone in self.active_zones:
            poly = self._denormalize_points(zone, width, height)
            if len(poly) >= 3 and cv2.pointPolygonTest(poly, point, False) >= 0:
                return True
        return False

    @staticmethod
    def _line_side(
        point: Tuple[int, int],
        line_p1: Tuple[int, int],
        line_p2: Tuple[int, int],
    ) -> float:
        return (
            (line_p2[0] - line_p1[0]) * (point[1] - line_p1[1])
            - (line_p2[1] - line_p1[1]) * (point[0] - line_p1[0])
        )

    @staticmethod
    def _orientation(a: Tuple[int, int], b: Tuple[int, int], c: Tuple[int, int]) -> int:
        """Return orientation of ordered triplet (a,b,c): 0 collinear, 1 cw, 2 ccw."""
        val = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
        if val == 0:
            return 0
        return 1 if val > 0 else 2

    @staticmethod
    def _on_segment(a: Tuple[int, int], b: Tuple[int, int], c: Tuple[int, int]) -> bool:
        """Return True when point b lies on segment ac."""
        return (
            min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
            and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
        )

    def _segments_intersect(
        self,
        p1: Tuple[int, int],
        p2: Tuple[int, int],
        q1: Tuple[int, int],
        q2: Tuple[int, int],
    ) -> bool:
        """Return True when segment p1p2 intersects segment q1q2."""
        o1 = self._orientation(p1, p2, q1)
        o2 = self._orientation(p1, p2, q2)
        o3 = self._orientation(q1, q2, p1)
        o4 = self._orientation(q1, q2, p2)

        if o1 != o2 and o3 != o4:
            return True

        if o1 == 0 and self._on_segment(p1, q1, p2):
            return True
        if o2 == 0 and self._on_segment(p1, q2, p2):
            return True
        if o3 == 0 and self._on_segment(q1, p1, q2):
            return True
        if o4 == 0 and self._on_segment(q1, p2, q2):
            return True
        return False

    def _crossing_allowed(self, previous_side: float, current_side: float) -> bool:
        if previous_side == 0 or current_side == 0:
            return False
        if previous_side * current_side >= 0:
            return False
        if self.entry_direction == "both":
            return True
        # "outside_to_inside" means positive -> negative crossing.
        if self.entry_direction == "outside_to_inside":
            return previous_side > 0 and current_side < 0
        return previous_side < 0 and current_side > 0

    def _cleanup_old_tracks(self) -> None:
        now = time.time()
        stale = [
            track_id
            for track_id, data in self.tracks.items()
            if (now - data["last_seen"]) > self.track_ttl_seconds
        ]
        for track_id in stale:
            self.tracks.pop(track_id, None)

    def _assign_track(self, centroid: Tuple[int, int]) -> int:
        best_id = None
        best_dist = self.track_match_distance_px
        for track_id, data in self.tracks.items():
            prev = data["centroid"]
            dist = float(np.hypot(prev[0] - centroid[0], prev[1] - centroid[1]))
            if dist < best_dist:
                best_dist = dist
                best_id = track_id

        if best_id is None:
            best_id = self.next_track_id
            self.next_track_id += 1
            now = time.time()
            self.tracks[best_id] = {
                "centroid": centroid,
                "previous_centroid": centroid,
                "last_seen": now,
                "created_at": now,
                "line_sides": {},
            }
            return best_id

        previous = self.tracks[best_id]["centroid"]
        self.tracks[best_id]["previous_centroid"] = previous
        self.tracks[best_id]["centroid"] = centroid
        self.tracks[best_id]["last_seen"] = time.time()
        return best_id

    def _night_bug_filter_active(self) -> bool:
        """Return True when night bug filter is enabled and schedule is active."""
        if not self.night_bug_filter_enabled:
            return False
        now_hour = time.localtime().tm_hour
        start = self.night_bug_start_hour
        end = self.night_bug_end_hour
        if start == end:
            return True
        if start < end:
            return start <= now_hour < end
        return now_hour >= start or now_hour < end

    def _is_bug_like_motion(
        self,
        contour_area: float,
        track_id: int,
        centroid: Tuple[int, int],
        frame_w: int,
        frame_h: int,
    ) -> bool:
        """
        Balanced nighttime insect suppression.

        Treat motion as bug-like when most traits match:
        tiny, short-lived, fast darting, and edge-near.
        """
        frame_area = float(max(1, frame_w * frame_h))
        area_ratio = contour_area / frame_area
        tiny = area_ratio < 0.004

        track = self.tracks.get(track_id, {})
        age_seconds = max(0.0, time.time() - float(track.get("created_at", time.time())))
        short_lived = age_seconds < 1.4

        prev = track.get("previous_centroid", centroid)
        speed_px = float(np.hypot(centroid[0] - prev[0], centroid[1] - prev[1]))
        fast = speed_px > 30.0

        edge_margin_x = int(frame_w * 0.12)
        edge_margin_y = int(frame_h * 0.12)
        edge_near = (
            centroid[0] <= edge_margin_x
            or centroid[0] >= (frame_w - edge_margin_x)
            or centroid[1] <= edge_margin_y
            or centroid[1] >= (frame_h - edge_margin_y)
        )

        score = int(tiny) + int(short_lived) + int(fast) + int(edge_near)
        return score >= 3

    def _line_crossed_for_track(
        self,
        track_id: int,
        centroid: Tuple[int, int],
        width: int,
        height: int,
    ) -> bool:
        if not self.entry_lines:
            return False

        track = self.tracks.get(track_id)
        if not track:
            return False

        sides = track.setdefault("line_sides", {})
        previous_centroid = track.get("previous_centroid", centroid)
        crossed = False
        for idx, line in enumerate(self.entry_lines):
            if len(line) != 2:
                continue
            p1 = (int(float(line[0][0]) * width), int(float(line[0][1]) * height))
            p2 = (int(float(line[1][0]) * width), int(float(line[1][1]) * height))
            current_side = self._line_side(centroid, p1, p2)
            previous_side = sides.get(idx)
            sides[idx] = current_side
            if previous_side is None:
                continue
            if not self._segments_intersect(previous_centroid, centroid, p1, p2):
                continue
            if self._crossing_allowed(previous_side, current_side):
                crossed = True
        return crossed

    def detect_motion(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], List[Tuple[int, int, int, int]], bool, Optional[str]]:
        """
        Capture a frame and detect motion.

        Returns:
            Tuple containing:
            - The processed/annotated frame (or None if no frame could be captured)
            - The raw original frame (or None)
            - List of motion rectangles (x, y, w, h)
            - Boolean indicating if gated motion was detected
            - Recording status message (or None if not recording)
        """
        with self._lock:
            if not self.feed_active or not self.cap or not self.cap.isOpened():
                return None, None, [], False, None
            cap = self.cap

        try:
            # Perform blocking network I/O WITHOUT the lock
            ret, frame = cap.read()
            
            # Re-acquire lock for the heavy lifting and shared state access
            with self._lock:
                if not ret:
                    raise RuntimeError("Failed to read frame from camera")
                self.consecutive_read_failures = 0

                # Make copies for different uses
                raw_frame = frame.copy()
                self.last_raw_frame = raw_frame.copy()
                recording_frame = frame.copy()
                
                # Process frame for motion detection
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blurred = cv2.GaussianBlur(gray, (21, 21), 0)
                fgmask = self.fgbg.apply(blurred)
                kernel = np.ones((5, 5), np.uint8)
                fgmask = cv2.dilate(fgmask, kernel, iterations=2)
                contours, _ = cv2.findContours(
                    fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                # Filter contours by size and draw rectangles
                motion_detected = False
                motion_rects = []
                gated_motion_detected = False
                frame_h, frame_w = frame.shape[:2]

                for contour in contours:
                    contour_area = cv2.contourArea(contour)
                    if contour_area < self.min_contour_area:
                        continue

                    (x, y, w, h) = cv2.boundingRect(contour)
                    centroid = (x + w // 2, y + h // 2)
                    in_active_zone = self._point_in_active_zone(centroid, frame_w, frame_h)

                    track_id = self._assign_track(centroid)
                    if self._night_bug_filter_active() and self._is_bug_like_motion(
                        contour_area,
                        track_id,
                        centroid,
                        frame_w,
                        frame_h,
                    ):
                        continue
                    crossed_line = self._line_crossed_for_track(
                        track_id, centroid, frame_w, frame_h
                    )
                    has_zones = bool(self.active_zones)
                    has_lines = bool(self.entry_lines)
                    if not has_zones and not has_lines:
                        should_trigger = True
                    elif has_zones and has_lines:
                        should_trigger = in_active_zone or crossed_line
                    elif has_zones:
                        should_trigger = in_active_zone
                    else:
                        should_trigger = crossed_line
                    if not should_trigger:
                        continue

                    motion_rects.append((x, y, w, h))

                    # Draw rectangle and label on annotated/recording frame
                    cv2.rectangle(recording_frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                    cv2.putText(
                        recording_frame,
                        "Motion Detected",
                        (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 255, 0),
                        2,
                    )
                    motion_detected = True
                    gated_motion_detected = True

                self._cleanup_old_tracks()

                # Handle recording based on motion detection
                recording_status, _ = self.check_recording_status(
                    gated_motion_detected, recording_frame
                )

                # Add recording indicator to the frame if recording
                if self.recording:
                    elapsed_time = time.time() - self.recording_start_time
                    remaining_time = max(0, RECORDING_DURATION - elapsed_time)

                    # Add recording indicator with time remaining
                    cv2.putText(
                        recording_frame,
                        f"REC {remaining_time:.1f}s",
                        (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 0, 255),
                        2,
                    )

                    # Add red circle as recording indicator
                    cv2.circle(recording_frame, (30, 50), 8, (0, 0, 255), -1)

                return recording_frame, raw_frame, motion_rects, gated_motion_detected, recording_status
        except Exception as e:
            with self._lock:
                print(f"Error in motion detection: {e}")
                self.last_error_message = str(e)
                self.consecutive_read_failures += 1
                app_logger.log(
                    "error",
                    "camera_read_error",
                    camera_id=str(self.camera_index),
                    camera_source=str(self.camera_index),
                    component="motion_detector",
                    action="detect_motion",
                    error_message=str(e),
                    retry_outcome=(
                        "camera_stopped"
                        if self.consecutive_read_failures >= self.max_consecutive_read_failures
                        else "will_retry"
                    ),
                    consecutive_failures=self.consecutive_read_failures,
                    max_failures=self.max_consecutive_read_failures,
                )
                if self.consecutive_read_failures >= self.max_consecutive_read_failures:
                    self.stop_camera()
                return None, None, [], False, None

    def capture_image(self, orientation: str = "horizontal") -> Optional[Tuple[np.ndarray, str]]:
        """
        Capture a still image from the camera.
        
        Args:
            orientation: Camera orientation ('horizontal' or 'vertical')
            
        Returns:
            Tuple containing the captured frame and filename, or None if capture failed
        """
        with self._lock:
            if not self.feed_active or not self.cap or not self.cap.isOpened():
                return None
            cap = self.cap

        try:
            ret, frame = cap.read()
            if not ret:
                raise RuntimeError("Failed to capture image")

            if orientation == "vertical":
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{CAPTURE_DIR}/capture_{timestamp}.png"

            return frame, filename
        except Exception as e:
            print(f"Error capturing image: {e}")
            return None

    def cleanup(self) -> None:
        """Clean up camera resources."""
        if self.recording:
            self.stop_recording()
        self.stop_camera()
