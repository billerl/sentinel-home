"""
Nightly backup to a USB thumb drive, and (optionally) the important part to another computer over SSH.

On the drive, in a "Sentinel Backup" folder:
  snapshots/<date>/  the databases, settings, family/car photos and recipes (tens of MB): one per night, the last
                     SNAPSHOTS_KEPT nights, so a database that goes bad doesn't overwrite the only good copy
  mirror/            what is big but changes slowly (Wi-Fi motion logs, models, firmware): kept in step
  videos/<day>/      the last few days of clips (Settings: backup_video_days), newest first while there's room

A drive is only used once its "Sentinel Backup" folder exists (Settings → Backup → Use this drive), so a
stranger's stick that gets plugged in is left alone, and nothing outside that folder is ever changed or deleted:
the drive can hold the family's own files too. Sentinel runs as a service, outside the desktop session,
so mounting a drive needs a one-time polkit rule (installation-guide.md "Backup drive").

The remote copy (backup_remote: an SSH host, e.g. a Raspberry Pi elsewhere in the house) gets the snapshot only,
in <date> folders hard-linked to the night before, keeping the same number of nights.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from sentinel.logging_utils import app_logger

FOLDER = "Sentinel Backup"
STATE_FILE = "backup.json"
SNAPSHOTS_KEPT = 7
DRIVE_RESERVE = 500 * 1024 * 1024  # left free on the drive (videos stop there)
FILESYSTEMS = {"vfat", "exfat", "ext4", "ext3", "ext2", "ntfs", "ntfs3", "btrfs", "xfs", "f2fs"}
DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BIG = {"wifi_motion"}  # data folders that go to mirror/ (hundreds of MB), not into every night's snapshot
# Recreated on their own, or temporary: never backed up.
SKIP_NAMES = {".web_cache", ".packages", "__pycache__", ".pio", STATE_FILE}
SKIP_FILE = re.compile(r"(?:\.bak(?:-\d+)?|-wal|-shm|-journal)$")
SKIP_DIR = re.compile(r"^build(?:-.*)?$")  # firmware build output
REMOTE_HOST = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
README = """This drive holds Sentinel's nightly backup.

snapshots/<date>  databases, settings.json, family/car photos, recipes - one folder per night
mirror/           Wi-Fi motion logs, models and firmware (recorded_videos/..., models/, firmware/)
videos/<day>      the last few days of camera clips
recovery/         keys, notes and other files this install needs (read recovery/RECOVERY.md first)

To restore on a fresh install: stop Sentinel, copy the newest snapshot's files into recorded_videos/ (except
settings.json, which goes in the sentinel-home folder), copy mirror/ back over sentinel-home/, and optionally the
videos/ day folders into recorded_videos/. Then start Sentinel.
"""
RECOVERY_DEFAULT = ["~/.ssh", "CLAUDE.local.md"]  # backup_extra: what else a new install would need
RECOVERY = """# Bringing Sentinel back from this drive

For whoever sets Sentinel up again, a person or an AI assistant such as Claude Code: read this first.
Written {when}.

1. Install Sentinel on the new computer from GitHub (installation-guide.md), ideally as the same user name
   ({user}). Don't start it yet.
2. Copy the files of the newest folder in snapshots/ into sentinel-home/recorded_videos/, except settings.json,
   which goes in sentinel-home/ itself (it holds the camera, Telegram and other logins).
3. Copy mirror/ over sentinel-home/ (mirror/recorded_videos/wifi_motion -> recorded_videos/wifi_motion, and
   models/, firmware/). Optionally copy the videos/ day folders into recorded_videos/ too.
4. files/ holds what else this install needed, at its original path (files/home/{user}/.ssh was ~/.ssh):
{paths}
   Copy them back. USB drives don't keep Linux permissions, so afterwards: chmod 700 ~/.ssh && chmod 600 ~/.ssh/*
   (and the same for any other key folder). Notes files such as CLAUDE.local.md describe this particular house
   and its devices: read them before changing anything.
5. Start Sentinel and check sentinel_events.log for engine_started and camera_connected.
{remote}"""


def _disks(run=subprocess.run) -> List[Dict[str, Any]]:
    """Filesystems on USB drives (lsblk), with the drive's transport passed down to its partitions."""
    out = run(["lsblk", "-J", "-b", "-o", "NAME,PKNAME,PATH,TRAN,TYPE,FSTYPE,MOUNTPOINT,LABEL,SIZE"],
              capture_output=True, text=True, timeout=10)
    devs: List[Dict[str, Any]] = []

    def flatten(items):  # lsblk nests partitions under their disk in some versions and lists them flat in others
        for d in items or []:
            devs.append(d)
            flatten(d.get("children"))

    flatten(json.loads(out.stdout or "{}").get("blockdevices"))
    tran = {d.get("name"): d.get("tran") for d in devs}

    def usb(d, hops=0):
        return d.get("tran") == "usb" or (hops < 4 and d.get("pkname") in tran and
                                          usb(next(x for x in devs if x.get("name") == d["pkname"]), hops + 1))

    return [{"path": d.get("path"), "label": d.get("label") or "", "size": int(d.get("size") or 0),
             "mount": d.get("mountpoint") or ""}
            for d in devs if (d.get("fstype") or "") in FILESYSTEMS and usb(d)]


def _mount(path: str, run=subprocess.run) -> str:
    """Mount a USB filesystem through udisks (as the desktop would); its mount point, or "" if not allowed."""
    out = run(["udisksctl", "mount", "--no-user-interaction", "-b", path], capture_output=True, text=True, timeout=30)
    m = re.search(r" at (/.+?)\.?$", (out.stdout or "").strip())
    return m.group(1) if out.returncode == 0 and m else ""


def _skip(name: str, is_dir: bool) -> bool:
    return name in SKIP_NAMES or (is_dir and bool(SKIP_DIR.match(name))) or (not is_dir and bool(SKIP_FILE.search(name)))


def _same(src: os.stat_result, dst: str) -> bool:
    try:
        st = os.stat(dst)
    except OSError:
        return False
    return st.st_size == src.st_size and abs(st.st_mtime - src.st_mtime) <= 2  # FAT keeps 2-second times


def sync_tree(src: str, dst: str, skip_db: bool = False, room: Callable[[int], bool] = lambda n: True) -> Dict[str, int]:
    """Make dst a copy of src: copy what's new or changed, delete what's gone. Databases are left to
    copy_database (skip_db) because a plain copy of one being written can be corrupt."""
    counts = {"copied": 0, "bytes": 0, "failed": 0, "full": 0}
    os.makedirs(dst, exist_ok=True)
    keep = set()
    with os.scandir(src) as entries:
        for e in sorted(entries, key=lambda x: x.name):
            is_dir = e.is_dir(follow_symlinks=False)
            if _skip(e.name, is_dir) or e.is_symlink() or (skip_db and e.name.endswith(".db")):
                continue
            keep.add(e.name)
            target = os.path.join(dst, e.name)
            if is_dir:
                for k, v in sync_tree(e.path, target, room=room).items():
                    counts[k] += v
                continue
            st = e.stat()
            if _same(st, target):
                continue
            if not room(st.st_size):
                counts["full"] += 1
                continue
            try:
                shutil.copy2(e.path, target)
                counts["copied"] += 1
                counts["bytes"] += st.st_size
            except OSError:
                counts["failed"] += 1
    with os.scandir(dst) as entries:
        for e in entries:
            if e.name not in keep:
                _remove(e.path)
    return counts


def _remove(path: str) -> None:
    """Delete something Sentinel put there itself: never anything outside a "Sentinel Backup" folder (the rest of
    the drive is the owner's own files) or the temporary copy made for the other computer."""
    real = os.path.realpath(path)
    if f"{os.sep}{FOLDER}{os.sep}" not in real and f"{os.sep}sentinel-backup-" not in real:
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError:
        pass


def copy_database(src: str, dst: str) -> None:
    """A consistent copy of a SQLite database that Sentinel may be writing (SQLite's own backup)."""
    tmp = dst + ".part"
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30) as source, sqlite3.connect(tmp) as target:
        source.backup(target)
    os.replace(tmp, dst)


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class Backup:
    """The nightly backup: when it's due, the drive to use, the run itself, and what happened last time."""

    def __init__(self, settings: Dict[str, Any], data_dir: str, app_dir: str, warn: Callable[[str], None],
                 run=subprocess.run, clock=time.time) -> None:
        self.settings = settings
        self.data_dir = data_dir  # recorded_videos: databases, photos, day folders of clips
        self.app_dir = app_dir  # settings.json, models/, firmware/
        self.warn = warn  # (text) -> the owner's Telegram chat
        self.run_cmd = run
        self.clock = clock
        self.state_path = os.path.join(data_dir, STATE_FILE)
        self._lock = threading.Lock()
        self.running = ""  # what it's doing now ("" when idle)

    # ------------------------------------------------------------------ state
    def state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes) -> None:
        state = dict(self.state(), **changes)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)

    # ----------------------------------------------------------------- drives
    def drives(self) -> List[Dict[str, Any]]:
        """USB drives, mounted if possible, each with whether it's Sentinel's backup drive and its free space."""
        try:
            disks = _disks(self.run_cmd)
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        for d in disks:
            if not d["mount"]:
                try:
                    d["mount"] = _mount(d["path"], self.run_cmd)
                except (OSError, subprocess.SubprocessError):
                    pass
            d["mounted"] = bool(d["mount"])
            d["ours"] = d["mounted"] and os.path.isdir(os.path.join(d["mount"], FOLDER))
            d["free"] = shutil.disk_usage(d["mount"]).free if d["mounted"] else 0
        return disks

    def use_drive(self, path: str):
        """Make a plugged-in drive the backup drive (creates its "Sentinel Backup" folder)."""
        drive = next((d for d in self.drives() if d["path"] == path), None)
        if drive is None:
            return False, "That drive isn't plugged in any more."
        if not drive["mounted"]:
            return False, "Sentinel couldn't open that drive (see Help → Backup)."
        try:
            os.makedirs(os.path.join(drive["mount"], FOLDER), exist_ok=True)
            with open(os.path.join(drive["mount"], FOLDER, "README.txt"), "w") as f:
                f.write(README)
        except OSError as e:
            return False, f"Couldn't write to the drive ({e.strerror or e})."
        return True, f"{drive['label'] or 'The drive'} is now the backup drive. The first backup is starting."

    # --------------------------------------------------------------- schedule
    def due(self, now: float) -> bool:
        """Once a night at the set time (within 3 hours of it, so a restart doesn't skip a night)."""
        if not self.settings.get("backup_enabled", True) or self.running:
            return False
        hh, mm = (int(x) for x in str(self.settings.get("backup_time", "03:30")).split(":"))
        t = time.localtime(now)
        minutes = (t.tm_hour * 60 + t.tm_min - (hh * 60 + mm)) % 1440
        if minutes >= 180:
            return False
        return self.state().get("last_try_day") != time.strftime("%Y-%m-%d", time.localtime(now - minutes * 60))

    def start(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.running = "Starting"
        threading.Thread(target=self._run_locked, name="backup", daemon=True).start()
        return True

    def _run_locked(self) -> None:
        try:
            self.run()
        except Exception as e:  # never take the command loop's thread down with it
            app_logger.log("error", "backup_failed", error_message=str(e)[:200], component="backup", action="run")
        finally:
            self._lock.release()

    # -------------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        try:
            return self._run()
        finally:
            self.running = ""

    def _run(self) -> Dict[str, Any]:
        now = self.clock()
        started = time.monotonic()
        state = self.state()
        result: Dict[str, Any] = {"last_try": now, "last_try_day": time.strftime("%Y-%m-%d", time.localtime(now))}
        problems = []
        self.running = "Looking for the backup drive"
        drive = next((d for d in self.drives() if d["ours"]), None)
        if drive is None:
            if state.get("drive_label") is not None:  # it had a drive before: say so (never before one is set up)
                problems.append("the backup drive isn't plugged in")
        else:
            try:
                result.update(self._to_drive(os.path.join(drive["mount"], FOLDER), now))
                result.update(drive_label=drive["label"], drive_ok=now)
                if result["failed_files"]:
                    problems.append(f"{result['failed_files']} files couldn't be copied")
            except OSError as e:
                problems.append(f"the drive gave an error ({e.strerror or e})")
        remote = str(self.settings.get("backup_remote") or "").strip()
        if remote:
            self.running = f"Copying to {remote}"
            err = self._to_remote(remote, now)
            if err:
                problems.append(f"the copy to {remote} failed ({err})")
            else:
                result["remote_ok"] = now
        result.update(problems=problems, seconds=round(time.monotonic() - started))
        if drive is not None or remote:
            result["last_ok" if not problems else "last_failed"] = now
        self._save_state(**result)
        app_logger.log("error" if problems else "info", "backup_failed" if problems else "backup_done",
                       problems=problems or None, copied_mb=round(result.get("bytes", 0) / 1e6, 1),
                       video_days=result.get("video_days"), seconds=result["seconds"],
                       component="backup", action="run")
        if problems:
            self.warn("⚠️ Last night's backup didn't fully work: " + "; ".join(problems) + ".")
        return result

    def _snapshot(self, dest: str) -> int:
        """The important part: databases (copied safely), settings and the small files and photos."""
        os.makedirs(dest, exist_ok=True)
        total = 0
        with os.scandir(self.data_dir) as entries:
            for e in entries:
                if e.name.endswith(".db") and e.is_file() and not _skip(e.name, False):
                    copy_database(e.path, os.path.join(dest, e.name))
                    total += os.path.getsize(os.path.join(dest, e.name))
        with os.scandir(self.data_dir) as entries:
            for e in entries:
                is_dir = e.is_dir(follow_symlinks=False)
                if (_skip(e.name, is_dir) or e.is_symlink() or e.name.endswith(".db") or DAY_DIR.match(e.name)
                        or e.name in BIG):
                    continue
                target = os.path.join(dest, e.name)
                if is_dir:
                    total += sync_tree(e.path, target)["bytes"]
                else:
                    shutil.copy2(e.path, target)
                    total += e.stat().st_size
        settings_file = os.path.join(self.app_dir, "settings.json")
        if os.path.isfile(settings_file):
            shutil.copy2(settings_file, os.path.join(dest, "settings.json"))
        return total

    def _recovery_kit(self, dest: str, now: float) -> int:
        """Keys and notes a new install would need (backup_extra), at their original paths, plus RECOVERY.md.
        Only ever on the drive: the remote copy doesn't get them."""
        files = os.path.join(dest, "files")
        os.makedirs(files, exist_ok=True)
        total, kept, listed = 0, set(), []
        for item in self.settings.get("backup_extra", RECOVERY_DEFAULT) or []:
            src = os.path.abspath(os.path.join(self.app_dir, os.path.expanduser(str(item))))
            if not os.path.exists(src):
                continue
            target = os.path.join(files, src.lstrip(os.sep))
            kept.add(src.lstrip(os.sep).split(os.sep)[0])
            listed.append(f"   - {src}")
            try:
                if os.path.isdir(src):
                    total += sync_tree(src, target)["bytes"]
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(src, target)
                    total += os.path.getsize(src)
            except OSError:
                pass  # unreadable (another user's file): the rest still counts
        for name in os.listdir(files):
            if name not in kept:
                _remove(os.path.join(files, name))
        remote = str(self.settings.get("backup_remote") or "").strip()
        with open(os.path.join(dest, "RECOVERY.md"), "w") as f:
            f.write(RECOVERY.format(
                when=time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                user=os.path.basename(os.path.expanduser("~")),
                paths="\n".join(listed) or "   (nothing)",
                remote=(f"\nIf this drive is lost too: the databases, settings and photos are also on {remote}, in\n"
                        f"~/sentinel-backup/<date> (a folder per night).\n") if remote else ""))
        return total

    def _to_drive(self, root: str, now: float) -> Dict[str, Any]:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        snaps = os.path.join(root, "snapshots")
        self.running = "Copying the databases and settings"
        total = self._snapshot(os.path.join(snaps, day))
        total += self._recovery_kit(os.path.join(root, "recovery"), now)
        for old in sorted(n for n in os.listdir(snaps) if DAY_DIR.match(n))[:-SNAPSHOTS_KEPT]:
            _remove(os.path.join(snaps, old))

        def room(n: int) -> bool:
            return shutil.disk_usage(root).free - n > DRIVE_RESERVE

        failed = 0
        self.running = "Copying the Wi-Fi motion logs, models and firmware"
        mirror = os.path.join(root, "mirror")
        for src, dst in [(os.path.join(self.data_dir, n), os.path.join(mirror, os.path.basename(self.data_dir), n))
                         for n in sorted(BIG)] + [(os.path.join(self.app_dir, n), os.path.join(mirror, n))
                                                       for n in ("models", "firmware")]:
            if os.path.isdir(src):
                c = sync_tree(src, dst, room=room)
                total += c["bytes"]
                failed += c["failed"]

        # Videos: drop the days that are now too old first (makes room), then copy the newest day first.
        keep_days = int(self.settings.get("backup_video_days", 3))
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - keep_days * 86400))
        videos = os.path.join(root, "videos")
        os.makedirs(videos, exist_ok=True)
        for name in os.listdir(videos):
            if not DAY_DIR.match(name) or name < cutoff or keep_days == 0:
                _remove(os.path.join(videos, name))
        days = sorted((n for n in os.listdir(self.data_dir) if DAY_DIR.match(n) and n >= cutoff), reverse=True)
        copied_days, full = 0, False
        for name in days if keep_days else []:
            self.running = f"Copying the videos from {name}"
            c = sync_tree(os.path.join(self.data_dir, name), os.path.join(videos, name), room=room)
            total += c["bytes"]
            failed += c["failed"]
            if c["full"]:
                full = True
                for older in days[days.index(name) + 1:]:  # no room: older days give way to newer ones
                    _remove(os.path.join(videos, older))
                break
            copied_days += 1
        usage = shutil.disk_usage(root)
        return {"bytes": total, "failed_files": failed, "video_days": copied_days,
                "video_full": full, "drive_free": usage.free, "drive_size": usage.total}

    def _to_remote(self, host: str, now: float) -> str:
        """The snapshot to <host>:sentinel-backup/<date>/, hard-linked to the previous night's; "" when it worked."""
        if not REMOTE_HOST.match(host):
            return "not a valid host name"
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        try:
            listing = self.run_cmd(SSH + [host, "mkdir -p sentinel-backup && chmod 700 sentinel-backup && ls sentinel-backup"],
                                   capture_output=True, text=True, timeout=60)
            if listing.returncode:
                return (listing.stderr or "no answer").strip().splitlines()[-1][:120]
            old = sorted(n for n in listing.stdout.split() if DAY_DIR.match(n) and n != day)
            with tempfile.TemporaryDirectory(prefix="sentinel-backup-") as tmp:
                self._snapshot(tmp)
                cmd = ["rsync", "-a", "--delete", "-e", " ".join(SSH)]
                if old:
                    cmd.append(f"--link-dest=../{old[-1]}")
                out = self.run_cmd(cmd + [tmp + "/", f"{host}:sentinel-backup/{day}/"],
                                   capture_output=True, text=True, timeout=1800)
            if out.returncode:
                return (out.stderr or f"rsync exit {out.returncode}").strip().splitlines()[-1][:120]
            drop = (old + [day])[:-SNAPSHOTS_KEPT]
            if drop:
                self.run_cmd(SSH + [host, "cd sentinel-backup && rm -rf " + " ".join(drop)],
                             capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError, sqlite3.Error) as e:
            return str(e)[:120]
        return ""

    # ----------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        """For the Settings page: the drives plugged in, last night's result, and what's running now."""
        drives = self.drives()
        state = self.state()
        return {
            "enabled": bool(self.settings.get("backup_enabled", True)),
            "running": self.running,
            "drives": [{k: d[k] for k in ("path", "label", "size", "free", "mounted", "ours")} for d in drives],
            "last_ok": state.get("last_ok"),
            "last_failed": state.get("last_failed"),
            "last_try": state.get("last_try"),
            "problems": state.get("problems") or [],
            "video_days": state.get("video_days"),
            "video_full": bool(state.get("video_full")),
            "seconds": state.get("seconds"),
            "remote": str(self.settings.get("backup_remote") or ""),
            "remote_ok": state.get("remote_ok"),
        }
