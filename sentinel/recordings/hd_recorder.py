"""
High-resolution recording with pre-roll.

ffmpeg copies a camera's main stream (no re-encoding, ~1% CPU) into short
Matroska segments in a RAM-backed ring buffer. (MPEG-TS segments from these
cameras crash the bundled static ffmpeg when read back; Matroska is fine.) When a motion clip finishes, the
segments covering [start - pre-roll, end + post-roll] are joined into an MP4
that browsers and Telegram play directly.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.logging_utils import app_logger, redact
from sentinel.recordings.store import find_ffmpeg

SEGMENT_SECONDS = 2
BUFFER_SECONDS = 90
PRE_ROLL_SECONDS = 5.0
POST_ROLL_SECONDS = 3.0
_SEGMENT_RE = re.compile(r"^(\d{14})\.mkv$")


def main_stream_url(sub_url: str) -> Optional[str]:
    """Best-guess main-stream URL for common substream paths."""
    for sub, main in (("/sub/", "/main/"), ("subtype=1", "subtype=0"), ("/102", "/101"), ("stream2", "stream1")):
        if sub in sub_url:
            return sub_url.replace(sub, main, 1)
    return None


def default_buffer_root() -> str:
    return "/dev/shm/sentinel-buffer" if os.path.isdir("/dev/shm") else os.path.join(
        tempfile.gettempdir(), "sentinel-buffer"
    )


class HDRecorder:
    """One camera's main-stream ring buffer plus clip assembly."""

    def __init__(self, url: str, name: str, buffer_root: Optional[str] = None) -> None:
        self.url = url
        self.name = name
        self.buffer_dir = os.path.join(buffer_root or default_buffer_root(), re.sub(r"[^A-Za-z0-9.-]", "_", name))
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # (start_ts, end_ts, out_path, callback)
        self._pending: List[Tuple[float, float, str, Callable[[Optional[str]], None]]] = []
        self.running = False
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        shutil.rmtree(self.buffer_dir, ignore_errors=True)
        os.makedirs(self.buffer_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name=f"hd-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_ffmpeg()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        # Clips still waiting get whatever is buffered now.
        with self._lock:
            pending, self._pending = self._pending, []
        for start_ts, end_ts, out_path, callback in pending:
            callback(self._assemble(start_ts, end_ts, out_path))
        shutil.rmtree(self.buffer_dir, ignore_errors=True)

    def request_clip(
        self, start_ts: float, end_ts: float, out_path: str, callback: Callable[[Optional[str]], None]
    ) -> bool:
        """Build an MP4 of [start - pre-roll, end + post-roll]; calls back with its path or None."""
        if not self.running:
            return False
        with self._lock:
            self._pending.append((start_ts - PRE_ROLL_SECONDS, end_ts + POST_ROLL_SECONDS, out_path, callback))
        return True

    def keyframe_near(self, ts: float, max_gap: float = SEGMENT_SECONDS, wait: bool = True) -> Optional[np.ndarray]:
        """Full-resolution frame from about ``ts``, or None.

        Every segment starts on a keyframe, so the first frame of the segment that
        starts nearest ``ts`` decodes cheaply (~0.25 s for 1440p) and is at most about
        one segment off; callers re-find moving objects in it. ffmpeg only writes a
        segment out when it closes, so with ``wait`` this blocks until the segment
        holding ``ts`` is on disk (up to ~2.5 s); call it from a background thread.
        """
        delay = ts + SEGMENT_SECONDS + 0.5 - time.time()
        if wait and 0 < delay <= SEGMENT_SECONDS + 1:
            time.sleep(delay)
        segments = self._segments()
        best: Optional[Tuple[float, str]] = None
        for i, (name_ts, path) in enumerate(segments):
            try:
                if os.path.getsize(path) == 0:
                    continue
                # Names have 1 s resolution; the previous segment's last write is a closer start time.
                start = os.path.getmtime(segments[i - 1][1]) if i > 0 else name_ts
            except OSError:
                continue
            gap = abs(start - ts)
            if gap <= max_gap and (best is None or gap < best[0]):
                best = (gap, path)
        if best is None:
            return None
        cap = cv2.VideoCapture(best[1], cv2.CAP_FFMPEG)
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        return frame if ok else None

    # ----------------------------------------------------------------- loop
    def _ffmpeg_cmd(self, ffmpeg: str) -> List[str]:
        return [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rtsp_transport", "tcp", "-timeout", "10000000",
            "-i", self.url,
            "-map", "0:v:0", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-segment_format", "matroska", "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(self.buffer_dir, "%Y%m%d%H%M%S.mkv"),
        ]

    def _run(self) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.last_error = "ffmpeg not available"
            return
        backoff = 5.0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._proc = subprocess.Popen(
                    self._ffmpeg_cmd(ffmpeg),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
            except OSError as e:
                self.last_error = str(e)
                self._stop.wait(backoff)
                continue
            self.running = True
            while not self._stop.is_set() and self._proc.poll() is None:
                self._service()
                self._stop.wait(0.5)
            self.running = False
            if self._stop.is_set():
                break
            err = (self._proc.stderr.read() or b"").decode("utf-8", "replace").strip()[-300:]
            self.last_error = redact(err) or f"ffmpeg exited ({self._proc.returncode})"
            app_logger.log("error", "hd_recorder_restart", camera_display=self.name,
                           error_message=self.last_error, component="hd_recorder", action="run")
            backoff = 5.0 if time.time() - started > 60 else min(120.0, backoff * 2)
            self._stop.wait(backoff)

    def _kill_ffmpeg(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()  # ffmpeg may ignore this while blocked on the network
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _segments(self) -> List[Tuple[float, str]]:
        """(start_ts, path) for complete-or-current segments, oldest first."""
        out = []
        try:
            names = os.listdir(self.buffer_dir)
        except OSError:
            return out
        for name in names:
            m = _SEGMENT_RE.match(name)
            if m:
                ts = time.mktime(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
                out.append((ts, os.path.join(self.buffer_dir, name)))
        return sorted(out)

    def _service(self) -> None:
        now = time.time()
        # Assemble clips whose post-roll has been fully written to disk.
        with self._lock:
            ready = [p for p in self._pending if now >= p[1] + SEGMENT_SECONDS + 1]
            self._pending = [p for p in self._pending if p not in ready]
            oldest_needed = min((p[0] for p in self._pending), default=now)
        for start_ts, end_ts, out_path, callback in ready:
            try:
                callback(self._assemble(start_ts, end_ts, out_path))
            except Exception as e:  # never let a callback kill the recorder
                app_logger.log("error", "hd_clip_callback_failed", error_message=str(e),
                               component="hd_recorder", action="service")
        # Drop segments older than the buffer window (unless a pending clip needs them).
        cutoff = min(now - BUFFER_SECONDS, oldest_needed - SEGMENT_SECONDS * 2)
        for ts, path in self._segments():
            if ts < cutoff:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _assemble(self, start_ts: float, end_ts: float, out_path: str) -> Optional[str]:
        segments = self._segments()
        chosen = [
            path for i, (ts, path) in enumerate(segments)
            # segment i covers [ts, next_ts); keep it if that overlaps [start, end]
            if ts <= end_ts and (segments[i + 1][0] if i + 1 < len(segments) else ts + SEGMENT_SECONDS) > start_ts
        ]
        # The newest segment may still be open for writing; leave it out unless it's all we have.
        if len(chosen) > 1 and chosen[-1] == segments[-1][1] and self.running:
            chosen = chosen[:-1]
        if not chosen:
            return None
        ffmpeg = find_ffmpeg()
        list_fd, list_path = tempfile.mkstemp(suffix=".txt", dir=self.buffer_dir)
        try:
            with os.fdopen(list_fd, "w") as handle:
                for path in chosen:
                    handle.write(f"file '{path}'\n")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            tmp = out_path + ".part"
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-f", "concat", "-safe", "0", "-i", list_path,
                 "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
                check=True, timeout=60, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            os.replace(tmp, out_path)
            return out_path
        except (subprocess.SubprocessError, OSError) as e:
            detail = getattr(e, "stderr", None)
            detail = detail.decode("utf-8", "replace").strip()[-300:] if isinstance(detail, bytes) else str(e)[:300]
            app_logger.log("error", "hd_clip_failed", camera_display=self.name, error_message=redact(detail),
                           component="hd_recorder", action="assemble")
            return None
        finally:
            try:
                os.remove(list_path)
            except OSError:
                pass
