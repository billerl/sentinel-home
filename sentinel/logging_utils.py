"""
Structured application logging helpers.
"""

import json
import os
import time
from threading import Lock
from typing import Any, Dict


class StructuredLogger:
    """Append JSONL event/error records to a local log file."""

    def __init__(self, log_path: str = "sentinel_events.log"):
        self.log_path = log_path
        self._lock = Lock()

    def log(self, level: str, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "event": event,
        }
        record.update(fields)
        self._append_record(record)

    def _append_record(self, record: Dict[str, Any]) -> None:
        parent = os.path.dirname(self.log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = json.dumps(record, sort_keys=True)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(payload + "\n")


app_logger = StructuredLogger()
