"""
Holding a room temperature with a heater that only has a remote (an EdenPURE): Sentinel reads the room from the
BroadLink's thermometer and steps the heater's own setting (Temp up / Temp down: its column of lights) toward the
target, one step every 15 minutes, leaving the heater's Auto mode to switch itself on and off in between. Far
below the target it goes to the top step until the room is close, then back to the step that held it last time.

It never presses Power or Mode. Both toggle, and with no way to see the heater it could switch it on, or to High,
when it meant the opposite (a plug that reports its power use would make that safe: not here yet). Turning the
setting down can only make it cooler, so every safety step does that: the room too warm, the thermometer silent.

The setting can't be seen either, so it's counted: all the way down (more presses than there are steps), then up
to the wanted one; again every few hours, in case someone used the panel. In High the steps do nothing: a room
that stays warm with the setting at the bottom is reported, not "fixed" by pressing Mode.
"""

import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from sentinel.logging_utils import app_logger

STATE_FILE = "heater.json"
STEPS = 13  # lights in the EdenPURE's column; 1 = the bottom one
CHECK_SECONDS = 15 * 60  # rooms warm slowly: one step, then wait and see
READ_SECONDS = 60
DEADBAND_F = 0.5
WARMUP_F = 3.0  # this far below the target: the top step until within CLOSE_F
CLOSE_F = 1.0
MAX_F = 78.0  # the room this warm: the setting goes all the way down, whatever the target
TARGET_RANGE = (50.0, 76.0)
STALE_SECONDS = 10 * 60
RECOUNT_SECONDS = 6 * 3600
STUCK_SECONDS = 45 * 60  # at the bottom step and still this far over the target for this long: on High?
STUCK_F = 3.0
WARN_AGAIN_SECONDS = 2 * 3600
PRESS_GAP = 0.7  # the panel misses presses closer together
START_LEVEL = 7


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


class HeaterControl:
    def __init__(self, root: str, remotes, settings: Dict[str, Any], notify: Callable[[str], None],
                 clock: Callable[[], float] = time.time) -> None:
        self.path = os.path.join(os.path.abspath(root), STATE_FILE)
        self.remotes = remotes
        self.settings = settings
        self.notify = notify  # (text): every Sentinel chat
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.temp_f: Optional[float] = None
        self.humidity: Optional[float] = None
        self.read_at = 0.0
        self._last_read_try = -1e9
        self._warned: Dict[str, float] = {}
        self.state: Dict[str, Any] = {"enabled": False, "target_f": 69.0, "remote": "", "up": "", "down": "",
                                      "level": None, "counted_at": 0.0, "changed_at": 0.0, "held": None,
                                      "bottom_since": None, "status": "Off."}
        try:
            with open(self.path) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                self.state.update({k: v for k, v in saved.items() if k in self.state})
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        threading.Thread(target=self._run, name="heater", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let one bad reading stop it
                app_logger.log("error", "heater_failed", error_message=str(e)[:200], component="heater", action="tick")
            self._wake.wait(READ_SECONDS)
            self._wake.clear()

    def _save(self) -> None:
        with self._lock:
            data = dict(self.state)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def _status(self, text: str) -> None:
        self.state["status"] = text
        self._save()

    def _warn(self, key: str, text: str) -> None:
        now = self.clock()
        if now - self._warned.get(key, -1e9) < WARN_AGAIN_SECONDS:
            return
        self._warned[key] = now
        app_logger.log("warning", "heater_" + key, message=text, component="heater", action="warn")
        self.notify("🔥 " + text)

    # ------------------------------------------------------------ the buttons
    def buttons(self) -> Optional[Tuple[str, str, str]]:
        """(remote, warmer button, cooler button): the chosen ones, else guessed from the names
        ("EdenPURE heater" / "Temp up" / "Temp down")."""
        known = self.remotes.buttons()
        remote, up, down = self.state.get("remote"), self.state.get("up"), self.state.get("down")
        if remote in known and up in known[remote] and down in known[remote]:
            return remote, up, down
        for name in sorted(known, key=lambda n: not re.search(r"heat|eden", n, re.I)):
            ups = [b for b in known[name] if re.search(r"\b(temp\w*|warm\w*)\b.*\bup\b|\bwarmer\b", b, re.I)]
            downs = [b for b in known[name] if re.search(r"\b(temp\w*|cool\w*)\b.*\bdown\b|\bcooler\b", b, re.I)]
            if ups and downs:
                return name, ups[0], downs[0]
        return None

    def choose_buttons(self, remote: str, up: str, down: str) -> Tuple[bool, str]:
        known = self.remotes.buttons()
        if remote not in known or up not in known[remote] or down not in known[remote] or up == down:
            return False, "Pick the heater's remote and its two different buttons."
        with self._lock:
            self.state.update({"remote": remote, "up": up, "down": down, "level": None})
        self._save()
        return True, "Saved."

    def _press(self, which: str, times: int) -> bool:
        found = self.buttons()
        if found is None or times <= 0:
            return found is not None
        remote, up, down = found
        ok, message = self.remotes.press(remote, up if which == "up" else down, times=times, source="heater",
                                         gap=PRESS_GAP)
        if not ok:
            with self._lock:
                self.state["level"] = None  # count again once it answers
            self._warn("unreachable", f"I couldn't reach the BroadLink to change the heater ({message}).")
        return ok

    def _to_bottom(self) -> bool:
        if not self._press("down", STEPS + 1):
            return False
        now = self.clock()
        with self._lock:
            self.state.update({"level": 1, "counted_at": now, "changed_at": now})
            if self.state.get("bottom_since") is None:
                self.state["bottom_since"] = now
        return True

    def _set_level(self, want: int, recount: bool = False) -> bool:
        want = max(1, min(STEPS, int(want)))
        level = self.state.get("level")
        if recount or level is None:
            if not self._to_bottom():
                return False
            level = 1
        if want != level and not self._press("up" if want > level else "down", abs(want - level)):
            return False
        now = self.clock()
        with self._lock:
            self.state.update({"level": want, "changed_at": now})
            self.state["bottom_since"] = (self.state.get("bottom_since") or now) if want == 1 else None
        app_logger.log("info", "heater_setting", setting=want, room_f=self.temp_f, target_f=self.state["target_f"],
                       recount=recount, component="heater", action="set")
        return True

    # ------------------------------------------------------------ the room
    def read(self) -> None:
        """The BroadLink's thermometer, smoothed (the heater's own fan stirs the air)."""
        self._last_read_try = self.clock()
        got = self.remotes.climate()
        if not got:
            return
        f = c_to_f(float(got["temperature"]))
        self.temp_f = f if self.temp_f is None or self.clock() - self.read_at > STALE_SECONDS else \
            round(0.5 * self.temp_f + 0.5 * f, 2)
        self.humidity = got.get("humidity")
        self.read_at = self.clock()

    def tick(self) -> None:
        now = self.clock()
        if now - self._last_read_try >= READ_SECONDS - 1:
            self.read()
        st = self.state
        if not st["enabled"]:
            return
        if self.buttons() is None:
            self._status("Teach the heater's Temp up and Temp down buttons first.")
            return
        if self.temp_f is None or now - self.read_at > STALE_SECONDS:
            if st.get("level") != 1 and self._to_bottom():
                self._warn("no_reading", "I can't read the room temperature from the BroadLink, so I turned the "
                                         "heater's setting all the way down.")
            self._status("No room temperature from the BroadLink: the setting is all the way down.")
            return
        t, target = self.temp_f, float(st["target_f"])
        if t >= MAX_F:
            if st.get("level") != 1:
                self._to_bottom()
                self._warn("too_warm", f"The room is {t:.0f}°F, so I turned the heater's setting all the way down.")
            self._stuck(now, t, target)
            self._status(f"Too warm ({t:.1f}°F): the setting is all the way down.")
            return
        self._stuck(now, t, target)
        level = st.get("level")
        due = now - st.get("changed_at", 0) >= CHECK_SECONDS
        if level is None or now - st.get("counted_at", 0) >= RECOUNT_SECONDS:
            want = level if level is not None else (st.get("held") or START_LEVEL)
            if self._set_level(want, recount=True):
                self._status(f"Counted the setting: {want} of {STEPS}.")
            return
        error = target - t
        held = st.get("held")
        if level == STEPS and error <= CLOSE_F and held and int(held) < STEPS - 1:
            want, why = int(held) + 1, "nearly there"  # out of the warm-up straight away, before it overshoots
        elif abs(error) <= DEADBAND_F:
            if level < STEPS:
                with self._lock:
                    st["held"] = level  # this step holds the target: where to come back to after a warm-up
            self._status(f"Holding {target:.0f}°F: room {t:.1f}°F, setting {level} of {STEPS}.")
            return
        elif not due:
            return
        elif error > WARMUP_F:
            want, why = STEPS, "warming up"
        elif error > 0:
            want, why = level + 1, "a little warmer"
        else:
            want, why = level - (2 if error < -2 else 1), "a little cooler"
        want = max(1, min(STEPS, want))
        if want == level:
            self._status(f"Room {t:.1f}°F, target {target:.0f}°F: the setting is already at {level} of {STEPS}.")
            with self._lock:
                st["changed_at"] = now  # wait another round before trying again
            return
        if self._set_level(want):
            self._status(f"Room {t:.1f}°F, target {target:.0f}°F: {why}, setting {want} of {STEPS} "
                         f"({time.strftime('%-I:%M %p', time.localtime(now))}).")

    def _stuck(self, now: float, t: float, target: float) -> None:
        """At the bottom step and still well over the target: the heater may be on High (the steps do nothing
        then) or someone turned it up on the panel. Reported, not "fixed": Mode is a toggle."""
        since = self.state.get("bottom_since")
        if since is not None and t >= target + STUCK_F and now - since >= STUCK_SECONDS:
            self._warn("stuck", f"The room is {t:.0f}°F with the heater's setting at its lowest for "
                                f"{round((now - since) / 60)} minutes. It may be on High (the Temp buttons don't "
                                f"work then) or turned up on the panel: please check it.")

    # ------------------------------------------------------------ asked for
    def hold(self, target_f: Optional[float] = None) -> Tuple[bool, str]:
        if self.buttons() is None:
            return False, "First teach the heater's Temp up and Temp down buttons on the Remotes page."
        with self._lock:
            if target_f is not None:
                self.state["target_f"] = float(max(TARGET_RANGE[0], min(TARGET_RANGE[1], round(float(target_f) * 2) / 2)))
            was = self.state["enabled"]
            self.state["enabled"] = True
            self.state["changed_at"] = 0.0  # act on the next check
            if not was:
                self.state["level"] = None  # count from the bottom: someone may have used the panel meanwhile
        self._save()
        app_logger.log("info", "heater_hold", target_f=self.state["target_f"], component="heater", action="hold")
        self._wake.set()
        room = f" The room is {self.temp_f:.0f}°F." if self.temp_f is not None else ""
        return True, f"Holding the heater at {self.state['target_f']:g}°F.{room}"

    def release(self) -> Tuple[bool, str]:
        with self._lock:
            self.state["enabled"] = False
        level = self.state.get("level")
        self._status("Off." + (f" The heater keeps its own setting ({level} of {STEPS})." if level else ""))
        app_logger.log("info", "heater_release", component="heater", action="release")
        return True, "Stopped holding the temperature. The heater keeps its current setting."

    def view(self) -> Dict[str, Any]:
        found = self.buttons()
        unit = str(self.settings.get("weather_units") or "fahrenheit")
        return {"enabled": self.state["enabled"], "target_f": self.state["target_f"], "level": self.state.get("level"),
                "steps": STEPS, "status": self.state.get("status", ""), "room_f": self.temp_f,
                "humidity": self.humidity, "read_at": self.read_at or None, "celsius": unit.startswith("c"),
                "buttons": {"remote": found[0], "up": found[1], "down": found[2]} if found else None,
                "remotes": self.remotes.buttons(), "max_f": MAX_F, "range": list(TARGET_RANGE)}

    # ------------------------------------------------------------ words
    _HOLD = re.compile(r"^(?:please\s+)?(?:(?:set|turn|put)\s+)?(?:the\s+)?heater\s+(?:to|at|on)\s+(\d{2}(?:\.\d)?)"
                       r"(?:\s*(?:degrees?|°)?\s*(f|c|fahrenheit|celsius)?)?$|"
                       r"^(?:please\s+)?(?:keep|hold)\s+(?:the\s+)?(?:room|house|living\s+room|it)\s+(?:at\s+)?"
                       r"(\d{2}(?:\.\d)?)(?:\s*(?:degrees?|°)?\s*(f|c|fahrenheit|celsius)?)?$")
    _RELEASE = re.compile(r"^(?:please\s+)?(?:stop\s+holding(?:\s+the)?\s+(?:room\s+)?temperature|"
                          r"(?:turn\s+)?(?:off\s+)?(?:the\s+)?heater\s+thermostat(?:\s+off)?|stop\s+the\s+heater\s+thermostat)$")
    _ASK = re.compile(r"^(?:what'?s|what\s+is)\s+the\s+(?:room|inside|indoor)\s+temperature|"
                      r"^how\s+warm\s+is\s+it\s+(?:inside|in\s+here)|^heater\s+status$|"
                      r"^what'?s\s+the\s+heater\s+(?:set\s+to|doing)")

    def parse(self, text: str) -> Optional[Tuple[str, Optional[float]]]:
        """("hold", °F) / ("release", None) / ("ask", None); None when it isn't about the heater thermostat."""
        low = re.sub(r"[?.!]+$", "", (text or "").strip().lower()).replace("’", "'")
        m = self._HOLD.match(low)
        if m:
            value, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            n = float(value)
            celsius = (unit or "").startswith("c") or (not unit and n < 40)
            return "hold", c_to_f(n) if celsius else n
        if self._RELEASE.match(low):
            return "release", None
        if self._ASK.match(low):
            return "ask", None
        return None

    def understands(self, text: str) -> bool:
        return self.parse(text) is not None

    def command(self, text: str, source: str) -> Optional[str]:
        parsed = self.parse(text)
        if parsed is None:
            return None
        kind, value = parsed
        if kind == "hold":
            return self.hold(value)[1]
        if kind == "release":
            return self.release()[1]
        return self.summary() or "The heater thermostat isn't set up."

    def summary(self) -> str:
        """One line for questions and Claude's context."""
        if self.buttons() is None:
            return ""
        room = f"room {self.temp_f:.1f}°F" if self.temp_f is not None else "no room temperature yet"
        if not self.state["enabled"]:
            return f"Heater thermostat: off ({room}). Say 'set the heater to 69' to hold a temperature."
        level = self.state.get("level")
        return (f"Heater thermostat: holding {self.state['target_f']:g}°F, {room}"
                + (f", setting {level} of {STEPS}." if level else "."))
