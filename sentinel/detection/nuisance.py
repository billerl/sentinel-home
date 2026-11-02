"""
Learns the spots in a camera's view where motion never stops: a spider or a strand of web
swinging in front of the lens at night (lit up white by the infrared), a branch in the wind.
One of those can start a recording every 20 seconds all night (900 clips one night on the
Garage camera) without anything ever happening.

The frame is split into a coarse grid. A cell that has had motion in a large share of the
last few minutes is "muted": motion entirely inside muted cells no longer starts a recording.
Safeguards, so real activity is still caught:
- the object detector keeps looking at those frames; a person, animal or moving vehicle
  unmutes the cells under it immediately (see ``clear``) and starts a recording;
- motion that reaches outside the muted cells (someone walking in from elsewhere) is normal;
- a cell unmutes by itself once it has been quiet for a while.
"""

import time
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Tuple

GRID_COLS = 16
GRID_ROWS = 9
SLOT_SECONDS = 10.0  # activity is counted in 10-second slots ...
WINDOW_SECONDS = 15 * 60.0  # ... over the last 15 minutes
MUTE_SLOTS = 30  # active in a third of the window's slots = never-ending motion
UNMUTE_QUIET_SECONDS = 5 * 60.0  # a muted cell that's been quiet this long is normal again
MUTED_SHARE = 0.8  # a motion box is muted when this share of its cells are muted

Box = Tuple[int, int, int, int]


class NuisanceMap:
    def __init__(self) -> None:
        self._slots: Dict[Tuple[int, int], Deque[int]] = {}
        self._last_active: Dict[Tuple[int, int], float] = {}

    def _cells(self, box: Box, frame_w: int, frame_h: int) -> List[Tuple[int, int]]:
        x, y, w, h = box
        c0 = max(0, min(GRID_COLS - 1, int(x * GRID_COLS / max(1, frame_w))))
        c1 = max(0, min(GRID_COLS - 1, int((x + max(1, w) - 1) * GRID_COLS / max(1, frame_w))))
        r0 = max(0, min(GRID_ROWS - 1, int(y * GRID_ROWS / max(1, frame_h))))
        r1 = max(0, min(GRID_ROWS - 1, int((y + max(1, h) - 1) * GRID_ROWS / max(1, frame_h))))
        return [(c, r) for c in range(c0, c1 + 1) for r in range(r0, r1 + 1)]

    def observe(self, boxes: Iterable[Box], frame_w: int, frame_h: int, now: Optional[float] = None) -> None:
        """Record that motion happened in these boxes."""
        now = time.time() if now is None else now
        slot = int(now // SLOT_SECONDS)
        oldest = slot - int(WINDOW_SECONDS // SLOT_SECONDS)
        for box in boxes:
            for cell in self._cells(box, frame_w, frame_h):
                slots = self._slots.setdefault(cell, deque())
                if not slots or slots[-1] != slot:
                    slots.append(slot)
                while slots and slots[0] <= oldest:
                    slots.popleft()
                self._last_active[cell] = now

    def _muted(self, cell: Tuple[int, int], now: float) -> bool:
        slots = self._slots.get(cell)
        if not slots:
            return False
        if now - self._last_active.get(cell, 0.0) > UNMUTE_QUIET_SECONDS:
            return False
        oldest = int(now // SLOT_SECONDS) - int(WINDOW_SECONDS // SLOT_SECONDS)
        return sum(1 for s in slots if s > oldest) >= MUTE_SLOTS

    def is_muted(self, box: Box, frame_w: int, frame_h: int, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        cells = self._cells(box, frame_w, frame_h)
        muted = sum(1 for c in cells if self._muted(c, now))
        return muted >= MUTED_SHARE * len(cells)

    def clear(self, box: Box, frame_w: int, frame_h: int) -> None:
        """Something real is here (the object detector saw it): these cells aren't a nuisance."""
        for cell in self._cells(box, frame_w, frame_h):
            self._slots.pop(cell, None)
            self._last_active.pop(cell, None)

    def muted_cells(self, now: Optional[float] = None) -> List[Tuple[int, int]]:
        now = time.time() if now is None else now
        return [c for c in self._slots if self._muted(c, now)]
