"""
Recording index, retention, and browser playback for Sentinel Home.

Clips are stored as ``<root>/<YYYY-MM-DD>/<camera>_<HHMMSS>_<ms>.avi`` and indexed
in a small SQLite database so the web timeline (and Telegram) can query by time
without scanning tens of thousands of files.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

VIDEO_EXTENSIONS = (".avi", ".mp4")
LEGACY_NAME_RE = re.compile(r"^motion_(\d{8})_(\d{6})\.(avi|mp4)$")
LEGACY_CLIP_SECONDS = 10.0
UNKNOWN_CAMERA_KEY = "unknown"
UNKNOWN_CAMERA_NAME = "Unknown camera"
WEB_CACHE_DIRNAME = ".web_cache"
WEB_CACHE_MAX_AGE_SECONDS = 2 * 86400
DB_FILENAME = "recordings.db"
# The low-disk safety net never deletes clips newer than this.
LOW_DISK_KEEP_SECONDS = 24 * 3600


def camera_slug(value: str) -> str:
    """Filesystem-safe camera name, e.g. '192.168.0.10' -> '192-168-0-10'."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").lower()
    return slug[:40] or UNKNOWN_CAMERA_KEY


def local_day_bounds(date_str: str) -> Tuple[float, float]:
    """Epoch seconds for local midnight of ``YYYY-MM-DD`` and the next midnight."""
    day = time.strptime(date_str, "%Y-%m-%d")
    start = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    end = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return start, end


def find_ffmpeg() -> Optional[str]:
    """Bundled ffmpeg from imageio-ffmpeg, else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


class RecordingStore:
    """Thread-safe recording index plus retention and web transcoding."""

    def __init__(self, root: str, db_path: Optional[str] = None):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = db_path or os.path.join(self.root, DB_FILENAME)
        self.cache_dir = os.path.join(self.root, WEB_CACHE_DIRNAME)
        self._lock = threading.Lock()
        self._transcode_locks: Dict[int, threading.Lock] = {}
        self._transcode_slots = threading.Semaphore(2)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._init_db()

    # ----------------------------------------------------------------- database
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    camera_key TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'motion',
                    size_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recordings_start ON recordings(start_ts)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
            if "hd_path" not in columns:
                # High-res MP4 from the main stream (see hd_recorder.py), when available.
                conn.execute("ALTER TABLE recordings ADD COLUMN hd_path TEXT")

    def _abs(self, rel_path: str) -> str:
        return os.path.join(self.root, rel_path)

    def _rel(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self.root)

    def _inside_root(self, path: str) -> bool:
        real = os.path.realpath(path)
        return real.startswith(os.path.realpath(self.root) + os.sep)

    # ---------------------------------------------------------------- recording
    def new_clip_path(self, camera_key: str, start_ts: Optional[float] = None) -> str:
        """Absolute path for a new clip; creates the day folder."""
        ts = time.time() if start_ts is None else start_ts
        local = time.localtime(ts)
        day_dir = os.path.join(self.root, time.strftime("%Y-%m-%d", local))
        os.makedirs(day_dir, exist_ok=True)
        millis = int((ts % 1) * 1000)
        name = f"{camera_slug(camera_key)}_{time.strftime('%H%M%S', local)}_{millis:03d}.avi"
        return os.path.join(day_dir, name)

    def add_recording(
        self,
        path: str,
        camera_key: str,
        camera_name: str,
        start_ts: float,
        end_ts: float,
        reason: str = "motion",
    ) -> Optional[int]:
        """Index a completed clip; returns its id."""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO recordings
                    (path, camera_key, camera_name, start_ts, end_ts, reason, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self._rel(path), camera_key, camera_name, start_ts, end_ts, reason, size),
            )
            return cur.lastrowid

    def attach_hd(self, recording_id: int, hd_path: str) -> None:
        """Record the high-res MP4 that goes with a clip."""
        try:
            size = os.path.getsize(hd_path)
        except OSError:
            size = 0
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE recordings SET hd_path = ?, size_bytes = size_bytes + ? WHERE id = ?",
                (self._rel(hd_path), size, recording_id),
            )

    def hd_absolute_path(self, record: Dict[str, Any]) -> Optional[str]:
        rel = record.get("hd_path")
        if not rel:
            return None
        path = self._abs(rel)
        if not self._inside_root(path) or not os.path.isfile(path):
            return None
        return path

    def query(self, start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        """Clips that start within [start_ts, end_ts), oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, path, camera_key, camera_name, start_ts, end_ts, reason, hd_path
                FROM recordings WHERE start_ts >= ? AND start_ts < ?
                ORDER BY start_ts
                """,
                (start_ts, end_ts),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, recording_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        return dict(row) if row else None

    def absolute_path(self, record: Dict[str, Any]) -> Optional[str]:
        """Absolute clip path, or None if it escapes the root or is missing."""
        path = self._abs(record["path"])
        if not self._inside_root(path) or not os.path.isfile(path):
            return None
        return path

    def stats(self) -> Dict[str, Any]:
        """Totals for the status page: clip count, size, today's clips, oldest clip."""
        day_start, day_end = local_day_bounds(time.strftime("%Y-%m-%d"))
        with self._connect() as conn:
            total, size, oldest = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), MIN(start_ts) FROM recordings"
            ).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) FROM recordings WHERE start_ts >= ? AND start_ts < ?",
                (day_start, day_end),
            ).fetchone()[0]
        return {
            "clips": total,
            "size_gb": round(size / 1e9, 2),
            "today": today,
            "oldest": time.strftime("%Y-%m-%d", time.localtime(oldest)) if oldest else None,
        }

    # --------------------------------------------------------- legacy backfill
    def backfill_legacy(self) -> int:
        """Index old flat ``motion_YYYYmmdd_HHMMSS.avi`` clips (camera unknown)."""
        with self._connect() as conn:
            known = {row[0] for row in conn.execute("SELECT path FROM recordings")}
        rows = []
        with os.scandir(self.root) as entries:
            for entry in entries:
                match = LEGACY_NAME_RE.match(entry.name)
                if not match or not entry.is_file(follow_symlinks=False):
                    continue
                if entry.name in known:
                    continue
                try:
                    start = time.mktime(
                        time.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
                    )
                    size = entry.stat().st_size
                except (ValueError, OSError):
                    continue
                rows.append(
                    (
                        entry.name,
                        UNKNOWN_CAMERA_KEY,
                        UNKNOWN_CAMERA_NAME,
                        start,
                        start + LEGACY_CLIP_SECONDS,
                        "motion",
                        size,
                    )
                )
        if rows:
            with self._lock, self._connect() as conn:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO recordings
                        (path, camera_key, camera_name, start_ts, end_ts, reason, size_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return len(rows)

    # ---------------------------------------------------------------- retention
    def _delete_file(self, path: str) -> int:
        try:
            if not self._inside_root(path) or os.path.islink(path):
                return 0
            size = os.path.getsize(path)
            os.remove(path)
            return size
        except OSError:
            return 0

    def _delete_records(self, records: List[sqlite3.Row]) -> Tuple[int, int]:
        deleted, freed = 0, 0
        for record in records:
            freed += self._delete_file(self._abs(record["path"]))
            if record["hd_path"]:
                freed += self._delete_file(self._abs(record["hd_path"]))
            self._delete_file(os.path.join(self.cache_dir, f"{record['id']}.mp4"))
            deleted += 1
        with self._lock, self._connect() as conn:
            conn.executemany(
                "DELETE FROM recordings WHERE id = ?", [(r["id"],) for r in records]
            )
        return deleted, freed

    def _free_percent(self) -> float:
        usage = shutil.disk_usage(self.root)
        return 100.0 * usage.free / usage.total if usage.total else 100.0

    def _remove_empty_day_dirs(self) -> None:
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and re.match(
                    r"^\d{4}-\d{2}-\d{2}$", entry.name
                ):
                    try:
                        os.rmdir(entry.path)
                    except OSError:
                        pass  # not empty

    def _prune_web_cache(self) -> None:
        if not os.path.isdir(self.cache_dir):
            return
        cutoff = time.time() - WEB_CACHE_MAX_AGE_SECONDS
        with os.scandir(self.cache_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                        os.remove(entry.path)
                except OSError:
                    pass

    def apply_retention(
        self,
        retention_days: float,
        min_free_percent: float = 0.0,
        free_percent_fn: Optional[Callable[[], float]] = None,
    ) -> Dict[str, int]:
        """Delete clips older than ``retention_days``; then, if free space is below
        ``min_free_percent``, delete the oldest clips until it recovers (always
        keeping the last 24 hours)."""
        free_percent_fn = free_percent_fn or self._free_percent
        deleted, freed = 0, 0

        if retention_days and retention_days > 0:
            cutoff = time.time() - float(retention_days) * 86400
            with self._connect() as conn:
                old = conn.execute(
                    "SELECT id, path, hd_path FROM recordings WHERE start_ts < ?", (cutoff,)
                ).fetchall()
            d, f = self._delete_records(old)
            deleted, freed = deleted + d, freed + f

            # Unindexed stray clips (e.g. from a crash mid-recording).
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [n for n in dirnames if n != WEB_CACHE_DIRNAME]
                for name in filenames:
                    if not name.lower().endswith(VIDEO_EXTENSIONS):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        if os.path.getmtime(path) < cutoff:
                            freed += self._delete_file(path)
                            deleted += 1
                    except OSError:
                        pass

        if min_free_percent and min_free_percent > 0:
            keep_after = time.time() - LOW_DISK_KEEP_SECONDS
            while free_percent_fn() < min_free_percent:
                with self._connect() as conn:
                    oldest = conn.execute(
                        "SELECT id, path, hd_path FROM recordings WHERE start_ts < ? "
                        "ORDER BY start_ts LIMIT 200",
                        (keep_after,),
                    ).fetchall()
                if not oldest:
                    break
                d, f = self._delete_records(oldest)
                deleted, freed = deleted + d, freed + f

        self._remove_empty_day_dirs()
        self._prune_web_cache()
        return {"deleted": deleted, "freed_bytes": freed}

    def run_maintenance(self, settings: Dict[str, Any]) -> None:
        """Backfill legacy clips and apply retention per current settings."""
        try:
            added = self.backfill_legacy()
            result = {"deleted": 0, "freed_bytes": 0}
            if settings.get("recording_retention_enabled", True):
                result = self.apply_retention(
                    float(settings.get("recording_retention_days", 30)),
                    float(settings.get("recording_min_free_percent", 10)),
                )
            if added or result["deleted"]:
                app_logger.log(
                    "info",
                    "recording_maintenance",
                    legacy_indexed=added,
                    deleted=result["deleted"],
                    freed_mb=round(result["freed_bytes"] / 1e6, 1),
                    component="recording_store",
                    action="run_maintenance",
                )
        except Exception as e:
            app_logger.log(
                "error",
                "recording_maintenance_failed",
                error_message=str(e),
                component="recording_store",
                action="run_maintenance",
            )

    def start_maintenance(
        self, settings_getter: Callable[[], Dict[str, Any]], interval_seconds: float = 3600
    ) -> None:
        """Run maintenance now and then every ``interval_seconds`` in a daemon thread."""
        if self._worker and self._worker.is_alive():
            return

        def _loop() -> None:
            while not self._stop.is_set():
                self.run_maintenance(settings_getter())
                self._stop.wait(interval_seconds)

        self._worker = threading.Thread(target=_loop, name="sentinel-retention", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- playback
    def web_playable_path(self, recording_id: int) -> Optional[str]:
        """H.264 MP4 copy of a clip for browsers (cached), or None on failure."""
        record = self.get(recording_id)
        if not record:
            return None
        hd = self.hd_absolute_path(record)
        if hd:
            return hd  # already H.264 MP4: browsers play it as-is
        source = self.absolute_path(record)
        if not source:
            return None
        os.makedirs(self.cache_dir, exist_ok=True)
        target = os.path.join(self.cache_dir, f"{recording_id}.mp4")

        with self._lock:
            clip_lock = self._transcode_locks.setdefault(recording_id, threading.Lock())
        with clip_lock:
            if os.path.isfile(target) and os.path.getmtime(target) >= os.path.getmtime(source):
                return target
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                return None
            tmp = target + ".part"
            with self._transcode_slots:
                try:
                    subprocess.run(
                        [
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", source,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
                            "-f", "mp4", tmp,
                        ],
                        check=True,
                        timeout=120,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    os.replace(tmp, target)
                except (subprocess.SubprocessError, OSError) as e:
                    app_logger.log(
                        "error",
                        "recording_transcode_failed",
                        recording_id=recording_id,
                        error_message=str(e)[:500],
                        component="recording_store",
                        action="web_playable_path",
                    )
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    return None
        return target
