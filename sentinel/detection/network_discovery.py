"""
Network camera discovery helpers.
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional

import cv2


@dataclass
class CameraEndpoint:
    """Represents a discovered network camera stream endpoint."""

    label: str
    source: str
    host: str


class NetworkCameraDiscovery:
    """Discovers RTSP-capable cameras on the local subnet."""

    COMMON_RTSP_MAIN_PATHS = [
        "",
        "/h264/ch1/main/av_stream",
        "/Streaming/Channels/101",
        "/cam/realmonitor?channel=1&subtype=0",
        "/live/ch00_0",
    ]
    COMMON_RTSP_SUB_PATHS = [
        "/h264/ch1/sub/av_stream",
        "/Streaming/Channels/102",
        "/cam/realmonitor?channel=1&subtype=1",
        "/live/ch00_1",
    ]
    COMMON_PORTS = [554, 8554]

    def __init__(self, socket_timeout: float = 0.25):
        self.socket_timeout = socket_timeout

    def discover(
        self,
        username: str = "",
        password: str = "",
        scan_limit: int = 80,
        prefer_substream: bool = False,
    ) -> List[CameraEndpoint]:
        """Scan local subnet and return endpoints that provide a readable stream."""
        network = self._detect_local_network()
        if network is None:
            return []

        hosts = [str(host) for host in network.hosts()]
        hosts = hosts[:scan_limit]
        candidate_hosts = self._find_hosts_with_rtsp_port(hosts)

        results: List[CameraEndpoint] = []
        for host in candidate_hosts:
            endpoint = self._find_working_rtsp_endpoint(
                host,
                username,
                password,
                prefer_substream=prefer_substream,
            )
            if endpoint:
                results.append(
                    CameraEndpoint(
                        label=endpoint["label"],
                        source=endpoint["source"],
                        host=host,
                    )
                )
        return results

    def _detect_local_network(self) -> Optional[ipaddress.IPv4Network]:
        """Infer local /24 subnet from active interface."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            sock.close()
            return ipaddress.ip_network(f"{local_ip}/24", strict=False)
        except OSError:
            return None

    def _find_hosts_with_rtsp_port(self, hosts: List[str]) -> List[str]:
        found = []
        with ThreadPoolExecutor(max_workers=64) as executor:
            futures = {executor.submit(self._host_has_rtsp_port, host): host for host in hosts}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    if future.result():
                        found.append(host)
                except Exception:
                    # Ignore individual host probe failures.
                    pass
        return sorted(found)

    def _host_has_rtsp_port(self, host: str) -> bool:
        for port in self.COMMON_PORTS:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.socket_timeout)
                if sock.connect_ex((host, port)) == 0:
                    return True
        return False

    def _find_working_rtsp_endpoint(
        self,
        host: str,
        username: str,
        password: str,
        prefer_substream: bool = False,
    ) -> Optional[dict]:
        auth = ""
        if username:
            auth = f"{username}:{password}@"

        sub_patterns = tuple(
            path for path in self.COMMON_RTSP_SUB_PATHS if path
        )
        ordered_paths = (
            self.COMMON_RTSP_SUB_PATHS + self.COMMON_RTSP_MAIN_PATHS
            if prefer_substream
            else self.COMMON_RTSP_MAIN_PATHS + self.COMMON_RTSP_SUB_PATHS
        )
        for port in self.COMMON_PORTS:
            for path in ordered_paths:
                url = f"rtsp://{auth}{host}:{port}{path}"
                if self._stream_is_readable(url):
                    stream_kind = "Substream" if path in sub_patterns else "Main"
                    return {
                        "source": url,
                        "label": f"Network {host} ({stream_kind})",
                    }
        return None

    def _stream_is_readable(self, source: str) -> bool:
        cap = None
        try:
            cap = cv2.VideoCapture(source)
            if not cap.isOpened():
                return False
            return cap.grab()
        except Exception:
            return False
        finally:
            if cap:
                cap.release()
