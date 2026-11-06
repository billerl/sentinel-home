"""
The TV remote's screen mirror: what a Fire TV shows, live on the phone, so the menus can be read from the couch.

While someone has the remote open, the Fire TV records its own screen with its hardware video encoder
(``FireTVDriver.mirror_start``; it only sends frames when the picture changes) and Sentinel fetches what's new
a few times a second over the same ADB connection as the key presses. The bundled ffmpeg turns that H.264 into
JPEG pictures, which the web page shows like a camera. Nobody watching for IDLE_SECONDS: everything stops.
Streaming apps (Netflix, Prime Video) show black: their video is copy-protected.
"""

import subprocess
import threading
import time
from typing import Callable, Optional, Tuple

from sentinel.logging_utils import app_logger

POLL_SECONDS = 0.25
IDLE_SECONDS = 15.0  # no one asked for a picture this long: stop recording on the TV
RETRY_SECONDS = 5.0
JPEG_START, JPEG_END = b"\xff\xd8", b"\xff\xd9"
# An H.264 "access unit delimiter": ffmpeg only knows a frame is complete when the next one starts, so on a still
# menu the newest picture would wait for the next change. Once the TV has sent nothing new for a poll (it writes
# whole frames), this marks the frame finished.
FRAME_DONE = b"\x00\x00\x00\x01\x09\xf0"


def split_jpegs(buffer: bytes) -> Tuple[list, bytes]:
    """Whole JPEGs at the front of ``buffer`` (ffmpeg's image2pipe output) and the unfinished rest.
    (Inside a JPEG's data every 0xFF byte is followed by 0x00, so FF D9 only appears at the end.)"""
    frames = []
    while True:
        start = buffer.find(JPEG_START)
        if start < 0:
            return frames, b""
        end = buffer.find(JPEG_END, start + 2)
        if end < 0:
            return frames, buffer[start:]
        frames.append(buffer[start:end + 2])
        buffer = buffer[end + 2:]


class TVMirror:
    """One Fire TV's screen. ``latest()`` marks it watched and returns (jpeg, number of the picture)."""

    def __init__(self, driver, host: str, ffmpeg: Optional[str], name: str = "TV",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.driver = driver
        self.host = host
        self.ffmpeg = ffmpeg
        self.name = name
        self.clock = clock
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self._count = 0
        self._wanted = 0.0
        self._thread: Optional[threading.Thread] = None
        self.error = ""

    def latest(self) -> Tuple[Optional[bytes], int]:
        with self._lock:
            self._wanted = self.clock()
            if self.ffmpeg and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name=f"tv-mirror-{self.host}", daemon=True)
                self._thread.start()
            return self._jpeg, self._count

    def _watched(self) -> bool:
        with self._lock:
            return self.clock() - self._wanted < IDLE_SECONDS

    def _picture(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._count += 1

    def _decoder(self) -> subprocess.Popen:
        proc = subprocess.Popen([self.ffmpeg, "-hide_banner", "-loglevel", "error",
                                 # One thread each way and no frame-rate conversion: otherwise ffmpeg holds back the
                                 # last ~6 pictures. (Not -fflags nobuffer: that gives no output at all.)
                                 "-flags", "low_delay", "-threads", "1", "-probesize", "32", "-analyzeduration", "0",
                                 "-f", "h264", "-i", "-", "-fps_mode", "passthrough", "-threads", "1",
                                 "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6", "-"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def read() -> None:
            rest = b""
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return
                frames, rest = split_jpegs(rest + chunk)
                if frames:
                    self._picture(frames[-1])

        threading.Thread(target=read, name=f"tv-mirror-jpeg-{self.host}", daemon=True).start()
        return proc

    def _run(self) -> None:
        app_logger.log("info", "tv_mirror_started", tv=self.name, component="tv_mirror", action="start")
        proc = None
        try:
            while self._watched():
                try:
                    proc = self._decoder()
                    self._record(proc)
                except (OSError, ValueError) as e:
                    self.error = str(e)[:200]
                    app_logger.log("warning", "tv_mirror_failed", tv=self.name, error_message=self.error,
                                   component="tv_mirror", action="record")
                    time.sleep(RETRY_SECONDS)
                finally:
                    if proc is not None:
                        proc.kill()
                        proc.wait()
                        proc = None
        finally:
            with self._lock:
                self._jpeg = None  # next time, never an old picture first
            try:
                self.driver.mirror_stop(self.host)
            except (OSError, ValueError):
                pass
            app_logger.log("info", "tv_mirror_stopped", tv=self.name, component="tv_mirror", action="stop")

    def _record(self, proc: subprocess.Popen) -> None:
        """One recording on the TV, fed to ffmpeg until it runs out or nobody is watching."""
        from sentinel.engine.fire_tv import MIRROR_SECONDS

        self.driver.mirror_start(self.host)
        self.error = ""
        started, offset, unfinished = self.clock(), 0, False
        while self._watched() and self.clock() - started < MIRROR_SECONDS:
            data = self.driver.mirror_read(self.host, offset)
            if data:
                offset += len(data)
                proc.stdin.write(data)
                unfinished = True
            elif unfinished:
                proc.stdin.write(FRAME_DONE)
                unfinished = False
            proc.stdin.flush()
            time.sleep(POLL_SECONDS)
