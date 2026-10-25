"""
One camera's pipeline: capture -> motion analysis -> recording, in its own thread.

The pipeline owns reconnecting its camera, so one camera dropping never affects
the others. It reports what happens (motion, finished clips, up/down) to the
engine through callbacks; it never touches Telegram, audio or the web directly.
"""

import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import cv2
import numpy as np

from sentinel.constants import MIN_CONTOUR_AREA, MOTION_COOLDOWN, VAR_THRESHOLD
from sentinel.detection import MotionDetector
from sentinel.logging_utils import app_logger, redact
from sentinel.recordings.hd_recorder import HDRecorder, main_stream_url

if TYPE_CHECKING:
    from sentinel.engine.objects import DetectionWorker, ObjectEvent
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


def zoomed_view(frame: np.ndarray, box: Tuple[int, int, int, int], width: int = 960) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    """A 16:9 close-up around ``box`` (about 4x its size), scaled to ``width``; returns (image, box in it)."""
    fh, fw = frame.shape[:2]
    x, y, w, h = box
    cw = int(min(fw, max(4 * w, 4 * h * 16 / 9, 480)))
    ch = int(min(fh, cw * 9 / 16))
    x0 = int(min(max(0, x + w / 2 - cw / 2), fw - cw))
    y0 = int(min(max(0, y + h / 2 - ch / 2), fh - ch))
    scale = width / cw
    crop = cv2.resize(frame[y0:y0 + ch, x0:x0 + cw], (width, int(ch * scale)), interpolation=cv2.INTER_AREA)
    return crop, (int((x - x0) * scale), int((y - y0) * scale), int(w * scale), int(h * scale))


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
        detection_worker: Optional["DetectionWorker"] = None,
        on_object: Optional[Callable[["CameraPipeline", "ObjectEvent"], None]] = None,
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
        self._last_hd_grab = 0.0
        self._broker = frame_broker
        self._settings = settings
        self._on_motion = on_motion
        self._on_clip = on_clip
        self._on_status = on_status
        self._detections = detection_worker
        self._on_object = on_object
        self._clip_labels: set = set()
        self.tracker = None
        if detection_worker is not None:
            from sentinel.engine.objects import CameraObjectTracker

            memory = None
            if recording_store is not None:
                memory = os.path.join(recording_store.root, ".object_memory.json")
            self.tracker = CameraObjectTracker(self.host, memory)
            self.tracker.person_min_score = float(profile.get("person_min_score", self.tracker.person_min_score))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending_clip_chats: List[str] = []
        self._clip_chats: List[str] = []
        self.last_clip_labels: List[str] = []
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
        if getattr(self, "tracker", None) is not None and "person_min_score" in profile:
            self.tracker.person_min_score = float(profile["person_min_score"])
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
            if self._detections is not None and raw is not None and self.detector.activity_streak > 0:
                self._detections.submit(self.host, raw, self._handle_detections)
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
        if span and span[2] is not None and self._clip_labels and self._store is not None:
            self._store.set_labels(span[2], sorted(self._clip_labels))
        labels, self._clip_labels = sorted(self._clip_labels), set()
        self.last_clip_labels = labels
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

    # ------------------------------------------------------------ objects
    def zone_name_at(self, point: Tuple[int, int], width: int, height: int) -> Optional[str]:
        names = self.profile.get("zone_names") or []
        for i, zone in enumerate(self.profile.get("active_zones") or []):
            if i >= len(names) or not names[i]:
                continue
            poly = np.array([[int(px * width), int(py * height)] for px, py in zone], dtype=np.int32)
            if len(poly) >= 3 and cv2.pointPolygonTest(poly, (float(point[0]), float(point[1])), False) >= 0:
                return names[i]
        return None

    def _handle_detections(self, detections, frame: np.ndarray, ts: float) -> None:
        """Runs on the detection worker thread."""
        from sentinel.engine.objects import wants_type_check

        h, w = frame.shape[:2]

        def in_zone(box) -> bool:
            with self.detector._lock:
                return self.detector._box_in_active_zone(*box, w, h)

        ready = self.tracker.update(detections, frame, ts, in_zone)
        confirmed = [t for t in self.tracker.tracks if t.hits >= 2 and t.best_score >= 0.5]
        checker = getattr(self._detections, "subject_checker", None)
        for track in confirmed + [t for t in ready if t not in confirmed]:
            if track.group in ("person", "animal") and not track.subject_checked:
                self._check_subject(checker, track)
        confirmed = [t for t in confirmed if not t.not_a_subject]
        if self.detector.recording:
            # A parked car seen while the wind moves the trees doesn't make it a "car" clip.
            self._clip_labels.update(
                t.label for t in confirmed
                if t.group != "vehicle" or t.vehicle_moved(w))
        classifier = getattr(self._detections, "vehicle_classifier", None)
        presence = getattr(self._detections, "presence", None)
        for track in ready:
            if track.not_a_subject:
                continue  # a bin or a fan, not a person
            if track.group == "vehicle" and presence is not None and self._is_parked_car(presence, track):
                continue
            if classifier is not None and wants_type_check(track):
                if self._check_in_hd_later(classifier, track, confirmed, ts, first=True):
                    continue  # reported once the HD look is done
                self._check_vehicle_type(classifier, track)
            self._report_track(track, confirmed, ts)
        if classifier is None:
            return
        # Vehicles that already alerted get another look as they come closer: a van that
        # pulls up to the curb is much easier to recognise than when it first appeared.
        for track in self.tracker.tracks:
            if track.alerted and track.type_checks and wants_type_check(track):
                if self._check_in_hd_later(classifier, track, confirmed, ts, first=False):
                    continue
                if self._check_vehicle_type(classifier, track):
                    self._report_track(track, confirmed, ts)

    # Vehicles smaller than this fraction of the frame width are looked at in the
    # main (HD) stream: 4x the pixels makes a van on the street recognisable.
    HD_FAR_FRACTION = 0.25
    HD_MIN_INTERVAL = 2.0  # seconds between HD grabs per camera (each costs ~0.6 CPU-seconds)

    def _check_in_hd_later(self, classifier, track, confirmed, ts: float, first: bool) -> bool:
        """Queue a type check on the HD frame for a far vehicle; False if not applicable/busy."""
        hd, worker = self.hd, self._detections
        if hd is None or not hd.running or not hasattr(worker, "run_later"):
            return False
        _, _, bw, bh = track.big_box
        if max(bw, bh) >= self.HD_FAR_FRACTION * track.big_frame.shape[1]:
            return False
        now = time.time()
        if now - self._last_hd_grab < self.HD_MIN_INTERVAL:
            return False
        track.type_pending = True

        def job() -> None:
            try:
                view = self._hd_view(track)
                newly_delivery = self._check_vehicle_type(classifier, track, view)
                if first or newly_delivery:
                    self._report_track(track, confirmed, ts)
            except Exception as e:
                app_logger.log("error", "hd_vehicle_check_failed", error_message=str(e),
                               camera_display=self.display_name, component="camera", action="hd_check")
                if first and not track.alerted:
                    self._report_track(track, confirmed, ts)  # never lose the alert itself
            finally:
                track.type_pending = False

        if not worker.run_later(job):
            track.type_pending = False
            return False
        self._last_hd_grab = now
        return True

    def _hd_view(self, track) -> Optional[Tuple[np.ndarray, Tuple[int, int, int, int]]]:
        """(HD frame, vehicle box in it) for the track's closest sighting, or None."""
        big = self.hd.keyframe_near(track.big_ts) if self.hd is not None else None
        detector = getattr(self._detections, "detector", None)
        if big is None or detector is None:
            return None
        sub_h, sub_w = track.big_frame.shape[:2]
        hd_h, hd_w = big.shape[:2]
        sx, sy = hd_w / sub_w, hd_h / sub_h
        x, y, w, h = track.big_box
        cx, cy, bw, bh = (x + w / 2) * sx, (y + h / 2) * sy, w * sx, h * sy
        # The keyframe can be up to ~1 s away from the sighting, so search around it.
        rx0, rx1 = int(max(0, cx - 2.5 * bw)), int(min(hd_w, cx + 2.5 * bw))
        ry0, ry1 = int(max(0, cy - 1.5 * bh)), int(min(hd_h, cy + 1.5 * bh))
        region = big[ry0:ry1, rx0:rx1]
        if region.size == 0:
            return None
        found = [d for d in detector.detect(region, min_score=0.3) if d.group == "vehicle" and d.label != "bicycle"]

        def cost(d) -> float:
            dx = d.box[0] + d.box[2] / 2 + rx0 - cx
            dy = d.box[1] + d.box[3] / 2 + ry0 - cy
            size = abs(np.log(max(1, d.box[2] * d.box[3]) / max(1.0, bw * bh)))
            return (dx * dx + dy * dy) ** 0.5 / max(bw, bh) + size

        if not found:
            return None
        best = min(found, key=cost)
        if cost(best) > 3.0:
            return None  # probably a different vehicle
        bx, by, bw2, bh2 = best.box
        return big, (bx + rx0, by + ry0, bw2, bh2)

    def _is_parked_car(self, presence, track) -> bool:
        """A car the driveway check already knows is parked here: people opening doors or walking
        around it shouldn't make it alert. It still alerts once it has driven out of its spot."""
        spot = presence.parked_spot(self.host, track.boxes[0][1])
        if spot is None:
            return False
        if presence.parked_spot(self.host, track.box, min_iou=0.3) is spot:
            track.reported = False  # still in its spot: look again on the next detection
            return True
        return False  # it started in a parked spot and has left it: "… is leaving"

    def _check_subject(self, checker, track) -> None:
        """CLIP second opinion on a person/animal track: relabel it (a "person" that's really a
        cat) or mark it as an object (a bin) so it never alerts."""
        track.subject_checked = True
        if checker is None:
            return
        try:
            verdict = checker.check(track.best_frame, track.best_box or track.box, track.group, track.best_score)
        except Exception as e:
            app_logger.log("error", "subject_check_failed", error_message=str(e), camera_display=self.display_name,
                           component="camera", action="subject_check")
            return
        if verdict is None or not verdict.kind:
            return
        app_logger.log("info", "subject_relabelled", camera_display=self.display_name, detector_label=track.label,
                       verdict=verdict.kind, probability=round(verdict.probs.get(verdict.kind, 0.0), 2),
                       component="camera", action="subject_check")
        if verdict.kind == "object":
            track.not_a_subject = True
            return
        track.detector_group = track.group
        track.label = verdict.kind
        track.group = "person" if verdict.kind == "person" else "animal"
        track.votes = {verdict.kind: 1e9}  # later detections keep the corrected label

    def _check_vehicle_type(self, classifier, track, view=None) -> bool:
        """Classify the track's closest view (HD if given); True if it just turned out to be a delivery."""
        from sentinel.detection.vehicle_type import crop_for_clip, is_greyscale

        frame, box = view if view is not None else (track.big_frame, track.big_box)
        try:
            embedding = classifier.embed(crop_for_clip(frame, box))
            vt = classifier.classify(frame, box, embedding)
            household = getattr(self._detections, "household", None)
            if household is not None and track.known_vehicle is None:
                vehicle_id, _ = household.identify(embedding, is_greyscale(frame))
                if vehicle_id is not None:
                    track.known_vehicle = household.vehicle(vehicle_id)
            if vt is not None and vt.delivery and track.known_vehicle is not None:
                vt = None  # one of the family's cars, not an Amazon van
        except Exception as e:  # never let the classifier break detection
            app_logger.log("error", "vehicle_classifier_failed", error_message=str(e),
                           camera_display=self.display_name, component="camera", action="classify")
            vt = None
        track.type_checks += 1
        track.type_checked_area = track.big_box[2] * track.big_box[3]
        if view is not None:
            track.hd_view = view
        if vt is None:
            return False
        newly_delivery = vt.delivery and not (track.vehicle_type and track.vehicle_type.delivery)
        track.vehicle_type = vt
        if vt.delivery and self.detector.recording:
            self._clip_labels.add("delivery")
        return newly_delivery and track.alerted

    def _report_track(self, track, confirmed, ts: float) -> None:
        from sentinel.engine.objects import ObjectEvent, describe

        source = track.best_frame
        h, w = source.shape[:2]
        x, y, bw, bh = track.best_box or track.box
        zone = self.zone_name_at((x + bw // 2, y + bh), w, h)
        companions = sum(1 for t in confirmed if t is not track and t.group == track.group == "person")
        vt = track.vehicle_type
        tag = vt.phrase if vt is not None and vt.delivery else track.label
        if track.hd_view is not None:
            snapshot, (x, y, bw, bh) = zoomed_view(*track.hd_view)  # far away: send the HD close-up
        else:
            snapshot = source.copy()
        cv2.rectangle(snapshot, (x, y), (x + bw, y + bh), (0, 200, 255), 2)
        cv2.putText(snapshot, tag, (x, max(12, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 2)
        event = ObjectEvent(
            camera_key=self.host,
            camera_name=self.display_name,
            track=track,
            zone_name=zone,
            description=describe(track, self.display_name, zone, source, w, companions),
            snapshot=snapshot,
            timestamp=ts,
        )
        track.alerted = True
        self.detector.note_subject(track.box, w, h)
        if self.detector.recording:
            self._clip_labels.add(track.label)
            if vt is not None and vt.delivery:
                self._clip_labels.add("delivery")
        if self._on_object is not None:
            self._on_object(self, event)

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
        elif web.get("show_zones", True):
            frame = draw_zones_and_lines(annotated, self.profile)
        else:
            frame = annotated  # zones still apply; they just aren't drawn
        self._broker.publish(
            self.camera_id,
            self.display_name,
            frame,
            jpeg_quality=int(web.get("jpeg_quality", 80)),
        )
