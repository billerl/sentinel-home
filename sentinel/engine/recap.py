"""
Morning recap: at a set time (7:00 by default) one Telegram message about the night ("Overnight 9 pm – 7 am:
a deer in the Back Yard at 2:14 AM; 5 cars drove past; Stephanie's Edge got home at 1:45 AM ...") and a
highlight video of up to HIGHLIGHT_MAX moments, a few seconds each, captioned with the time and camera.
Also on request: "recap" or "what happened last night?" in Telegram.

The text comes from the events table (the same sightings as the alerts and the timeline); the video is cut
from the motion clips with OpenCV and encoded by the bundled ffmpeg (H.264, so it plays inside Telegram).
"""

import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.logging_utils import app_logger

HIGHLIGHT_MAX = 8
CLIP_BEFORE, CLIP_AFTER = 1.0, 3.0  # seconds around each moment
VIDEO_WIDTH = 960
# "What happened last night?" already has its own answer (the visits, with photos): the recap is asked for by name.
RECAP = re.compile(r"^(?:(?:the |my )?(?:morning |night |overnight )?recap|overnight recap)\??$", re.IGNORECASE)
PASSING = re.compile(r"\b(?:driving|walking) past\b")
# What makes the video, best first (a car driving past is only counted).
PRIORITY = {"person": 0, "delivery": 1, "animal": 2, "vehicle": 3}  # (packages are snapshots, not clips)


def _clock(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d} {'AM' if t.tm_hour < 12 else 'PM'}"


def _short(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour % 12 or 12}{'' if not t.tm_min else f':{t.tm_min:02d}'} {'am' if t.tm_hour < 12 else 'pm'}"


def window(now: float, since: str, until: str) -> Tuple[float, float]:
    """(start, end) of the night ending today at ``until`` ("07:00"), starting yesterday at ``since`` ("21:00")."""
    def at(day_ts: float, hhmm: str) -> float:
        h, m = (int(x) for x in hhmm.split(":"))
        t = time.localtime(day_ts)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))

    end = at(now, until)
    if end > now + 60:
        end = at(now - 86400, until)
    start = at(end, since)  # the last ``since`` before the end: last evening, or this evening when asked late
    if start >= end:
        start = at(end - 86400, since)
    return start, min(end, now)


def summary(events: List[Dict[str, Any]], start: float, end: float, extra: List[str]) -> str:
    """The message: one line per kind of thing that happened, most interesting first."""
    lines = [f"🌅 Overnight, {_short(start)} – {_short(end)}"]
    people = [e for e in events if e["grp"] == "person"]
    animals = [e for e in events if e["grp"] == "animal"]
    deliveries = [e for e in events if e["grp"] in ("delivery", "package")]
    vehicles = [e for e in events if e["grp"] == "vehicle"]
    family = [e for e in events if e["grp"] == "household"]
    passing = [e for e in vehicles if PASSING.search(e["description"])]  # only counted
    stopping = [e for e in vehicles if e not in passing]

    def listed(items: List[Dict[str, Any]], limit: int = 4) -> List[str]:
        out, seen = [], set()
        for e in items:
            text = re.sub(r"[.!]$", "", e["description"])
            key = (text, int(e["ts"] // 600))  # the same sentence within 10 minutes: once
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{text} at {_clock(e['ts'])}")
        more = len(out) - limit
        return out[:limit] + ([f"…and {more} more"] if more > 0 else [])

    for items in (people, deliveries, animals):
        lines += listed(items)
    if stopping:
        lines += listed(stopping, 3)
    if passing:
        lines.append(f"🚗 {len(passing)} {'car' if len(passing) == 1 else 'cars'} drove past")
    lines += listed(family, 6)
    if len(lines) == 1:
        lines.append("A quiet night: nothing came by.")
    lines += extra
    return "\n".join(lines)


def highlights(events: List[Dict[str, Any]], limit: int = HIGHLIGHT_MAX) -> List[Dict[str, Any]]:
    """The moments worth seeing: people, deliveries, animals, then cars that stopped or left, in time order."""
    wanted = [e for e in events if e["grp"] in PRIORITY
              and not (e["grp"] == "vehicle" and PASSING.search(e["description"]))]
    wanted.sort(key=lambda e: (PRIORITY[e["grp"]], e["ts"]))
    picked: List[Dict[str, Any]] = []
    for e in wanted:
        if all(abs(e["ts"] - p["ts"]) > 120 or e["camera_key"] != p["camera_key"] for p in picked):
            picked.append(e)
        if len(picked) >= limit:
            break
    return sorted(picked, key=lambda e: e["ts"])


def _caption(frame: np.ndarray, text: str) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = w / 960.0
    cv2.rectangle(frame, (0, h - int(44 * scale)), (w, h), (0, 0, 0), -1)
    cv2.putText(frame, text, (int(14 * scale), h - int(14 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.75 * scale,
                (255, 255, 255), max(1, int(2 * scale)), cv2.LINE_AA)
    return frame


def make_video(moments: List[Tuple[str, float, str]], out_path: str, ffmpeg: str, fps: float = 12.0) -> bool:
    """moments: (clip path, clip start time, caption) -> one H.264 MP4. False if nothing could be cut."""
    width, height = VIDEO_WIDTH, VIDEO_WIDTH * 9 // 16
    proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset",
                             "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    written = 0
    try:
        for path, at, caption in moments:
            cap = cv2.VideoCapture(path)
            clip_fps = cap.get(cv2.CAP_PROP_FPS) or fps
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            first = max(0, int((at - CLIP_BEFORE) * clip_fps))
            if frames and first >= frames:
                first = max(0, frames - int((CLIP_BEFORE + CLIP_AFTER) * clip_fps))
            cap.set(cv2.CAP_PROP_POS_FRAMES, first)
            step = clip_fps / fps
            for k in range(int((CLIP_BEFORE + CLIP_AFTER) * fps)):
                cap.set(cv2.CAP_PROP_POS_FRAMES, first + int(k * step)) if step != 1 else None
                ok, frame = cap.read()
                if not ok:
                    break
                proc.stdin.write(_caption(cv2.resize(frame, (width, height)), caption).tobytes())
                written += 1
            cap.release()
        proc.stdin.close()
        proc.wait(timeout=120)
    except (OSError, subprocess.SubprocessError, ValueError):
        proc.kill()
        return False
    return written > 0 and proc.returncode == 0 and os.path.isfile(out_path)


class Recap:
    def __init__(self, settings: Dict[str, Any], store, extra, send_text, send_video, state_path: str) -> None:
        self.settings = settings
        self.store = store  # RecordingStore: events() and recordings
        self.extra = extra  # () -> extra lines (packages waiting, weather today, warnings in effect)
        self.send_text = send_text  # (chat or None for everyone, text)
        self.send_video = send_video  # (chat or None, path, caption)
        self.state_path = state_path
        self._busy = False

    def _last_day(self) -> str:
        try:
            with open(self.state_path) as f:
                return f.read().strip()
        except OSError:
            return ""

    def due(self, now: float) -> bool:
        """Once a day, at the set time (and not again after a restart)."""
        if not self.settings.get("recap_enabled", True) or self._busy:
            return False
        hh, mm = (int(x) for x in str(self.settings.get("recap_time", "07:00")).split(":"))
        t = time.localtime(now)
        if (t.tm_hour, t.tm_min) < (hh, mm) or t.tm_hour > hh + 2:
            return False  # before the time, or Sentinel was down all morning: skip today
        return self._last_day() != time.strftime("%Y-%m-%d", t)

    def run(self, chat: Optional[str] = None, now: Optional[float] = None) -> None:
        """Build and send the recap (to one chat on request, or to everyone at the set time)."""
        now = now or time.time()
        self._busy = True
        try:
            if chat is None:
                try:
                    with open(self.state_path, "w") as f:
                        f.write(time.strftime("%Y-%m-%d", time.localtime(now)))
                except OSError:
                    pass
            start, end = window(now, str(self.settings.get("recap_since", "21:00")),
                                str(self.settings.get("recap_time", "07:00")) if chat is None
                                else time.strftime("%H:%M", time.localtime(now)))
            events = self.store.events(start, end)
            self.send_text(chat, summary(events, start, end, self.extra()))
            moments = self._moments(highlights(events))
            if moments:
                self._send_video(chat, moments)
            app_logger.log("info", "recap_sent", events=len(events), clips=len(moments), on_request=chat is not None,
                           component="recap", action="send")
        except Exception as e:
            app_logger.log("error", "recap_failed", error_message=str(e)[:200], component="recap", action="send")
        finally:
            self._busy = False

    def _moments(self, picked: List[Dict[str, Any]]) -> List[Tuple[str, float, str]]:
        out = []
        for e in picked:
            clips = [r for r in self.store.query(e["ts"] - 120, e["ts"] + 5)
                     if r["camera_key"] == e["camera_key"] and r["start_ts"] - 2 <= e["ts"] <= r["end_ts"] + 2]
            path = self.store.absolute_path(clips[-1]) if clips else None
            if path and os.path.isfile(path):
                text = re.sub(r"^\W+\s*", "", e["description"]).rstrip(".")
                out.append((path, e["ts"] - clips[-1]["start_ts"], f"{_clock(e['ts'])}  {e['camera_name']}: {text}"))
        return out

    def _send_video(self, chat: Optional[str], moments: List[Tuple[str, float, str]]) -> None:
        from sentinel.recordings.store import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        fd, path = tempfile.mkstemp(prefix="sentinel-recap-", suffix=".mp4")
        os.close(fd)
        try:
            if make_video(moments, path, ffmpeg):
                seconds = int(len(moments) * (CLIP_BEFORE + CLIP_AFTER))
                self.send_video(chat, path, f"🎬 Overnight highlights ({len(moments)} moments, {seconds} s)")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
