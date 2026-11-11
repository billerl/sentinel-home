"""
House view: a 3D drawing of the home (rooms, stairs, doors, windows) with the real devices placed in it,
shown live on the House page: switches you can tap, cameras, and the Wi-Fi motion sensors with the paths
between them lighting up when something moves.

The drawing lives in house.json next to the other stores (recordings folder, so it never goes into git:
a floor plan is personal). Coordinates are "plan units" from a floor-plan drawing (x right, y down, any
scale; the page scales them); heights are in storeys-ish units (a ceiling is about 3), so a split-level
can have rooms a few steps up or down. Everything coming in is cleaned here: the page trusts this file.

Devices are placed by reference to the real thing (switch id, camera id, sensor board id), so a rename
shows up on the map by itself. The drawing itself (rooms, features, marks) is replaced as a whole.
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

FILE_NAME = "house.json"
KINDS = ("sensor", "switch", "camera", "remote")  # remote = a remote taught to the BroadLink, by its name
ROOM_KINDS = ("", "garage", "outside")
FEATURES = ("steps", "window", "door", "box", "rail")
MAX_ROOMS = 80
MAX_FEATURES = 300
MAX_MARKS = 80
MAX_DEVICES = 120
MAX_POINTS = 24
COORD = 10000.0  # plan units either way
HEIGHT = 20.0
ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
REF_OK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
REMOTE_REF_OK = re.compile(r"^[\w '&().\-]{1,64}$")  # a remote's name (sentinel/engine/remotes.py NAME_OK)
COLOR_OK = re.compile(r"^#[0-9a-fA-F]{6}$")


def _name(value: Any, limit: int = 40) -> str:
    """Printable text only; non-strings (a null from a sloppy editor) become ""."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:limit]


def _num(value: Any, low: float, high: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return round(min(high, max(low, float(value))), 3)


def _point(value: Any) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = _num(value[0], -COORD, COORD), _num(value[1], -COORD, COORD)
    return None if x is None or y is None else [x, y]


def _rect(value: Any) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    nums = [_num(v, -COORD, COORD) for v in value]
    if None in nums:
        return None
    x1, y1, x2, y2 = nums
    if x2 <= x1 or y2 <= y1:
        return None
    return nums


def _height(value: Any) -> Optional[float]:
    return _num(value, -HEIGHT, HEIGHT)


def _level(value: Any) -> int:
    return 1 if value == 1 else 0


def _room(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    rid = str(raw.get("id") or "").lower()
    name = _name(raw.get("name"))
    if not ID_OK.match(rid) or not name:
        return None
    parts = [r for r in (_rect(p) for p in (raw.get("parts") or [])[:MAX_POINTS]) if r] if isinstance(raw.get("parts"), list) else []
    if not parts:
        r = _rect(raw.get("r"))
        if r is None:
            return None
        parts = [r]
    room: Dict[str, Any] = {"id": rid, "name": name, "parts": parts, "level": _level(raw.get("level"))}
    if isinstance(raw.get("poly"), list):
        poly = [p for p in (_point(q) for q in raw["poly"][:MAX_POINTS]) if p]
        if len(poly) >= 3:
            room["poly"] = poly
    kind = raw.get("kind") if raw.get("kind") in ROOM_KINDS else ""
    if kind:
        room["kind"] = kind
    for key in ("y", "top"):
        h = _height(raw.get(key))
        if h is not None:
            room[key] = h
    label = _point(raw.get("label"))
    if label:
        room["label"] = label
    if raw.get("guess") is True:
        room["guess"] = True
    gaps = []
    for g in (raw.get("gaps") or [])[:MAX_POINTS] if isinstance(raw.get("gaps"), list) else []:
        if isinstance(g, (list, tuple)) and len(g) in (4, 5):
            nums = [_num(v, -COORD, COORD) for v in g[:4]]
            if None not in nums:
                gaps.append(nums + (["door"] if len(g) == 5 and g[4] == "door" else []))
    if gaps:
        room["gaps"] = gaps
    return room


def _feature(raw: Any) -> Optional[Dict[str, Any]]:
    """steps (treads from `from` at height y0 to `to` at y1; solid = filled risers), window (a-b, door = a
    sliding door), door (a-b, coloured panel), box (furniture, a fireplace: at, size [w, d], h), rail (a
    half wall a-b, h high)."""
    if not isinstance(raw, dict) or raw.get("type") not in FEATURES:
        return None
    kind = raw["type"]
    out: Dict[str, Any] = {"type": kind, "level": _level(raw.get("level"))}
    if kind == "steps":
        a, b = _point(raw.get("from")), _point(raw.get("to"))
        y0, y1 = _height(raw.get("y0")), _height(raw.get("y1"))
        width = _num(raw.get("width"), 1, 1000)
        count = raw.get("count") if isinstance(raw.get("count"), int) and not isinstance(raw.get("count"), bool) else 6
        if not a or not b or y0 is None or y1 is None or width is None:
            return None
        out.update({"from": a, "to": b, "y0": y0, "y1": y1, "width": width, "count": max(2, min(30, count)),
                    "solid": raw.get("solid") is True})
    elif kind == "box":
        at, size = _point(raw.get("at")), _point(raw.get("size"))
        h = _num(raw.get("h"), 0.01, HEIGHT)
        if not at or not size or h is None or size[0] <= 0 or size[1] <= 0:
            return None
        out.update({"at": at, "size": size, "h": h})
        y = _height(raw.get("y"))
        if y is not None:
            out["y"] = y
    else:
        a, b = _point(raw.get("a")), _point(raw.get("b"))
        if not a or not b or a == b:
            return None
        out.update({"a": a, "b": b})
        if kind == "window" and raw.get("door") is True:
            out["door"] = True
        for key in ("y", "h"):
            h = _height(raw.get(key))
            if h is not None:
                out[key] = h
    color = raw.get("color")
    if isinstance(color, str) and COLOR_OK.match(color):
        out["color"] = color.lower()
    return out


def _mark(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name, at, h = _name(raw.get("name")), _point(raw.get("at")), _height(raw.get("h"))
    if not name or not at:
        return None
    return {"name": name, "at": at, "h": h if h is not None else 2.0, "level": _level(raw.get("level"))}


def clean_drawing(raw: Any) -> Dict[str, Any]:
    """The drawing part of a layout (rooms, features, marks, notes); raises ValueError when it's unusable."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    rooms = [r for r in (_room(x) for x in (raw.get("rooms") or [])[:MAX_ROOMS]) if r] if isinstance(raw.get("rooms"), list) else []
    if not rooms:
        raise ValueError("a house needs at least one room")
    seen = set()
    rooms = [r for r in rooms if not (r["id"] in seen or seen.add(r["id"]))]
    features = ([f for f in (_feature(x) for x in raw["features"][:MAX_FEATURES]) if f]
                if isinstance(raw.get("features"), list) else [])
    marks = [m for m in (_mark(x) for x in raw["marks"][:MAX_MARKS]) if m] if isinstance(raw.get("marks"), list) else []
    return {"rooms": rooms, "features": features, "marks": marks, "note": _name(raw.get("note"), 300)}


def clean_device(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    kind, ref = raw.get("kind"), raw.get("ref")
    if kind not in KINDS:
        raise ValueError("unknown kind of device")
    if not isinstance(ref, str) or not (REMOTE_REF_OK if kind == "remote" else REF_OK).match(ref):
        raise ValueError("which device?")
    at = _point(raw.get("at"))
    if not at:
        raise ValueError("where should it go?")
    dev = {"id": f"{kind}:{ref}", "kind": kind, "ref": ref, "name": _name(raw.get("name")), "at": at,
           "level": _level(raw.get("level"))}
    turn = _num(raw.get("turn"), -360, 360)
    if turn:
        dev["turn"] = turn
    return dev


class HouseLayout:
    """The saved drawing plus where each device sits; one JSON file, written whole (atomic replace)."""

    def __init__(self, folder: str) -> None:
        self.path = os.path.join(folder, FILE_NAME)
        self._lock = threading.Lock()
        self._drawing: Optional[Dict[str, Any]] = None
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        try:
            self._drawing = clean_drawing(data)
        except ValueError:
            self._drawing = None
        devices = data.get("devices") if isinstance(data, dict) else None
        for raw in devices if isinstance(devices, list) else []:
            try:
                dev = clean_device(raw)
            except ValueError:
                continue
            self._devices[dev["id"]] = dev

    def _save(self) -> None:
        data = {**(self._drawing or {}), "devices": list(self._devices.values()), "saved": time.time()}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"drawing": self._drawing, "devices": [dict(d) for d in self._devices.values()]}

    def set_drawing(self, raw: Any) -> Tuple[bool, str]:
        try:
            drawing = clean_drawing(raw)
        except ValueError as e:
            return False, str(e)
        with self._lock:
            self._drawing = drawing
            self._save()
        app_logger.log("info", "house_drawing_saved", rooms=len(drawing["rooms"]), component="house", action="save")
        return True, f"saved {len(drawing['rooms'])} rooms"

    def place(self, raw: Any) -> Tuple[bool, str]:
        """Put a device on the map, or move it (same kind + ref)."""
        try:
            dev = clean_device(raw)
        except ValueError as e:
            return False, str(e)
        with self._lock:
            if dev["id"] not in self._devices and len(self._devices) >= MAX_DEVICES:
                return False, "the map is full"
            self._devices[dev["id"]] = dev
            self._save()
        return True, "placed"

    def rename_ref(self, kind: str, old: str, new: str) -> None:
        """A device was renamed where it's known by its name (a remote): it keeps its place on the map."""
        with self._lock:
            dev = self._devices.pop(f"{kind}:{old}", None)
            if dev is None:
                return
            dev.update(id=f"{kind}:{new}", ref=new, name=new)
            self._devices[dev["id"]] = dev
            self._save()

    def remove(self, device_id: Any) -> Tuple[bool, str]:
        with self._lock:
            if not isinstance(device_id, str) or self._devices.pop(device_id, None) is None:
                return False, "it isn't on the map"
            self._save()
        return True, "removed"
