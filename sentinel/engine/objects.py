"""
Object events: a shared detection worker, per-camera tracking, and plain-English
descriptions ("A red car pulled into the Driveway").

Cameras submit frames only while there is motion; one worker thread runs the
detector on the newest frame from each camera in turn, so camera threads never
block on inference. Tracks turn into at most one event each.
"""

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.detection.objects import Detection, ObjectDetector

TRACK_TTL_SECONDS = 8.0
MIN_HITS = 2
VEHICLE_MIN_TRAVEL = 0.03  # fraction of frame width a vehicle must move to count as arriving/leaving
STATIC_MEMORY_SECONDS = 7 * 86400
# Minimum best score per group before a track can alert. Tuned on real footage:
# every "person" under ~0.62 was a fan/stand misread in a dim garage.
MIN_SCORE = {"person": 0.62, "animal": 0.6, "vehicle": 0.5}


@dataclass
class Track:
    track_id: int
    label: str
    group: str
    first_seen: float
    last_seen: float
    boxes: List[Tuple[float, Tuple[int, int, int, int]]] = field(default_factory=list)
    best_score: float = 0.0
    hits: int = 0
    reported: bool = False
    best_frame: Optional[np.ndarray] = None
    votes: Dict[str, float] = field(default_factory=dict)

    def vote(self, label: str, score: float) -> None:
        """Small models flip between e.g. car/truck/bus; the label is the score-weighted majority."""
        self.votes[label] = self.votes.get(label, 0.0) + score
        self.label = max(self.votes, key=self.votes.get)

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return self.boxes[-1][1]

    def center(self, index: int = -1) -> Tuple[float, float]:
        x, y, w, h = self.boxes[index][1]
        return x + w / 2.0, y + h / 2.0

    def travel(self) -> Tuple[float, float]:
        (x0, y0), (x1, y1) = self.center(0), self.center(-1)
        return x1 - x0, y1 - y0

    def is_still(self, frame_width: int) -> bool:
        """Hasn't really moved: less than ~one body width (box jitter on a static
        object can be tens of pixels) and less than 3% of the frame."""
        dx, dy = self.travel()
        widths = sorted(b[2] for _, b in self.boxes)
        limit = max(frame_width * VEHICLE_MIN_TRAVEL, widths[len(widths) // 2])
        return max(abs(dx), abs(dy)) < limit


@dataclass
class ObjectEvent:
    camera_key: str
    camera_name: str
    track: Track
    zone_name: Optional[str]
    description: str
    snapshot: np.ndarray  # frame with the object boxed
    timestamp: float


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


# ------------------------------------------------------------------ language
def vehicle_color(frame: np.ndarray, box: Tuple[int, int, int, int]) -> Optional[str]:
    """Colour name for a vehicle only when it is unambiguous, else None.

    Security cameras exaggerate saturation and tint shade and sky reflections blue,
    so grey and black cars often measure as "blue". A wrong colour is worse than
    none, so only clear whites/blacks and vivid colours are named.
    """
    x, y, w, h = box
    crop = frame[y + h // 4: y + 3 * h // 4, x + w // 4: x + 3 * w // 4]
    if crop.size == 0:
        return None
    # IR night images are greyscale: almost no saturation anywhere in the frame.
    small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
    if float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 1].mean()) < 12.0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    hue, sat, val = (float(np.median(hsv[:, i])) for i in range(3))
    vivid = float(((hsv[:, 1] > 120) & (hsv[:, 2] > 70)).mean())
    if val > 190 and sat < 40:
        return "white"
    if val < 50:
        return "black"
    if vivid < 0.6:
        return None
    if hue < 8 or hue >= 170:
        return "red"
    if hue < 20:
        return "orange"
    if hue < 34:
        return "yellow"
    if hue < 85:
        return "green"
    if 100 <= hue < 130 and sat > 180 and val > 90:
        return "blue"
    return None


_EMOJI = {"person": "🚶", "car": "🚗", "truck": "🚚", "bus": "🚌", "motorcycle": "🏍️", "bicycle": "🚲",
          "dog": "🐕", "cat": "🐈", "bird": "🐦", "horse": "🦌", "cow": "🦌", "sheep": "🦌", "bear": "🐻"}
_VEHICLE_NOUN = {"car": "car", "truck": "truck", "bus": "bus", "motorcycle": "motorcycle", "bicycle": "bicycle"}


def describe(track: Track, camera_name: str, zone_name: Optional[str], frame: np.ndarray,
             frame_width: int, companions: int = 0) -> str:
    """One short sentence about what was seen, e.g. 'A red car pulled into the Driveway (Garage).'"""
    emoji = _EMOJI.get(track.label, "👀")
    dx, dy = track.travel()
    if track.group == "vehicle":
        moving = max(abs(dx), abs(dy)) > frame_width * VEHICLE_MIN_TRAVEL
    else:
        moving = not track.is_still(frame_width)
    # "in the Driveway (Garage)" with a named zone, otherwise "at the Front Door"
    at = f"in the {zone_name} ({camera_name})" if zone_name else f"at the {camera_name}"
    past = f"the {zone_name} ({camera_name})" if zone_name else f"the {camera_name}"

    if track.group == "person":
        if companions:
            who, be = f"{companions + 1} people", "are"
        else:
            who, be = "Someone", "is"
        return f"{emoji} {who} {be} walking past {past}." if moving else f"{emoji} {who} {be} {at}."

    if track.group == "vehicle":
        if track.label == "bicycle":
            return f"{emoji} Someone on a bicycle is {at}."
        noun = _VEHICLE_NOUN.get(track.label, "vehicle")
        if track.label in ("bus", "truck", "motorcycle") and track.best_score < 0.7:
            noun, emoji = "vehicle", _EMOJI["car"]  # the small model often mistakes cars for these
        color = vehicle_color(frame, track.box)
        thing = f"{color} {noun}" if color else noun
        a = "An" if thing[0] in "aeiou" else "A"
        if moving and abs(dy) >= abs(dx):
            # Moving toward the camera reads as arriving, away as leaving.
            if zone_name:
                place = f"the {zone_name} ({camera_name})"
                return f"{emoji} {a} {thing} {'pulled into' if dy > 0 else 'is leaving'} {place}."
            return f"{emoji} {a} {thing} {'pulled in' if dy > 0 else 'is leaving'} at the {camera_name}."
        if moving:
            return f"{emoji} {a} {thing} is driving past {past}."
        return f"{emoji} {a} {thing} is {at}."

    if track.label in ("dog", "cat", "bird", "bear"):
        return f"{emoji} A {track.label} is {at}."
    return f"{emoji} An animal (looks like a {track.label}, maybe a deer) is {at}."


# ------------------------------------------------------------------ tracking
class CameraObjectTracker:
    """Turns a camera's detections into events (once per object)."""

    _ids = itertools.count(1)

    def __init__(self, camera_key: str) -> None:
        self.camera_key = camera_key
        self.tracks: List[Track] = []
        # Places where a never-moving "person"/"animal" was detected before: usually a
        # coat, fan or sack the model misreads. [box, sightings, last_seen]
        self.static_spots: List[List] = []

    def update(self, detections: List[Detection], frame: np.ndarray, now: float,
               in_zone: Callable[[Tuple[int, int, int, int]], bool]) -> List[Track]:
        """Match detections to tracks; return tracks that just became reportable."""
        self._expire(now, frame.shape[1])
        unmatched = list(detections)
        for track in self.tracks:
            best, best_score = None, 0.0
            tx, ty = track.center()
            _, _, tw, th = track.box
            reach = 1.5 * max(tw, th)  # how far it can plausibly move between samples (~0.5 s)
            for det in unmatched:
                if det.group != track.group:
                    continue
                overlap = _iou(det.box, track.box)
                dx = det.box[0] + det.box[2] / 2.0 - tx
                dy = det.box[1] + det.box[3] / 2.0 - ty
                near = 1.0 - (dx * dx + dy * dy) ** 0.5 / reach
                # Prefer overlap; fall back to nearness for fast movers whose boxes don't overlap.
                score = overlap + 0.5 if overlap > 0.25 else (near if near > 0 else 0.0)
                if score > best_score:
                    best, best_score = det, score
            if best is not None:
                unmatched.remove(best)
                track.vote(best.label, best.score)
                track.boxes.append((now, best.box))
                track.boxes = track.boxes[-30:]
                track.last_seen = now
                track.hits += 1
                if best.score > track.best_score:
                    track.best_score = best.score
                    track.best_frame = frame
        for det in unmatched:
            track = Track(next(self._ids), det.label, det.group, now, now,
                          [(now, det.box)], det.score, 1, best_frame=frame)
            track.vote(det.label, det.score)
            self.tracks.append(track)

        frame_w = frame.shape[1]
        ready: List[Track] = []
        for track in self.tracks:
            if track.reported or track.hits < MIN_HITS or track.best_score < MIN_SCORE.get(track.group, 0.5):
                continue
            if not in_zone(track.box):
                continue
            if track.group == "vehicle":
                dx, dy = track.travel()
                if max(abs(dx), abs(dy)) < frame_w * VEHICLE_MIN_TRAVEL:
                    # A parked vehicle re-detected when something else moves: after a few
                    # sightings without moving, retire it silently.
                    if track.hits >= 4:
                        track.reported = True
                    continue
            else:
                if track.group == "animal" and any(
                    t.group == "person" and now - t.last_seen < 1.0 and _iou(t.box, track.box) > 0.3
                    for t in self.tracks
                ):
                    track.reported = True  # the small model sometimes calls a person a dog
                    continue
                if track.is_still(frame_w):
                    if track.best_score < 0.65:
                        continue  # a motionless, uncertain "person" needs more evidence
                    if any(self._matches_spot(track.box, spot[0]) for spot in self.static_spots):
                        track.reported = True  # learned scenery; stay quiet
                        continue
            track.reported = True
            ready.append(track)

        return ready

    def _expire(self, now: float, frame_w: int) -> None:
        """Drop tracks not seen recently; remember where motionless 'people' were."""
        alive = []
        for t in self.tracks:
            if now - t.last_seen < TRACK_TTL_SECONDS:
                alive.append(t)
            elif t.group != "vehicle" and t.hits >= MIN_HITS and t.is_still(frame_w):
                self._remember_static(t.box, now)
        self.tracks = alive
        self.static_spots = [s for s in self.static_spots if now - s[2] < STATIC_MEMORY_SECONDS]

    @staticmethod
    def _matches_spot(box: Tuple[int, int, int, int], spot: Tuple[int, int, int, int]) -> bool:
        cx, cy = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
        inside = spot[0] <= cx <= spot[0] + spot[2] and spot[1] <= cy <= spot[1] + spot[3]
        return inside or _iou(box, spot) > 0.4

    def _remember_static(self, box: Tuple[int, int, int, int], now: float) -> None:
        for spot in self.static_spots:
            if self._matches_spot(box, spot[0]):
                spot[1] += 1
                spot[2] = now
                return
        self.static_spots.append([box, 1, now])


# ------------------------------------------------------------------ worker
class DetectionWorker:
    """One thread running the detector over the newest frame from each camera."""

    def __init__(self, detector: ObjectDetector, min_interval: float = 0.5) -> None:
        self.detector = detector
        self.min_interval = min_interval
        self._slots: Dict[str, Tuple[np.ndarray, float, Callable]] = {}
        self._last_run: Dict[str, float] = {}
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="object-detector", daemon=True)
        self.inference_ms = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def submit(self, camera_key: str, frame: np.ndarray, callback: Callable[[List[Detection], np.ndarray, float], None]) -> bool:
        """Offer a frame; skipped if this camera was analysed too recently."""
        now = time.time()
        with self._cond:
            if now - self._last_run.get(camera_key, 0.0) < self.min_interval:
                return False
            self._last_run[camera_key] = now
            self._slots[camera_key] = (frame, now, callback)
            self._cond.notify()
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._slots and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                key = next(iter(self._slots))
                frame, ts, callback = self._slots.pop(key)
            started = time.time()
            try:
                detections = self.detector.detect(frame)
            except Exception:
                detections = []
            self.inference_ms = 0.8 * self.inference_ms + 0.2 * (time.time() - started) * 1000
            try:
                callback(detections, frame, ts)
            except Exception:
                pass
