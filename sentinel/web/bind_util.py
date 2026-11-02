"""Web bind address validation (stdlib only)."""

from __future__ import annotations

import ipaddress


def bind_host_allowed(bind_host: str, camera_subnet: str) -> tuple[bool, str]:
    try:
        addr = ipaddress.ip_address(bind_host)
        net = ipaddress.ip_network(camera_subnet, strict=False)
        if addr in net:
            return (
                False,
                f"Refusing web.bind_host={bind_host}: inside camera_subnet_block {camera_subnet}",
            )
        return True, ""
    except ValueError:
        if bind_host in ("127.0.0.1", "localhost", "::1"):
            return True, ""
        return False, f"Invalid web.bind_host: {bind_host}"
