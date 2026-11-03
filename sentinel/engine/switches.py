"""
Smart plugs and switches on the home network: the Live page's Switches panel, "Hey Jarvis, patio
lights on", and Telegram's "patio lights off".

Everything stays on the home network: Kasa (TP-Link) plugs and wall switches answer their own local
protocol on port 9999 (JSON, lightly scrambled with an XOR "autokey"), so no Kasa account or cloud is
involved. A plug with several outlets (the EP40 outdoor plug, power strips) shows up as one switch per
outlet. Names are the ones set in the Kasa app; renaming here writes the new name back to the plug, so
the app, the web page and voice all use the same one.

Other makes are other drivers with the same methods (discover, status, set, rename): Samsung TVs
(samsung_tv.py) are one, with apps, volume and mute on top ("put on Netflix", "mute the TV"). Shelly and
Tasmota have simple local HTTP APIs. Newer Kasa/Tapo devices that only speak the encrypted
KLAP protocol don't answer on 9999 and aren't supported yet.

Known devices (MAC -> last address) are kept in switches.json next to the other stores, so a restart
finds them straight away and a plug that moved to a new address is found again by its MAC.
"""

import json
import os
import re
import socket
import struct
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

KNOWN_FILE = "switches.json"
POLL_SECONDS = 10.0  # changes made with the plug's own button or the Kasa app show up within this
DISCOVER_SECONDS = 15 * 60
NAME_MAX = 30
NAME_OK = re.compile(r"^[\w '&().\-]{1,%d}$" % NAME_MAX)


# ------------------------------------------------------------------ Kasa local protocol
def _kasa_scramble(text: str) -> bytes:
    key, out = 171, bytearray()
    for byte in text.encode():
        key ^= byte
        out.append(key)
    return bytes(out)


def _kasa_unscramble(data: bytes) -> str:
    key, out = 171, bytearray()
    for byte in data:
        out.append(key ^ byte)
        key = byte
    return out.decode("utf-8", "replace")


class KasaDriver:
    """Kasa plugs, power strips and wall switches (type IOT.SMARTPLUGSWITCH) over the local network."""

    kind = "kasa"
    PORT = 9999

    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout

    def _send(self, host: str, request: Dict[str, Any]) -> Dict[str, Any]:
        payload = _kasa_scramble(json.dumps(request))
        with socket.create_connection((host, self.PORT), timeout=self.timeout) as sock:
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            head = b""
            while len(head) < 4:
                chunk = sock.recv(4 - len(head))
                if not chunk:
                    raise OSError("no reply")
                head += chunk
            size = struct.unpack(">I", head)[0]
            if size > 1 << 20:
                raise ValueError("reply too large")
            body = b""
            while len(body) < size:
                chunk = sock.recv(min(65536, size - len(body)))
                if not chunk:
                    raise OSError("reply cut short")
                body += chunk
        return json.loads(_kasa_unscramble(body))

    def status(self, host: str) -> Dict[str, Any]:
        """The device's sysinfo (raises OSError/ValueError when it doesn't answer)."""
        info = self._send(host, {"system": {"get_sysinfo": {}}})["system"]["get_sysinfo"]
        if info.get("err_code", 0):
            raise ValueError(f"device error {info.get('err_code')}")
        return info

    @staticmethod
    def _context(child_id: Optional[str]) -> Dict[str, Any]:
        return {"context": {"child_ids": [child_id]}} if child_id else {}

    def set(self, host: str, child_id: Optional[str], on: bool) -> None:
        reply = self._send(host, {**self._context(child_id), "system": {"set_relay_state": {"state": int(on)}}})
        if reply.get("system", {}).get("set_relay_state", {}).get("err_code", 0):
            raise ValueError("the plug refused")

    def rename(self, host: str, child_id: Optional[str], name: str) -> None:
        reply = self._send(host, {**self._context(child_id), "system": {"set_dev_alias": {"alias": name}}})
        if reply.get("system", {}).get("set_dev_alias", {}).get("err_code", 0):
            raise ValueError("the plug refused the name")

    def discover(self, wait: float = 2.0) -> List[Tuple[str, Dict[str, Any]]]:
        """Ask the whole network (UDP broadcast) who's there: [(address, sysinfo)]."""
        found: Dict[str, Dict[str, Any]] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.3)
            message = _kasa_scramble(json.dumps({"system": {"get_sysinfo": {}}}))
            end = time.time() + wait
            for _ in range(3):  # UDP: say it a few times
                sock.sendto(message, ("255.255.255.255", self.PORT))
                time.sleep(0.1)
            while time.time() < end:
                try:
                    data, (host, _port) = sock.recvfrom(65536)
                    info = json.loads(_kasa_unscramble(data))["system"]["get_sysinfo"]
                except (socket.timeout, ValueError, KeyError, TypeError):
                    continue
                if isinstance(info, dict) and ("relay_state" in info or "children" in info):
                    found[host] = info  # bulbs and cameras answer too; only on/off devices are wanted
        except OSError as e:
            app_logger.log("warning", "switch_discover_failed", error_message=str(e), component="switches",
                           action="discover")
        finally:
            sock.close()
        return list(found.items())


# ------------------------------------------------------------------ what the page and voice see
@dataclass
class Switch:
    """One thing that turns on and off: a plug, a wall switch, or one outlet of a multi-outlet plug."""
    id: str  # "kasa-<mac>-1": driver, MAC, outlet number (stays the same when the address changes)
    name: str
    device: str  # the box it's part of, e.g. "Kasa EP40"
    model: str
    on: bool
    online: bool
    since: Optional[float]  # switched on at (None when off or unknown)
    icon: str  # light / fan / tree / plug: a guess from the name, for the page


def guess_icon(name: str) -> str:
    n = name.lower()
    if re.search(r"\b(tv|television)\b", n):
        return "tv"
    if re.search(r"\b(christmas|xmas|tree|holiday|decorations?|inflatable)\b", n):
        return "tree"
    if re.search(r"\b(lights?|lamps?|lanterns?|bulbs?|sconces?|porch|string)\b", n):
        return "light"
    if re.search(r"\b(fan|heater|air|ac|dehumidifier|humidifier)\b", n):
        return "fan"
    if re.search(r"\b(pump|fountain|pond|pool|hot tub|spa)\b", n):
        return "water"
    return "plug"


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _words(text: str) -> List[str]:
    """Lower-case words with plurals and 'the' dropped, so 'the patio light' finds 'Patio lights'."""
    out = []
    for w in re.findall(r"[a-z0-9']+", (text or "").lower()):
        if w in ("the", "my", "a", "an", "please"):
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


_TURN = re.compile(r"^(?:please )?(?:can you |could you )?(?:turn|switch|shut|power|put)\s+(on|off)\s+(.+?)"
                   r"(?:\s+please)?$|^(?:please )?(?:can you |could you )?(?:turn|switch|shut|power|put)\s+(.+?)"
                   r"\s+(on|off)(?:\s+please)?$|^(.+?)\s+(on|off)(?:\s+please)?$", re.IGNORECASE)
_ASK = re.compile(r"^(?:is|are)\s+(.+?)\s+on or off$|^(?:is|are)\s+(.+?)\s+(?:on|off|still on|turned on|switched on)$",
                  re.IGNORECASE)
COMMAND = re.compile(r"^turn (?:on|off) [\w' -]{1,40}$|^turn [\w' -]{1,40} (?:on|off)$", re.IGNORECASE)  # Claude's button
# TVs: apps, volume, mute, "what's on". "Play ..." is left to Spotify.
_TV = r"(?:the )?(?:[\w']+ ){0,2}?(?:tv|television)"  # "the TV", "the Sentinel TV", "the fire TV"
_TV_APP = re.compile(r"^(?:please )?(?:can you |could you )?(?:open|launch|start|put on|go to|switch to|turn on)\s+(.+?)"
                     r"(?:\s+on " + _TV + r")?(?:\s+please)?$", re.IGNORECASE)
# "turn on the TV to Netflix", "turn the TV on to Hulu", "put the TV on YouTube TV", "switch the TV to Disney Plus"
_TV_TO = re.compile(r"^(?:please )?(?:can you |could you )?(?:turn|switch|put|change|set)\s+(?:on\s+)?" + _TV +
                    r"(?:\s+on)?\s+(?:to|on)\s+(.+?)(?:\s+please)?$", re.IGNORECASE)
_TV_VOLUME = re.compile(r"^(?:turn )?" + _TV + r"(?: volume)? (up|down)$|^turn (up|down) " + _TV + r"(?: volume)?$"
                        r"|^" + _TV + r" (louder|quieter)$", re.IGNORECASE)
_TV_MUTE = re.compile(r"^(mute|unmute) " + _TV + r"$", re.IGNORECASE)
_TV_LIVE = re.compile(r"^(?:put|show|bring up|switch)\s+(?:the )?(?:cameras?|camera view|live view|security cameras|sentinel)"
                      r"\s+(?:on|to)\s+" + _TV + r"$|^(reload|refresh|restart)\s+(?:the )?(?:cameras?|camera view|live view)"
                      r"(?:\s+on\s+" + _TV + r")?$", re.IGNORECASE)
_TV_WHAT = re.compile(r"^what(?:'s| is) (?:on|playing on) " + _TV + r"$", re.IGNORECASE)
VOLUME_STEPS = 3
# The web remote's buttons -> the TV's key names (nothing else is sent).
TV_KEYS = {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "ok": "KEY_ENTER",
           "back": "KEY_RETURN", "home": "KEY_HOME", "menu": "KEY_MENU", "source": "KEY_SOURCE", "guide": "KEY_GUIDE",
           "info": "KEY_INFO", "exit": "KEY_EXIT", "vol_up": "KEY_VOLUP", "vol_down": "KEY_VOLDOWN", "mute": "KEY_MUTE",
           "ch_up": "KEY_CHUP", "ch_down": "KEY_CHDOWN", "ch_list": "KEY_CH_LIST", "play": "KEY_PLAY",
           "pause": "KEY_PAUSE", "rewind": "KEY_REWIND", "forward": "KEY_FF",
           **{str(n): f"KEY_{n}" for n in range(10)}}
TV_SYSTEM_APPS = {"Gallery", "Internet", "e-Manual", "PrivacyChoices", "Universal Guide"}


def _app_words(name: str) -> List[str]:
    """'Disney+' and 'disney plus' alike; store suffixes ('- Music and Podcasts') dropped."""
    name = re.split(r"\s[-:]\s|:", name.replace("+", " plus").replace("＆", "&"))[0]
    return _words(name)


class SmartSwitches:
    def __init__(self, root: str, drivers: Optional[List[Any]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = os.path.join(os.path.abspath(root), KNOWN_FILE)
        if drivers is None:
            from sentinel.engine.samsung_tv import SamsungTVDriver

            drivers = [KasaDriver(), SamsungTVDriver(root)]
            try:
                from sentinel.engine.fire_tv import FireTVDriver

                drivers.append(FireTVDriver(root))
            except ImportError:  # adb-shell not installed: no Fire TVs
                pass
        self._drivers = {d.kind: d for d in drivers}
        self._clock = clock
        self._lock = threading.Lock()
        self._known: Dict[str, Dict[str, Any]] = {}  # device key ("kasa-<mac>") -> {kind, host, model}
        self._state: Dict[str, Dict[str, Any]] = {}  # device key -> last sysinfo
        self._failures: Dict[str, int] = {}
        self._last_discover = 0.0
        self.scanning = False
        self._scan_wanted = False
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        try:
            with open(self.path) as f:
                data = json.load(f)
            self._known = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("kind") in self._drivers}
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="smart-switches", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def request_scan(self) -> None:
        """"Find new devices" on the page: look on the next round (never blocks the web request)."""
        self._scan_wanted = self.scanning = True
        self._wake.set()

    def _run(self) -> None:
        self.discover()
        while True:
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()
            if self._stopped.is_set():
                return
            since = self._clock() - self._last_discover
            lost = any(n >= 3 for n in self._failures.values())
            if self._scan_wanted or since > DISCOVER_SECONDS or (lost and since > 60):
                self._scan_wanted = False
                self.discover()
            else:
                self.poll()

    # ------------------------------------------------------------ finding and reading devices
    @staticmethod
    def _key(kind: str, info: Dict[str, Any]) -> Optional[str]:
        mac = re.sub(r"[^0-9a-f]", "", str(info.get("mac") or info.get("mic_mac") or "").lower())
        return f"{kind}-{mac}" if len(mac) == 12 else None

    def discover(self) -> int:
        """Look for devices (new ones, and known ones at a new address); returns how many answered."""
        self.scanning = True
        try:
            count = 0
            for kind, driver in self._drivers.items():
                for host, info in driver.discover():
                    key = self._key(kind, info)
                    if not key:
                        continue
                    count += 1
                    with self._lock:
                        new = key not in self._known
                        moved = not new and self._known[key].get("host") != host
                        self._known[key] = {"kind": kind, "host": host, "model": str(info.get("model") or "")}
                        self._state[key] = info
                        self._failures.pop(key, None)
                    if new or moved:
                        app_logger.log("info", "switch_found" if new else "switch_moved", device=key, host=host,
                                       model=info.get("model"), component="switches", action="discover")
            self._last_discover = self._clock()
            self._save()
            self.poll(skip_fresh=True)  # known ones that didn't answer the broadcast
            return count
        finally:
            self.scanning = False

    def poll(self, skip_fresh: bool = False) -> None:
        with self._lock:
            known = dict(self._known)
        for key, dev in known.items():
            if skip_fresh and key in self._state and self._failures.get(key, 0) == 0:
                continue
            try:
                info = self._drivers[dev["kind"]].status(dev["host"])
            except (OSError, ValueError, KeyError, TypeError) as e:
                with self._lock:
                    self._failures[key] = self._failures.get(key, 0) + 1
                    if self._failures[key] == 3:
                        app_logger.log("warning", "switch_offline", device=key, error_message=str(e),
                                       component="switches", action="poll")
                continue
            with self._lock:
                before = self._state.get(key)
                self._state[key] = info
                if self._failures.pop(key, 0) >= 3:
                    app_logger.log("info", "switch_back_online", device=key, component="switches", action="poll")
            if before is not None:
                for sw_before, sw_after in zip(self._outlets(key, before), self._outlets(key, info)):
                    if sw_before.on != sw_after.on:  # the plug's own button, the Kasa app or a schedule
                        app_logger.log("info", "switch_changed", switch=sw_after.name, on=sw_after.on,
                                       source="outside", component="switches", action="poll")

    def _outlets(self, key: str, info: Dict[str, Any], online: bool = True) -> List[Switch]:
        now = self._clock()
        model = str(info.get("model") or "").split("(")[0]
        alias = _clean_name(str(info.get("alias") or ""))
        device = f"Kasa {model}".strip() if not alias or alias.upper().startswith("TP-LINK_") else alias
        children = info.get("children")
        if isinstance(children, list) and children:
            out = []
            for i, child in enumerate(children):
                on = bool(child.get("state"))
                name = _clean_name(str(child.get("alias") or "")) or f"{device} outlet {i + 1}"
                on_time = child.get("on_time") or 0
                out.append(Switch(f"{key}-{i + 1}", name, device, model, on, online,
                                  now - on_time if on and on_time else None, guess_icon(name)))
            return out
        on = bool(info.get("relay_state"))
        on_time = info.get("on_time") or 0
        name = alias if alias and not alias.upper().startswith("TP-LINK_") else device
        return [Switch(key, name, device, model, on, online, now - on_time if on and on_time else None,
                       guess_icon(name))]

    def switches(self) -> List[Switch]:
        with self._lock:
            items = [(k, self._state.get(k), self._failures.get(k, 0) < 3) for k in self._known]
        out: List[Switch] = []
        for key, info, online in items:
            if info is not None:
                out.extend(self._outlets(key, info, online))
        return out

    def view(self) -> Dict[str, Any]:
        """For the web page."""
        return {"switches": [asdict(s) for s in self.switches()], "scanning": self.scanning}

    def _locate(self, switch_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
        """switch id -> (device key, known device, Kasa child id or None)."""
        m = re.fullmatch(r"([a-z]+-[0-9a-f]{12})(?:-(\d{1,2}))?", switch_id or "")
        if not m:
            return None, None, None
        key = m.group(1)
        with self._lock:
            dev, info = self._known.get(key), self._state.get(key)
        if dev is None or info is None:
            return None, None, None
        children = info.get("children") or []
        if m.group(2) is None:
            return (key, dev, None) if not children else (None, None, None)
        index = int(m.group(2)) - 1
        if not 0 <= index < len(children):
            return None, None, None
        return key, dev, str(children[index].get("id") or "")

    # ------------------------------------------------------------ changing them
    def set(self, switch_id: str, on: bool, source: str = "web") -> Tuple[bool, str]:
        key, dev, child = self._locate(switch_id)
        if key is None:
            return False, "I don't know that switch."
        name = next((s.name for s in self.switches() if s.id == switch_id), "it")
        try:
            self._drivers[dev["kind"]].set(dev["host"], child, on)
            info = self._drivers[dev["kind"]].status(dev["host"])
        except (OSError, ValueError) as e:
            app_logger.log("warning", "switch_failed", switch=name, on=on, error_message=str(e), source=source,
                           component="switches", action="set")
            self._wake.set()
            return False, f"{name} isn't answering."
        with self._lock:
            self._state[key] = info
            self._failures.pop(key, None)
        app_logger.log("info", "switch_changed", switch=name, on=on, source=source, component="switches",
                       action="set")
        return True, f"{name} {'on' if on else 'off'}."

    def rename(self, switch_id: str, name: str) -> Tuple[bool, str]:
        name = _clean_name(name)
        if not NAME_OK.match(name):
            return False, f"Use letters, numbers and spaces (up to {NAME_MAX})."
        if any(s.name.lower() == name.lower() and s.id != switch_id for s in self.switches()):
            return False, "Another switch already has that name."
        key, dev, child = self._locate(switch_id)
        if key is None:
            return False, "I don't know that switch."
        driver = self._drivers[dev["kind"]]
        try:
            driver.rename(dev["host"], child, name)
            info = driver.status(dev["host"])
        except (OSError, ValueError) as e:
            return False, f"The plug didn't take the name ({e})."
        with self._lock:
            self._state[key] = info
        app_logger.log("info", "switch_renamed", switch=name, component="switches", action="rename")
        return True, name

    def _save(self) -> None:
        with self._lock:
            data = dict(self._known)
        try:
            tmp = self.path + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ words (voice and Telegram)
    def find(self, words: str) -> List[Switch]:
        """Switches a spoken/typed name means: an exact name first, else every switch whose name has all the words."""
        wanted = _words(words)
        if not wanted:
            return []
        items = self.switches()
        exact = [s for s in items if _words(s.name) == wanted]
        if exact:
            return exact
        if wanted in (["light"], ["all", "light"], ["outside", "light"], ["outdoor", "light"]):
            return [s for s in items if s.icon == "light"]
        return [s for s in items if set(wanted) <= set(_words(s.name))]

    def parse(self, text: str) -> Optional[Tuple[Optional[bool], List[Switch]]]:
        """'turn on the patio lights' -> (True, [patio lights]); 'are the patio lights on' -> (None, [...]).
        None when it isn't about a known switch (so 'turn on alerts' etc. go elsewhere)."""
        t = re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", text or "")).strip()
        m = _ASK.match(t)
        if m:
            found = self.find(m.group(1) or m.group(2))
            return (None, found) if found else None
        m = _TURN.match(t)
        if not m:
            return None
        if m.group(1):
            on, words = m.group(1), m.group(2)
        elif m.group(3):
            words, on = m.group(3), m.group(4)
        else:
            words, on = m.group(5), m.group(6)
        found = self.find(words)
        return (on.lower() == "on", found) if found else None

    # ------------------------------------------------------------ TVs
    def _tvs(self) -> List[Tuple[Switch, Any, str]]:
        """(switch, driver, address) for each TV."""
        out = []
        for sw in self.switches():
            key, dev, _ = self._locate(sw.id)
            if key is not None and hasattr(self._drivers[dev["kind"]], "launch"):
                out.append((sw, self._drivers[dev["kind"]], dev["host"]))
        return out

    @staticmethod
    def _pick_tv(text: str, tvs: List[Tuple[Switch, Any, str]]) -> Tuple[Switch, Any, str]:
        """The TV a command names ("the Sentinel TV", "the fire TV"), else the plain "TV" (a Samsung before a
        Fire TV: 'turn the TV down' means the television)."""
        said = set(_words(text))
        named = [tv for tv in tvs if set(_words(tv[0].name)) - {"tv"} and set(_words(tv[0].name)) <= said]
        if named:
            return max(named, key=lambda tv: len(_words(tv[0].name)))
        if "fire" in said:
            fire = [tv for tv in tvs if tv[1].kind == "firetv"]
            if fire:
                return fire[0]
        return sorted(tvs, key=lambda tv: tv[1].kind == "firetv")[0]

    def _find_app(self, words: str, driver, host: str) -> Optional[Tuple[str, str]]:
        wanted = _app_words(words)
        if not wanted:
            return None
        apps = driver.apps(host)
        for want_all in (True, False):  # every word of the app's name said, else a name starting with what was said
            for app_id, name in apps.items():
                have = _app_words(name)
                if (want_all and have == wanted) or (not want_all and have[:len(wanted)] == wanted):
                    return app_id, name
        return None

    def tv_parse(self, text: str) -> Optional[Tuple[str, Any]]:
        """('app', (tv, app id, app name)) / ('volume', (tv, 'up')) / ('mute', (tv, 'mute')) / ('what', tv), or None."""
        t = re.sub(r"\s+", " ", re.sub(r"[^\w\s'+-]", " ", text or "")).strip()
        tvs = self._tvs()
        if not tvs:
            return None
        tv = self._pick_tv(t, tvs)
        m = _TV_LIVE.match(t)
        if m:
            live = [x for x in tvs if hasattr(x[1], "show_live") and x[1].live_app(x[2])]
            if live:
                return "live", ((tv if tv in live else live[0]), bool(m.group(1)))
        m = _TV_VOLUME.match(t)
        if m:
            way = next(g for g in m.groups() if g).lower()
            return "volume", (tv, "up" if way in ("up", "louder") else "down")
        m = _TV_MUTE.match(t)
        if m:
            return "mute", (tv, m.group(1).lower())
        if _TV_WHAT.match(t):
            return "what", tv
        m = _TV_TO.match(t)
        if m:
            app = self._find_app(m.group(1), tv[1], tv[2])
            return ("app", (tv, app[0], app[1])) if app else ("no_app", (tv, m.group(1)))
        m = _TV_APP.match(t)
        if m and not self.find(m.group(1)):  # "turn on the patio lights" is a switch, not an app
            app = self._find_app(m.group(1), tv[1], tv[2])
            if app:
                return "app", (tv, app[0], app[1])
        return None

    def tv_command(self, text: str, source: str) -> Optional[str]:
        parsed = self.tv_parse(text)
        if parsed is None:
            return None
        what, detail = parsed
        tv = detail[0] if what != "what" else detail
        sw, driver, host = tv
        if what == "live":
            try:
                driver.show_live(host, reload=detail[1])
            except (OSError, ValueError) as e:
                return f"The {sw.name} didn't do it ({e})."
            app_logger.log("info", "tv_live_view", tv=sw.name, reload=detail[1], source=source, component="switches", action="tv")
            return f"Okay, {'reloading' if detail[1] else 'showing'} the cameras on the {sw.name}."
        if what == "no_app":
            return f"I couldn't find {detail[1]} on the {sw.name}. I can open its apps, like {', '.join(self.tv_apps()[:4])}."
        try:
            if what == "what":
                if not sw.on:
                    return f"The {sw.name} is off."
                app = driver.showing(host)
                return f"{app} is on the {sw.name}." if app else f"The {sw.name} is on (no app open: the home screen or an input)."
            if what == "app":
                if not sw.on:
                    ok, message = self.set(sw.id, True, source)
                    if not ok:
                        return message
                    time.sleep(3)  # let it finish starting up (it opens its last app first)
                driver.launch(host, detail[1])
                app_logger.log("info", "tv_app", app=detail[2], source=source, component="switches", action="tv")
                return f"Okay, {detail[2]} on the {sw.name}."
            if not sw.on:
                return f"The {sw.name} is off."
            if what == "volume":
                driver.press(host, *(["KEY_VOLUP" if detail[1] == "up" else "KEY_VOLDOWN"] * VOLUME_STEPS))
                return f"Okay, {sw.name} {'louder' if detail[1] == 'up' else 'quieter'}."
            driver.press(host, "KEY_MUTE")  # the TV has one button for both
            return f"Okay, {sw.name} {'muted' if detail[1] == 'mute' else 'unmuted'}."
        except (OSError, ValueError) as e:
            app_logger.log("warning", "tv_failed", what=what, error_message=str(e), source=source,
                           component="switches", action="tv")
            return f"The {sw.name} didn't do it ({e})."

    def tv_apps(self) -> List[str]:
        """The TVs' apps by their short names (for the speech recogniser's hint and replies)."""
        out: List[str] = []
        for _sw, driver, host in self._tvs():
            for name in driver.apps(host).values():
                short = re.split(r"\s[-:]\s|:", name)[0].strip()
                if short and short not in TV_SYSTEM_APPS and short != "Sentinel live view" and short not in out:
                    out.append(short)
        return out

    # ------------------------------------------------------------ the web remote
    def _tv_for(self, switch_id: str) -> Optional[Tuple[Switch, Any, str]]:
        return next((tv for tv in self._tvs() if tv[0].id == switch_id), None)

    def tv_view(self, switch_id: str) -> Optional[Dict[str, Any]]:
        tv = self._tv_for(switch_id)
        if tv is None:
            return None
        sw, driver, host = tv
        apps = [{"id": app_id, "name": re.split(r"\s[-:]\s|:", name)[0].strip()}
                for app_id, name in driver.apps(host).items()]
        supports = getattr(driver, "supports", lambda key: True)
        live = hasattr(driver, "show_live") and driver.live_app(host) is not None
        return {"id": sw.id, "name": sw.name, "on": sw.on, "online": sw.online, "kind": driver.kind,
                "keys": [k for k, name in TV_KEYS.items() if supports(name)], "live": live,
                "apps": [a for a in apps if a["name"] not in TV_SYSTEM_APPS and not (live and a["name"] == "Sentinel live view")]}

    def tv_key(self, switch_id: str, key: str) -> Tuple[bool, str]:
        tv = self._tv_for(switch_id)
        if tv is None or key not in TV_KEYS or not getattr(tv[1], "supports", lambda k: True)(TV_KEYS[key]):
            return False, "I don't know that button."
        try:
            tv[1].press(tv[2], TV_KEYS[key])
        except (OSError, ValueError) as e:
            return False, f"The {tv[0].name} didn't answer ({e})." if tv[0].on else f"The {tv[0].name} is off."
        return True, key

    def tv_open(self, switch_id: str, app_id: str) -> Tuple[bool, str]:
        tv = self._tv_for(switch_id)
        if tv is None:
            return False, "I don't know that TV."
        sw, driver, host = tv
        name = driver.apps(host).get(app_id)
        if name is None:
            return False, "That app isn't on the TV."
        if not sw.on:
            ok, message = self.set(sw.id, True, "web")
            if not ok:
                return False, message
            time.sleep(3)  # let it finish starting up (it opens its last app first)
        try:
            driver.launch(host, app_id)
        except (OSError, ValueError) as e:
            return False, f"The {sw.name} didn't open it ({e})."
        app_logger.log("info", "tv_app", app=name, source="web", component="switches", action="tv")
        return True, name

    def tv_type(self, switch_id: str, text: str) -> Tuple[bool, str]:
        tv = self._tv_for(switch_id)
        text = (text or "").strip()[:100]
        if tv is None or not text:
            return False, "Nothing to type."
        try:
            tv[1].type_text(tv[2], text)
        except (OSError, ValueError) as e:
            return False, f"The {tv[0].name} didn't take it ({e})."
        return True, "typed"

    def tv_live(self, switch_id: str, reload: bool) -> Tuple[bool, str]:
        tv = self._tv_for(switch_id)
        if tv is None or not hasattr(tv[1], "show_live"):
            return False, "That TV has no Sentinel live view."
        try:
            tv[1].show_live(tv[2], reload=reload)
        except (OSError, ValueError) as e:
            return False, f"The {tv[0].name} didn't do it ({e})."
        return True, "reloaded" if reload else "showing"

    def tv_icon(self, switch_id: str, app_id: str) -> Optional[bytes]:
        tv = self._tv_for(switch_id)
        return tv[1].icon(tv[2], app_id) if tv is not None else None

    def understands(self, text: str) -> bool:
        """A switch or TV command (for routing Telegram messages and Claude's suggestions)."""
        return self.parse(text) is not None or self.tv_parse(text) is not None

    def command(self, text: str, source: str) -> Optional[str]:
        """Do a parsed switch (or TV) command and say what happened; None when the words aren't one."""
        parsed = self.parse(text)
        if parsed is None:
            return self.tv_command(text, source)
        on, found = parsed
        if on is None:
            return " ".join(f"{s.name} {'is' if not s.name.lower().endswith('s') else 'are'} "
                            f"{'not answering' if not s.online else ('on' if s.on else 'off')}." for s in found)
        results = [self.set(s.id, on, source) for s in found]
        if all(ok for ok, _ in results):
            return "Okay, " + " and ".join(s.name for s in found) + (" on." if on else " off.")
        return " ".join(msg for _, msg in results)

    def summary(self) -> str:
        """For Claude's context: one line per switch (a TV with its apps)."""
        lines = [f"{s.name}: {'not answering' if not s.online else ('on' if s.on else 'off')}" for s in self.switches()]
        for sw, driver, host in self._tvs():
            apps = sorted(set(re.split(r"\s[-:]\s|:", n)[0] for n in driver.apps(host).values()))
            if apps:
                lines.append(f"{sw.name} apps (say 'open <app>'): {', '.join(apps)}")
        return "\n".join(lines)
