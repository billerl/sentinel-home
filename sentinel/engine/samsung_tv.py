"""
Samsung smart TVs (Tizen, 2016 on) on the home network, as a "switch" beside the smart plugs, plus apps,
volume and mute ("Hey Jarvis, put on Netflix").

All local: the TV's own REST API (http :8001, no login) says who it is and which apps are installed and
open; buttons (power, volume) go over its remote-control websocket (wss :8002). The first time, the TV
shows an "Allow" pop-up for "Sentinel"; the token it then hands out is kept in samsung_tv.json (owner
only). Turning on: a powered-off TV doesn't answer at all, so it gets a Wake-on-LAN packet (the TV's
"Power On with Mobile" setting must be on; it works over Wi-Fi too), which wakes its network with the
screen still dark, then the power button. Tested 2026-11-03 on a UN65NU6900 (2018): off within 5 s;
on ~10 s after the wake packet, picture once the power button followed.

Is the picture on? A 2018 TV has no "screen on" flag, and switched off it still wakes its network now and
then with the screen dark (93-133 s at a time, every few minutes; everything it serves looks the same as
when the picture is on, 2026-11-03). So Sentinel keeps track itself: on when it turned the TV on, off when
it turned it off or the TV stops answering, and on when the TV has answered for SCREEN_AFTER without a break
(someone used the TV's own remote). Turning on while it's awake but dark is just the power button.
"""

import base64
import json
import os
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

STATE_FILE = "samsung_tv.json"
REST_PORT, WS_PORT = 8001, 8002
CLIENT_NAME = "Sentinel"
WAKE_SECONDS = 25  # how long a wake (packet, then waiting for its network) may take
SCREEN_AFTER = 240.0  # answering this long without a break = the picture is on (dark wake-ups last ~2 min)
APPS_EVERY = 3600.0
IDLE_SECONDS = 60.0  # the remote-control connection is kept open this long between button presses
# Just after being switched off the TV is still shutting down for a few minutes: a power press then is lost and
# its app service goes away for ~30-40 s (2026-11-03 18:39, 2.5 min after switch-off; a press at 18:40 worked).
# So after pressing power, watch it this long; if it goes away, wait for it to come back and press again.
PRESS_CHECK_SECONDS = 8
# The power button only works once the TV is fully awake. Its services start in stages after a wake (8187 first,
# the media service on 9197 among the last, ~30 s) and 9197 is the first to stop when it's switched off; a press
# before then was ignored (2026-11-03 18:48: pressed 1 s after its network answered, picture stayed dark; 18:40,
# fully awake: worked). 9197 is open during dark wake-ups too, so it says "awake", not "picture on".
READY_PORT = 9197
READY_SECONDS = 45  # a TV without that service (another model) gets the press anyway after this
ICONS_DIRNAME = ".tv_icons"
# Apps a TV may have, by the ids Samsung's store uses (the TV's own list is used once it has allowed us).
COMMON_APPS = {
    "11101200001": "Netflix", "3201907018807": "Netflix", "111299001912": "YouTube", "3201707014489": "YouTube TV",
    "3201512006785": "Prime Video", "3201910019365": "Prime Video", "3201901017640": "Disney+",
    "3201601007625": "Hulu", "3202301029760": "Max", "3202006020991": "Peacock", "3201807016597": "Apple TV",
    "3201606009684": "Spotify", "3201808016802": "Pluto TV", "3201504001965": "Tubi", "3201512006963": "Plex",
    "3201710015037": "Gallery", "3201908019041": "Paramount+", "3201508004642": "Sling TV",
}
BROWSER_APP = "org.tizen.browser"  # the TV's "Internet": Sentinel's camera page opens in it (Chrome 56 on a 2018 TV)
# YouTube and YouTube TV draw their own search keyboard, so text sent for the TV's keyboard never gets there
# (2026-11-10). Their grid, as on this TV: four rows of seven, letters or (after the key right of N) numbers and
# signs; below them SPACE, CLEAR and SEARCH, reached down from the first column (and back up to it). A new search
# opens on A. The key to the right of the 2nd row swaps letters and numbers, and stays highlighted.
GRIDS = {"abc": ("ABCDEFG", "HIJKLMN", "OPQRSTU", "VWXYZ-'"),
         "123": ("123&#()", "456@!?:", '789.-_"', "0/$%+[]")}
GRID_SWAP = (1, 7)
GRID_SPACE, GRID_CLEAR, GRID_SEARCH = (4, 0), (4, 1), (4, 2)


class SamsungTVDriver:
    """The SmartSwitches driver interface (discover, status, set, rename) plus apps and remote buttons."""

    kind = "samsung"

    def __init__(self, root: str, timeout: float = 2.0) -> None:
        self.path = os.path.join(os.path.abspath(root), STATE_FILE)
        self.timeout = timeout
        self.live_url: Optional[str] = None  # Sentinel's page on the home network (SmartSwitches.set_live_url)
        self._before_live: Dict[str, str] = {}  # TV address -> the app that was on when the cameras came up
        self._lock = threading.Lock()
        self._tvs: Dict[str, Dict[str, Any]] = {}  # mac -> {host, token, name, model, apps, apps_at, on_since}
        self._off_at: Dict[str, float] = {}  # host -> when we pressed power to turn it off
        self._net_since: Dict[str, float] = {}  # host -> answering without a break since
        self._conns: Dict[str, List[Any]] = {}  # host -> [open remote-control socket, last used]
        self._conn_lock = threading.Lock()
        self.icons_dir = os.path.join(os.path.abspath(root), ICONS_DIRNAME)
        try:
            with open(self.path) as f:
                self._tvs = {k: v for k, v in json.load(f).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ talking to the TV
    def _rest(self, host: str, path: str = "", method: str = "GET", timeout: Optional[float] = None) -> Dict[str, Any]:
        req = urllib.request.Request(f"http://{host}:{REST_PORT}/api/v2/{path}", method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            body = r.read(1 << 20)
        return json.loads(body) if body.strip() else {}

    def _tv(self, host: str) -> Tuple[Optional[str], Dict[str, Any]]:
        with self._lock:
            for mac, tv in self._tvs.items():
                if tv.get("host") == host:
                    return mac, tv
        return None, {}

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

    def _socket(self, host: str, path: str, timeout: float) -> ssl.SSLSocket:
        """A websocket (RFC 6455, just what the TV needs) to the TV's remote-control service."""
        raw = socket.create_connection((host, WS_PORT), timeout=timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # the TV's certificate is self-signed
        s = ctx.wrap_socket(raw)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{WS_PORT}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = s.recv(1)
            if not chunk:
                raise OSError("the TV closed the connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(head.split(b"\r\n")[0].decode("latin-1"))
        return s

    @staticmethod
    def _recv_exact(s, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = s.recv(n - len(out))
            if not chunk:
                raise OSError("the TV closed the connection")
            out += chunk
        return out

    def _read(self, s) -> Optional[Dict[str, Any]]:
        b1, b2 = self._recv_exact(s, 2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv_exact(s, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv_exact(s, 8))[0]
        if n > 1 << 22:
            raise ValueError("message too large")
        data = self._recv_exact(s, n)
        if b1 & 0x0F != 1:
            return None  # not text (ping, close)
        try:
            return json.loads(data)
        except ValueError:
            return None

    @staticmethod
    def _write(s, message: Dict[str, Any]) -> None:
        data, mask = json.dumps(message).encode(), os.urandom(4)
        size = bytes([0x80 | len(data)]) if len(data) < 126 else bytes([0x80 | 126]) + struct.pack(">H", len(data))
        s.sendall(bytes([0x81]) + size + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def _remote(self, host: str, timeout: float = 10.0):
        """Connected remote control (raises ValueError when the TV hasn't allowed Sentinel)."""
        mac, tv = self._tv(host)
        name = base64.b64encode(CLIENT_NAME.encode()).decode()
        token = tv.get("token") or ""
        s = self._socket(host, f"/api/v2/channels/samsung.remote.control?name={name}"
                               + (f"&token={token}" if token else ""), timeout)
        try:
            for _ in range(10):
                msg = self._read(s)
                if not msg:
                    continue
                if msg.get("event") == "ms.channel.connect":
                    new = (msg.get("data") or {}).get("token")
                    if mac and new and new != token:
                        self._remember(mac, token=new)
                    return s
                if msg.get("event") == "ms.channel.unauthorized":
                    raise ValueError("the TV hasn't allowed Sentinel: press Allow on the TV's pop-up "
                                     "(or Settings > General > External Device Manager > Device Connection Manager)")
            raise OSError("the TV didn't say hello")
        except Exception:
            s.close()
            raise

    def _drop(self, host: str) -> None:
        conn = self._conns.pop(host, None)
        if conn:
            try:
                conn[0].close()
            except OSError:
                pass

    def _send(self, host: str, messages: List[Dict[str, Any]], gap: float = 0.35) -> None:
        """Send on the kept-open remote-control connection (a fresh TLS handshake per press made the
        remote's arrows lag), reconnecting once if the TV closed it."""
        with self._conn_lock:
            for attempt in (0, 1):
                conn = self._conns.get(host)
                try:
                    if conn and time.time() - conn[1] > IDLE_SECONDS:
                        self._drop(host)
                        conn = None
                    if conn:  # anything waiting? the TV's own messages, or that it hung up
                        while select.select([conn[0]], [], [], 0)[0]:
                            if not conn[0].recv(65536):
                                raise OSError("the TV closed the connection")
                    else:
                        conn = self._conns[host] = [self._remote(host), 0.0]
                    for i, message in enumerate(messages):
                        if i:
                            time.sleep(gap)
                        self._write(conn[0], message)
                    conn[1] = time.time()
                    return
                except ValueError:
                    self._drop(host)
                    raise  # not allowed: trying again won't help
                except OSError:
                    self._drop(host)
                    if attempt:
                        raise

    def press(self, host: str, *keys: str) -> None:
        self._send(host, [{"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}} for key in keys])

    def type_text(self, host: str, text: str) -> None:
        """Into whatever box the TV's on-screen keyboard is showing (an app's search)."""
        self._send(host, [{"method": "ms.remote.control", "params": {
            "Cmd": base64.b64encode(text.encode()).decode(), "DataOfCmd": "base64", "TypeOfRemote": "SendInputString"}},
            {"method": "ms.remote.control", "params": {"TypeOfRemote": "SendInputEnd"}}])

    def type_on_grid(self, host: str, text: str, start: Tuple[int, int] = (0, 0), mode: str = "abc") -> None:
        """Search an app's own on-screen keyboard (GRIDS) for ``text``: CLEAR, the text by arrows and OK, then
        SEARCH. Starts on A (a fresh search) unless told where it is. Never presses left of the first column
        (that opens the app's menu)."""
        text = re.sub(r"\s+", " ", text.upper()).strip()
        bad = sorted({ch for ch in text if ch != " " and not any(ch in "".join(g) for g in GRIDS.values())})
        if bad:
            raise ValueError("that keyboard has no " + " ".join(bad))
        keys: List[str] = []
        at = start
        bottom = len(GRIDS["abc"])

        def go(to: Tuple[int, int]) -> None:
            nonlocal at
            if at[0] == bottom:  # SPACE / CLEAR / SEARCH: back along to SPACE, then up
                keys.extend(["KEY_LEFT"] * at[1] + ["KEY_UP"])
                at = (bottom - 1, 0)
            if to[0] == bottom:
                go((bottom - 1, 0))
                keys.extend(["KEY_DOWN"] + ["KEY_RIGHT"] * to[1])
                at = to
                return
            steps_v = ["KEY_DOWN" if to[0] > at[0] else "KEY_UP"] * abs(to[0] - at[0])
            steps_h = ["KEY_RIGHT" if to[1] > at[1] else "KEY_LEFT"] * abs(to[1] - at[1])
            keys.extend(steps_h + steps_v if at[1] == GRID_SWAP[1] else steps_v + steps_h)  # the 8th column is short
            at = to

        def press(spot: Tuple[int, int]) -> None:
            go(spot)
            keys.append("KEY_ENTER")

        press(GRID_CLEAR)
        for ch in text:
            if ch == " ":
                press(GRID_SPACE)
                continue
            if ch not in "".join(GRIDS[mode]):
                press(GRID_SWAP)
                mode = "123" if mode == "abc" else "abc"
            press(next((r, row.index(ch)) for r, row in enumerate(GRIDS[mode]) if ch in row))
        press(GRID_SEARCH)
        self.press(host, *keys)

    def _wake(self, mac: str, host: str) -> None:
        packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in ("255.255.255.255", host):
                for port in (9, 7):
                    try:
                        sock.sendto(packet, (target, port))
                    except OSError:
                        pass
        finally:
            sock.close()

    def _ready(self, host: str) -> bool:
        """Fully awake (READY_PORT answers)."""
        try:
            socket.create_connection((host, READY_PORT), timeout=1.0).close()
            return True
        except OSError:
            return False

    def _wait_ready(self, mac: str, host: str) -> bool:
        for _ in range(READY_SECONDS // 3):
            if self._ready(host):
                return True
            self._wake(mac, host)  # (no harm when its network is up; wakes it if it went to sleep meanwhile)
            time.sleep(2)
        return False

    def _power_on(self, mac: str, host: str) -> None:
        """The power button for a TV whose network is awake and picture dark: once it's fully awake, and again
        if the press is lost (it was still shutting down, PRESS_CHECK_SECONDS)."""
        if not self._wait_ready(mac, host):
            app_logger.log("warning", "tv_not_ready", component="switches", action="tv",
                           error_message=f"port {READY_PORT} never answered: pressing power anyway")
        self.press(host, "KEY_POWER")
        for _ in range(PRESS_CHECK_SECONDS):
            time.sleep(1)
            if self.answering(host, timeout=1.0) is None:
                break
        else:
            return  # stayed up: the picture is coming on
        self._drop(host)
        if not self._wait_ready(mac, host) and self.answering(host, timeout=1.5) is None:
            raise OSError("the TV was still switching off and didn't come back")
        app_logger.log("info", "tv_power_again", component="switches", action="tv",
                       error_message="the first power press was lost (the TV was still switching off)")
        time.sleep(1)
        self.press(host, "KEY_POWER")

    def answering(self, host: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        try:
            return self._rest(host, timeout=timeout)
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------ the switch interface
    def _info(self, mac: str, tv: Dict[str, Any], on: bool) -> Dict[str, Any]:
        return {"mac": mac, "model": tv.get("model") or "Samsung TV", "alias": tv.get("name") or "TV",
                "relay_state": int(on), "on_time": time.time() - tv["on_since"] if on and tv.get("on_since") else 0}

    def _learn(self, host: str, reply: Dict[str, Any]) -> Optional[str]:
        dev = reply.get("device") or {}
        mac = re.sub(r"[^0-9a-f]", "", str(dev.get("wifiMac") or "").lower())
        if len(mac) != 12 or dev.get("type") != "Samsung SmartTV":
            return None
        _, known = self._tv(host)
        name = re.sub(r"^\[TV\]\s*", "", str(dev.get("name") or "")).strip() or "TV"
        fields = {"host": host, "model": f"Samsung {dev.get('modelName') or 'TV'}"}
        if not self._tvs.get(mac, {}).get("name"):
            fields["name"] = name
        self._remember(mac, **fields)
        return mac

    def discover(self, wait: float = 2.0) -> List[Tuple[str, Dict[str, Any]]]:
        """Samsung TVs that answer an SSDP search (UPnP), plus known ones that answer at their address."""
        hosts = set()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.3)
            search = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\n"
                      "ST: urn:samsung.com:device:RemoteControlReceiver:1\r\n\r\n").encode()
            for _ in range(2):
                sock.sendto(search, ("239.255.255.250", 1900))
            end = time.time() + wait
            while time.time() < end:
                try:
                    _, (host, _port) = sock.recvfrom(4096)
                    hosts.add(host)
                except socket.timeout:
                    continue
        except OSError as e:
            app_logger.log("warning", "switch_discover_failed", error_message=str(e), component="switches",
                           action="discover_tv")
        finally:
            sock.close()
        with self._lock:
            hosts |= {tv["host"] for tv in self._tvs.values() if tv.get("host")}
        found = []
        for host in hosts:
            reply = self.answering(host)
            mac = self._learn(host, reply) if reply else None
            if mac:
                found.append((host, self._info(mac, self._tvs[mac], True)))
        return found

    def _screen(self, mac: str, on: bool) -> None:
        if bool(self._tvs.get(mac, {}).get("on_since")) != on:
            self._remember(mac, on_since=time.time() if on else None)

    def _app_on_screen(self, host: str) -> bool:
        """An app showing = the picture is on (the home screen or an input like cable show none)."""
        _, tv = self._tv(host)
        for app_id in (tv.get("apps") or COMMON_APPS):
            try:
                if self._rest(host, f"applications/{app_id}", timeout=1.0).get("visible"):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def status(self, host: str) -> Dict[str, Any]:
        """Off is normal for a TV: it just doesn't answer (raises only for a TV we've never met)."""
        mac, tv = self._tv(host)
        if mac is None:
            raise OSError("unknown TV")
        now = time.time()
        if self.answering(host) is None:
            self._net_since.pop(host, None)
            self._screen(mac, False)
        else:
            since = self._net_since.setdefault(host, now)
            if not tv.get("on_since") and now - since >= SCREEN_AFTER and now - self._off_at.get(host, 0.0) >= SCREEN_AFTER:
                self._screen(mac, True)  # up all along: turned on with the TV's own remote, not a dark wake-up
        return self._info(mac, self._tvs[mac], bool(self._tvs[mac].get("on_since")))

    def set(self, host: str, child_id: Optional[str], on: bool) -> None:
        mac, tv = self._tv(host)
        if mac is None:
            raise OSError("unknown TV")
        recently_off = time.time() - self._off_at.get(host, 0.0) < 15
        if not on:
            # Answering = on, unless Sentinel switched it off itself lately (its network stays up a few minutes).
            # Someone asking for "off" sees it on: the TV can't say whether its picture is on, and watching cable
            # or an antenna shows no app, so a TV turned on with its own remote looked off for its first
            # SCREEN_AFTER and "turn off the TV" did nothing (2026-11-03 18:43). The rare miss, asked during a
            # dark wake-up, lights it instead, and asking again switches it off.
            if self.answering(host) is not None and time.time() - self._off_at.get(host, 0.0) >= SCREEN_AFTER:
                self.press(host, "KEY_POWER")
                self._off_at[host] = time.time()
            self._screen(mac, False)
            return
        if tv.get("on_since") and self.answering(host) is not None:
            return  # on, as far as we know: pressing power would switch it off
        if recently_off:  # wait for its network to go, or the wake below would be taken for "already on"
            end = time.time() + 15
            while time.time() < end and self.answering(host, timeout=1.0) is not None:
                time.sleep(1)
            self._off_at.pop(host, None)
        if self.answering(host) is not None:
            if not self._app_on_screen(host):
                self._power_on(mac, host)  # awake but dark (a wake-up of its own): the button lights it
            self._screen(mac, True)
            return
        end = time.time() + WAKE_SECONDS
        while time.time() < end:
            self._wake(mac, host)
            if self.answering(host, timeout=1.5) is not None:
                break
            time.sleep(1)
        else:
            raise OSError("the TV didn't wake up (is 'Power On with Mobile' on in its network settings?)")
        time.sleep(1)
        self._power_on(mac, host)  # the wake packet only wakes its network: the picture needs the button
        self._screen(mac, True)

    def rename(self, host: str, child_id: Optional[str], name: str) -> None:
        mac, _ = self._tv(host)
        if mac is None:
            raise OSError("unknown TV")
        self._remember(mac, name=name)  # the TV keeps its own name; this one is Sentinel's

    # ------------------------------------------------------------ apps
    def apps(self, host: str) -> Dict[str, str]:
        """{app id: name} installed on the TV (asked hourly while it's on; remembered while it's off)."""
        mac, tv = self._tv(host)
        if mac is None:
            return {}
        fresh = time.time() - tv.get("apps_at", 0) < APPS_EVERY and "icons" in tv
        if tv.get("apps") and (fresh or not tv.get("on_since")):
            return dict(tv["apps"])  # fresh, or the TV is off (asking would only time out)
        if not tv.get("on_since"):
            return {}
        found: Dict[str, str] = {}
        try:
            s = self._remote(host)
            try:
                self._write(s, {"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}})
                for _ in range(20):
                    msg = self._read(s)
                    if msg and msg.get("event") == "ed.installedApp.get":
                        listed = [a for a in (msg.get("data") or {}).get("data", []) if a.get("appId") and a.get("name")]
                        found = {str(a["appId"]): str(a["name"]) for a in listed}
                        self._remember(mac, icons={str(a["appId"]): str(a["icon"]) for a in listed if a.get("icon")})
                        break
            finally:
                s.close()
        except (OSError, ValueError):
            pass
        if not found:  # not allowed yet, or an older TV: ask about the common ones (no icons that way)
            self._remember(mac, icons={})
            for app_id, name in COMMON_APPS.items():
                try:
                    found[app_id] = str(self._rest(host, f"applications/{app_id}").get("name") or name)
                except (OSError, ValueError):
                    continue
        if found:
            self._remember(mac, apps=found, apps_at=time.time())
        return found or dict(tv.get("apps") or {})

    def launch(self, host: str, app_id: str) -> None:
        """Open an app. One already running behind another is brought back where it was (the way the TV's own app
        bar does it); starting it over the REST API instead began it again from its first screen (2026-11-10)."""
        try:
            running = self._rest(host, f"applications/{app_id}").get("running")
        except (OSError, ValueError):
            running = False
        if running:
            self._send(host, [{"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host",
                                                                       "data": {"appId": app_id, "action_type": "DEEP_LINK"}}}])
            return
        self._rest(host, f"applications/{app_id}", method="POST", timeout=5.0)

    def live_app(self, host: str) -> Optional[str]:
        """The browser, once Sentinel has said where its camera page is (``live_url``)."""
        return BROWSER_APP if self.live_url else None

    def show_live(self, host: str, reload: bool = False) -> None:
        """Sentinel's camera page in the TV's browser. Opening the address again loads it afresh, so a reload is
        the same (signed in once with the QR code, the browser keeps it). The app that was on is remembered for
        live_back: it keeps running behind the browser."""
        if not self.live_url:
            raise ValueError("Sentinel's web page isn't on the home network")
        before = self._visible_app(host)  # (the browser itself isn't one of the TV's listed apps)
        if before:
            self._before_live[host] = before
        self._send(host, [{"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host", "data": {
            "appId": BROWSER_APP, "action_type": "NATIVE_LAUNCH", "metaTag": self.live_url}}}])

    def live_return(self, host: str) -> Optional[str]:
        """The app the cameras covered, while it's still running behind them, else None."""
        app_id = self._before_live.get(host)
        if not app_id:
            return None
        try:
            state = self._rest(host, f"applications/{app_id}")
        except (OSError, ValueError):
            return None
        if not state.get("running"):
            self._before_live.pop(host, None)
            return None
        return None if state.get("visible") else self.apps(host).get(app_id, app_id)

    def live_back(self, host: str) -> str:
        """Back to the app the cameras covered, where it was. Returns its name."""
        app_id = self._before_live.pop(host, None)
        if not app_id:
            raise ValueError("nothing was on before the cameras")
        self.launch(host, app_id)
        return self.apps(host).get(app_id, app_id)

    def close(self, host: str, app_id: str) -> None:
        """Quit an app, so the next launch starts at its first screen (a TV shortcut needs that)."""
        self._rest(host, f"applications/{app_id}", method="DELETE", timeout=5.0)

    def showing(self, host: str) -> Optional[str]:
        """The app on screen, if any (None: the home screen, an input like cable, or off)."""
        app_id = self._visible_app(host)
        return self.apps(host).get(app_id) if app_id else None

    def _visible_app(self, host: str) -> Optional[str]:
        for app_id in self.apps(host):
            try:
                if self._rest(host, f"applications/{app_id}").get("visible"):
                    return app_id
            except (OSError, ValueError):
                continue
        return None

    # ------------------------------------------------------------ app icons (the TV's own, for the remote)
    def icon(self, host: str, app_id: str) -> Optional[bytes]:
        """The app's icon as the TV shows it (fetched once over the remote connection, then kept on disk)."""
        if not re.fullmatch(r"[\w.]{1,40}", app_id or ""):
            return None
        path = os.path.join(self.icons_dir, f"{app_id}.png")
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
        _, tv = self._tv(host)
        icon_path = (tv.get("icons") or {}).get(app_id)
        if not icon_path or not tv.get("on_since"):
            return None
        data = None
        try:
            s = self._remote(host, timeout=6.0)
            try:
                self._write(s, {"method": "ms.channel.emit", "params": {
                    "event": "ed.apps.icon", "to": "host", "data": {"iconPath": icon_path}}})
                for _ in range(20):
                    msg = self._read(s)
                    if msg and msg.get("event") == "ed.apps.icon":
                        data = base64.b64decode((msg.get("data") or {}).get("imageBase64") or "")
                        break
            finally:
                s.close()
        except (OSError, ValueError):
            return None
        if not data:
            return None
        try:
            os.makedirs(self.icons_dir, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
        return data
