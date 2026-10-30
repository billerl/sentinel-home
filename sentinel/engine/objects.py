"""
Object events: a shared detection worker, per-camera tracking, and plain-English
descriptions ("A red car pulled into the Driveway").

Cameras submit frames only while there is motion; one worker thread runs the
detector on the newest frame from each camera in turn, so camera threads never
block on inference. Tracks turn into at most one event each.
"""

import itertools
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.detection.objects import Detection, ObjectDetector
from sentinel.detection.vehicle_type import VehicleClassifier, VehicleType

TRACK_TTL_SECONDS = 8.0
MIN_HITS = 2
VEHICLE_MIN_TRAVEL = 0.03  # fraction of frame width a vehicle must move to count as arriving/leaving
# ... and this fraction of its own size: opening a parked car's door widens its box and shifts its
# centre by a fifth of its width or so, which used to read as "A car is driving past".
VEHICLE_MIN_OWN_TRAVEL = 0.35
# A car driving straight towards or away from the camera mostly changes size, not position
# (an opening door adds only ~25% to the box).
VEHICLE_MIN_AREA_CHANGE = 1.5
STATIC_MEMORY_SECONDS = 7 * 86400
# Minimum best score per group before a track can alert. Tuned on real footage:
# a person sitting on a porch at night scored 0.40-0.52 with the tiny model, while
# a fan in a dim garage scored 0.53-0.65 (handled by static-spot learning + zones).
MIN_SCORE = {"person": 0.45, "animal": 0.6, "vehicle": 0.5}
CONFIDENT_SCORE = 0.62  # below this, a track needs EXTRA_HITS sightings
EXTRA_HITS = 3
# Vehicle type (CLIP) is checked when a vehicle first alerts, then again as it comes
# closer (box area grown by TYPE_RECHECK_GROWTH), up to TYPE_MAX_CHECKS times.
TYPE_MAX_CHECKS = 3
TYPE_RECHECK_GROWTH = 1.6


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
    reported: bool = False  # the tracker has decided about it (alerted or retired)
    alerted: bool = False  # an event was sent for it
    best_frame: Optional[np.ndarray] = None
    best_box: Optional[Tuple[int, int, int, int]] = None  # the box in best_frame
    votes: Dict[str, float] = field(default_factory=dict)
    vehicle_type: Optional["VehicleType"] = None  # set by the CLIP classifier, if any
    known_vehicle: Optional[Any] = None  # a household vehicle (sentinel.engine.household.Vehicle), if recognised
    subject_checked: bool = False  # CLIP second opinion done (person/animal tracks)
    not_a_subject: bool = False  # CLIP says it's an object (bin, fan, coat...): never alert or label
    detector_group: Optional[str] = None  # what the detector calls it, if CLIP relabelled it (still matches)
    # Closest (largest) view so far, for the vehicle-type check.
    big_frame: Optional[np.ndarray] = None
    big_box: Optional[Tuple[int, int, int, int]] = None
    big_ts: float = 0.0
    type_pending: bool = False  # a background (HD) type check is queued
    hd_view: Optional[Tuple[np.ndarray, Tuple[int, int, int, int]]] = None  # HD frame + box, if one was used
    type_checks: int = 0
    type_checked_area: int = 0
    left_spot: bool = False  # a parked car whose parking spot is now empty: it's leaving

    def see(self, frame: np.ndarray, box: Tuple[int, int, int, int], now: float = 0.0) -> None:
        if self.big_box is None or box[2] * box[3] > self.big_box[2] * self.big_box[3]:
            self.big_frame, self.big_box, self.big_ts = frame, box, now

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

    def vehicle_moved(self, frame_width: int) -> bool:
        """Really drove (not box jitter or a door opening): moved 3% of the frame and a third of its
        size, or its box area changed 1.5x (coming straight at the camera or driving away from it)."""
        dx, dy = self.travel()
        sizes = sorted(max(b[2], b[3]) for _, b in self.boxes)
        limit = max(frame_width * VEHICLE_MIN_TRAVEL, VEHICLE_MIN_OWN_TRAVEL * sizes[len(sizes) // 2])
        if max(abs(dx), abs(dy)) >= limit:
            return True
        areas = [b[2] * b[3] for _, b in self.boxes]
        first, last = max(1, areas[0]), max(1, areas[-1])
        return max(first, last) / min(first, last) >= VEHICLE_MIN_AREA_CHANGE

    def is_still(self, frame_width: int) -> bool:
        """Hasn't really moved: less than ~one body width (box jitter on a static
        object can be tens of pixels) and less than 3% of the frame."""
        dx, dy = self.travel()
        widths = sorted(b[2] for _, b in self.boxes)
        limit = max(frame_width * VEHICLE_MIN_TRAVEL, widths[len(widths) // 2])
        return max(abs(dx), abs(dy)) < limit

    def stayed_put(self, frame_width: int) -> bool:
        """is_still for the whole path, not just where it started and ended: a delivery driver who
        walks to the door and back ends where they began, but isn't scenery."""
        widths = sorted(b[2] for _, b in self.boxes)
        limit = max(frame_width * VEHICLE_MIN_TRAVEL, widths[len(widths) // 2])
        x0, y0 = self.center(0)
        return all(max(abs(x0 - (b[0] + b[2] / 2.0)), abs(y0 - (b[1] + b[3] / 2.0))) < limit
                   for _, b in self.boxes)


@dataclass
class ObjectEvent:
    camera_key: str
    camera_name: str
    track: Track
    zone_name: Optional[str]
    description: str
    snapshot: np.ndarray  # frame with the object boxed
    timestamp: float
    alert_group: str = ""  # 'delivery' for recognised delivery vehicles, else the track's group

    def __post_init__(self) -> None:
        # Fixed at creation: the track keeps changing after the event is sent.
        if not self.alert_group:
            vt = self.track.vehicle_type
            self.alert_group = "delivery" if vt is not None and vt.delivery else self.track.group


def wants_type_check(track: Track) -> bool:
    """An alerted vehicle not yet known to be a delivery that is now clearly bigger than when last checked."""
    if track.group != "vehicle" or track.label == "bicycle" or track.big_box is None or track.type_pending:
        return False
    if track.vehicle_type is not None and track.vehicle_type.delivery:
        return False
    if track.type_checks == 0:
        return True
    if track.type_checks >= TYPE_MAX_CHECKS:
        return False
    _, _, w, h = track.big_box
    return w * h >= TYPE_RECHECK_GROWTH * track.type_checked_area


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
    # IR night images are greyscale: no colour to name.
    from sentinel.detection.vehicle_type import is_greyscale

    if is_greyscale(frame):
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
          "dog": "🐕", "cat": "🐈", "bird": "🐦", "horse": "🦌", "cow": "🦌", "sheep": "🦌", "bear": "🐻",
          "delivery": "📦", "pickup": "🛻", "suv": "🚙", "minivan": "🚐", "van": "🚐", "school_bus": "🚌",
          "garbage": "🚛", "sedan": "🚗", "deer": "🦌"}
_VEHICLE_NOUN = {"car": "car", "truck": "truck", "bus": "bus", "motorcycle": "motorcycle", "bicycle": "bicycle"}


def _article(thing: str) -> str:
    # "an SUV", "an Amazon van", "an orange car", but "a UPS truck"
    return "An" if thing.startswith("SUV") or thing[0].lower() in "aeio" else "A"


def vehicle_phrase(track: Track, frame: np.ndarray) -> Tuple[str, str]:
    """(emoji, noun phrase) for a vehicle track, e.g. ('📦', 'UPS truck') or ('🛻', 'red pickup truck')."""
    vt = track.vehicle_type
    if vt is not None and vt.delivery:
        return _EMOJI["delivery"], vt.phrase  # brand colours speak for themselves
    if track.known_vehicle is not None:  # one of the family's cars: "Luke's Sierra"
        return _EMOJI.get(track.known_vehicle.kind, _EMOJI["car"]), track.known_vehicle.name
    emoji = _EMOJI.get(track.label, "👀")
    noun = _VEHICLE_NOUN.get(track.label, "vehicle")
    if vt is not None and (vt.key != "motorcycle" or track.label == "motorcycle") and track.label != "bicycle":
        noun, emoji = vt.phrase, _EMOJI.get(vt.key, _EMOJI["car"])
    elif track.label in ("bus", "truck", "motorcycle") and track.best_score < 0.7:
        noun, emoji = "vehicle", _EMOJI["car"]  # the small model often mistakes cars for these
    color = vehicle_color(frame, track.best_box or track.box)
    return emoji, f"{color} {noun}" if color else noun


_IN_PLACES = {"yard", "backyard", "driveway", "drive", "garden", "garage", "lawn", "carport", "field", "lot"}
_ON_PLACES = {"porch", "deck", "patio", "street", "road", "sidewalk", "steps", "stoop", "walkway", "path"}


def _place(zone_name: Optional[str], camera_name: str) -> str:
    """'the Driveway (Garage)'; just 'the Back Yard' when the zone is named after its camera."""
    if not zone_name or zone_name.strip().lower() == camera_name.strip().lower():
        return f"the {zone_name or camera_name}"
    return f"the {zone_name} ({camera_name})"


def _preposition(name: str) -> str:
    """in the Back Yard, on the Porch, at the Front Door."""
    words = name.lower().split()
    last = words[-1] if words else ""
    return "in" if last in _IN_PLACES else "on" if last in _ON_PLACES else "at"


def describe(track: Track, camera_name: str, zone_name: Optional[str], frame: np.ndarray,
             frame_width: int, companions: int = 0) -> str:
    """One short sentence about what was seen, e.g. 'A red car pulled into the Driveway (Garage).'"""
    emoji = _EMOJI.get(track.label, "👀")
    dx, dy = track.travel()
    if track.group == "vehicle":
        moving = track.vehicle_moved(frame_width)
    else:
        moving = not track.is_still(frame_width)
    # "in the Driveway (Garage)" with a named zone, otherwise "at the Front Door"
    # (A bare camera name keeps "at": "in the Garage" would sound like inside it.)
    at = f"{_preposition(zone_name) if zone_name else 'at'} {_place(zone_name, camera_name)}"
    past = _place(zone_name, camera_name)

    if track.group == "person":
        if companions:
            who, be = f"{companions + 1} people", "are"
        else:
            who, be = "Someone", "is"
        return f"{emoji} {who} {be} walking past {past}." if moving else f"{emoji} {who} {be} {at}."

    if track.group == "vehicle":
        if track.label == "bicycle":
            return f"{emoji} Someone on a bicycle is {at}."
        emoji, thing = vehicle_phrase(track, frame)
        if track.known_vehicle is not None and not (track.vehicle_type and track.vehicle_type.delivery):
            subject = thing[:1].upper() + thing[1:]  # a name: "Luke's Sierra", "The blue Civic"
        else:
            subject = f"{_article(thing)} {thing}"
        if track.left_spot:
            return f"{emoji} {subject} is leaving {past}."
        if moving and abs(dy) >= abs(dx):
            # Moving toward the camera reads as arriving, away as leaving.
            if zone_name:
                arriving = "pulled into" if _preposition(zone_name) == "in" else "pulled up at"
                return f"{emoji} {subject} {arriving if dy > 0 else 'is leaving'} {past}."
            return f"{emoji} {subject} {'pulled in' if dy > 0 else 'is leaving'} at the {camera_name}."
        if moving:
            return f"{emoji} {subject} is driving past {past}."
        return f"{emoji} {subject} is {at}."

    if track.label in ("dog", "cat", "bird", "bear", "deer"):
        return f"{emoji} A {track.label} is {at}."
    return f"{emoji} An animal (looks like a {track.label}, maybe a deer) is {at}."


# ------------------------------------------------------------------ tracking
class CameraObjectTracker:
    """Turns a camera's detections into events (once per object)."""

    _ids = itertools.count(1)

    def __init__(self, camera_key: str, memory_path: Optional[str] = None) -> None:
        self.camera_key = camera_key
        self.tracks: List[Track] = []
        # Places where a never-moving "person"/"animal" was detected before: usually a
        # coat, fan or sack the model misreads. [box, sightings, last_seen]
        self.static_spots: List[List] = []
        self.memory_path = memory_path
        self.person_min_score = MIN_SCORE["person"]
        self._load_memory()

    def _load_memory(self) -> None:
        if not self.memory_path:
            return
        try:
            with open(self.memory_path) as handle:
                data = json.load(handle).get(self.camera_key, [])
            self.static_spots = [[tuple(box), int(n), float(ts)] for box, n, ts in data]
        except (OSError, ValueError, TypeError):
            self.static_spots = []

    def _save_memory(self) -> None:
        if not self.memory_path:
            return
        try:
            with open(self.memory_path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        data[self.camera_key] = [[list(box), n, ts] for box, n, ts in self.static_spots]
        tmp = self.memory_path + ".tmp"
        try:
            with open(tmp, "w") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.memory_path)
        except OSError:
            pass

    def update(self, detections: List[Detection], frame: np.ndarray, now: float,
               in_zone: Callable[[Tuple[int, int, int, int]], bool]) -> List[Track]:
        """Match detections to tracks; return tracks that just became reportable."""
        self._expire(now, frame.shape[1])
        unmatched = list(detections)
        same_kind = lambda det, track: det.group == track.group or det.group == track.detector_group  # noqa: E731
        # (a cat relabelled from "person" keeps collecting the detector's "person" boxes)
        # First every track takes the box overlapping it; only then may a leftover track reach for
        # a nearby box (a fast mover). Otherwise a parked car the detector missed for a frame grabs
        # the neighbouring parked car's box and "drives" across the driveway (night IR, Garage).
        pairs = sorted(((_iou(det.box, track.box), i, j) for i, track in enumerate(self.tracks)
                        for j, det in enumerate(unmatched) if same_kind(det, track)), reverse=True)
        matches: Dict[int, Detection] = {}
        taken = set()
        for overlap, i, j in pairs:
            if overlap <= 0.25:
                break
            if i not in matches and j not in taken:
                matches[i] = unmatched[j]
                taken.add(j)
        for i, track in enumerate(self.tracks):
            if i in matches:
                continue
            best, best_score = None, 0.0
            tx, ty = track.center()
            _, _, tw, th = track.box
            reach = 1.0 * max(tw, th)  # how far it can plausibly move between samples (~0.5 s)
            for j, det in enumerate(unmatched):
                if j in taken or not same_kind(det, track):
                    continue
                if any(other is not track and now - other.last_seen < 2.0 and _iou(det.box, other.box) > 0.5
                       for other in self.tracks):
                    continue  # that's where another object is standing
                dx = det.box[0] + det.box[2] / 2.0 - tx
                dy = det.box[1] + det.box[3] / 2.0 - ty
                near = 1.0 - (dx * dx + dy * dy) ** 0.5 / reach
                if near > best_score:
                    best, best_score, best_j = det, near, j
            if best is not None:
                matches[i] = best
                taken.add(best_j)
        unmatched = [det for j, det in enumerate(unmatched) if j not in taken]
        for i, track in enumerate(self.tracks):
            best = matches.get(i)
            if best is not None:
                track.vote(best.label, best.score)
                track.see(frame, best.box, now)
                track.boxes.append((now, best.box))
                track.boxes = track.boxes[-30:]
                track.last_seen = now
                track.hits += 1
                if best.score > track.best_score:
                    track.best_score = best.score
                    track.best_frame = frame
                    track.best_box = best.box
        for det in unmatched:
            track = Track(next(self._ids), det.label, det.group, now, now,
                          [(now, det.box)], det.score, 1, best_frame=frame, best_box=det.box)
            track.vote(det.label, det.score)
            track.see(frame, det.box, now)
            self.tracks.append(track)

        frame_w = frame.shape[1]
        ready: List[Track] = []
        for track in self.tracks:
            min_score = self.person_min_score if track.group == "person" else MIN_SCORE.get(track.group, 0.5)
            if track.reported or track.hits < MIN_HITS or track.best_score < min_score:
                continue
            if track.best_score < CONFIDENT_SCORE and track.hits < EXTRA_HITS:
                continue  # unsure: wait for another sighting
            if not in_zone(track.box):
                continue
            if track.group == "vehicle":
                dx, dy = track.travel()
                if not track.vehicle_moved(frame_w):
                    # Parked (or just jiggling: people at its doors). Keep watching rather than
                    # retiring it, so it still alerts if it starts to drive off.
                    continue
            else:
                if track.group == "animal" and any(
                    t.group == "person" and now - t.last_seen < 1.0 and _iou(t.box, track.box) > 0.3
                    for t in self.tracks
                ):
                    track.reported = True  # the small model sometimes calls a person a dog
                    continue
                if track.stayed_put(frame_w) and any(self._matches_spot(track.box, spot[0])
                                                     for spot in self.static_spots):
                    # Learned scenery: stay quiet while it stays put, but keep watching. A person
                    # who first stands there (the top of the front walk) and then walks still alerts.
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
            elif t.group != "vehicle" and t.hits >= MIN_HITS and t.stayed_put(frame_w):
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
                break
        else:
            self.static_spots.append([box, 1, now])
        self._save_memory()


# ------------------------------------------------------------------ worker
class DetectionWorker:
    """One thread running the detector over the newest frame from each camera."""

    def __init__(self, detector: ObjectDetector, min_interval: float = 0.5,
                 vehicle_classifier: Optional["VehicleClassifier"] = None) -> None:
        self.detector = detector
        self.vehicle_classifier = vehicle_classifier  # runs on this thread via the camera callbacks
        self.household = None  # HouseholdStore: names the family's own cars (set by the engine)
        self.subject_checker = None  # SubjectChecker: second opinion on people/animals (set by the engine)
        self.presence = None  # PresenceTracker: which cars are parked where (set by the engine)
        # Slow extras (HD frame grabs) run on their own thread so detection never waits.
        self._jobs: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=2)
        self._job_thread = threading.Thread(target=self._run_jobs, name="object-extras", daemon=True)
        self.min_interval = min_interval
        self._slots: Dict[str, Tuple[np.ndarray, float, Callable]] = {}
        self._last_run: Dict[str, float] = {}
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="object-detector", daemon=True)
        self.inference_ms = 0.0

    def start(self) -> None:
        self._thread.start()
        self._job_thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass

    def run_later(self, job: Callable[[], None]) -> bool:
        """Queue a slow job; False (caller should do without) if two are already waiting."""
        if self._stop:
            return False
        try:
            self._jobs.put_nowait(job)
            return True
        except queue.Full:
            return False

    def _run_jobs(self) -> None:
        while not self._stop:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                pass

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
                # Low floor so tracks stay continuous; alerting needs a higher best score.
                detections = self.detector.detect(frame, min_score=0.3)
            except Exception:
                detections = []
            self.inference_ms = 0.8 * self.inference_ms + 0.2 * (time.time() - started) * 1000
            try:
                callback(detections, frame, ts)
            except Exception:
                pass
