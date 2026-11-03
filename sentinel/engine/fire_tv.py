"""
Amazon Fire TV sticks/boxes on the home network over ADB (Android's debug bridge), as a "switch" beside the
smart plugs, with the same remote, apps and voice commands as a Samsung TV, plus "show the cameras" for a
Fire TV that runs Sentinel's live view in a kiosk browser.

Only Fire TVs where someone turned on Settings > My Fire TV > Developer Options > ADB debugging answer on
port 5555; the first connection shows "Allow USB debugging?" on the TV ("Always allow" keeps it). Sentinel's
key lives in recorded_videos/.adb/ (owner only). Unlike a 2018 Samsung TV, a Fire TV says whether its screen
is on (``dumpsys power``), and it passes power/volume on to the TV over HDMI-CEC when the TV supports it.
Pure-Python ADB (adb-shell, Apache-2.0); without it installed, Fire TVs are simply not offered.
"""

import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

STATE_FILE = "fire_tv.json"
KEY_DIR = ".adb"
PORT = 5555
SCAN_EVERY = 6 * 3600.0  # look for newly enabled Fire TVs across the home network at most this often
APPS_EVERY = 3600.0
LIVE_VIEW_APPS = ("de.ozerov.fully", "com.fullykiosk.emm")  # Fully Kiosk Browser: Sentinel's live view
# The remote's keys (named as Samsung's, see switches.TV_KEYS) -> Android key codes.
KEYS = {"KEY_UP": "KEYCODE_DPAD_UP", "KEY_DOWN": "KEYCODE_DPAD_DOWN", "KEY_LEFT": "KEYCODE_DPAD_LEFT",
        "KEY_RIGHT": "KEYCODE_DPAD_RIGHT", "KEY_ENTER": "KEYCODE_DPAD_CENTER", "KEY_RETURN": "KEYCODE_BACK",
        "KEY_HOME": "KEYCODE_HOME", "KEY_MENU": "KEYCODE_MENU", "KEY_VOLUP": "KEYCODE_VOLUME_UP",
        "KEY_VOLDOWN": "KEYCODE_VOLUME_DOWN", "KEY_MUTE": "KEYCODE_VOLUME_MUTE", "KEY_PLAY": "KEYCODE_MEDIA_PLAY",
        "KEY_PAUSE": "KEYCODE_MEDIA_PAUSE", "KEY_REWIND": "KEYCODE_MEDIA_REWIND", "KEY_FF": "KEYCODE_MEDIA_FAST_FORWARD",
        "KEY_CHUP": "KEYCODE_CHANNEL_UP", "KEY_CHDOWN": "KEYCODE_CHANNEL_DOWN", "KEY_GUIDE": "KEYCODE_GUIDE",
        "KEY_INFO": "KEYCODE_INFO", "KEY_POWER": "KEYCODE_POWER",
        **{f"KEY_{n}": f"KEYCODE_{n}" for n in range(10)}}
# The same keys as Linux input codes, best first, for writing straight into one of the Fire TV's input devices
# (`sendevent`, a few ms; `input keyevent` starts a Java program per press, ~0.5-1 s on a Fire TV).
LINUX_KEYS = {"KEYCODE_DPAD_UP": [103], "KEYCODE_DPAD_DOWN": [108], "KEYCODE_DPAD_LEFT": [105],
              "KEYCODE_DPAD_RIGHT": [106], "KEYCODE_BACK": [158],
              "KEYCODE_HOME": [172], "KEYCODE_MENU": [139], "KEYCODE_VOLUME_UP": [115], "KEYCODE_VOLUME_DOWN": [114],
              "KEYCODE_VOLUME_MUTE": [113], "KEYCODE_MEDIA_PLAY": [200, 207], "KEYCODE_MEDIA_PAUSE": [201],
              "KEYCODE_MEDIA_REWIND": [168], "KEYCODE_MEDIA_FAST_FORWARD": [208], "KEYCODE_CHANNEL_UP": [402],
              "KEYCODE_CHANNEL_DOWN": [403], "KEYCODE_GUIDE": [365], "KEYCODE_INFO": [358],
              **{f"KEYCODE_{n}": [11 if n == 0 else n + 1] for n in range(10)}}
# Not DPAD_CENTER: written as KEY_SELECT (353) the Fire TV took it but didn't select anything (2026-11-03;
# each input device's key layout differs), so the select button always goes through `input keyevent`.
SLOW_PRESS_MS = 400  # presses slower than this are logged (to see where remote lag comes from)
# Package -> the name people say (Fire OS can't report app labels over ADB without extra tools).
APP_NAMES = {
    "com.netflix.ninja": "Netflix", "com.amazon.firetv.youtube": "YouTube", "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvunplugged": "YouTube TV", "com.amazon.avod": "Prime Video",
    "com.amazon.firebat": "Prime Video", "com.hulu.plus": "Hulu", "com.disney.disneyplus": "Disney+",
    "com.wbd.stream": "Max", "com.hbo.hbonow": "HBO Max", "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+", "com.apple.atve.amazon.appletv": "Apple TV", "tv.pluto.android": "Pluto TV",
    "com.tubitv": "Tubi", "com.plexapp.android": "Plex", "com.spotify.tv.android": "Spotify",
    "com.fubo.firetv.screen": "Fubo", "com.sling": "Sling TV", "com.philo.philo": "Philo", "org.xbmc.kodi": "Kodi",
    "com.discovery.discoveryplus.firetv": "discovery+", "com.starz.amazon": "STARZ", "com.amazon.cloud9": "Silk Browser",
    "com.frndlytv.android": "Frndly TV", "com.pbs.video": "PBS", "com.nbcuni.nbc": "NBC", "com.amazon.rialto": "Freevee",
    "de.ozerov.fully": "Sentinel live view", "com.fullykiosk.emm": "Sentinel live view",
    "com.esaba.downloader": "Downloader", "com.amazon.tv.launcher": "Home",
}


def _local_networks() -> List[ipaddress.IPv4Network]:
    """The /24 of each address this computer has on a private network (where the Fire TVs are)."""
    nets = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent: this just picks the home-network address
        addr = s.getsockname()[0]
        s.close()
        if ipaddress.ip_address(addr).is_private:
            nets.append(ipaddress.ip_network(f"{addr}/24", strict=False))
    except OSError:
        pass
    return nets


class FireTVDriver:
    """The SmartSwitches driver interface (discover, status, set, rename) plus the TV remote's methods."""

    kind = "firetv"

    def __init__(self, root: str, timeout: float = 5.0) -> None:
        from adb_shell.adb_device import AdbDeviceTcp  # noqa: F401  (ImportError: no Fire TV support)

        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, STATE_FILE)
        self.key_path = os.path.join(self.root, KEY_DIR, "adbkey")
        self.timeout = timeout
        self._lock = threading.RLock()
        self._devices: Dict[str, Any] = {}  # host -> connected AdbDeviceTcp
        self._last_scan = 0.0
        self._input: Dict[str, Optional[Tuple[str, set]]] = {}  # host -> (input device, its key codes) or None
        self._tvs: Dict[str, Dict[str, Any]] = {}  # mac -> {host, name, model, apps, apps_at}
        try:
            with open(self.path) as f:
                self._tvs = {k: v for k, v in json.load(f).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ talking to it
    def _signer(self):
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner

        if not os.path.exists(self.key_path):
            os.makedirs(os.path.dirname(self.key_path), mode=0o700, exist_ok=True)
            keygen(self.key_path)
            os.chmod(self.key_path, 0o600)
        return PythonRSASigner.FromRSAKeyPath(self.key_path)

    def _shell(self, host: str, command: str, timeout: Optional[float] = None) -> str:
        """Run a shell command on the Fire TV (one kept-open connection per Fire TV, reconnected when lost).
        Raises OSError when it doesn't answer, ValueError when it hasn't allowed Sentinel."""
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.exceptions import (AdbConnectionError, AdbTimeoutError, DeviceAuthError, InvalidChecksumError,
                                          InvalidCommandError, InvalidResponseError, TcpTimeoutException)

        with self._lock:
            for attempt in (0, 1):
                dev = self._devices.get(host)
                try:
                    if dev is None or not dev.available:
                        dev = AdbDeviceTcp(host, PORT, default_transport_timeout_s=self.timeout)
                        dev.connect(rsa_keys=[self._signer()], auth_timeout_s=self.timeout)
                        self._devices[host] = dev
                    return dev.shell(command, timeout_s=timeout or self.timeout) or ""
                except DeviceAuthError:
                    self._close(host)
                    raise ValueError("the Fire TV hasn't allowed Sentinel: press OK on its 'Allow USB debugging?' "
                                     "pop-up (tick 'Always allow')")
                except (OSError, AdbConnectionError, AdbTimeoutError, TcpTimeoutException, InvalidChecksumError,
                        InvalidCommandError, InvalidResponseError) as e:
                    self._close(host)
                    if attempt:
                        raise OSError(str(e) or type(e).__name__)
        return ""

    def _close(self, host: str) -> None:
        self._input.pop(host, None)
        dev = self._devices.pop(host, None)
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    def _remember(self, mac: str, **fields) -> None:
        with self._lock:
            self._tvs.setdefault(mac, {}).update(fields)
            data = json.loads(json.dumps(self._tvs))
        try:
            tmp = self.path + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _tv(self, host: str) -> Tuple[Optional[str], Dict[str, Any]]:
        with self._lock:
            for mac, tv in self._tvs.items():
                if tv.get("host") == host:
                    return mac, tv
        return None, {}

    def _learn(self, host: str) -> Optional[str]:
        out = self._shell(host, "getprop ro.product.manufacturer; getprop ro.product.model; "
                                "settings get global device_name; getprop ro.serialno")
        lines = [ln.strip() for ln in out.splitlines()]
        if len(lines) < 4 or lines[0].lower() != "amazon" or not lines[3]:
            return None
        # Its id: from the serial number, which (unlike the network address) stays when it moves from Wi-Fi to a
        # cable; 12 hex digits like the other devices' MACs. (Fire OS doesn't let ADB read its MAC files.)
        mac = hashlib.sha256(lines[3].encode()).hexdigest()[:12]
        fields = {"host": host, "model": f"Fire TV {lines[1]}".strip()}
        if not self._tvs.get(mac, {}).get("name"):
            fields["name"] = lines[2] if lines[2] and lines[2] != "null" else "Fire TV"
        self._remember(mac, **fields)
        return mac

    # ------------------------------------------------------------ the switch interface
    def _screen_on(self, host: str) -> bool:
        return "mWakefulness=Awake" in self._shell(host, "dumpsys power | grep mWakefulness=")

    def _info(self, mac: str, on: bool) -> Dict[str, Any]:
        tv = self._tvs[mac]
        return {"mac": mac, "model": tv.get("model") or "Fire TV", "alias": tv.get("name") or "Fire TV",
                "relay_state": int(on), "on_time": 0}

    def discover(self, wait: float = 2.0) -> List[Tuple[str, Dict[str, Any]]]:
        """Known Fire TVs at their address; every few hours (or with none known) the whole home network too."""
        with self._lock:
            hosts = {tv["host"] for tv in self._tvs.values() if tv.get("host")}
        if not hosts or time.time() - self._last_scan > SCAN_EVERY:
            self._last_scan = time.time()
            hosts |= set(self._scan())
        found = []
        for host in sorted(hosts):
            try:
                mac = self._learn(host)
                if mac:
                    found.append((host, self._info(mac, self._screen_on(host))))
            except (OSError, ValueError) as e:
                app_logger.log("info", "firetv_not_answering", host=host, error_message=str(e)[:200],
                               component="switches", action="discover")
        return found

    @staticmethod
    def _scan() -> List[str]:
        def probe(ip: str) -> Optional[str]:
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect((ip, PORT))
                return ip
            except OSError:
                return None
            finally:
                s.close()

        ips = [str(ip) for net in _local_networks() for ip in net.hosts()]
        with ThreadPoolExecutor(64) as pool:
            return [ip for ip in pool.map(probe, ips) if ip]

    def status(self, host: str) -> Dict[str, Any]:
        mac, _ = self._tv(host)
        if mac is None:
            raise OSError("unknown Fire TV")
        return self._info(mac, self._screen_on(host))

    def set(self, host: str, child_id: Optional[str], on: bool) -> None:
        self._shell(host, f"input keyevent {'KEYCODE_WAKEUP' if on else 'KEYCODE_SLEEP'}")

    def rename(self, host: str, child_id: Optional[str], name: str) -> None:
        mac, _ = self._tv(host)
        if mac is None:
            raise OSError("unknown Fire TV")
        self._remember(mac, name=name)  # Sentinel's name for it (the Fire TV keeps its own)

    # ------------------------------------------------------------ the remote
    def supports(self, key: str) -> bool:
        return key in KEYS

    def _input_device(self, host: str) -> Optional[Tuple[str, set]]:
        """The Fire TV's input device with the most remote keys (often its HDMI-CEC one), if Sentinel may write to
        it; found once per connection. None: use `input keyevent`."""
        if host in self._input:
            return self._input[host]
        found = None
        try:
            devices: Dict[str, set] = {}
            current, section = None, None
            for line in self._shell(host, "getevent -p 2>/dev/null").splitlines():
                m = re.match(r"add device \d+: (/dev/input/event\d+)", line)
                if m:
                    current = m.group(1)
                    devices[current] = set()
                    section = None
                    continue
                m = re.match(r"\s+(\w+) \(([0-9a-f]{4})\):(.*)", line)
                if m:
                    section = m.group(2)
                    line = m.group(3)
                elif not line.startswith("        ") or current is None:
                    section = None if not line.startswith(" ") else section
                if current and section == "0001":  # EV_KEY: the codes it can send
                    devices[current].update(int(c, 16) for c in re.findall(r"\b([0-9a-f]{4})\b", line))
            wanted = {codes[0] for codes in LINUX_KEYS.values()}
            best = max(devices.items(), key=lambda kv: len(kv[1] & wanted), default=None)
            if best and len(best[1] & {103, 108, 105, 106}) == 4 and \
                    "ok" in self._shell(host, f"sendevent {best[0]} 0 0 0 && echo ok"):
                found = best
        except (OSError, ValueError):
            found = None
        self._input[host] = found
        app_logger.log("info", "firetv_keys", method=f"sendevent {found[0]}" if found else "input keyevent",
                       component="switches", action="firetv")
        return found

    def press(self, host: str, *keys: str) -> None:
        names = [KEYS[k] for k in keys if k in KEYS]
        if not names:
            return
        started = time.time()
        device = self._input_device(host) if "KEY_POWER" not in keys else None
        commands = []
        for name in names:
            code = next((c for c in LINUX_KEYS.get(name, []) if device and c in device[1]), None)
            if code is None:
                commands.append(f"input keyevent {name}")
            else:
                dev = device[0]
                commands.append(f"sendevent {dev} 1 {code} 1; sendevent {dev} 0 0 0; "
                                f"sendevent {dev} 1 {code} 0; sendevent {dev} 0 0 0")
        self._shell(host, "; ".join(commands))
        took = int((time.time() - started) * 1000)
        if took > SLOW_PRESS_MS:
            app_logger.log("info", "firetv_slow_key", keys=",".join(names), ms=took, component="switches", action="firetv")

    def type_text(self, host: str, text: str) -> None:
        # `input text` takes %s for spaces; quote the rest for the shell
        self._shell(host, "input text " + shlex.quote(re.sub(r"[^\w .,@'!?:-]", "", text).replace(" ", "%s")))

    def apps(self, host: str) -> Dict[str, str]:
        """{package: name} of the apps on the Fire TV that people open (asked hourly; remembered)."""
        mac, tv = self._tv(host)
        if mac is None:
            return {}
        if tv.get("apps") and time.time() - tv.get("apps_at", 0) < APPS_EVERY:
            return dict(tv["apps"])
        try:  # what the family installed, plus Amazon's own apps people open (Prime Video, Silk)
            listed = self._shell(host, "pm list packages -3; echo ---; pm list packages").split("---")
            packages = [[ln.split(":", 1)[1].strip() for ln in part.splitlines() if ln.startswith("package:")]
                        for part in listed]
        except (OSError, ValueError):
            return dict(tv.get("apps") or {})
        found: Dict[str, str] = {}
        for pkg in sorted(set(packages[0]) | {p for p in (packages[1] if len(packages) > 1 else []) if p in APP_NAMES}):
            if pkg in APP_NAMES:
                name = APP_NAMES[pkg]
            else:  # an app we don't know: a readable guess from its package name
                parts = [p for p in pkg.split(".")[1:] if p not in ("android", "tv", "firetv", "amazon", "app", "ott", "fire")]
                if not parts:
                    continue
                name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", parts[-1]).replace("_", " ").title()
            if name not in found.values():  # Prime Video comes as two packages
                found[pkg] = name
        found.pop("com.amazon.tv.launcher", None)
        ordered = dict(sorted(found.items(), key=lambda kv: (kv[1] != "Sentinel live view", kv[1].lower())))
        self._remember(mac, apps=ordered, apps_at=time.time())
        return ordered

    def launch(self, host: str, package: str) -> None:
        if not re.fullmatch(r"[\w.]{1,100}", package or ""):
            raise ValueError("not an app")
        self._shell(host, "input keyevent KEYCODE_WAKEUP; "
                          f"monkey -p {package} -c android.intent.category.LEANBACK_LAUNCHER 1 >/dev/null 2>&1 || "
                          f"monkey -p {package} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")

    def showing(self, host: str) -> Optional[str]:
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/", self._shell(host, "dumpsys window | grep mCurrentFocus"))
        if not m:
            return None
        pkg = m.group(1)
        return None if pkg == "com.amazon.tv.launcher" else self.apps(host).get(pkg, APP_NAMES.get(pkg, pkg))

    def live_app(self, host: str) -> Optional[str]:
        """The kiosk browser showing Sentinel's live view, if this Fire TV has one."""
        return next((pkg for pkg in LIVE_VIEW_APPS if pkg in self.apps(host)), None)

    def show_live(self, host: str, reload: bool = False) -> None:
        package = self.live_app(host)
        if package is None:
            raise ValueError("this Fire TV has no Sentinel live view (Fully Kiosk Browser) installed")
        if reload:
            self._shell(host, f"am force-stop {package}")
            time.sleep(1)
        self.launch(host, package)

    def icon(self, host: str, app_id: str) -> Optional[bytes]:
        return None  # Fire OS doesn't hand out app icons over ADB without extra tools: the remote shows letters
