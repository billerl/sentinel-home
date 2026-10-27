"""
Settings schema, system status and remote-access info for the web Settings page
and the Telegram "access" command.
"""

import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from sentinel.constants import VIDEO_DIR


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("expected true/false")


def _num(lo: float, hi: float, cast: Callable = float) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("expected a number")
        return cast(min(hi, max(lo, cast(value))))

    return check


def _chat_ids(value: Any) -> str:
    ids = [p for p in re.split(r"[\s,]+", str(value).strip()) if p]
    for chat_id in ids:
        if not re.fullmatch(r"-?\d{3,20}", chat_id):
            raise ValueError(f"not a Telegram chat id: {chat_id}")
    return ", ".join(dict.fromkeys(ids))


def _url(value: Any) -> str:
    text = str(value).strip()
    if text and not re.fullmatch(r"https?://[A-Za-z0-9.\-:/_~%?=&#]{1,200}", text):
        raise ValueError("expected an http(s) URL")
    return text


def _username(value: Any) -> str:
    text = str(value).strip()
    if text and not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", text):
        raise ValueError("expected a Linux username")
    return text


def _place(value: Any) -> str:
    text = " ".join(str(value if isinstance(value, str) else "").split())
    if len(text) > 60 or re.search(r"[<>\"{}\\]", text):
        raise ValueError("enter a town name or ZIP code")
    return text


def available_voices(models_dir: str = "models") -> List[str]:
    return sorted(
        os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(models_dir, "*.onnx"))
    )


def _spotify_client_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text and not re.fullmatch(r"[0-9a-f]{32}", text):
        raise ValueError("a Spotify Client ID is 32 letters and numbers (0-9, a-f)")
    return text


def _choice(value: Any, options: tuple) -> str:
    if value not in options:
        raise ValueError("choose one of: " + ", ".join(options))
    return value


def _alert_groups(value: Any) -> List[str]:
    allowed = ("person", "vehicle", "delivery", "animal")
    if not isinstance(value, list) or any(v not in allowed for v in value):
        raise ValueError("choose from person, vehicle, delivery, animal")
    return [g for g in allowed if g in value]


def available_detector_models(models_dir: Optional[str] = None) -> List[str]:
    from sentinel.detection.objects import MODELS_DIR

    models_dir = models_dir or MODELS_DIR
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(models_dir, "*.onnx")))


def _detector_model(value: Any) -> str:
    text = str(value)
    if text not in available_detector_models():
        raise ValueError("unknown model")
    return text


def _voice(value: Any) -> str:
    text = str(value)
    if text and text not in available_voices():
        raise ValueError("unknown voice")
    return text


# Settings editable from the web page (dotted keys live under nested dicts).
SETTINGS_SCHEMA: Dict[str, Callable[[Any], Any]] = {
    "telegram_enabled": _bool,
    "telegram_chat_ids": _chat_ids,
    "telegram_cooldown": _num(0, 3600),
    "telegram_motion_alerts": _bool,
    "telegram_include_video": _bool,
    "recording_retention_enabled": _bool,
    "recording_retention_days": _num(1, 365, int),
    "recording_min_free_percent": _num(0, 50, int),
    "auto_reconnect_enabled": _bool,
    "reconnect_interval_seconds": _num(5, 300, int),
    "feed_fps": _num(5, 30, int),
    "night_bug_start_hour": _num(0, 23, int),
    "night_bug_end_hour": _num(0, 23, int),
    "volume": _num(0, 1),
    "speech_speed": _num(80, 350, int),
    "voice_id": _voice,
    "object_detection_enabled": _bool,
    "vehicle_types_enabled": _bool,
    "inference_device": lambda v: _choice(v, ("auto", "GPU", "CPU")),
    "household_ask_enabled": _bool,
    "household_ask_per_day": _num(0, 20, int),
    "detector_model": _detector_model,
    "alert_mode": lambda v: _choice(v, ("objects", "motion")),
    "object_alert_groups": _alert_groups,
    "object_alert_cooldown": _num(0, 3600, int),
    "remote_web_url": _url,
    "remote_rdp_user": _username,
    "web.max_fps": _num(1, 20, int),
    "web.show_zones": _bool,
    "weather_location": _place,
    "assistant_enabled": _bool,
    "voice_enabled": _bool,
    "spotify_client_id": _spotify_client_id,
    "spotify_device": lambda v: str(v or "").strip()[:80],
    "voice_follow_up": _bool,
    "audio_output": lambda v: _choice(v or "speakers", ("speakers", "remote")),
    "voice_wake_threshold": _num(0.1, 0.95),
    "voice_gain": _num(1, 20),
    "assistant_daily_limit": _num(0, 200, int),
    "assistant_model": lambda v: _choice(v or "sonnet", ("haiku", "sonnet")),
    "weather_units": lambda v: _choice(v or "fahrenheit", ("fahrenheit", "celsius")),
}

# Secrets that may be replaced but are never sent back to the browser.
WRITE_ONLY_SETTINGS: Dict[str, Callable[[Any], Any]] = {
    "telegram_bot_token": lambda v: _token(v),
}


def _token(value: Any) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,80}", text):
        raise ValueError("that doesn't look like a Telegram bot token")
    return text


def get_setting(settings: Dict[str, Any], key: str) -> Any:
    node: Any = settings
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def set_setting(settings: Dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = settings
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# --------------------------------------------------------------- tailscale
_TAILSCALE_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}


def tailscale_status(max_age: float = 60.0) -> Dict[str, Any]:
    """{'installed', 'running', 'ip', 'name'} from the local tailscale CLI (cached)."""
    now = time.time()
    if _TAILSCALE_CACHE["value"] is not None and now - _TAILSCALE_CACHE["at"] < max_age:
        return _TAILSCALE_CACHE["value"]
    info: Dict[str, Any] = {"installed": bool(shutil.which("tailscale")), "running": False, "ip": None, "name": None}
    if info["installed"]:
        try:
            out = subprocess.run(
                ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=4
            ).stdout
            data = json.loads(out or "{}")
            me = data.get("Self") or {}
            info["running"] = data.get("BackendState") == "Running"
            ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
            info["ip"] = ips[0] if ips else None
            info["name"] = (me.get("DNSName") or "").rstrip(".") or None
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    _TAILSCALE_CACHE.update(at=now, value=info)
    return info


def remote_access_info(settings: Dict[str, Any]) -> Dict[str, Any]:
    ts = tailscale_status()
    return {
        "web_url": settings.get("remote_web_url") or "",
        "rdp_user": settings.get("remote_rdp_user") or getpass.getuser(),
        "tailscale": ts,
    }


def access_message(settings: Dict[str, Any]) -> str:
    """Plain-text guide to reaching Sentinel remotely (Telegram 'access' reply)."""
    info = remote_access_info(settings)
    ts = info["tailscale"]
    host = ts["ip"] or "(this PC's Tailscale IP)"
    lines = ["Sentinel remote access", ""]
    lines.append("1) Web viewer — live cameras, motion timeline, zones, settings, help")
    if info["web_url"]:
        lines.append(f"   {info['web_url']}")
    else:
        lines.append("   (web address not set — add it under Settings > Remote access)")
    lines.append("   Sign in with your Sentinel web username and password.")
    lines.append("")
    state = "connected" if ts["running"] else ("NOT connected" if ts["installed"] else "not installed")
    lines.append(f"2) Tailscale — private network to this PC ({state})")
    if ts["ip"]:
        lines.append(f"   This PC: {ts['name'] or 'sentinel'} — {ts['ip']}")
    lines.append("   On your laptop/phone: install Tailscale (tailscale.com/download),")
    lines.append("   sign in with the same account and make sure it shows Connected.")
    lines.append("")
    lines.append("3) Remote Desktop — the PC's full desktop (needs Tailscale on)")
    lines.append(f"   Windows: open Remote Desktop Connection, computer: {host}")
    lines.append(f"   Log in as {info['rdp_user']} (session: Xorg).")
    lines.append("   Black screen? Log out of the desktop at the PC itself, then retry.")
    lines.append("")
    lines.append("Send Help for all commands.")
    return "\n".join(lines)


# ------------------------------------------------------------ system status
class CpuMeter:
    """Process CPU % since the previous call, from /proc (no psutil needed)."""

    def __init__(self) -> None:
        self._last = self._sample()

    @staticmethod
    def _sample():
        try:
            with open("/proc/self/stat") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            return ticks / os.sysconf("SC_CLK_TCK"), time.monotonic()
        except (OSError, ValueError, IndexError):
            return 0.0, time.monotonic()

    def percent(self) -> Optional[float]:
        cpu, wall = self._sample()
        last_cpu, last_wall = self._last
        self._last = (cpu, wall)
        if wall - last_wall < 0.5:
            return None
        return round(100.0 * (cpu - last_cpu) / (wall - last_wall), 1)


# Routine chatter that would drown out what matters on the status page.
_NOISY_EVENTS = {
    "camera_connect_attempt", "camera_connect_success", "connect_camera_source",
    "camera_read_error", "web_server_start",
    "object_detected",  # shown under "Recently seen" instead
}

# Plain-English labels for the status page.
EVENT_LABELS = {
    "camera_connected": "Camera online",
    "camera_down": "Camera offline",
    "camera_connect_error": "Camera connection failed",
    "camera_added": "Camera added",
    "camera_removed": "Camera removed",
    "camera_config_updated": "Camera settings changed",
    "engine_started": "Sentinel started",
    "object_detector_ready": "Object detection ready",
    "object_detector_unavailable": "Object detection unavailable",
    "object_detected": "Recognised",
    "engine_stopped": "Sentinel stopped",
    "recording_maintenance": "Old clips cleaned up",
    "recording_maintenance_failed": "Clip cleanup failed",
    "settings_updated": "Settings changed",
    "remote_feature_toggle": "Changed from Telegram",
    "web_login_success": "Web sign-in",
    "web_login_failed": "Failed web sign-in",
    "web_login_locked": "Web sign-in locked out",
    "web_password_changed": "Web password changed",
    "web_password_change_failed": "Wrong password on password change",
    "hd_recorder_restart": "HD recorder restarted",
    "hd_clip_failed": "HD clip failed",
    "recording_transcode_failed": "Clip conversion failed",
    "telegram_command_failed": "Telegram command failed",
}


def _log_tail(log_path: str, max_bytes: int) -> List[Dict[str, Any]]:
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def recent_objects(log_path: str = "sentinel_events.log", limit: int = 15) -> List[Dict[str, Any]]:
    """Most recent recognised objects (from the event log, so they survive restarts)."""
    out = []
    for record in reversed(_log_tail(log_path, 256 * 1024)):
        if record.get("event") != "object_detected":
            continue
        try:
            ts = time.mktime(time.strptime(record["timestamp"], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError):
            continue
        out.append({"time": ts, "camera": record.get("camera_display"), "label": record.get("label"),
                    "text": record.get("description", "")})
        if len(out) >= limit:
            break
    return out


def object_log_records(log_path: str = "sentinel_events.log") -> List[Dict[str, Any]]:
    """Every 'object_detected' record in the current and rotated event logs, oldest first."""
    out = []
    for path in (log_path + ".1", log_path):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"object_detected"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                        record["ts"] = time.mktime(time.strptime(record["timestamp"], "%Y-%m-%dT%H:%M:%S"))
                    except (ValueError, KeyError):
                        continue
                    out.append(record)
        except OSError:
            continue
    return sorted(out, key=lambda r: r["ts"])


def recent_events(log_path: str = "sentinel_events.log", limit: int = 25) -> List[Dict[str, Any]]:
    """Last ``limit`` meaningful events (already credential-redacted by the logger)."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    keep = ("timestamp", "level", "event", "camera_display", "camera_ip", "error_message", "ip", "deleted")
    events = []
    for line in tail:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") in _NOISY_EVENTS:
            continue
        event = {k: record[k] for k in keep if k in record and record[k] is not None}
        event["label"] = EVENT_LABELS.get(record.get("event", ""), record.get("event", "").replace("_", " "))
        events.append(event)
    return list(reversed(events[-limit:]))


def disk_status(path: str = VIDEO_DIR) -> Dict[str, Any]:
    usage = shutil.disk_usage(path)
    return {
        "total_gb": round(usage.total / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
    }
