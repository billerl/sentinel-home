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
  two scans is "parked"; one gone for three scans has "left". A car the detector misses
  (often, at night) but whose spot looks unchanged is still there. Parked cars keep their
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
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.detection.vehicle_type import crop_for_clip, is_daylight, is_greyscale

DB_NAME = "household.db"
CROPS_DIRNAME = ".household"

# Matching (cosine similarity between CLIP embeddings). Calibrated on the household's own
# driveway footage: crops of the same parked car score ~0.90-0.97, different family cars
# ~0.75-0.86.
# Measured 2026-10-25 on the household's photos (each photo matched against the others taken more
# than 2 h apart): 0.87/0.025 named 3 of 155 day photos wrongly and 4 of 17 photos of an unnamed
# grey sedan as a family car; 0.88/0.03 named 1 wrongly and 3 of 17. A parked car also has to be
# named the same twice in a row (IDENTIFY_AGREE) before it counts, since it's re-checked every scan.
MATCH_MIN = 0.88
MATCH_MARGIN = 0.03
IDENTIFY_AGREE = 2
TOP_K = 3
# Every pair in a suggested group must be at least this alike. Infrared photos of different
# cars look more alike to CLIP than daytime ones, so night groups need a higher bar.
GROUP_SIMILARITY = 0.88
GROUP_SIMILARITY_NIGHT = 0.92

# Filing photos without asking: a photo this close to one named car (and clearly not
# another) becomes a reference automatically; the page only shows what's left.
AUTO_MATCH = 0.92
AUTO_MARGIN = 0.04
# Looks like something the user marked "not ours" (and more like it than any family car) -> hidden
# too. Only the user's own marks count: hidden photos used to hide further photos, and since every
# car on this driveway looks ~0.93 alike to CLIP, by 2026-10-27 ~100 family-car photos a day vanished.
IGNORE_SIMILARITY = 0.90

# Colour breaks ties CLIP can't: from above, a grey Fusion and a blue Edge score ~0.94 vs 0.94.
# When the closest cars are within COLOUR_POOL of each other (by day), the one whose photos are
# clearly closest in paint colour wins. Measured 2026-10-27 on the 140 photos the family named
# (each against photos from other days): 38 right / 2 wrong without colour, 69 / 6 with it.
COLOUR_POOL = 0.03
COLOUR_MARGIN = 0.08
LIT_MATCH_MIN = 0.80  # under Sentinel's white light at night shapes match less well (no photos in that light) ...
LIT_POOL = 0.06  # ... so colour picks among the cars this close to the best shape match
COLOUR_VETO = 0.05  # a daytime photo this much closer in paint colour to another car isn't filed automatically
MAX_REFS_PER_LIGHT = 80  # per car, per day/night: plenty, and keeps matching fast
MIN_DAYS = 2  # only cars seen on this many different days are suggested (skips visitors, deliveries)

ASK_AFTER_SCANS = 3  # parked and still unknown after ~3 minutes: worth asking whose it is

SCAN_INTERVAL = 60.0
CONFIRM_SCANS = 2  # seen this many scans in a row = parked
LEAVE_SCANS = 3  # missing this many scans in a row = left
LEAVE_SCANS_UNSURE = 8  # ... when its spot can't be compared (only seen in the other light: first dusk, headlights)
SAME_LOOK = 0.6  # a missed car's spot looking this much like when it was last seen = still there
MIN_BOX_FRACTION = 0.015  # ignore vehicles smaller than this share of the frame (far street)
SIGHTING_EVERY = 3600.0  # keep a new photo of a parked car at most hourly (and on day/night change)
MAX_UNLABELLED = 1500


def _covers(person: Tuple[int, int, int, int], spot: Tuple[int, int, int, int]) -> bool:
    """A person standing in front of (or loading) a parked car: most of them is over its spot."""
    px, py, pw, ph = person
    sx, sy, sw, sh = spot
    ix = max(0, min(px + pw, sx + sw) - max(px, sx))
    iy = max(0, min(py + ph, sy + sh) - max(py, sy))
    return ix * iy >= 0.5 * pw * ph


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def colour_signature(crop: np.ndarray) -> Optional[np.ndarray]:
    """Rough paint colour of a square vehicle crop (as saved for sightings): a hue/saturation/brightness
    histogram of its middle, unit length so two signatures compare with a dot product."""
    if crop is None or crop.size == 0:
        return None
    if crop.shape[:2] != (224, 224):
        crop = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(crop[56:168, 56:168], cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
    total = float(hist.sum())
    return np.sqrt(hist / total).astype(np.float32) if total else None


def crop_colour(frame: np.ndarray, box: Tuple[int, int, int, int], lit: bool = False) -> Optional[np.ndarray]:
    """Colour signature of a vehicle in a frame, cropped the way sightings are saved; None unless
    it's daylight or ``lit`` (the camera's own white light, which Sentinel turned on: see
    camera_light.py), not infrared or lamp light at night."""
    if is_greyscale(frame) or not (lit or is_daylight(frame)):
        return None
    return colour_signature(crop_for_clip(frame, box, pad=0.08))


def spot_look(frame: np.ndarray, box: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
    """A small brightness-normalised picture of a parking spot, to tell whether the car on it is
    still there when the detector misses it (at night it often does). Same car minutes later:
    0.66-0.95 alike; the spot after it left: 0.33 or less (SAME_LOOK sits between)."""
    x, y, w, h = box
    crop = frame[max(0, y): y + h, max(0, x): x + w]
    if crop.size == 0:
        return None
    g = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (48, 36), interpolation=cv2.INTER_AREA).astype(np.float32)
    std = float(g.std())
    return (g - g.mean()) / std if std > 1.0 else None


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
        self._day_ref_ids: Dict[int, List[int]] = {}  # per car, its daytime reference photos (for colour)
        self._colours: Dict[int, Optional[np.ndarray]] = {}  # sighting id -> colour signature (read lazily)
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sightings)")}
            if "auto" not in columns:  # filed automatically (not by the user)
                conn.execute("ALTER TABLE sightings ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")

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
                     vehicle_id: Optional[int] = None, auto: bool = False) -> int:
        night = is_greyscale(frame)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sightings (ts, camera_key, camera_name, box, night, embedding, vehicle_id, auto)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, camera_key, camera_name, json.dumps(list(map(int, box))), int(night),
                 embedding.astype(np.float32).tobytes(), vehicle_id, int(auto)),
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
        """The user labels sightings as a vehicle (references), as not ours (ignore), or clears the label."""
        ids = [int(i) for i in sighting_ids]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"UPDATE sightings SET vehicle_id = ?, ignored = ?, auto = 0 WHERE id IN ({marks})",
                               [vehicle_id, int(ignore)] + ids)
        self._ref_cache = None
        return cur.rowcount

    def ref_count(self, vehicle_id: int, night: bool) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM sightings WHERE vehicle_id = ? AND night = ?",
                                    (vehicle_id, int(night))).fetchone()[0])

    def ignored_embeddings(self) -> np.ndarray:
        with self._connect() as conn:
            rows = conn.execute("SELECT embedding FROM sightings WHERE ignored = 1 AND auto = 0").fetchall()
        return np.array([np.frombuffer(r[0], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 512), np.float32)

    def auto_file(self) -> int:
        """File unlabelled photos that clearly match a named car (or something marked
        "not ours"), so the Vehicles page only asks about the rest. Returns how many."""
        if not self._references():
            return 0
        ignored = self.ignored_embeddings()
        filed: Dict[Tuple[int, bool], List[int]] = {}
        hidden: List[int] = []
        for s in self.sightings(labelled=False, limit=MAX_UNLABELLED):
            e = s["embedding"]
            scores = sorted(self.scores(e, bool(s["night"])).items(), key=lambda kv: -kv[1])
            not_ours = float((ignored @ e).max()) if len(ignored) else 0.0
            if not_ours >= IGNORE_SIMILARITY and not_ours >= (scores[0][1] if scores else 0.0) + AUTO_MARGIN:
                hidden.append(s["id"])
                continue
            if not scores:
                continue
            best_id, best = scores[0]
            runner = scores[1][1] if len(scores) > 1 else 0.0
            if best >= AUTO_MATCH and best - runner >= AUTO_MARGIN and \
                    (s["night"] or self.colour_agrees(best_id, self._colour_of(s["id"]))):
                filed.setdefault((best_id, bool(s["night"])), []).append(s["id"])
        n = 0
        for (vid, night), ids in filed.items():
            room = MAX_REFS_PER_LIGHT - self.ref_count(vid, night)
            if room > 0:
                with self._lock, self._connect() as conn:
                    marks = ",".join("?" * len(ids[:room]))
                    conn.execute(f"UPDATE sightings SET vehicle_id = ?, auto = 1 WHERE id IN ({marks})",
                                 [vid] + ids[:room])
                n += len(ids[:room])
        if hidden:  # hidden, but not a "not ours" reference itself (see IGNORE_SIMILARITY)
            with self._lock, self._connect() as conn:
                marks = ",".join("?" * len(hidden))
                conn.execute(f"UPDATE sightings SET ignored = 1, auto = 1 WHERE id IN ({marks})", hidden)
        if n:
            self._ref_cache = None
        return n + len(hidden)

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
            day_ids: Dict[int, List[int]] = {}
            for s in self.sightings(labelled=True, limit=100000):
                refs.setdefault(s["vehicle_id"], {}).setdefault(bool(s["night"]), []).append(s["embedding"])
                if not s["night"]:
                    day_ids.setdefault(s["vehicle_id"], []).append(s["id"])
            self._day_ref_ids = day_ids
            self._ref_cache = {vid: {n: np.array(v) for n, v in by.items()} for vid, by in refs.items()}
        return self._ref_cache

    def _colour_of(self, sighting_id: int) -> Optional[np.ndarray]:
        if sighting_id not in self._colours:
            self._colours[sighting_id] = colour_signature(cv2.imread(self.crop_path(sighting_id)))
        return self._colours[sighting_id]

    def _colour_pick(self, colour: np.ndarray, candidates: List[int]) -> Optional[int]:
        """The candidate whose daytime photos are clearly closest in paint colour, if one is."""
        self._references()
        closeness = []
        for vid in candidates:
            sigs = [c for c in (self._colour_of(i) for i in self._day_ref_ids.get(vid, [])) if c is not None]
            if not sigs:
                return None  # can't compare a car we have no daytime photos of
            closeness.append((float(np.sort(np.array(sigs) @ colour)[::-1][:TOP_K].mean()), vid))
        closeness.sort(reverse=True)
        if len(closeness) > 1 and closeness[0][0] - closeness[1][0] >= COLOUR_MARGIN:
            return closeness[0][1]
        return None

    def colour_agrees(self, vehicle_id: int, colour: Optional[np.ndarray]) -> bool:
        """Before filing a daytime photo under a car automatically: is its paint colour not clearly
        another car's? 2026-10-28 the blue Edge, parked where the grey Fusion usually stands, was
        filed under the Fusion ten times; each wrong photo made the next match easier."""
        if colour is None:
            return True
        self._references()
        closeness = {}
        for vid, ids in self._day_ref_ids.items():
            sigs = [c for c in (self._colour_of(i) for i in ids) if c is not None]
            if sigs:
                closeness[vid] = float(np.sort(np.array(sigs) @ colour)[::-1][:TOP_K].mean())
        if vehicle_id not in closeness:
            return True
        return max(closeness.values()) - closeness[vehicle_id] < COLOUR_VETO

    def scores(self, embedding: np.ndarray, night: bool, exclude: Iterable[int] = ()) -> Dict[int, float]:
        """Per known vehicle (except ``exclude``): mean similarity of its TOP_K closest reference
        photos, using photos taken in the same light (day/IR) when there are any."""
        out = {}
        skip = set(exclude)
        for vid, by_light in self._references().items():
            if vid in skip:
                continue
            refs = by_light.get(night)
            if refs is None or len(refs) == 0:
                refs = np.concatenate(list(by_light.values()))
            sims = np.sort(refs @ embedding)[::-1][:TOP_K]
            out[vid] = float(sims.mean())
        return out

    def identify(self, embedding: np.ndarray, night: bool, exclude: Iterable[int] = (),
                 colour: Optional[np.ndarray] = None, lit: bool = False) -> Tuple[Optional[int], float]:
        """(vehicle id, similarity) when one known vehicle clearly matches, else (None, best).
        ``exclude``: cars that can't be this one (parked somewhere else right now). ``colour``
        (daytime crop_colour) settles near-ties between cars that are shaped alike."""
        scores = sorted(self.scores(embedding, night, exclude).items(), key=lambda kv: -kv[1])
        if not scores:
            return None, 0.0
        best_id, best = scores[0]
        runner = scores[1][1] if len(scores) > 1 else 0.0
        if best >= MATCH_MIN and best - runner >= MATCH_MARGIN:
            return best_id, best
        if best >= MATCH_MIN and colour is not None and not night:
            pick = self._colour_pick(colour, [vid for vid, score in scores if score >= best - COLOUR_POOL])
            if pick is not None:
                return pick, dict(scores)[pick]
        if lit and colour is not None and best >= LIT_MATCH_MIN:
            # 2026-10-29 21:44, white light on: Edge shape 0.88 / Fusion 0.85, but paint 0.81 / 0.67.
            pick = self._colour_pick(colour, [vid for vid, score in scores if score >= best - LIT_POOL])
            if pick is not None:
                return pick, dict(scores)[pick]
        return None, best

    def confident(self, embedding: np.ndarray, night: bool) -> Optional[int]:
        """The car this photo clearly is (strict bar, used before filing it as a reference)."""
        scores = sorted(self.scores(embedding, night).items(), key=lambda kv: -kv[1])
        if not scores:
            return None
        runner = scores[1][1] if len(scores) > 1 else 0.0
        return scores[0][0] if scores[0][1] >= AUTO_MATCH and scores[0][1] - runner >= AUTO_MARGIN else None

    def suggestion_groups(self, limit: int = 8, min_days: int = MIN_DAYS) -> List[Dict[str, Any]]:
        """Daytime photos not yet filed, grouped by look, for the Vehicles page. Only groups
        seen on ``min_days`` different days (the family's regular cars, not visitors or
        deliveries), most frequent first, each with the named car it most resembles.
        Night photos aren't asked about: they're filed from cars recognised by day."""
        items = [s for s in self.sightings(labelled=False, limit=MAX_UNLABELLED) if not s["night"]]
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
        days = [len({time.strftime("%Y-%m-%d", time.localtime(items[i]["ts"])) for i in g}) for g in groups]
        order = [g for g in sorted(range(len(groups)), key=lambda g: (-days[g], -len(groups[g])))
                 if days[g] >= min_days][:limit]
        out = []
        for g in order:
            members = [items[i] for i in groups[g]]
            centroid = centroids[g] / (np.linalg.norm(centroids[g]) or 1)
            night = sum(m["night"] for m in members) > len(members) / 2
            vid, score = self.identify(centroid, night)
            out.append({"ids": [m["id"] for m in members], "night": night, "suggested": vid, "days": days[g],
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
    asked: bool = False  # we asked "whose car is this?" (only once per parking)
    ignored: bool = False  # the user said it's not ours
    candidate: Optional[int] = None  # who it looked like at the last confident check ...
    candidate_hits: int = 0  # ... and how many confident checks in a row agreed
    day_sure: bool = False  # a daylight photo of this parking clearly matched its car (night photos may be filed)
    # spot_look per light (False = day, True = infrared), from the last time the car was seen there:
    # the daytime picture survives the night, so the morning has something to compare with.
    looks: Dict[bool, np.ndarray] = field(default_factory=dict)
    # Motion detection's view of the spot between scans (camera thread): since when, and in how many
    # frames in a row, nothing has been seen on it.
    empty_since: float = 0.0
    empty_frames: int = 0


class PresenceTracker:
    """Once a minute, which vehicles are parked in view of each camera, and who came/went."""

    def __init__(self, store: HouseholdStore, detector, classifier,
                 cameras: Callable[[], List[Any]],
                 on_change: Callable[[str, Any, Vehicle, float, Optional[bytes]], None],
                 state_path: Optional[str] = None, interval: float = SCAN_INTERVAL,
                 on_unknown: Optional[Callable[[Any, "Parked", np.ndarray, int, Dict[int, float]], None]] = None) -> None:
        self.store = store
        self.detector = detector
        self.classifier = classifier
        self.cameras = cameras  # -> objects with host, display_name, snapshot(), zone_name_at()
        self.on_change = on_change  # (kind 'arrived'/'left', camera, vehicle, ts, jpeg)
        self.on_unknown = on_unknown  # (camera, parked, frame, sighting id, scores): ask whose car it is
        self.state_path = state_path
        self.interval = interval
        self.parked: Dict[str, List[Parked]] = {}
        self.last_scan: Dict[str, float] = {}
        self._baseline: set = set()  # cameras whose first scan only sets the scene
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.lights = None  # CameraLights: colours are real while its white light is on
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
                except Exception as e:
                    from sentinel.logging_utils import app_logger

                    app_logger.log("error", "presence_scan_failed", error_message=str(e),
                                   camera_display=getattr(cam, "display_name", ""), component="household",
                                   action="scan")
            self._save()
            if int(time.time()) % 3600 < self.interval:
                self.store.auto_file()
            if int(time.time()) % 86400 < self.interval:
                self.store.prune()
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ scanning
    def _vehicles_in(self, frame: np.ndarray):
        """(parked-size vehicle boxes, people boxes) in the frame."""
        h, w = frame.shape[:2]
        min_area = MIN_BOX_FRACTION * w * h
        found = self.detector.detect(frame, min_score=0.45)
        if not is_daylight(frame):
            # Parked cars in infrared or lamp light score far better on a contrast-stretched copy
            # (2026-10-29 21:13: the Fusion and Edge right in front scored nothing; stretched 0.73).
            grey = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            more = self.detector.detect(cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR), min_score=0.45)
            found = list(found) + [d for d in more if not any(_iou(d.box, f.box) > 0.5 for f in found)]
        return ([d.box for d in found if d.group == "vehicle" and d.label not in ("bicycle", "motorcycle")
                 and d.box[2] * d.box[3] >= min_area],
                [d.box for d in found if d.group == "person"])

    def scan(self, cam, frame: np.ndarray, now: float) -> None:
        boxes = self._vehicles_in(frame)
        with self._lock:
            self._scan_locked(cam, frame, boxes, now)

    def _scan_locked(self, cam, frame: np.ndarray, boxes, now: float) -> None:
        key = cam.host
        night = is_greyscale(frame)
        lit = self.lights is not None and self.lights.is_on(key)
        parked = self.parked.setdefault(key, [])
        baseline = key not in self._baseline  # first look after start: cars already here didn't "arrive"
        self._baseline.add(key)
        boxes, people = boxes if isinstance(boxes, tuple) else (boxes, [])
        unmatched = list(boxes)
        for p in parked:
            best = max(unmatched, key=lambda b: _iou(b, p.box), default=None)
            if best is not None and _iou(best, p.box) > 0.5:
                unmatched.remove(best)
                p.box, p.last_seen, p.missed = best, now, 0
                p.seen += 1
                self._remember_look(p, frame, night)
            elif best is not None and _iou(best, p.box) > 0.2:
                # Still there, just partly hidden (doors open, someone loading it): don't let it
                # "leave" and "arrive" again.
                unmatched.remove(best)
                p.last_seen, p.missed = now, 0
            elif self._looks_unchanged(frame, p, night):
                # The detector missed it, but the spot looks just the same: nothing drove off it.
                p.last_seen, p.missed = now, 0
                self._remember_look(p, frame, night)  # follow slow changes (shadows, dusk, frost)
            elif any(_covers(b, p.box) for b in people):
                pass  # someone in front of it: can't tell, so wait (people crossing an empty spot
                # used to keep a car that had left "parked": the Edge, 2026-10-29 17:40)
            else:
                p.missed += 1
        for box in unmatched:
            parked.append(Parked(box, now, now, announced=baseline))
        self.last_scan[key] = now

        for p in list(parked):
            if p.missed >= (LEAVE_SCANS if night in p.looks or not p.looks else LEAVE_SCANS_UNSURE):
                parked.remove(p)
                if p.confirmed and p.vehicle_id is not None and p.announced and not self._parked_elsewhere(p):
                    vehicle = self.store.vehicle(p.vehicle_id)
                    if vehicle is not None:
                        self.on_change("left", cam, vehicle, p.last_seen, None)
                continue
            if p.missed:
                continue  # hidden this time (someone walking past); keep its identity
            photo_due = p.last_night != night or now - p.last_photo >= SIGHTING_EVERY
            embedding = None
            if p.vehicle_id is None or photo_due:
                embedding = self.classifier.embed(crop_for_clip(frame, p.box))
                if p.vehicle_id is None and not p.ignored:
                    # Unknown so far (e.g. arrived in the dark): try again every scan, and name it once
                    # two checks in a row agree (one lucky match would stick for the whole parking).
                    guess, _ = self.store.identify(embedding, night, exclude=self._home_except(p),
                                                   colour=crop_colour(frame, p.box, lit), lit=lit)
                    if guess is not None:
                        p.candidate_hits = p.candidate_hits + 1 if guess == p.candidate else 1
                        p.candidate = guess
                        if p.candidate_hits >= IDENTIFY_AGREE:
                            p.vehicle_id = guess
                if not night and p.vehicle_id is not None and not p.day_sure:
                    p.day_sure = self.store.confident(embedding, False) == p.vehicle_id
                if photo_due:
                    # File the photo under the car only when this photo itself clearly is that car (by day),
                    # or at night for a parking recognised clearly by day. Filing on the parking's identity
                    # alone let one wrong guess grow: a grey sedan's photos ended up under the blue Edge.
                    sure = p.day_sure if night else (self.store.confident(embedding, False) == p.vehicle_id
                                                     and self.store.colour_agrees(p.vehicle_id, crop_colour(frame, p.box, lit)))
                    if p.vehicle_id is not None and p.confirmed and sure and \
                            self.store.ref_count(p.vehicle_id, night) < MAX_REFS_PER_LIGHT:
                        # A car we already know, parked in the same spot: its photo (including the
                        # first infrared one at dusk) is filed under it without asking.
                        self.store.add_sighting(now, key, cam.display_name, frame, p.box, embedding,
                                                vehicle_id=p.vehicle_id, auto=True)
                    else:
                        self.store.add_sighting(now, key, cam.display_name, frame, p.box, embedding)
                    p.last_photo, p.last_night = now, night
            if p.seen >= CONFIRM_SCANS:
                p.confirmed = True
            if (self.on_unknown is not None and p.vehicle_id is None and not p.ignored and not p.asked
                    and not night and p.seen >= ASK_AFTER_SCANS and embedding is not None
                    and p.box[0] > 2 and p.box[0] + p.box[2] < frame.shape[1] - 2):  # whole car in view
                p.asked = True
                sid = self.store.add_sighting(now, key, cam.display_name, frame, p.box, embedding)
                self.on_unknown(cam, p, frame, sid, self.store.scores(embedding, night))
            if p.confirmed and p.vehicle_id is not None and not p.announced:
                p.announced = True
                if self._parked_elsewhere(p):
                    continue  # already home (seen in another spot or by another camera)
                vehicle = self.store.vehicle(p.vehicle_id)
                if vehicle is not None:
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    self.on_change("arrived", cam, vehicle, p.first_seen, jpeg.tobytes() if ok else None)

    @staticmethod
    def _remember_look(p: "Parked", frame: np.ndarray, night: bool) -> None:
        look = spot_look(frame, p.box)
        if look is not None:
            p.looks[night] = look

    @staticmethod
    def _looks_unchanged(frame: np.ndarray, p: "Parked", night: bool) -> bool:
        then = p.looks.get(night)
        if then is None:
            return False  # nothing to compare in this light
        now = spot_look(frame, p.box)
        return now is not None and float((now * then).mean()) >= SAME_LOOK

    def _home_except(self, p: "Parked") -> set:
        """Family cars parked in other spots right now: p can't be one of them."""
        return {o.vehicle_id for cars in self.parked.values() for o in cars
                if o is not p and o.vehicle_id is not None and o.confirmed and not o.missed}

    def home_ids(self) -> set:
        """Family cars parked in view right now."""
        with self._lock:
            return self._home_except(None)

    def spots(self, camera_key: str) -> List[Parked]:
        """Spots with a parked car (named or not, confirmed or not). Includes cars the last check or two
        missed: at night a parked truck scores 0.3-0.6, so scans at 0.45 often miss it, and that let
        passing headlights make it "drive past" (2026-10-27 19:17). Gone only after LEAVE_SCANS misses."""
        with self._lock:
            return list(self.parked.get(camera_key, []))

    def _parked_elsewhere(self, p: "Parked") -> bool:
        """Is p's car also in another (announced) parking spot right now?"""
        return any(o is not p and o.vehicle_id == p.vehicle_id and o.confirmed and o.announced and not o.missed
                   for cars in self.parked.values() for o in cars)

    # ---------------------------------------------------------------- state
    def present(self) -> Dict[str, List[Parked]]:
        with self._lock:
            return {k: [p for p in v if p.confirmed] for k, v in self.parked.items()}

    def set_identity(self, parked: "Parked", vehicle_id: Optional[int], ignored: bool = False) -> None:
        """The user told us whose car this parked one is (or that it isn't ours)."""
        with self._lock:
            parked.vehicle_id = vehicle_id
            parked.ignored = ignored
            parked.day_sure = vehicle_id is not None  # the family said so: its night photos may be filed

    def forget_identities(self) -> None:
        """After the user relabels photos: re-identify parked cars on the next scan."""
        with self._lock:
            for cars in self.parked.values():
                for p in cars:
                    p.vehicle_id = None

    def parked_spot(self, camera_key: str, box: Tuple[int, int, int, int], min_iou: float = 0.4) -> Optional[Parked]:
        """The parked car (confirmed) that ``box`` sits on, if any."""
        with self._lock:
            spots = [p for p in self.parked.get(camera_key, []) if p.confirmed]
        best = max(spots, key=lambda p: _iou(p.box, box), default=None)
        return best if best is not None and _iou(best.box, box) >= min_iou else None

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
                                       announced=True, asked=True)
                                for c in cars if c.get("last_seen", 0) >= cutoff]
            self._baseline.add(key)  # restored: changes from here on are real
