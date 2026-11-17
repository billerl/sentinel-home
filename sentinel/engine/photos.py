"""
Photos: the family's photos on the backup drive, in the web app (the Photos page).

The backup drive (Sentinel's USB drive, sentinel/engine/backup.py) also holds the family's own photo backups
(2026-11-17: "Backup of Photos", two laptops' backups). Two kinds of folder are shown:

- Shared folders: top-level folders on the drive that an admin picks (setting photos_shared_folders). Everyone
  with a login (not guests) can look; nobody can change them from here: they are the original backups.
- Albums: each person's own, in ALBUMS_DIR on the drive (ALBUMS_DIR/<owner>/<album>/). Their owner adds photos
  and chooses who sees each one: just them (private) or the family (shared). A private album is its owner's
  alone, whatever the role (like private notes).

Photos are read from the drive (the old PC is the backup's backup: the nightly copy goes drive -> old PC, so
nothing here ever writes there). Thumbnails and screen-sized copies are made once and kept on this computer's
own disk (CACHE_DIR), so scrolling doesn't read the USB drive each time. An index of the drive's photos
(photos.db) is made in the background (a few minutes for ~70,000 photos), so pages don't walk the drive.

Laptop backups hold program pictures too: system/program folders (SKIP_DIRS) and tiny images (icons) are left out.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

ALBUMS_DIR = "Family Photos"  # on the drive: everyone's albums (copied nightly to the old PC too)
NOT_SHAREABLE = {"Sentinel Backup", "$RECYCLE.BIN", "System Volume Information", ALBUMS_DIR}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".3gp", ".wmv", ".mpg", ".mpeg", ".mkv"}
PLAYS = {".mp4", ".m4v", ".mov"}  # what a browser plays itself (the rest: download)
MIN_PHOTO_BYTES = 30 * 1024  # smaller is an icon or a program's picture (laptop backups have thousands)
SKIP_DIRS = {"appdata", "application data", "local settings", "program files", "program files (x86)", "programdata",
             "windows", "$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__",
             "site-packages", "cache", "caches", ".cache", "temp", "tmp", "thumbnails", ".thumbnails", ".trash",
             "library", "$windows.~bt", "msocache", "intel", "drivers", "sentinel backup"}
THUMB, DISPLAY = 320, 2048
CACHE_DIR = ".photo_cache"
DISPLAY_CACHE_MAX = 3 * 1024 ** 3  # screen-sized copies kept (the oldest go first)
UPLOAD_MAX = 500 * 1024 * 1024  # one file (a phone video)
RESCAN_HOURS = 12
PAGE = 120
NAME = re.compile(r"^[\w][\w '&(),.!+-]{0,59}$")
EXFAT_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# What an upload is, by its first bytes (not its name)
_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"))


def upload_kind(head: bytes) -> Optional[str]:
    """The extension an uploaded file should have, from its first bytes; None when it isn't a photo or video."""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return ".mov"
        if brand.startswith(b"3gp"):
            return ".3gp"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return ".heic"  # (iPhones; phones usually send JPEG to a web page)
        return ".mp4"
    return None


def media_kind(name: str) -> Optional[str]:
    ext = os.path.splitext(name)[1].lower()
    return "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else None


def clean_rel(path: str) -> Optional[str]:
    """A path inside the drive, "/"-separated, or None if it tries to leave it."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


class Photos:
    def __init__(self, data_dir: str, settings: Dict[str, Any], drive: Callable[[], Optional[str]],
                 ffmpeg: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._drive = drive  # -> the backup drive's mount point, None when it isn't plugged in
        self._clock = clock
        self._ffmpeg = ffmpeg
        # full paths: Flask's send_file reads a relative one from the web package's folder, not the working one
        # (every thumbnail came back missing on the first day, 2026-11-17: the data folder is "recorded_videos")
        self.db_path = os.path.join(os.path.abspath(data_dir), "photos.db")
        self.cache = os.path.join(os.path.abspath(data_dir), CACHE_DIR)
        self._lock = threading.Lock()
        self._makers = threading.Semaphore(3)  # thumbnails made at once (the drive is one USB disk)
        self._scan_thread: Optional[threading.Thread] = None
        self.scanning = False
        self._drive_at: Tuple[float, Optional[str]] = (0.0, None)
        self._display_writes = 0
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS albums (id INTEGER PRIMARY KEY, owner INTEGER, owner_name TEXT NOT NULL,
                    name TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0, folder TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS used_names (album INTEGER NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
                    PRIMARY KEY (album, name));
                CREATE TABLE IF NOT EXISTS media (path TEXT PRIMARY KEY, dir TEXT NOT NULL, name TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime REAL NOT NULL, kind TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS media_dir ON media (dir, name);
                CREATE TABLE IF NOT EXISTS dirs (path TEXT PRIMARY KEY, parent TEXT, name TEXT NOT NULL,
                    n INTEGER NOT NULL, total INTEGER NOT NULL, cover TEXT);
                CREATE INDEX IF NOT EXISTS dirs_parent ON dirs (parent, name);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ the drive and what's shared
    def drive(self) -> Optional[str]:
        """The drive's mount point (asked at most every 30 s: finding it runs lsblk)."""
        at, mount = self._drive_at
        if self._clock() - at > 30:
            try:
                mount = self._drive()
            except Exception:
                mount = None
            self._drive_at = (self._clock(), mount)
        return mount if mount and os.path.isdir(mount) else None

    def shared_folders(self) -> List[str]:
        return [n for n in str(self.settings.get("photos_shared_folders") or "").split(",")
                if n and n not in NOT_SHAREABLE and "/" not in n and n != ".."]

    def folder_choices(self) -> List[str]:
        """Top-level folders on the drive an admin can share (Settings)."""
        root = self.drive()
        if root is None:
            return self.shared_folders()
        try:
            names = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))
                           and n not in NOT_SHAREABLE and not n.startswith("."))
        except OSError:
            return []
        return names

    # ------------------------------------------------------------------ who sees what
    @staticmethod
    def _visible(album: sqlite3.Row, person: Optional[int], household: bool) -> bool:
        if album["shared"]:
            return True
        return album["owner"] == person if album["owner"] is not None else household and person is None

    @staticmethod
    def _mine(album: sqlite3.Row, person: Optional[int], household: bool) -> bool:
        return album["owner"] == person if album["owner"] is not None else household and person is None

    def _albums(self, conn) -> List[sqlite3.Row]:
        return conn.execute("SELECT * FROM albums ORDER BY owner_name, name COLLATE NOCASE").fetchall()

    def _allowed(self, conn, rel: str, person: Optional[int], household: bool) -> bool:
        """May this viewer see the folder (or file) rel?"""
        top = rel.split("/")[0]
        if top == ALBUMS_DIR:
            album = next((a for a in self._albums(conn) if rel == a["folder"] or rel.startswith(a["folder"] + "/")),
                         None)
            return album is not None and self._visible(album, person, household)
        return top in self.shared_folders()

    # ------------------------------------------------------------------ the index
    def index_age(self) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'scanned'").fetchone()
        return self._clock() - float(row[0]) if row else None

    def rescan(self, wait: bool = False) -> bool:
        """Index the drive again in the background (the shared folders and the albums). False when it can't."""
        with self._lock:
            if self.scanning or self.drive() is None:
                return False
            self.scanning = True
            self._scan_thread = threading.Thread(target=self._scan, name="photo-index", daemon=True)
            self._scan_thread.start()
        if wait:
            self._scan_thread.join()
        return True

    def rescan_if_old(self) -> None:
        age = self.index_age()
        if age is None or age > RESCAN_HOURS * 3600:
            self.rescan()

    def _walk(self, root: str, top: str) -> List[Tuple[str, str, str, int, float, str]]:
        found = []
        base = os.path.join(root, *top.split("/"))
        album = top.split("/")[0] == ALBUMS_DIR  # (people's own photos: no icon filtering)
        for here, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if album or (d.lower() not in SKIP_DIRS and not d.startswith("."))]
            rel_dir = os.path.relpath(here, root).replace(os.sep, "/")
            for name in files:
                kind = media_kind(name)
                if kind is None or name.startswith("._"):  # (a Mac's shadow file)
                    continue
                try:
                    st = os.stat(os.path.join(here, name))
                except OSError:
                    continue
                if kind == "photo" and st.st_size < MIN_PHOTO_BYTES and not album:
                    continue
                found.append((f"{rel_dir}/{name}", rel_dir, name, st.st_size, st.st_mtime, kind))
        return found

    def _scan(self) -> None:
        started = time.monotonic()
        try:
            root = self.drive()
            if root is None:
                return
            tops = self.shared_folders() + [ALBUMS_DIR]
            media = []
            for top in tops:
                if os.path.isdir(os.path.join(root, top)):
                    media += self._walk(root, top)
            with self._connect() as conn:
                conn.execute("DELETE FROM media")
                conn.executemany("INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?)", media)
                self._rebuild_dirs(conn)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('scanned', ?)", (str(self._clock()),))
            app_logger.log("info", "photos_indexed", photos=len(media), seconds=round(time.monotonic() - started),
                           component="photos", action="index")
        except Exception as e:
            app_logger.log("warning", "photos_index_failed", error_message=str(e)[:200], component="photos",
                           action="index")
        finally:
            self.scanning = False

    @staticmethod
    def _rebuild_dirs(conn) -> None:
        """Folder counts (own photos, and with everything below) and a cover photo, from the media table."""
        own: Dict[str, int] = {}
        cover: Dict[str, str] = {}
        for d, n, first in conn.execute("SELECT dir, COUNT(*), MIN(path) FROM media GROUP BY dir"):
            own[d] = n
            cover[d] = first
        total: Dict[str, int] = {}
        for d, n in own.items():
            parts = d.split("/")
            for i in range(1, len(parts) + 1):
                p = "/".join(parts[:i])
                total[p] = total.get(p, 0) + n
                cover.setdefault(p, cover[d])
        conn.execute("DELETE FROM dirs")
        conn.executemany("INSERT INTO dirs VALUES (?, ?, ?, ?, ?, ?)",
                         [(p, p.rsplit("/", 1)[0] if "/" in p else None, p.rsplit("/", 1)[-1], own.get(p, 0), t,
                           cover.get(p)) for p, t in total.items()])

    def _reindex(self, folder: str) -> None:
        """One album again after a change (quick: no need to walk the whole drive)."""
        root = self.drive()
        if root is None:
            return
        found = self._walk(root, folder) if os.path.isdir(os.path.join(root, folder)) else []
        with self._connect() as conn:
            conn.execute("DELETE FROM media WHERE dir = ? OR dir LIKE ?", (folder, folder + "/%"))
            conn.executemany("INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?)", found)
            self._rebuild_dirs(conn)

    # ------------------------------------------------------------------ browsing
    def overview(self, person: Optional[int], household: bool) -> Dict[str, Any]:
        """The Photos page's first screen: the shared folders, then the albums this viewer can see."""
        root = self.drive()
        with self._connect() as conn:
            dirs = {r["path"]: r for r in conn.execute("SELECT * FROM dirs WHERE parent IS NULL OR parent = ?",
                                                      (ALBUMS_DIR,))}
            folders = [{"path": f, "name": f, "count": dirs[f]["total"] if f in dirs else 0,
                        "cover": dirs[f]["cover"] if f in dirs else None} for f in self.shared_folders()]
            albums = []
            for a in self._albums(conn):
                if not self._visible(a, person, household):
                    continue
                row = conn.execute("SELECT total, cover FROM dirs WHERE path = ?", (a["folder"],)).fetchone()
                albums.append({"id": a["id"], "name": a["name"], "owner": a["owner_name"], "shared": bool(a["shared"]),
                               "mine": self._mine(a, person, household), "path": a["folder"],
                               "count": row["total"] if row else 0, "cover": row["cover"] if row else None})
        return {"drive": root is not None, "scanning": self.scanning, "indexed": self.index_age() is not None,
                "folders": folders, "albums": albums}

    def listing(self, rel: str, person: Optional[int], household: bool, offset: int = 0) -> Optional[Dict[str, Any]]:
        """A folder: its subfolders and a page of its photos; None when this viewer may not see it."""
        rel = clean_rel(rel)
        if rel is None:
            return None
        with self._connect() as conn:
            if not self._allowed(conn, rel, person, household):
                return None
            subs = [{"path": r["path"], "name": r["name"], "count": r["total"], "cover": r["cover"]}
                    for r in conn.execute("SELECT * FROM dirs WHERE parent = ? AND total > 0 ORDER BY name COLLATE NOCASE",
                                          (rel,))]
            n = conn.execute("SELECT COUNT(*) FROM media WHERE dir = ?", (rel,)).fetchone()[0]
            items = [{"path": r["path"], "name": r["name"], "kind": r["kind"], "size": r["size"], "mtime": r["mtime"],
                      "plays": os.path.splitext(r["name"])[1].lower() in PLAYS}
                     for r in conn.execute("SELECT * FROM media WHERE dir = ? ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                                           (rel, PAGE, max(0, int(offset))))]
            album = next((a for a in self._albums(conn) if a["folder"] == rel), None)
        out = {"path": rel, "folders": subs, "items": items, "count": n, "offset": offset, "page": PAGE}
        if album is not None:
            out["album"] = {"id": album["id"], "name": album["name"], "shared": bool(album["shared"]),
                            "owner": album["owner_name"], "mine": self._mine(album, person, household)}
        return out

    def file_for(self, rel: str, person: Optional[int], household: bool) -> Optional[Tuple[str, sqlite3.Row]]:
        """(the file on the drive, its index row) when this viewer may see it; only files the index knows."""
        rel = clean_rel(rel)
        root = self.drive()
        if rel is None or root is None:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM media WHERE path = ?", (rel,)).fetchone()
            if row is None or not self._allowed(conn, rel, person, household):
                return None
        path = os.path.join(root, *rel.split("/"))
        real_root = os.path.realpath(root)
        if not os.path.realpath(path).startswith(real_root + os.sep) or not os.path.isfile(path):
            return None
        return path, row

    # ------------------------------------------------------------------ thumbnails and screen-sized copies
    def _cached(self, kind: str, row: sqlite3.Row) -> str:
        key = hashlib.sha1(f"{row['path']}|{row['size']}|{row['mtime']}".encode()).hexdigest()
        return os.path.join(self.cache, kind, key[:2], key + ".jpg")

    def thumbnail(self, path: str, row: sqlite3.Row) -> Optional[str]:
        out = self._cached("thumbs", row)
        if os.path.exists(out):
            return out
        with self._makers:
            if not os.path.exists(out):
                os.makedirs(os.path.dirname(out), exist_ok=True)
                ok = self._video_frame(path, out) if row["kind"] == "video" else self._resize(path, out, THUMB, 80)
                if not ok:
                    return None
        return out

    def display(self, path: str, row: sqlite3.Row) -> Optional[str]:
        """A photo sized for a screen (2048 px, the right way up): a 10 MB camera photo loads fast on a phone."""
        out = self._cached("display", row)
        if os.path.exists(out):
            return out
        with self._makers:
            if not os.path.exists(out):
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if not self._resize(path, out, DISPLAY, 85):
                    return None
                self._display_writes += 1
                if self._display_writes % 50 == 0:
                    self._trim_display()
        return out

    @staticmethod
    def _resize(src: str, out: str, size: int, quality: int) -> bool:
        try:
            from PIL import Image, ImageOps

            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((size, size))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                tmp = out + ".tmp"
                im.save(tmp, "JPEG", quality=quality)
            os.replace(tmp, out)
            return True
        except Exception:
            return False

    def _video_frame(self, src: str, out: str) -> bool:
        ffmpeg = self._ffmpeg
        if ffmpeg is None:
            try:
                import imageio_ffmpeg

                ffmpeg = self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                return False
        tmp = out + ".tmp.jpg"
        for at in ("1", "0"):  # (a clip shorter than a second)
            try:
                subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", at, "-i", src, "-frames:v", "1",
                                "-vf", f"scale={THUMB}:-2", tmp], capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                return False
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                return True
        return False

    def _trim_display(self) -> None:
        folder = os.path.join(self.cache, "display")
        files = []
        for here, _dirs, names in os.walk(folder):
            for n in names:
                p = os.path.join(here, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_atime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _at, size, p in sorted(files):
            if total <= DISPLAY_CACHE_MAX:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass

    # ------------------------------------------------------------------ albums
    def create_album(self, name: str, shared: bool, person: Optional[int], owner_name: str) -> Tuple[bool, str]:
        name = re.sub(r"\s+", " ", str(name or "")).strip()
        if not NAME.match(name) or EXFAT_BAD.search(name) or name.endswith("."):
            return False, "Give it a name (letters, numbers and spaces; up to 60)."
        root = self.drive()
        if root is None:
            return False, "The backup drive isn't plugged in."
        owner_dir = EXFAT_BAD.sub("", owner_name).strip(" .") or "Household"
        folder = f"{ALBUMS_DIR}/{owner_dir}/{name}"
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM albums WHERE folder = ? COLLATE NOCASE", (folder,)).fetchone():
                return False, "You already have an album with that name."
            try:
                os.makedirs(os.path.join(root, *folder.split("/")), exist_ok=True)
            except OSError as e:
                return False, f"The drive wouldn't take it ({e.strerror or e})."
            cur = conn.execute("INSERT INTO albums (owner, owner_name, name, shared, folder, created) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (person, owner_name, name, int(bool(shared)), folder,
                                                             self._clock()))
        app_logger.log("info", "photo_album_created", album=cur.lastrowid, shared=bool(shared), component="photos",
                       action="album")
        return True, str(cur.lastrowid)

    def _my_album(self, conn, album_id: int, person: Optional[int], household: bool) -> Optional[sqlite3.Row]:
        row = conn.execute("SELECT * FROM albums WHERE id = ?", (int(album_id),)).fetchone()
        return row if row is not None and self._mine(row, person, household) else None

    def set_shared(self, album_id: int, shared: bool, person: Optional[int], household: bool) -> Tuple[bool, str]:
        with self._connect() as conn:
            album = self._my_album(conn, album_id, person, household)
            if album is None:
                return False, "Only its owner can change that album."
            conn.execute("UPDATE albums SET shared = ? WHERE id = ?", (int(bool(shared)), album["id"]))
        return True, "Shared with the family." if shared else "Private: only you can see it."

    def delete_album(self, album_id: int, person: Optional[int], household: bool) -> Tuple[bool, str]:
        """The album and its photos, from the drive (the nightly copy on the old PC keeps them)."""
        root = self.drive()
        with self._connect() as conn:
            album = self._my_album(conn, album_id, person, household)
            if album is None:
                return False, "Only its owner can delete that album."
            if root is None:
                return False, "The backup drive isn't plugged in."
            shutil.rmtree(os.path.join(root, *album["folder"].split("/")), ignore_errors=True)
            conn.execute("DELETE FROM albums WHERE id = ?", (album["id"],))
            conn.execute("DELETE FROM used_names WHERE album = ?", (album["id"],))
        self._reindex(album["folder"])
        app_logger.log("info", "photo_album_deleted", album=album["id"], component="photos", action="album")
        return True, "Deleted."

    def upload(self, album_id: int, stream: Any, filename: str, person: Optional[int],
               household: bool) -> Tuple[bool, str]:
        """A photo or video (read from stream, straight to the drive) into one of this viewer's albums. Never
        overwrites, and never reuses a name the album has had (the nightly copy never replaces a file on the old
        PC, so a reused name would never get there)."""
        head = stream.read(16)
        if not head:
            return False, "That file is empty."
        ext = upload_kind(head)
        if ext is None:
            return False, "That isn't a photo or video."
        root = self.drive()
        if root is None:
            return False, "The backup drive isn't plugged in."
        with self._connect() as conn:
            album = self._my_album(conn, album_id, person, household)
            if album is None:
                return False, "You can only add photos to your own albums."
            stem = EXFAT_BAD.sub("", os.path.splitext(os.path.basename(str(filename or "").replace("\\", "/")))[0])
            stem = stem.strip(" .")[:100] or time.strftime("Photo %Y-%m-%d %H%M%S", time.localtime(self._clock()))
            folder = os.path.join(root, *album["folder"].split("/"))
            os.makedirs(folder, exist_ok=True)
            name, n = stem + ext, 1
            while (conn.execute("SELECT 1 FROM used_names WHERE album = ? AND name = ?", (album["id"], name)).fetchone()
                   or os.path.exists(os.path.join(folder, name))):
                n += 1
                name = f"{stem} ({n}){ext}"
            target = os.path.join(folder, name)
            written = len(head)
            try:
                with open(target + ".part", "wb") as f:
                    f.write(head)
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > UPLOAD_MAX:
                            raise ValueError("too big")
                        f.write(chunk)
                os.replace(target + ".part", target)
            except (OSError, ValueError) as e:
                try:
                    os.remove(target + ".part")
                except OSError:
                    pass
                if isinstance(e, ValueError):
                    return False, f"That file is too big (over {UPLOAD_MAX // (1024 * 1024)} MB)."
                return False, f"The drive wouldn't take it ({e.strerror or e})."
            conn.execute("INSERT OR IGNORE INTO used_names VALUES (?, ?)", (album["id"], name))
        self._reindex(album["folder"])
        app_logger.log("info", "photo_uploaded", album=album["id"], kind=media_kind(name), bytes=written,
                       component="photos", action="upload")
        return True, name

    def delete_photo(self, rel: str, person: Optional[int], household: bool) -> Tuple[bool, str]:
        """From the viewer's own album only (the shared folders are the original backups: never changed here)."""
        rel = clean_rel(rel)
        root = self.drive()
        if rel is None or root is None:
            return False, "I can't find that photo."
        with self._connect() as conn:
            album = next((a for a in self._albums(conn) if rel.startswith(a["folder"] + "/")), None)
            if album is None or not self._mine(album, person, household):
                return False, "Only photos in your own albums can be deleted."
            if conn.execute("SELECT 1 FROM media WHERE path = ?", (rel,)).fetchone() is None:
                return False, "I can't find that photo."
        try:
            os.remove(os.path.join(root, *rel.split("/")))
        except OSError as e:
            return False, f"It couldn't be deleted ({e.strerror or e})."
        self._reindex(album["folder"])
        app_logger.log("info", "photo_deleted", album=album["id"], component="photos", action="delete")
        return True, "Deleted."
