"""
Learned remotes: the infrared and radio (RF) buttons of things that only come with a remote (a heater, a
soundbar, ceiling fans), sent by a BroadLink RM4 Pro or RM4 mini on the home network. The Remotes page
teaches a button by pointing the real remote at the BroadLink; then "Hey Jarvis, soundbar volume up 3 times"
or Telegram's "heater power" press it.

Local only: python-broadlink speaks the device's own encrypted LAN protocol, so no BroadLink account is used
after the Wi-Fi setup in its app. There "Lock device" must be off: a locked BroadLink answers only the app.
The codes are kept in remotes.json next to the other stores (the BroadLink's address, then each remote's buttons).

Infrared is one press. RF (the RM4 Pro only) is two steps: hold the button while the BroadLink finds the
frequency, then press it once more so it can record it. Some fans use other radios (2.4 GHz, Bluetooth) the
BroadLink can't hear at all.
"""

import base64
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

STORE_FILE = "remotes.json"
NAME_MAX = 30
NAME_OK = re.compile(r"^[\w '&().\-]{1,%d}$" % NAME_MAX)
LEARN_SECONDS = 30.0  # each step's wait for the button
LET_GO_SECONDS = 2.5  # radio: between finding the frequency (button held) and recording the press
RETRY_SECONDS = 60.0  # after the BroadLink didn't answer: don't search again on every press
MAX_TIMES = 20  # "volume up 50 times" is a typo
PRESS_GAP = 0.3
# Spoken extras around "<remote> <button>": "press the soundbar power button please".
FILLER = {"press", "push", "hit", "tap", "click", "the", "my", "a", "button", "please", "jarvis", "hey"}
LOOSE_FILLER = {"turn", "set", "switch", "put", "to", "speed", "it", "at", "make"}
COUNTS = {"once": 1, "twice": 2, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10}
# "turn the soundbar up" / "louder" mean a button called "Volume up" (only if the remote has one).
ALIASES = [(re.compile(r"\bturn (.+) up\b|\bturn up\b|\blouder\b"), "volume up"),
           (re.compile(r"\bturn (.+) down\b|\bturn down\b|\bquieter\b|\bsofter\b"), "volume down")]
LOCKED = "The BroadLink is locked: in the BroadLink app, open its settings and turn off “Lock device”."


def command_of(code: bytes) -> Optional[str]:
    """What a recorded radio signal says, without the timing wobble: the most common burst as long/short pulses.
    Two recordings of one button differ byte for byte but say the same; two buttons never do. None when it can't
    be read (infrared, or another layout)."""
    try:
        if len(code) < 8:
            return None
        n = int.from_bytes(code[2:4], "little")
        pulses, i = [], 4
        while i < min(len(code), 4 + n):
            v = code[i]
            i += 1
            if v == 0:
                v = int.from_bytes(code[i:i + 2], "big")
                i += 2
            pulses.append(v * 8192 / 269)  # microseconds
        bursts, cur = [], []
        for us in pulses:
            if us > 5000:  # the gap between repeats
                if cur:
                    bursts.append(cur)
                cur = []
            else:
                cur.append(us)
        if cur:
            bursts.append(cur)
        marks = []
        for b in bursts:
            if len(b) < 16:
                continue
            ordered = sorted(b)
            cut = (ordered[len(b) // 4] + ordered[3 * len(b) // 4]) / 2
            marks.append("".join("L" if us > cut else "s" for us in b))
        if len(marks) < 2:
            return None
        best = max(set(marks), key=marks.count)
        return best if marks.count(best) >= 2 else None
    except (IndexError, ValueError):
        return None


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _clean(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


class Remotes:
    def __init__(self, root: str, lib: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.path = os.path.join(os.path.abspath(root), STORE_FILE)
        self._lib = lib  # python-broadlink (imported when first needed; tests pass a fake)
        self._clock = clock
        self._sleep = sleep
        self._found_mhz: Optional[float] = None  # the radio frequency the button being taught came in on
        self._lock = threading.Lock()  # the store
        self._io = threading.Lock()  # one conversation with the BroadLink at a time
        self._device: Any = None
        self._failed_at = -1e9
        self.error = ""  # why the BroadLink isn't reachable (shown on the page)
        self.learning: Dict[str, Any] = {}  # the button being taught: remote, button, kind, step, message
        self._cancel = threading.Event()
        self.hub: Dict[str, Any] = {}
        self.remotes: List[Dict[str, Any]] = []  # [{name, buttons: [{name, kind, code (base64)}]}]
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.hub = data.get("hub") if isinstance(data.get("hub"), dict) else {}
            self.remotes = [r for r in data.get("remotes", []) if isinstance(r, dict) and r.get("name")]
        except (OSError, ValueError, AttributeError):
            pass

    # ------------------------------------------------------------ the BroadLink
    def _broadlink(self):
        if self._lib is None:
            import broadlink

            self._lib = broadlink
        return self._lib

    def _connect(self, force: bool = False):
        """The authenticated BroadLink (its last address first, else a search of the home network)."""
        if self._device is not None and not force:
            return self._device
        if not force and self._clock() - self._failed_at < RETRY_SECONDS:
            return None
        try:
            lib = self._broadlink()
        except ImportError:
            self.error = "The broadlink package isn't installed (pip install -r requirements.txt)."
            return None
        found, self.error = [], ""
        if self.hub.get("host"):
            try:
                found = [lib.hello(self.hub["host"], timeout=3)]
            except Exception:
                found = []
        if not found or (self.hub.get("mac") and found[0].mac.hex() != self.hub["mac"]):
            try:
                found = [d for d in lib.discover(timeout=3) if hasattr(d, "send_data")]
            except Exception as e:
                found = []
                self.error = f"Couldn't search the network: {e}"
            mine = [d for d in found if d.mac.hex() == self.hub.get("mac")]
            found = mine or found
        if not found:
            self._device, self._failed_at = None, self._clock()
            self.error = self.error or "No BroadLink found on the home network."
            return None
        dev = found[0]
        if getattr(dev, "is_locked", False):
            self._device, self._failed_at, self.error = None, self._clock(), LOCKED
            return None
        try:
            dev.auth()
        except Exception as e:
            self._device, self._failed_at = None, self._clock()
            self.error = LOCKED if "auth" in type(e).__name__.lower() else f"The BroadLink didn't answer: {e}"
            return None
        hub = {"host": dev.host[0], "mac": dev.mac.hex(), "model": f"{getattr(dev, 'manufacturer', '')} "
               f"{getattr(dev, 'model', '')}".strip() or "BroadLink", "rf": hasattr(dev, "sweep_frequency")}
        if hub != self.hub:
            app_logger.log("info", "remote_hub_found", host=hub["host"], model=hub["model"], component="remotes",
                           action="connect")
            with self._lock:
                self.hub = hub
            self._save()
        self._device, self.error = dev, ""
        return dev

    def find_hub(self) -> Tuple[bool, str]:
        """The page's "Look again" button."""
        with self._io:
            self._device, self._failed_at, self.error = None, -1e9, ""
            dev = self._connect(force=True)
        return (True, f"Found the {self.hub.get('model', 'BroadLink')}.") if dev else (False, self.error)

    # ------------------------------------------------------------ the store
    def _save(self) -> None:
        with self._lock:
            data = {"hub": self.hub, "remotes": self.remotes}
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)

    def _remote(self, name: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self.remotes if r["name"].lower() == _clean(name).lower()), None)

    @staticmethod
    def _button(remote: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
        return next((b for b in remote.get("buttons", []) if b["name"].lower() == _clean(name).lower()), None)

    def view(self) -> Dict[str, Any]:
        with self._lock:
            remotes = [{"name": r["name"], "buttons": [{"name": b["name"], "kind": b.get("kind", "ir")}
                                                       for b in r.get("buttons", [])]} for r in self.remotes]
        learning = dict(self.learning)
        if learning.get("step") in ("done", "failed") and self._clock() - learning.get("at", 0) > 120:
            learning = {}  # an old result: not shown again when the page is opened later
        return {"hub": dict(self.hub) if self.hub else None, "connected": self._device is not None,
                "error": self.error, "remotes": remotes, "learning": learning}

    def rename(self, remote: str, button: Optional[str], new: str) -> Tuple[bool, str]:
        new = _clean(new)
        if not NAME_OK.match(new):
            return False, f"Use letters, numbers and spaces (up to {NAME_MAX})."
        with self._lock:
            r = self._remote(remote)
            if r is None:
                return False, "I don't know that remote."
            if button:
                b = self._button(r, button)
                if b is None:
                    return False, "I don't know that button."
                other = self._button(r, new)
                if other is not None and other is not b:
                    return False, f"{r['name']} already has a {other['name']} button."
                b["name"] = new
            else:
                other = self._remote(new)
                if other is not None and other is not r:
                    return False, f"There's already a remote called {other['name']}."
                r["name"] = new
        self._save()
        return True, "Renamed."

    def delete(self, remote: str, button: Optional[str] = None) -> Tuple[bool, str]:
        with self._lock:
            r = self._remote(remote)
            if r is None:
                return False, "I don't know that remote."
            if button:
                b = self._button(r, button)
                if b is None:
                    return False, "I don't know that button."
                r["buttons"].remove(b)
            if not button or not r["buttons"]:
                self.remotes.remove(r)
        self._save()
        return True, "Deleted."

    # ------------------------------------------------------------ teaching a button
    def learn(self, remote: str, button: str, kind: str = "ir") -> Tuple[bool, str]:
        remote, button = _clean(remote), _clean(button)
        if not NAME_OK.match(remote) or not NAME_OK.match(button):
            return False, f"Name the remote and the button with letters, numbers and spaces (up to {NAME_MAX})."
        if kind not in ("ir", "rf"):
            return False, "Choose infrared or radio (RF)."
        if self.learning and self.learning.get("step") not in ("done", "failed"):
            return False, "Already learning a button: finish or cancel that one first."
        self._cancel.clear()
        self.learning = {"remote": remote, "button": button, "kind": kind, "step": "connecting",
                         "message": "Connecting to the BroadLink…"}
        threading.Thread(target=self._learn, args=(remote, button, kind), name="remote-learn", daemon=True).start()
        return True, "Learning."

    def cancel(self) -> None:
        self._cancel.set()

    def _step(self, step: str, message: str) -> None:
        self.learning = {**self.learning, "step": step, "message": message, "at": self._clock()}
        if step != "done":  # (each step logged: only successes were, and a fan that wouldn't learn left no trace)
            app_logger.log("info", "remote_learn_step", step=step, message=message[:160],
                           remote=self.learning.get("remote"), button=self.learning.get("button"),
                           component="remotes", action="learn")

    def _wait(self, check: Callable[[], Any]) -> Any:
        """check() until it gives something (the BroadLink raises while it has nothing yet), or time's up."""
        end = self._clock() + LEARN_SECONDS
        while self._clock() < end and not self._cancel.is_set():
            self._sleep(1.0)
            try:
                got = check()
            except Exception as e:  # ReadError / StorageError: nothing yet
                if "Offline" in type(e).__name__ or "Timeout" in type(e).__name__:
                    raise
                continue
            if got:
                return got
        return None

    def _learn(self, remote: str, button: str, kind: str) -> None:
        with self._lock:
            known = (self._remote(remote) or {}).get("mhz") if kind == "rf" else None
        self._found_mhz = None
        with self._io:
            try:
                code = self._record(kind, known)
            except Exception as e:
                self._device = None
                code = None
                self._step("failed", f"The BroadLink stopped answering ({e}). Try again.")
        if code is None:
            if self.learning.get("step") != "failed":
                self._step("failed", "Cancelled." if self._cancel.is_set() else
                           "Nothing came through. Try again, closer to the BroadLink (about 15 cm away).")
            return
        encoded = base64.b64encode(code).decode()
        mark = command_of(code)
        with self._lock:
            r = self._remote(remote)
            twin = next((x["name"] for x in (r or {}).get("buttons", []) if x["name"].lower() != button.lower()
                         and (x.get("code") == encoded or (mark and command_of(base64.b64decode(x["code"])) == mark))),
                        None)
        if twin:  # (two buttons of one remote never send the very same thing)
            self._step("failed", f"That came through exactly like {twin}, so it wasn't this button. Try again: "
                                 "press it a few quick times when asked.")
            return
        with self._lock:
            r = self._remote(remote)
            if r is None:
                r = {"name": remote, "buttons": []}
                self.remotes.append(r)
            b = self._button(r, button)
            entry = {"name": b["name"] if b else button, "kind": kind, "code": encoded}
            if b is not None:
                r["buttons"][r["buttons"].index(b)] = entry
            else:
                r["buttons"].append(entry)
            if kind == "rf" and self._found_mhz:  # its other buttons: no holding to find the frequency again
                r["mhz"] = round(float(self._found_mhz), 2)
        self._save()
        app_logger.log("info", "remote_learned", remote=remote, button=button, kind=kind, component="remotes",
                       action="learn")
        self._step("done", f"Got it: {remote} {button}. Tap it to try it.")

    def _record(self, kind: str, known_mhz: Optional[float] = None) -> Optional[bytes]:
        dev = self._connect(force=True)
        if dev is None:
            self._step("failed", self.error)
            return None
        # The BroadLink keeps its last recording and hands it back again until a new one comes in: three fan buttons
        # were all saved as the light's signal (2026-11-10). So: what it holds now doesn't count as the new press.
        try:
            stale = dev.check_data()
        except Exception:
            stale = None

        def fresh():
            code = dev.check_data()
            return code if code and code != stale else None

        if kind == "ir":
            dev.enter_learning()
            self._step("press", "Point the remote at the BroadLink and press the button once.")
            return self._wait(fresh)
        if not hasattr(dev, "sweep_frequency"):
            self._step("failed", "This BroadLink can only learn infrared (radio needs the RM4 Pro).")
            return None
        if known_mhz:  # this remote's frequency is known from an earlier button: one press is enough
            dev.find_rf_packet(known_mhz)
            self._found_mhz = known_mhz
            self._step("press", f"Press the button once, close to the BroadLink ({known_mhz:.1f} MHz, this remote's "
                                "radio). If nothing happens, press it again.")
            return self._wait(fresh)
        dev.sweep_frequency()
        self._step("hold", "Hold the button down on the remote, close to the BroadLink, until this changes.")
        found = self._wait(lambda: (lambda f: f[1] if f[0] else None)(dev.check_frequency()))
        if not found:
            try:
                dev.cancel_sweep_frequency()
            except Exception:
                pass
            if not self._cancel.is_set():
                self._step("failed", "The BroadLink didn't hear a radio signal. The remote may use a radio it "
                                     "can't hear (2.4 GHz or Bluetooth), or try again closer.")
            return None
        # Let go first: recording straight away caught the button still held (the light's, held to find the fan
        # remote's frequency, was saved for Fan Power, 2026-11-10)
        self._step("letgo", f"Found it ({found:.1f} MHz). Let go of the button now…")
        self._sleep(LET_GO_SECONDS)
        if self._cancel.is_set():
            return None
        dev.find_rf_packet(found)
        self._found_mhz = found
        self._step("again", "Now press the button once (a short press). If nothing happens, press it again.")
        return self._wait(fresh)

    # ------------------------------------------------------------ pressing buttons
    def climate(self) -> Optional[Dict[str, float]]:
        """The BroadLink's thermometer (the RM4's HTS2 sensor cable): {"temperature": °C, "humidity": %}, or None."""
        if not self.hub:
            return None  # never found one: no network search every minute
        with self._io:
            dev = self._connect()
            if dev is None or not hasattr(dev, "check_sensors"):
                return None
            try:
                got = dev.check_sensors()
            except Exception:
                self._device = None
                return None
        if not isinstance(got, dict) or got.get("temperature") is None:
            return None
        if not got.get("temperature") and not got.get("humidity"):
            return None  # no sensor cable: it answers zeros
        return got

    def buttons(self) -> Dict[str, List[str]]:
        """Remote name -> its button names."""
        with self._lock:
            return {r["name"]: [b["name"] for b in r.get("buttons", [])] for r in self.remotes}

    def press(self, remote: str, button: str, times: int = 1, source: str = "web",
              gap: float = PRESS_GAP) -> Tuple[bool, str]:
        with self._lock:
            r = self._remote(remote)
            b = self._button(r, button) if r else None
            if b is None:
                return False, "I don't know that button."
            name, code = f"{r['name']} {b['name']}", base64.b64decode(b["code"])
        times = max(1, min(MAX_TIMES, int(times)))  # (the heater's 14-press recount fits)
        with self._io:
            for attempt in (0, 1):  # once more after finding the BroadLink again (a new address)
                dev = self._connect(force=attempt == 1)
                if dev is None:
                    return False, self.error or "The BroadLink isn't answering."
                try:
                    for i in range(times):
                        if i:
                            self._sleep(gap)
                        dev.send_data(code)
                    break
                except Exception as e:
                    self._device = None
                    if attempt:
                        app_logger.log("warning", "remote_failed", button=name, error_message=str(e),
                                       source=source, component="remotes", action="press")
                        return False, "The BroadLink isn't answering."
        app_logger.log("info", "remote_pressed", button=name, times=times, source=source, component="remotes",
                       action="press")
        return True, name + (f" ({times} times)" if times > 1 else "")

    def parse(self, text: str) -> Optional[Tuple[str, str, int]]:
        """'press the soundbar volume up button 3 times' -> ('Soundbar', 'Volume up', 3): the words left over
        must be exactly one remote's name and one of its buttons. None when it isn't (so it goes elsewhere)."""
        low = " ".join(_words(text))
        times = 1
        m = re.search(r"\b(?:(\d+|once|twice|one|two|three|four|five|six|seven|eight|nine|ten) times|"
                      r"(once|twice)|x ?(\d+))$", low)
        if m:
            count = m.group(1) or m.group(2) or m.group(3)
            times = int(count) if count.isdigit() else COUNTS[count]
            low = low[:m.start()].strip()
        tries = [low]
        for pattern, button in ALIASES:
            if pattern.search(low):
                tries.append(pattern.sub(lambda mm: f"{mm.group(1) or ''} {button}", low))
        with self._lock:
            remotes = [(r["name"], [b["name"] for b in r.get("buttons", [])]) for r in self.remotes]
        for attempt in tries:
            words = [w for w in attempt.split() if w not in FILLER]
            for drop_on in (False, True):  # "power on the soundbar": "on" is a button on some remotes
                said = sorted(w for w in words if not (drop_on and w == "on"))
                for rname, buttons in remotes:
                    for bname in buttons:
                        want = sorted(w for w in _words(rname) + _words(bname) if w not in FILLER)
                        if want and said == want:
                            return rname, bname, times
        return self._loose(low, remotes, times)

    @staticmethod
    def _loose(low: str, remotes, times: int) -> Optional[Tuple[str, str, int]]:
        """The short way people say it: "fan high", "fan light on", "set the fan to medium", "turn off the ceiling
        fan" for "Living Room Ceiling Fan" / "Fan High"... Every word said must belong to the remote's name or the
        button's, at least one to the remote's, and every word that sets the button apart from the remote's name
        ("high", "light on") must be said: "turn on the fan" is no button (not the fan's Light On). Only one answer."""
        said = [w for w in low.split() if w not in FILLER and w not in LOOSE_FILLER]
        if not said:
            return None
        hits = []
        for rname, buttons in remotes:
            rwords = set(_words(rname)) - FILLER
            for bname in buttons:
                own = set(_words(bname)) - FILLER - rwords  # what sets this button apart
                if not own or not own <= set(said) or not set(said) <= rwords | own | set(_words(bname)):
                    continue
                if not set(said) & rwords:
                    continue
                hits.append((rname, bname))
        return (hits[0][0], hits[0][1], times) if len(hits) == 1 else None

    def understands(self, text: str) -> bool:
        return self.parse(text) is not None

    def command(self, text: str, source: str) -> Optional[str]:
        parsed = self.parse(text)
        if parsed is None:
            return None
        ok, message = self.press(*parsed, source=source)
        return f"Okay, {message}." if ok else message

    def summary(self) -> str:
        """For Claude's context: the remotes and their buttons."""
        with self._lock:
            lines = [f"{r['name']} remote buttons (say '{r['name']} <button>'): "
                     + ", ".join(b["name"] for b in r.get("buttons", [])) for r in self.remotes]
        return "\n".join(lines)
