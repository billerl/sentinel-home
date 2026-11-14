"""
Package alerts: something new left on the porch that looks like a parcel -> "📦 A package was left at the
Front Door" with a photo; at sunset, a reminder if it's still there; if it disappears while nobody is home,
a message saying so.

Every SNAPSHOT_SECONDS the door cameras' latest frame is compared with the last one that had nobody in it
(the reference). New, compact changes are checked by CLIP (package_check); one that still looks like a
package on the next quiet snapshot, in the same place, is reported. A delivery takes 10-20 seconds, so a
snapshot may never see the driver: it doesn't need to.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.logging_utils import app_logger

SNAPSHOT_SECONDS = 10.0
MIN_AREA = 0.001  # of the frame: a mailer by the door was ~0.5%
MAX_AREA = 0.06
MAX_CANDIDATES = 4  # biggest changes checked by CLIP per snapshot
# Left out: the top strip, where cameras print the ticking clock (CLIP took the digits for a shipping label),
# and the picture's edges (corners flicker; a parcel isn't left half out of view).
TOP_STRIP = 0.09
EDGE = 0.015
SAME_PLACE_IOU = 0.3
GONE_LOOK = 0.45  # the package's spot looking less alike than this = it's gone ...
GONE_SNAPSHOTS = 3  # ... this many quiet snapshots in a row
DOOR_WORDS = ("door", "porch", "entry", "step")
# "I got the package" / "the package is in": stop the reminders (as the ✅ Got it button does).
TAKEN_IN = re.compile(r"^(?:ok(?:ay)?,? )?(?:(?:i|we)(?: have| 've|'ve)? (?:got|grabbed|brought in|took in|picked up) "
                      r"the (?:package|parcel|box)s?|(?:the )?(?:package|parcel)s? (?:is|are|was|were)(?: brought)? "
                      r"(?:in|inside|picked up|taken in)|got it)[.!]?$", re.IGNORECASE)
# Only changes soon after someone was at the door count: parcels are brought by people. Without it, a cat
# crossing the mat at night, morning sun shadows and a bug on the lens all looked like parcels (5 days of
# Front Door clips: 21 false alerts, 2 real deliveries).
VISIT_WINDOW = 180.0
GONE_QUIET = 20.0  # ... and once they've gone: someone sitting by the door isn't a parcel
# The quiet picture is saved when Sentinel stops and used again after a restart (a deploy takes ~40 s
# before the first snapshot). Without it, the first picture after a restart became the reference, and a
# parcel left during the restart was part of it: never new, never reported (2026-11-08, 5:25pm). As always, only a
# person the camera sees makes changes count: counting the restart itself as a visit turned sun shadows that moved
# during it into two "packages" (2026-11-09).
REFERENCE_KEEP = 180.0  # older than this (Sentinel was off a while): the light has changed, start afresh
REFERENCE_INDEX = "references.json"


def changes(before: np.ndarray, after: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Compact regions that differ between two pictures of the same scene, biggest first. Overall brightness
    is matched first (clouds, the camera's exposure); tiny specks and big swathes (a shadow moving across,
    the camera switching to infrared) are left out."""
    if before.shape != after.shape:
        return []
    h, w = before.shape[:2]
    g0, g1 = (cv2.GaussianBlur(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY).astype(np.float32), (7, 7), 0) for x in (before, after))
    g1 *= g0.mean() / max(1.0, float(g1.mean()))
    d = np.abs(g1 - g0)
    mask = (d > max(25.0, float(np.percentile(d, 99)) * 0.5)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    boxes = []
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, bw, bh = cv2.boundingRect(contour)
        if not MIN_AREA <= bw * bh / float(w * h) <= MAX_AREA or y < TOP_STRIP * h:
            continue
        if x < EDGE * w or y + bh > (1 - EDGE) * h or x + bw > (1 - EDGE) * w:
            continue
        boxes.append((x, y, bw, bh))
    return sorted(boxes, key=lambda b: -b[2] * b[3])


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix, iy = max(0, min(ax + aw, bx + bw) - max(ax, bx)), max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter) if inter else 0.0


def _look(frame: np.ndarray, box) -> Optional[np.ndarray]:
    """Small brightness-normalised picture of a spot (as household.spot_look)."""
    x, y, w, h = box
    crop = frame[max(0, y): y + h, max(0, x): x + w]
    if crop.size == 0:
        return None
    g = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    std = float(g.std())
    return (g - g.mean()) / std if std > 1.0 else None


def _alike(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    return float((a * b).mean()) if a is not None and b is not None else 0.0


@dataclass
class Package:
    box: Tuple[int, int, int, int]
    look: Optional[np.ndarray]
    since: float
    reminded: bool = False
    missing: int = 0


@dataclass
class _Door:
    reference: Optional[np.ndarray] = None
    restored: bool = False  # looked for a saved reference already
    pending: List[Tuple[Tuple[int, int, int, int], float]] = field(default_factory=list)  # (box, probability)
    packages: List[Package] = field(default_factory=list)


class PackageWatch:
    def __init__(self, settings: Dict[str, Any], cameras: Callable[[], List[Any]], detector, checker,
                 notify: Callable[..., None], sunset: Callable[[float], Optional[float]],
                 nobody_home: Callable[[], bool], clock: Callable[[], float] = time.time,
                 on_left: Optional[Callable[[str, str, str, Optional[bytes]], None]] = None,
                 on_waiting: Optional[Callable[[str], None]] = None, on_gone: Optional[Callable[[str], None]] = None,
                 state_dir: Optional[str] = None) -> None:
        self.settings = settings
        self.cameras = cameras  # -> connected pipelines (display_name, host, snapshot())
        self.detector = detector
        self.checker = checker
        self.notify = notify  # (text, jpeg or None[, ask_done]): every Sentinel chat; ask_done adds "✅ Got it"
        self.sunset = sunset  # (now) -> today's sunset time, or None
        self.nobody_home = nobody_home
        self.clock = clock
        self.on_left = on_left  # (camera key, camera name, sentence, jpeg): the history and routines
        self.on_waiting = on_waiting  # (camera key): a parcel is still there (each quiet snapshot): repeating routines
        self.on_gone = on_gone  # (camera key): no parcel there any more (taken in, or "Got it")
        self.state_dir = state_dir  # the doors' last quiet pictures, kept through a restart
        self._hosts: Dict[str, str] = {}  # camera name -> its key (routines and the events table use keys)
        self._doors: Dict[str, _Door] = {}
        self._visits: Dict[str, float] = {}  # camera name -> when the camera last saw a person
        self._lock = threading.Lock()
        self._save_soon = False  # the waiting packages changed: saved at the end of this check
        self._stop = threading.Event()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        threading.Thread(target=self._run, name="packages", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.save()

    def save(self) -> None:
        """Keep each door's quiet picture for the next start (see REFERENCE_KEEP), and the packages waiting there:
        the quiet picture already shows them, so they'd never be found again (bug hunt 2026-11-13: forgotten at a
        restart, so no sunset reminder, no "gone while nobody's home", and "any packages?" said none)."""
        if not self.state_dir:
            return
        with self._lock:
            refs = {k: d.reference for k, d in self._doors.items() if d.reference is not None}
            waiting = {k: [{"box": list(p.box), "since": p.since, "reminded": p.reminded,
                            "look": p.look.round(4).tolist() if p.look is not None else None} for p in d.packages]
                       for k, d in self._doors.items()}
        index = {}
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            for name, ref in refs.items():
                file = re.sub(r"[^\w-]+", "_", name) + ".png"
                if cv2.imwrite(os.path.join(self.state_dir, file), ref):
                    index[name] = {"file": file, "saved": self.clock(), "packages": waiting.get(name, [])}
            with open(os.path.join(self.state_dir, REFERENCE_INDEX), "w") as f:
                json.dump(index, f)
        except Exception as e:
            app_logger.log("error", "package_reference_failed", error_message=str(e)[:200], component="packages",
                           action="save")

    def _restore(self, name: str, door: _Door, now: float) -> None:
        door.restored = True
        try:
            with open(os.path.join(self.state_dir, REFERENCE_INDEX)) as f:
                entry = json.load(f).get(name) or {}
            saved = float(entry.get("saved", 0))
            if not 0 <= now - saved <= REFERENCE_KEEP:
                return
            ref = cv2.imread(os.path.join(self.state_dir, os.path.basename(str(entry.get("file", "")))))
        except (OSError, ValueError, AttributeError):
            return
        if ref is not None:
            door.reference = ref
            for p in entry.get("packages") or []:
                try:
                    look = np.asarray(p["look"], dtype=np.float32).reshape(32, 32) if p.get("look") else None
                    door.packages.append(Package(tuple(int(v) for v in p["box"]), look, float(p["since"]),
                                                 reminded=bool(p.get("reminded"))))
                except (KeyError, TypeError, ValueError):
                    pass

    def _run(self) -> None:
        self._stop.wait(30)  # let cameras connect
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let one bad frame stop the watch
                app_logger.log("error", "package_watch_failed", error_message=str(e)[:200], component="packages",
                               action="watch")
            self._stop.wait(SNAPSHOT_SECONDS)

    def door_cameras(self) -> List[Any]:
        """The cameras named in Settings, else every camera whose name sounds like a door or porch."""
        wanted = [n.strip().lower() for n in str(self.settings.get("package_cameras") or "").split(",") if n.strip()]
        cams = list(self.cameras())
        if wanted:
            return [c for c in cams if c.display_name.lower() in wanted]
        return [c for c in cams if any(word in c.display_name.lower() for word in DOOR_WORDS)]

    def visit(self, name: str, ts: float) -> None:
        """The camera's own detection saw someone (it watches every motion frame; the snapshots miss most)."""
        self._visits[name] = max(ts, self._visits.get(name, 0.0))

    def clear(self, name: Optional[str] = None) -> int:
        """"Got it": forget the parcels (at one camera, or everywhere); no more reminders or repeats."""
        with self._lock:
            doors = [(k, d) for k, d in self._doors.items() if name is None or k == name]
            count = sum(len(d.packages) for _, d in doors)
            for _, d in doors:
                d.packages = []
        for k, _ in doors:
            self._gone(k)
        if count:
            app_logger.log("info", "packages_cleared", count=count, component="packages", action="clear")
        return count

    def _gone(self, name: str) -> None:
        if self.on_gone is not None:
            self.on_gone(self._hosts.get(name, name))

    def packages(self) -> Dict[str, int]:
        """Camera name -> packages waiting there (for questions and the morning recap)."""
        with self._lock:
            return {k: len(d.packages) for k, d in self._doors.items() if d.packages}

    # --------------------------------------------------------------- watching
    def tick(self) -> None:
        if not self.settings.get("package_alerts_enabled", True) or not getattr(self.checker, "available", False):
            return
        now = self.clock()
        for cam in self.door_cameras():
            frame = cam.snapshot()
            self._hosts[cam.display_name] = getattr(cam, "host", cam.display_name)
            if frame is not None:
                self.look(cam.display_name, frame, now)
        if self._save_soon:
            self._save_soon = False
            self.save()

    def look(self, name: str, frame: np.ndarray, now: float) -> None:
        with self._lock:
            door = self._doors.setdefault(name, _Door())
        if not door.restored and self.state_dir:
            self._restore(name, door, now)
        people = [d.box for d in self.detector.detect(frame, min_score=0.3) if d.group in ("person", "animal")]
        if people or now - self._visits.get(name, -1e9) < GONE_QUIET:
            door.pending = []
            return  # someone (or the dog) on the porch: wait until they've gone
        if door.reference is None or door.reference.shape != frame.shape:
            door.reference = frame.copy()
            return
        self._follow_packages(name, door, frame, now)
        if door.packages and self.on_waiting is not None:
            self.on_waiting(self._hosts.get(name, name))
        confirmed = []
        for box, _p in door.pending:  # new last time: still there, and still a package?
            if _alike(_look(door.reference, box), _look(frame, box)) >= GONE_LOOK:
                p = self.checker.probability(frame, box)
                if p >= self._min():
                    confirmed.append((box, p))
        door.pending = []
        for box, p in confirmed:
            if any(_iou(box, k.box) >= SAME_PLACE_IOU for k in door.packages):
                continue
            door.packages.append(Package(box, _look(frame, box), now))
            self._report(name, frame, box, p)
            self._save_soon = True  # (kept on disk too, so a power cut doesn't lose it)
        recent = now - self._visits.get(name, -1e9) <= VISIT_WINDOW
        for box in changes(door.reference, frame)[:MAX_CANDIDATES] if recent else []:
            if any(_iou(box, k.box) >= SAME_PLACE_IOU for k in door.packages):
                continue
            p = self.checker.probability(frame, box)
            if p >= self._min():
                door.pending.append((box, p))
        door.reference = frame.copy()

    def _min(self) -> float:
        from sentinel.detection.package_check import PACKAGE_MIN

        return float(self.settings.get("package_min_probability", PACKAGE_MIN))

    def _follow_packages(self, name: str, door: _Door, frame: np.ndarray, now: float) -> None:
        sunset = self.sunset(now)
        for pkg in list(door.packages):
            if _alike(pkg.look, _look(frame, pkg.box)) < GONE_LOOK:
                pkg.missing += 1
                if pkg.missing >= GONE_SNAPSHOTS:
                    door.packages.remove(pkg)
                    self._save_soon = True
                    if not door.packages:
                        self._gone(name)
                    away = self.nobody_home()
                    app_logger.log("info", "package_gone", camera_display=name, nobody_home=away,
                                   minutes=round((now - pkg.since) / 60), component="packages", action="watch")
                    if away:  # taken in by the family: no message; taken while everyone's out: say so
                        self.notify(f"📦 The package at the {name} is gone, and nobody is home.", _jpeg(frame, pkg.box))
                continue
            pkg.missing = 0
            if not pkg.reminded and sunset is not None and now >= sunset and pkg.since < sunset:
                pkg.reminded = True
                self._save_soon = True
                self.notify(f"📦 Reminder: the package is still at the {name}.", _jpeg(frame, pkg.box), True)

    def _report(self, name: str, frame: np.ndarray, box, probability: float) -> None:
        app_logger.log("info", "package_left", camera_display=name, probability=round(probability, 2),
                       box=list(box), component="packages", action="watch")
        text, jpeg = f"📦 A package was left at the {name}.", _jpeg(frame, box)
        self.notify(text, jpeg, True)  # with a "✅ Got it" button
        if self.on_left is not None:
            self.on_left(self._hosts.get(name, name), name, text, jpeg)


def _jpeg(frame: np.ndarray, box) -> Optional[bytes]:
    shot = frame.copy()
    x, y, w, h = box
    pad = max(6, int(0.15 * max(w, h)))
    cv2.rectangle(shot, (x - pad, y - pad), (x + w + pad, y + h + pad), (0, 200, 255), 3)
    ok, data = cv2.imencode(".jpg", shot, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return data.tobytes() if ok else None
