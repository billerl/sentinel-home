"""
TV shortcuts (beta): button presses recorded on the web remote and played back later, e.g. "Judge Judy" = open
YouTube TV, OK on "Who's watching", then search. The TV can't say which buttons its own remote pressed, so the
recording is done here: while a TV is recording, each button, app and typed text sent through Sentinel's remote is
noted with the pause before it (SmartSwitches.on_tv_step).

Playback keeps those pauses, a little longer (apps load slower some days). A shortcut that starts by opening an app
quits that app first: a running app resumes wherever it was left, and the same presses would land on other buttons
(on YouTube TV, two presses right of Home is a "buy a membership" button).

Kept in tv_shortcuts.json next to the other stores.
"""

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

STORE_FILE = "tv_shortcuts.json"
NAME_MAX = 30
NAME_OK = re.compile(r"^[\w '&().\-]{1,%d}$" % NAME_MAX)
MAX_STEPS = 80
MAX_SHORTCUTS = 30
RECORD_SECONDS = 15 * 60  # a recording left running is dropped after this
LONGEST_WAIT = 20.0  # a long pause while recording (reading the screen) plays back as this at most
SLOWER = 1.25  # playback pauses are the recorded ones times this, plus EXTRA
EXTRA = 0.3
QUIT_WAIT = 2.0  # after quitting the first app, before opening it again


class TVShortcuts:
    def __init__(self, root: str, switches: Any, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 spawn: Optional[Callable[..., Any]] = None) -> None:
        self.path = os.path.join(os.path.abspath(root), STORE_FILE)
        self.switches = switches
        self._clock = clock
        self._sleep = sleep
        self._spawn = spawn or (lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start())
        self._lock = threading.Lock()
        self._recording: Dict[str, Dict[str, Any]] = {}  # TV id -> {"started", "last", "steps"}
        self._playing: Dict[str, str] = {}  # TV id -> the shortcut's name
        self._shortcuts: List[Dict[str, Any]] = []
        try:
            with open(self.path) as f:
                self._shortcuts = [s for s in json.load(f) if isinstance(s, dict) and s.get("steps")]
        except (OSError, ValueError):
            pass
        switches.on_tv_step = self.note

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._shortcuts, f, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ recording
    def _live_recording(self, tv_id: str) -> Optional[Dict[str, Any]]:
        rec = self._recording.get(tv_id)
        if rec is not None and self._clock() - rec["started"] > RECORD_SECONDS:
            self._recording.pop(tv_id, None)
            return None
        return rec

    def start(self, tv_id: str) -> Tuple[bool, str]:
        with self._lock:
            if tv_id in self._playing:
                return False, "Wait for the shortcut that's playing to finish."
            self._recording[tv_id] = {"started": self._clock(), "last": self._clock(), "steps": []}
        return True, "recording"

    def note(self, tv_id: str, do: str, value: str) -> None:
        """A button, app or text the remote just sent (ignored unless that TV is recording)."""
        with self._lock:
            rec = self._live_recording(tv_id)
            if rec is None or tv_id in self._playing or len(rec["steps"]) >= MAX_STEPS:
                return
            now = self._clock()
            rec["steps"].append({"do": do, "value": value, "wait": round(min(now - rec["last"], LONGEST_WAIT), 2)})
            rec["last"] = now

    def stop(self, tv_id: str, name: str) -> Tuple[bool, str]:
        name = re.sub(r"\s+", " ", str(name or "")).strip()
        if not NAME_OK.match(name):
            return False, f"Give it a short name (letters and numbers, up to {NAME_MAX})."
        with self._lock:
            rec = self._live_recording(tv_id)
            if rec is None:
                return False, "Nothing was being recorded."
            if not rec["steps"]:
                return False, "No buttons were pressed yet."
            if len([s for s in self._shortcuts if s["tv"] == tv_id]) >= MAX_SHORTCUTS:
                return False, "That TV has as many shortcuts as it can keep; delete one first."
            self._recording.pop(tv_id, None)
            steps = rec["steps"]
            steps[0]["wait"] = 0.0
            self._shortcuts = [s for s in self._shortcuts if not (s["tv"] == tv_id and s["name"].lower() == name.lower())]
            self._shortcuts.append({"id": uuid.uuid4().hex[:8], "tv": tv_id, "name": name, "steps": steps,
                                    "created": self._clock()})
            self._save()
        app_logger.log("info", "tv_shortcut_saved", shortcut=name, steps=len(steps), component="tv_shortcuts",
                       action="record")
        return True, f"Saved “{name}” ({len(steps)} steps)."

    def cancel(self, tv_id: str) -> Tuple[bool, str]:
        with self._lock:
            self._recording.pop(tv_id, None)
        return True, "discarded"

    # ------------------------------------------------------------ the list
    def view(self, tv_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._live_recording(tv_id)
            return {"recording": rec is not None, "steps": len(rec["steps"]) if rec else 0,
                    "playing": self._playing.get(tv_id),
                    "shortcuts": [{"id": s["id"], "name": s["name"], "steps": len(s["steps"])}
                                  for s in self._shortcuts if s["tv"] == tv_id]}

    def delete(self, tv_id: str, shortcut_id: str) -> Tuple[bool, str]:
        with self._lock:
            kept = [s for s in self._shortcuts if not (s["tv"] == tv_id and s["id"] == shortcut_id)]
            if len(kept) == len(self._shortcuts):
                return False, "That shortcut is already gone."
            self._shortcuts = kept
            self._save()
        return True, "deleted"

    # ------------------------------------------------------------ playing
    def play(self, tv_id: str, shortcut_id: str) -> Tuple[bool, str]:
        with self._lock:
            sc = next((s for s in self._shortcuts if s["tv"] == tv_id and s["id"] == shortcut_id), None)
            if sc is None:
                return False, "That shortcut is gone."
            if tv_id in self._recording:
                return False, "Stop recording first."
            if tv_id in self._playing:
                return False, f"“{self._playing[tv_id]}” is still playing."
            self._playing[tv_id] = sc["name"]
        self._spawn(self._run, tv_id, sc)
        return True, f"Playing “{sc['name']}”…"

    def _run(self, tv_id: str, sc: Dict[str, Any]) -> None:
        sw = self.switches
        ok, why = True, ""
        try:
            for i, step in enumerate(sc["steps"]):
                if i:
                    self._sleep(step["wait"] * SLOWER + EXTRA)
                if step["do"] == "open":
                    if i == 0:  # start from the app's first screen, every time
                        if sw.tv_close(tv_id, step["value"])[0]:
                            self._sleep(QUIT_WAIT)
                    ok, why = sw.tv_open(tv_id, step["value"])
                elif step["do"] == "type":
                    ok, why = sw.tv_type(tv_id, step["value"])
                else:
                    ok, why = sw.tv_key(tv_id, step["value"])
                if not ok:
                    break
        finally:
            with self._lock:
                self._playing.pop(tv_id, None)
        app_logger.log("info" if ok else "warning", "tv_shortcut_played", shortcut=sc["name"], ok=ok,
                       **({} if ok else {"why": why}), component="tv_shortcuts", action="play")
