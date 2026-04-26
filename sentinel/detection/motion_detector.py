"""
Motion detection functionality for Sentinel Home.
"""

import os
import cv2
import numpy as np
import time
from typing import Tuple, List, Optional, Union, Dict, Any

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
        self.motion_cooldown = MOTION_COOLDOWN

        # Zone and line based alert gating
        self.active_zones = DEFAULT_ACTIVE_ZONES.copy()
        self.entry_lines = DEFAULT_ENTRY_LINES.copy()
        self.entry_direction = DEFAULT_ENTRY_DIRECTION

        # Lightweight centroid tracking for line crossing
        self.tracks: Dict[int, Dict[str, Any]] = {}
        self.next_track_id = 1
        self.track_ttl_seconds = 1.2
        self.track_match_distance_px = 80.0

        # Reconnect/read-failure support
        self.consecutive_read_failures = 0
        self.max_consecutive_read_failures = MAX_CONSECUTIVE_READ_FAILURES
        self.last_raw_frame = None

        # Create video directory if it doesn't exist
        if not os.path.exists(VIDEO_DIR):
            os.makedirs(VIDEO_DIR)

    def set_active_zones(self, zones: List[List[List[float]]]) -> None:
        self.active_zones = zones or []

    def set_entry_lines(self, lines: List[List[List[float]]]) -> None:
        self.entry_lines = lines or []

    def set_entry_direction(self, direction: str) -> None:
        if direction in ("outside_to_inside", "inside_to_outside", "both"):
            self.entry_direction = direction

    def get_last_raw_frame(self) -> Optional[np.ndarray]:
        """Return the latest unannotated frame."""
        if self.last_raw_frame is None:
            return None
        return self.last_raw_frame.copy()

    def start_recording(self, frame_size):
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

            duration = time.time() - self.recording_start_time
            print(
                f"Stopped recording. Duration: {duration:.2f}s, Frames: {self.frames_recorded}"
            )

            return True
        except Exception as e:
            print(f"Error stopping recording: {e}")
            self.video_writer = None
            self.recording = False
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

            # Check if recording duration has elapsed
            current_time = time.time()
            if (current_time - self.recording_start_time) >= RECORDING_DURATION:
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

    def start_camera(self, camera_index: Union[int, str]) -> bool:
        """
        Start the camera at the specified index.

        Args:
            camera_index: Index of the camera to use

        Returns:
            True if camera started successfully, False otherwise
        """
        if self.feed_active:
            self.stop_camera()

        try:
            self.camera_index = camera_index
            if isinstance(camera_index, int):
                self.cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
            else:
                self.cap = cv2.VideoCapture(camera_index)
            if not self.cap.isOpened():
                raise RuntimeError(f"Failed to open camera {camera_index}")

            # Verify we can actually read frames (isOpened alone is not enough)
            if not self.cap.grab():
                raise RuntimeError(f"Camera {camera_index} opened but cannot grab frames")

            self.feed_active = True
            self.consecutive_read_failures = 0
            return True
        except Exception as e:
            print(f"Error starting camera: {e}")
            if self.cap:
                self.cap.release()
                self.cap = None
            self.feed_active = False
            return False

    def stop_camera(self) -> None:
        """Stop the currently active camera."""
        self.feed_active = False
        if self.cap:
            self.cap.release()
            self.cap = None
        self.consecutive_read_failures = 0
        self.tracks = {}

    def update_var_threshold(self, value):
        """Update the variance threshold and recreate the background subtractor."""
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
            self.tracks[best_id] = {"centroid": centroid, "last_seen": time.time(), "line_sides": {}}
            return best_id

        self.tracks[best_id]["centroid"] = centroid
        self.tracks[best_id]["last_seen"] = time.time()
        return best_id

    def _line_crossed_for_track(
        self,
        track_id: int,
        centroid: Tuple[int, int],
        width: int,
        height: int,
    ) -> bool:
        if not self.entry_lines:
            return True

        track = self.tracks.get(track_id)
        if not track:
            return False

        sides = track.setdefault("line_sides", {})
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
            if self._crossing_allowed(previous_side, current_side):
                crossed = True
        return crossed

    def detect_motion(self):
        """
        Capture a frame and detect motion.

        Returns:
            Tuple containing:
            - The processed frame (or None if no frame could be captured)
            - List of motion rectangles (x, y, w, h)
            - Boolean indicating if motion was detected
            - Recording status message (or None if not recording)
        """
        if not self.feed_active or not self.cap or not self.cap.isOpened():
            return None, [], False, None

        try:
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError("Failed to read frame from camera")
            self.consecutive_read_failures = 0

            # Make a copy of the original frame for recording
            original_frame = frame.copy()
            self.last_raw_frame = original_frame.copy()
            recording_frame = original_frame.copy()

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
                if cv2.contourArea(contour) < self.min_contour_area:
                    continue

                (x, y, w, h) = cv2.boundingRect(contour)
                centroid = (x + w // 2, y + h // 2)
                if not self._point_in_active_zone(centroid, frame_w, frame_h):
                    continue

                track_id = self._assign_track(centroid)
                crossed_line = self._line_crossed_for_track(track_id, centroid, frame_w, frame_h)

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
                if crossed_line:
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

            return recording_frame, motion_rects, gated_motion_detected, recording_status
        except Exception as e:
            print(f"Error in motion detection: {e}")
            self.consecutive_read_failures += 1
            if self.consecutive_read_failures >= self.max_consecutive_read_failures:
                self.stop_camera()
            return None, [], False, None

    def capture_image(self, orientation: str = "horizontal") -> Optional[Tuple[np.ndarray, str]]:
        """
        Capture a still image from the camera.
        
        Args:
            orientation: Camera orientation ('horizontal' or 'vertical')
            
        Returns:
            Tuple containing the captured frame and filename, or None if capture failed
        """
        if not self.feed_active or not self.cap or not self.cap.isOpened():
            return None

        try:
            ret, frame = self.cap.read()
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
