"""Optional HTTP web viewer (MJPEG grid)."""

from __future__ import annotations

__all__ = ["FrameBroker", "WebServer"]


def __getattr__(name: str):
    if name == "FrameBroker":
        from sentinel.web.frame_broker import FrameBroker

        return FrameBroker
    if name == "WebServer":
        from sentinel.web.server import WebServer

        return WebServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
