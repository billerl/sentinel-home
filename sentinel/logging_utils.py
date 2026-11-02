"""
Structured application logging helpers.
"""

import json
import os
import re
import time
from threading import Lock
from typing import Any, Dict

_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@")


def redact(value: Any) -> Any:
    """Mask passwords in URLs (``rtsp://user:pass@host`` -> ``rtsp://user:***@host``)."""
    if isinstance(value, str):
        return _URL_CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:***@", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class StructuredLogger:
    """Append JSONL event/error records to a local log file, with size-based rotation."""

    def __init__(
        self,
        log_path: str = "sentinel_events.log",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ):
        self.log_path = log_path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = Lock()

    def log(self, level: str, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "event": event,
        }
        record.update(redact(fields))
        self._append_record(record)

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.log_path) < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.backup_count - 1, 0, -1):
            src = f"{self.log_path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self.log_path}.{i + 1}")
        os.replace(self.log_path, f"{self.log_path}.1")

    def _append_record(self, record: Dict[str, Any]) -> None:
        parent = os.path.dirname(self.log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = json.dumps(record, sort_keys=True)
        with self._lock:
            self._rotate_if_needed()
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(payload + "\n")


app_logger = StructuredLogger()
