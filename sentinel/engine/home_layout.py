"""
The order of the home page tiles, one per login (moved by press-and-hold on the home page).

Kept in recorded_videos/home_layouts.json as {login key: [tile key, ...]}. A login key is "person:<id>" for a
family member's own login and "household" for the shared one. Only the order is stored: the page itself decides
which tiles exist, so a tile added in an update just appears at the end, and an unknown key is ignored.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

FILE_NAME = "home_layouts.json"
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAX_TILES = 60


def login_key(person_id: Optional[int]) -> str:
    return "household" if person_id is None else f"person:{int(person_id)}"


class HomeLayouts:
    def __init__(self, root: str) -> None:
        self.path = os.path.join(os.path.abspath(root), FILE_NAME)
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, List[str]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, person_id: Optional[int]) -> List[str]:
        with self._lock:
            order = self._load().get(login_key(person_id))
        return [k for k in order if isinstance(k, str) and _KEY.match(k)] if isinstance(order, list) else []

    def save(self, person_id: Optional[int], order) -> Tuple[bool, str]:
        """Store a new order; an empty list puts the tiles back in their usual order."""
        if not isinstance(order, list):
            return False, "The tile order must be a list."
        clean: List[str] = []
        for key in order[:MAX_TILES]:
            if isinstance(key, str) and _KEY.match(key) and key not in clean:  # (non-strings are dropped, never "None")
                clean.append(key)
        with self._lock:
            data = self._load()
            if clean:
                data[login_key(person_id)] = clean
            else:
                data.pop(login_key(person_id), None)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        return True, "Layout saved." if clean else "Tiles are back in their usual order."
