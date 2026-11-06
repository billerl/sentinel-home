"""
Plugs and switches running Tasmota (free, open firmware: https://tasmota.github.io), as switches beside the
Kasa plugs. Many cheap Wi-Fi plugs (old Tuya/Smart Life ones especially) can be moved to Tasmota, after
which they answer a plain local HTTP API (GET /cm?cmnd=...) with no app, account or cloud.

Tasmota doesn't announce itself, so the home network is swept for it (port 80, then a Status question that
only Tasmota answers in this shape) when nothing is known yet, when a known one stops answering at its
address (DHCP moved it), and otherwise every few hours. A device with several relays (a power strip) shows
up as one switch per relay, named from its FriendlyName list. A Tasmota web password isn't supported.
"""

import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sentinel.engine.fire_tv import _local_networks

SCAN_EVERY = 3 * 3600.0
MAX_REPLY = 64 * 1024


class TasmotaDriver:
    """The SmartSwitches driver interface (discover, status, set, rename) over Tasmota's HTTP commands."""

    kind = "tasmota"

    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._hosts: Dict[str, str] = {}  # mac -> last address
        self._last_scan = 0.0

    def _command(self, host: str, command: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        url = f"http://{host}/cm?" + urllib.parse.urlencode({"cmnd": command})
        with urllib.request.urlopen(url, timeout=timeout or self.timeout) as reply:
            body = reply.read(MAX_REPLY + 1)
        if len(body) > MAX_REPLY:
            raise ValueError("reply too large")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not a Tasmota reply")
        if "WARNING" in data or data.get("Command") == "Unknown":
            raise ValueError(str(data.get("WARNING") or "unknown command"))
        return data

    @staticmethod
    def _info(data: Dict[str, Any]) -> Dict[str, Any]:
        """Status 0 -> the sysinfo shape SmartSwitches reads (Kasa's: alias, relay_state or children)."""
        status, net, sts = data.get("Status"), data.get("StatusNET"), data.get("StatusSTS")
        if not isinstance(status, dict) or not isinstance(net, dict) or not isinstance(sts, dict):
            raise ValueError("not a Tasmota status")
        names = status.get("FriendlyName") if isinstance(status.get("FriendlyName"), list) else []
        relays = sorted((int(k[5:] or 1), v) for k, v in sts.items() if re.fullmatch(r"POWER\d*", k))
        device = str(status.get("DeviceName") or (names[0] if names else "") or "Tasmota plug")
        info: Dict[str, Any] = {"mac": str(net.get("Mac") or ""), "model": "Tasmota", "alias": device}
        if len(relays) > 1:
            info["children"] = [{"id": str(n), "state": int(state == "ON"),
                                 "alias": str(names[n - 1]) if n <= len(names) else f"{device} {n}"}
                                for n, state in relays]
        else:
            info["relay_state"] = int(bool(relays) and relays[0][1] == "ON")
            if names and names[0] not in ("", "Tasmota"):
                info["alias"] = str(names[0])
        return info

    def status(self, host: str) -> Dict[str, Any]:
        return self._info(self._command(host, "Status 0"))

    def set(self, host: str, child_id: Optional[str], on: bool) -> None:
        reply = self._command(host, f"Power{child_id or ''} {'On' if on else 'Off'}")
        if not any(k.startswith("POWER") for k in reply):
            raise ValueError("the plug refused")

    def rename(self, host: str, child_id: Optional[str], name: str) -> None:
        n = child_id or "1"
        self._command(host, f"FriendlyName{n} {name}")
        if not child_id:
            self._command(host, f"DeviceName {name}")

    def discover(self, wait: float = 2.0) -> List[Tuple[str, Dict[str, Any]]]:
        """Known plugs at their address; the whole home network too when one is missing or every few hours."""
        with self._lock:
            hosts = set(self._hosts.values())
        found = self._ask(hosts)
        missing = len(found) < len(hosts)
        if not hosts or missing or time.time() - self._last_scan > SCAN_EVERY:
            self._last_scan = time.time()
            found += self._ask(set(self._scan()) - hosts)
        with self._lock:
            for host, info in found:
                self._hosts[info["mac"]] = host
        return found

    def _ask(self, hosts) -> List[Tuple[str, Dict[str, Any]]]:
        def one(host: str) -> Optional[Tuple[str, Dict[str, Any]]]:
            try:
                return host, self._info(self._command(host, "Status 0", timeout=2.0))
            except (OSError, ValueError, KeyError, TypeError):
                return None

        if not hosts:
            return []
        with ThreadPoolExecutor(min(16, len(hosts))) as pool:
            return [r for r in pool.map(one, sorted(hosts)) if r]

    @staticmethod
    def _scan() -> List[str]:
        def probe(ip: str) -> Optional[str]:
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect((ip, 80))
                return ip
            except OSError:
                return None
            finally:
                s.close()

        ips = [str(ip) for net in _local_networks() for ip in net.hosts()]
        with ThreadPoolExecutor(64) as pool:
            return [ip for ip in pool.map(probe, ips) if ip]
