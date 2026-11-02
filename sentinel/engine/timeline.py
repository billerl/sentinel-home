"""
The family Timeline: a day of one person's phone positions turned into stops and drives,
like Google Maps Timeline ("Home until 7:28am, 9 min drive, School 7:45am-2:52pm, ...").

Phones report unevenly: an Android sends every few minutes, an iPhone mostly when it has moved
a good distance, and both go quiet while sitting still. So a stop is a run of positions close
together that lasted STOP_MIN_S, and a quiet phone is assumed to have stayed where it last was
until shortly before its next position somewhere else.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sentinel.engine.location import MOVING_KMH, Place, bearing_word, distance_m

STOP_RADIUS_M = 120  # positions this close to a stop's middle belong to it
STOP_MIN_S = 5 * 60  # shorter is a red light or a drive-through
MAX_ACC_M = 300  # vaguer positions are left out (cell-tower guesses)
QUIET_S = 10 * 60  # a gap longer than this before a position elsewhere: the phone was resting, not driving
DRIVE_MPS = 13.4  # ~30 mph, to guess when someone left a stop when the phone was quiet
PLACE_MARGIN_M = 50

Point = Tuple[float, float, float, Optional[float], Optional[float]]  # ts, lat, lon, acc, vel


@dataclass
class _Cluster:
    points: List[Point] = field(default_factory=list)
    lat: float = 0.0
    lon: float = 0.0

    def add(self, p: Point) -> None:
        self.points.append(p)
        n = len(self.points)
        self.lat += (p[1] - self.lat) / n
        self.lon += (p[2] - self.lon) / n

    @property
    def first(self) -> float:
        return self.points[0][0]

    @property
    def last(self) -> float:
        return self.points[-1][0]

    def near(self, p: Point) -> bool:
        acc = p[3] if p[3] is not None else 50.0
        return distance_m(self.lat, self.lon, p[1], p[2]) <= max(STOP_RADIUS_M, min(acc, 200.0))


@dataclass
class Stop:
    start: float
    end: Optional[float]  # None: still there (the phone's latest position)
    lat: float
    lon: float
    place: Optional[Place] = None


def _clusters(points: Sequence[Point]) -> List[_Cluster]:
    out: List[_Cluster] = []
    for p in points:
        if out and out[-1].near(p):
            out[-1].add(p)
        else:
            c = _Cluster()
            c.add(p)
            out.append(c)
    # One wild GPS reading in the middle of a stay splits it in two: glue it back together.
    merged: List[_Cluster] = []
    i = 0
    while i < len(out):
        c = out[i]
        while (i + 2 < len(out) and len(out[i + 1].points) <= 2 and out[i + 1].last - out[i + 1].first < STOP_MIN_S
               and distance_m(c.lat, c.lon, out[i + 2].lat, out[i + 2].lon) <= STOP_RADIUS_M):
            for p in out[i + 2].points:
                c.add(p)
            i += 2
        merged.append(c)
        i += 1
    return merged


def place_for(lat: float, lon: float, places: Sequence[Place]) -> Optional[Place]:
    inside = [p for p in places if distance_m(lat, lon, p.lat, p.lon) <= p.radius + PLACE_MARGIN_M]
    return min(inside, key=lambda p: distance_m(lat, lon, p.lat, p.lon)) if inside else None


def find_stops(points: Sequence[Point], places: Sequence[Place], latest: bool = True) -> List[Stop]:
    """Stops from positions sorted by time. ``latest``: the last position is the phone's newest one,
    so a stop there is still going on."""
    good = [p for p in points if p[3] is None or p[3] <= MAX_ACC_M]
    clusters = _clusters(good)
    stops: List[Stop] = []
    for i, c in enumerate(clusters):
        nxt = clusters[i + 1] if i + 1 < len(clusters) else None
        end: Optional[float] = c.last
        if nxt is not None and nxt.first - c.last > QUIET_S:
            # Quiet phone, then a position somewhere else: it stayed until it had to leave to get there.
            travel = max(60.0, distance_m(c.lat, c.lon, nxt.points[0][1], nxt.points[0][2]) / DRIVE_MPS)
            end = max(c.last, nxt.first - travel)
        elif nxt is None and latest:
            end = None
        moving = all(p[4] is not None and p[4] >= MOVING_KMH for p in c.points)
        if moving or (end is not None and end - c.first < STOP_MIN_S):
            continue
        stops.append(Stop(c.first, end, c.lat, c.lon, place_for(c.lat, c.lon, places)))
    # Walking around a big place (a school campus) can look like two stops in a row there.
    joined: List[Stop] = []
    for s in stops:
        prev = joined[-1] if joined else None
        if prev is not None and prev.end is not None and (
                (s.place is not None and prev.place is not None and s.place.id == prev.place.id)
                or (s.place is None and prev.place is None
                    and distance_m(prev.lat, prev.lon, s.lat, s.lon) <= STOP_RADIUS_M * 1.5)):
            prev.end = s.end
            continue
        joined.append(s)
    return joined


def _path_m(points: Sequence[Point]) -> float:
    return sum(distance_m(a[1], a[2], b[1], b[2]) for a, b in zip(points, points[1:]))


def describe(lat: float, lon: float, places: Sequence[Place]) -> str:
    """Where an unnamed stop is: '0.8 mi northeast of Home'."""
    if not places:
        return f"{lat:.5f}, {lon:.5f}"
    near = min(places, key=lambda p: distance_m(lat, lon, p.lat, p.lon))
    d = distance_m(lat, lon, near.lat, near.lon)
    if d < 400:
        return f"near {near.name}"
    mi = d / 1609.344
    return f"{mi:.1f} mi {bearing_word(near.lat, near.lon, lat, lon)} of {near.name}"


def day_window(date: str) -> Tuple[float, float]:
    """Local midnight to midnight for 'YYYY-MM-DD' (23 or 25 hours on clock-change days)."""
    t = time.strptime(date, "%Y-%m-%d")
    start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    end = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return start, end


def day_view(points: Sequence[Point], places: Sequence[Place], start: float, end: float,
             now: Optional[float] = None, latest: bool = True) -> Dict[str, Any]:
    """The Timeline page's day: stops (clipped to the day), the drives between them, and the route.

    ``points`` should reach well beyond the day on both sides so stays across midnight are whole."""
    now = time.time() if now is None else now
    stops = find_stops(points, places, latest=latest)
    shown = [s for s in stops if s.start < end and (s.end is None or s.end > start)]
    good = [p for p in points if p[3] is None or p[3] <= MAX_ACC_M]
    items: List[Dict[str, Any]] = []
    miles = 0.0
    for i, s in enumerate(shown):
        if i > 0:
            prev = shown[i - 1]
            leg = [p for p in good if prev.end is not None and prev.end <= p[0] <= s.start]
            route = [(0.0, prev.lat, prev.lon, None, None)] + leg + [(0.0, s.lat, s.lon, None, None)]
            m = _path_m(route)
            miles += m / 1609.344
            items.append({"type": "drive", "start": prev.end, "end": s.start, "miles": round(m / 1609.344, 1)})
        stop_end = s.end if s.end is not None else None
        items.append({
            "type": "stop", "start": s.start, "end": stop_end, "lat": round(s.lat, 6), "lon": round(s.lon, 6),
            "name": s.place.name if s.place else None,
            "home": bool(s.place and s.place.is_home),
            "where": None if s.place else describe(s.lat, s.lon, places),
            "before_day": s.start < start, "after_day": stop_end is not None and stop_end >= end,
            "ongoing": stop_end is None,
        })
    # Time away from home, from the day's first position (or midnight) until now / midnight.
    day_pts = [p for p in good if start <= p[0] < end]
    away = None
    if day_pts and any(p.place is not None and p.place.is_home for p in stops):
        lo = start if any(s.start <= start for s in stops) else day_pts[0][0]
        hi = min(end, now)
        home = sum(max(0.0, min(hi, s.end if s.end is not None else hi) - max(lo, s.start))
                   for s in shown if s.place is not None and s.place.is_home)
        away = max(0.0, hi - lo - home)
    return {
        "items": items,
        "path": [[round(p[1], 6), round(p[2], 6)] for p in day_pts][-3000:],
        "stops": sum(1 for s in shown if not (s.place and s.place.is_home)),
        "miles": round(miles, 1),
        "away_s": round(away) if away is not None else None,
        "points": len(day_pts),
    }
