"""
Household vehicles: learn the family's cars from photos, name them in alerts, and keep
track of which ones are parked at home ("When did Abigail get home?", "What cars are
in the driveway?").

How it works:
- Every vehicle crop is turned into a CLIP image embedding (the same encoder used for
  delivery vans). A known vehicle is just a name plus the embeddings of photos the user
  confirmed are that car, collected separately for day and infrared (night) views.
- ``identify`` compares a crop with those references (mean of the best three matches)
  and only answers when the best car is clearly ahead of the next one.
- ``PresenceTracker`` looks at each camera every minute. A car seen at the same spot in
  two scans is "parked"; one gone for three scans has "left". Parked cars keep their
  identity by position, so a car recognised in daylight stays named all night.
- Cars it sees but can't name are kept as "sightings" and grouped by look, so the user
  can name a whole group at once on the Vehicles page.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.detection.vehicle_type import crop_for_clip, is_greyscale

DB_NAME = "household.db"
CROPS_DIRNAME = ".household"

# Matching (cosine similarity between CLIP embeddings). Calibrated on the household's own
# driveway footage: crops of the same parked car score ~0.90-0.97, different family cars
# ~0.75-0.86.
MATCH_MIN = 0.87
MATCH_MARGIN = 0.025
TOP_K = 3
# Every pair in a suggested group must be at least this alike. Infrared photos of different
# cars look more alike to CLIP than daytime ones, so night groups need a higher bar.
GROUP_SIMILARITY = 0.88
GROUP_SIMILARITY_NIGHT = 0.92

SCAN_INTERVAL = 60.0
CONFIRM_SCANS = 2  # seen this many scans in a row = parked
LEAVE_SCANS = 3  # missing this many scans in a row = left
MIN_BOX_FRACTION = 0.015  # ignore vehicles smaller than this share of the frame (far street)
SIGHTING_EVERY = 3600.0  # keep a new photo of a parked car at most hourly (and on day/night change)
MAX_UNLABELLED = 1500


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def sentence_case(name: str) -> str:
    return name[:1].upper() + name[1:] if name else name


@dataclass
class Vehicle:
    id: int
    name: str  # how it's called in messages: "Luke's Sierra", "the blue Civic"
    owner: str = ""  # "Luke" — what people ask about ("is Luke home?")
    aliases: List[str] = field(default_factory=list)  # other names for the owner/car ("steph")
    kind: str = "car"  # emoji hint: car, suv, pickup, van, motorcycle

    def words(self) -> List[str]:
        """Lower-case words/phrases that refer to this vehicle in a question."""
        out = [self.name.lower(), self.name.lower().replace("the ", "", 1)]
        if self.owner:
            out += [self.owner.lower(), self.owner.lower() + "'s", self.owner.lower() + "s"]
        out += [a.lower() for a in self.aliases if a]
        return [w for w in dict.fromkeys(out) if w]


class HouseholdStore:
    """Known vehicles and photo sightings (SQLite + JPEG crops beside the recordings)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.crops_dir = os.path.join(self.root, CROPS_DIRNAME)
        os.makedirs(self.crops_dir, exist_ok=True)
        self.db_path = os.path.join(self.root, DB_NAME)
        self._lock = threading.RLock()
        self._ref_cache: Optional[Dict[int, Dict[bool, np.ndarray]]] = None
        # Optional embedding -> body type ("car", "suv", "pickup", "van"), from the CLIP
        # classifier; photos are only grouped with others of the same body type.
        self.body_of: Optional[Callable[[np.ndarray], str]] = None
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vehicles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                " owner TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'car')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                " camera_key TEXT NOT NULL, camera_name TEXT NOT NULL DEFAULT '', box TEXT NOT NULL, night INTEGER NOT NULL,"
                " embedding BLOB NOT NULL, vehicle_id INTEGER, ignored INTEGER NOT NULL DEFAULT 0)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ vehicles
    def vehicles(self) -> List[Vehicle]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        return [Vehicle(r["id"], r["name"], r["owner"], [a for a in r["aliases"].split(",") if a], r["kind"])
                for r in rows]

    def vehicle(self, vehicle_id: int) -> Optional[Vehicle]:
        return next((v for v in self.vehicles() if v.id == vehicle_id), None)

    def save_vehicle(self, name: str, owner: str = "", aliases: Optional[List[str]] = None, kind: str = "car",
                     vehicle_id: Optional[int] = None) -> int:
        alias_text = ",".join(a.strip() for a in (aliases or []) if a.strip())
        with self._lock, self._connect() as conn:
            if vehicle_id is None:
                cur = conn.execute("INSERT INTO vehicles (name, owner, aliases, kind) VALUES (?, ?, ?, ?)",
                                   (name, owner, alias_text, kind))
                return int(cur.lastrowid)
            conn.execute("UPDATE vehicles SET name = ?, owner = ?, aliases = ?, kind = ? WHERE id = ?",
                         (name, owner, alias_text, kind, vehicle_id))
            return vehicle_id

    def delete_vehicle(self, vehicle_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE sightings SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
            conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        self._ref_cache = None

    # ----------------------------------------------------------- sightings
    def add_sighting(self, ts: float, camera_key: str, camera_name: str, frame: np.ndarray,
                     box: Tuple[int, int, int, int], embedding: np.ndarray,
                     vehicle_id: Optional[int] = None) -> int:
        night = is_greyscale(frame)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sightings (ts, camera_key, camera_name, box, night, embedding, vehicle_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, camera_key, camera_name, json.dumps(list(map(int, box))), int(night),
                 embedding.astype(np.float32).tobytes(), vehicle_id),
            )
            sid = int(cur.lastrowid)
        crop = crop_for_clip(frame, box, pad=0.08)
        cv2.imwrite(self.crop_path(sid), cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA),
                    [cv2.IMWRITE_JPEG_QUALITY, 85])
        if vehicle_id is not None:
            self._ref_cache = None
        return sid

    def crop_path(self, sighting_id: int) -> str:
        return os.path.join(self.crops_dir, f"{int(sighting_id)}.jpg")

    def assign(self, sighting_ids: List[int], vehicle_id: Optional[int], ignore: bool = False) -> int:
        """Label sightings as a vehicle (references), as not ours (ignore), or clear the label."""
        ids = [int(i) for i in sighting_ids]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"UPDATE sightings SET vehicle_id = ?, ignored = ? WHERE id IN ({marks})",
                               [vehicle_id, int(ignore)] + ids)
        self._ref_cache = None
        return cur.rowcount

    def sightings(self, labelled: Optional[bool] = None, limit: int = 2000) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sightings WHERE ignored = 0"
        if labelled is True:
            sql += " AND vehicle_id IS NOT NULL"
        elif labelled is False:
            sql += " AND vehicle_id IS NULL"
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["embedding"] = np.frombuffer(r["embedding"], dtype=np.float32)
            d["box"] = json.loads(r["box"])
            out.append(d)
        return out

    def prune(self, max_age_days: float = 30) -> None:
        """Forget old unlabelled sightings (labelled ones are the references; kept)."""
        cutoff = time.time() - max_age_days * 86400
        with self._lock, self._connect() as conn:
            old = [r[0] for r in conn.execute(
                "SELECT id FROM sightings WHERE vehicle_id IS NULL AND (ts < ? OR id NOT IN "
                "(SELECT id FROM sightings WHERE vehicle_id IS NULL ORDER BY ts DESC LIMIT ?))",
                (cutoff, MAX_UNLABELLED))]
            conn.executemany("DELETE FROM sightings WHERE id = ?", [(i,) for i in old])
        for sid in old:
            try:
                os.remove(self.crop_path(sid))
            except OSError:
                pass

    # ------------------------------------------------------------ matching
    def _references(self) -> Dict[int, Dict[bool, np.ndarray]]:
        if self._ref_cache is None:
            refs: Dict[int, Dict[bool, List[np.ndarray]]] = {}
            for s in self.sightings(labelled=True, limit=100000):
                refs.setdefault(s["vehicle_id"], {}).setdefault(bool(s["night"]), []).append(s["embedding"])
            self._ref_cache = {vid: {n: np.array(v) for n, v in by.items()} for vid, by in refs.items()}
        return self._ref_cache

    def scores(self, embedding: np.ndarray, night: bool) -> Dict[int, float]:
        """Per known vehicle: mean similarity of its TOP_K closest reference photos,
        using photos taken in the same light (day/IR) when there are any."""
        out = {}
        for vid, by_light in self._references().items():
            refs = by_light.get(night)
            if refs is None or len(refs) == 0:
                refs = np.concatenate(list(by_light.values()))
            sims = np.sort(refs @ embedding)[::-1][:TOP_K]
            out[vid] = float(sims.mean())
        return out

    def identify(self, embedding: np.ndarray, night: bool) -> Tuple[Optional[int], float]:
        """(vehicle id, similarity) when one known vehicle clearly matches, else (None, best)."""
        scores = sorted(self.scores(embedding, night).items(), key=lambda kv: -kv[1])
        if not scores:
            return None, 0.0
        best_id, best = scores[0]
        runner = scores[1][1] if len(scores) > 1 else 0.0
        if best >= MATCH_MIN and best - runner >= MATCH_MARGIN:
            return best_id, best
        return None, best

    def suggestion_groups(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Unlabelled sightings grouped by look (biggest groups first), each with the
        known vehicle it most resembles, for the Vehicles page."""
        items = self.sightings(labelled=False, limit=MAX_UNLABELLED)
        if not items:
            return []
        vecs = np.array([s["embedding"] for s in items])
        sims = vecs @ vecs.T
        bodies = [self.body_of(v) if self.body_of else "" for v in vecs]
        groups: List[List[int]] = []
        for i in range(len(items)):
            # Join the group this photo resembles best, but only if it resembles *every*
            # member (complete linkage): averages let two similar cars merge.
            best, best_score = None, 0.0
            for g in groups:
                if items[g[0]]["night"] != items[i]["night"] or bodies[g[0]] != bodies[i]:
                    continue  # day and infrared photos of a car look nothing alike
                worst = float(sims[i, g].min())
                bar = GROUP_SIMILARITY_NIGHT if items[i]["night"] else GROUP_SIMILARITY
                if worst >= bar and worst > best_score:
                    best, best_score = g, worst
            if best is not None:
                best.append(i)
            else:
                groups.append([i])
        centroids = [vecs[g].mean(0) for g in groups]
        order = sorted(range(len(groups)), key=lambda g: -len(groups[g]))[:limit]
        out = []
        for g in order:
            members = [items[i] for i in groups[g]]
            centroid = centroids[g] / (np.linalg.norm(centroids[g]) or 1)
            night = sum(m["night"] for m in members) > len(members) / 2
            vid, score = self.identify(centroid, night)
            out.append({"ids": [m["id"] for m in members], "night": night, "suggested": vid,
                        "cameras": sorted({m["camera_name"] for m in members}),
                        "first": min(m["ts"] for m in members), "last": max(m["ts"] for m in members)})
        return out


@dataclass
class Parked:
    box: Tuple[int, int, int, int]
    first_seen: float
    last_seen: float
    seen: int = 1
    missed: int = 0
    vehicle_id: Optional[int] = None
    confirmed: bool = False
    last_photo: float = 0.0
    last_night: Optional[bool] = None
    announced: bool = False  # its arrival was reported (or it was already here at start)


class PresenceTracker:
    """Once a minute, which vehicles are parked in view of each camera, and who came/went."""

    def __init__(self, store: HouseholdStore, detector, classifier,
                 cameras: Callable[[], List[Any]],
                 on_change: Callable[[str, Any, Vehicle, float, Optional[bytes]], None],
                 state_path: Optional[str] = None, interval: float = SCAN_INTERVAL) -> None:
        self.store = store
        self.detector = detector
        self.classifier = classifier
        self.cameras = cameras  # -> objects with host, display_name, snapshot(), zone_name_at()
        self.on_change = on_change  # (kind 'arrived'/'left', camera, vehicle, ts, jpeg)
        self.state_path = state_path
        self.interval = interval
        self.parked: Dict[str, List[Parked]] = {}
        self.last_scan: Dict[str, float] = {}
        self._baseline: set = set()  # cameras whose first scan only sets the scene
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._load()

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="household", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._stop.wait(20)  # let cameras connect first
        while not self._stop.is_set():
            for cam in list(self.cameras()):
                try:
                    frame = cam.snapshot()
                    if frame is not None:
                        self.scan(cam, frame, time.time())
                except Exception:
                    pass
            self._save()
            if int(time.time()) % 86400 < self.interval:
                self.store.prune()
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ scanning
    def _vehicles_in(self, frame: np.ndarray) -> List[Tuple[int, int, int, int]]:
        h, w = frame.shape[:2]
        min_area = MIN_BOX_FRACTION * w * h
        return [d.box for d in self.detector.detect(frame, min_score=0.45)
                if d.group == "vehicle" and d.label not in ("bicycle", "motorcycle") and d.box[2] * d.box[3] >= min_area]

    def scan(self, cam, frame: np.ndarray, now: float) -> None:
        boxes = self._vehicles_in(frame)
        with self._lock:
            self._scan_locked(cam, frame, boxes, now)

    def _scan_locked(self, cam, frame: np.ndarray, boxes, now: float) -> None:
        key = cam.host
        night = is_greyscale(frame)
        parked = self.parked.setdefault(key, [])
        baseline = key not in self._baseline  # first look after start: cars already here didn't "arrive"
        self._baseline.add(key)
        unmatched = list(boxes)
        for p in parked:
            best = max(unmatched, key=lambda b: _iou(b, p.box), default=None)
            if best is not None and _iou(best, p.box) > 0.5:
                unmatched.remove(best)
                p.box, p.last_seen, p.missed = best, now, 0
                p.seen += 1
            else:
                p.missed += 1
        for box in unmatched:
            parked.append(Parked(box, now, now, announced=baseline))
        self.last_scan[key] = now

        for p in list(parked):
            if p.missed >= LEAVE_SCANS:
                parked.remove(p)
                if p.confirmed and p.vehicle_id is not None:
                    vehicle = self.store.vehicle(p.vehicle_id)
                    if vehicle is not None:
                        self.on_change("left", cam, vehicle, p.last_seen, None)
                continue
            if p.missed:
                continue  # hidden this time (someone walking past); keep its identity
            photo_due = p.last_night != night or now - p.last_photo >= SIGHTING_EVERY
            if p.vehicle_id is None or photo_due:
                embedding = self.classifier.embed(crop_for_clip(frame, p.box))
                if p.vehicle_id is None:
                    # Unknown so far (e.g. arrived in the dark): try again every scan.
                    p.vehicle_id, _ = self.store.identify(embedding, night)
                if photo_due:
                    # Photo for the Vehicles page; it only becomes a reference once the user confirms it.
                    self.store.add_sighting(now, key, cam.display_name, frame, p.box, embedding)
                    p.last_photo, p.last_night = now, night
            if p.seen >= CONFIRM_SCANS:
                p.confirmed = True
            if p.confirmed and p.vehicle_id is not None and not p.announced:
                p.announced = True
                vehicle = self.store.vehicle(p.vehicle_id)
                if vehicle is not None:
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    self.on_change("arrived", cam, vehicle, p.first_seen, jpeg.tobytes() if ok else None)

    # ---------------------------------------------------------------- state
    def present(self) -> Dict[str, List[Parked]]:
        with self._lock:
            return {k: [p for p in v if p.confirmed] for k, v in self.parked.items()}

    def forget_identities(self) -> None:
        """After the user relabels photos: re-identify parked cars on the next scan."""
        with self._lock:
            for cars in self.parked.values():
                for p in cars:
                    p.vehicle_id = None

    def where_is(self, vehicle_id: int) -> Optional[Tuple[str, Parked]]:
        for key, cars in self.present().items():
            for p in cars:
                if p.vehicle_id == vehicle_id:
                    return key, p
        return None

    def _save(self) -> None:
        if not self.state_path:
            return
        with self._lock:
            data = {k: [{"box": list(p.box), "first_seen": p.first_seen, "last_seen": p.last_seen,
                         "vehicle_id": p.vehicle_id, "confirmed": p.confirmed} for p in v]
                    for k, v in self.parked.items()}
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _load(self) -> None:
        """Restore parked cars after a restart (recent ones only), so they don't 'arrive' again."""
        if not self.state_path:
            return
        try:
            with open(self.state_path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        cutoff = time.time() - 15 * 60
        for key, cars in data.items():
            self.parked[key] = [Parked(tuple(c["box"]), c["first_seen"], c["last_seen"], seen=CONFIRM_SCANS,
                                       vehicle_id=c.get("vehicle_id"), confirmed=c.get("confirmed", False),
                                       announced=True)
                                for c in cars if c.get("last_seen", 0) >= cutoff]
            self._baseline.add(key)  # restored: changes from here on are real
