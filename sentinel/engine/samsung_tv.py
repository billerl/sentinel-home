"""
Samsung smart TVs (Tizen, 2016 on) on the home network, as a "switch" beside the smart plugs, plus apps,
volume and mute ("Hey Jarvis, put on Netflix").

All local: the TV's own REST API (http :8001, no login) says who it is and which apps are installed and
open; buttons (power, volume) go over its remote-control websocket (wss :8002). The first time, the TV
shows an "Allow" pop-up for "Sentinel"; the token it then hands out is kept in samsung_tv.json (owner
only). Turning on: a powered-off TV doesn't answer at all, so it gets a Wake-on-LAN packet (the TV's
"Power On with Mobile" setting must be on; it works over Wi-Fi too), which wakes its network with the
screen still dark, then the power button. Tested 2026-11-03 on a UN65NU6900 (2018): off within 5 s;
on ~10 s after the wake packet, picture once the power button followed. Answering = on: an older TV
has no "screen on" flag, and when the picture goes off it stops answering within seconds.
"""

import base64
import json
import os
import re
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
APPS_EVERY = 3600.0
# Apps a TV may have, by the ids Samsung's store uses (the TV's own list is used once it has allowed us).
COMMON_APPS = {
    "11101200001": "Netflix", "3201907018807": "Netflix", "111299001912": "YouTube", "3201707014489": "YouTube TV",
    "3201512006785": "Prime Video", "3201910019365": "Prime Video", "3201901017640": "Disney+",
    "3201601007625": "Hulu", "3202301029760": "Max", "3202006020991": "Peacock", "3201807016597": "Apple TV",
    "3201606009684": "Spotify", "3201808016802": "Pluto TV", "3201504001965": "Tubi", "3201512006963": "Plex",
    "3201710015037": "Gallery", "3201908019041": "Paramount+", "3201508004642": "Sling TV",
}


class SamsungTVDriver:
    """The SmartSwitches driver interface (discover, status, set, rename) plus apps and remote buttons."""

    kind = "samsung"

    def __init__(self, root: str, timeout: float = 2.0) -> None:
        self.path = os.path.join(os.path.abspath(root), STATE_FILE)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._tvs: Dict[str, Dict[str, Any]] = {}  # mac -> {host, token, name, model, apps, apps_at, on_since}
        self._off_at: Dict[str, float] = {}  # host -> when we pressed power to turn it off
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

    def press(self, host: str, *keys: str) -> None:
        s = self._remote(host)
        try:
            for key in keys:
                self._write(s, {"method": "ms.remote.control", "params": {
                    "Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}})
                time.sleep(0.35)
        finally:
            s.close()

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

    def status(self, host: str) -> Dict[str, Any]:
        """Off is normal for a TV: it just doesn't answer (raises only for a TV we've never met)."""
        mac, tv = self._tv(host)
        if mac is None:
            raise OSError("unknown TV")
        reply = self.answering(host)
        if reply is not None and time.time() - self._off_at.get(host, 0.0) < 15:
            reply = None  # just switched off: its network goes a few seconds after the picture
        on = reply is not None
        if on and not tv.get("on_since"):
            self._remember(mac, on_since=time.time())
        elif not on and tv.get("on_since"):
            self._remember(mac, on_since=None)
        return self._info(mac, self._tvs[mac], on)

    def set(self, host: str, child_id: Optional[str], on: bool) -> None:
        mac, _ = self._tv(host)
        if mac is None:
            raise OSError("unknown TV")
        recently_off = time.time() - self._off_at.get(host, 0.0) < 15
        if not on:
            if self.answering(host) is not None and not recently_off:
                self.press(host, "KEY_POWER")
                self._off_at[host] = time.time()
            return
        if recently_off:  # wait for its network to go, or the wake below would be taken for "already on"
            end = time.time() + 15
            while time.time() < end and self.answering(host, timeout=1.0) is not None:
                time.sleep(1)
            self._off_at.pop(host, None)
        if self.answering(host) is not None:
            return  # already on
        end = time.time() + WAKE_SECONDS
        while time.time() < end:
            self._wake(mac, host)
            if self.answering(host, timeout=1.5) is not None:
                break
            time.sleep(1)
        else:
            raise OSError("the TV didn't wake up (is 'Power On with Mobile' on in its network settings?)")
        time.sleep(1)
        self.press(host, "KEY_POWER")  # the wake packet only wakes its network: the picture needs the button

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
        if tv.get("apps") and (time.time() - tv.get("apps_at", 0) < APPS_EVERY or not tv.get("on_since")):
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
                        found = {str(a.get("appId")): str(a.get("name")) for a in (msg.get("data") or {}).get("data", [])
                                 if a.get("appId") and a.get("name")}
                        break
            finally:
                s.close()
        except (OSError, ValueError):
            pass
        if not found:  # not allowed yet, or an older TV: ask about the common ones
            for app_id, name in COMMON_APPS.items():
                try:
                    found[app_id] = str(self._rest(host, f"applications/{app_id}").get("name") or name)
                except (OSError, ValueError):
                    continue
        if found:
            self._remember(mac, apps=found, apps_at=time.time())
        return found or dict(tv.get("apps") or {})

    def launch(self, host: str, app_id: str) -> None:
        self._rest(host, f"applications/{app_id}", method="POST", timeout=5.0)

    def showing(self, host: str) -> Optional[str]:
        """The app on screen, if any (None: the home screen, an input like cable, or off)."""
        for app_id, name in self.apps(host).items():
            try:
                if self._rest(host, f"applications/{app_id}").get("visible"):
                    return name
            except (OSError, ValueError):
                continue
        return None
