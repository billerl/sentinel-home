"""
Development only: run a second Sentinel engine with web login bypassed, for UI checks.

    venv/bin/python tools/dev_harness.py [seconds]      # default 120, then exits

Serves http://127.0.0.1:18091 (localhost only). It copies settings.json and a snapshot
of the recordings index into a throwaway folder, so the live engine's files are never
touched, and keeps its HD ring buffers out of the live ones (SENTINEL_BUFFER_ROOT).
It DOES connect to the real cameras: they allow only a few RTSP sessions, so keep runs
short and check afterwards that no ffmpeg was left behind (`ps -eo ppid,args | grep ffmpeg`).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

WORK = tempfile.mkdtemp(prefix="sentinel-harness-")
os.environ["SENTINEL_BUFFER_ROOT"] = os.path.join(WORK, "buffer")
shutil.copy(os.path.join(REPO, "settings.json"), os.path.join(WORK, "settings.json"))
os.makedirs(os.path.join(WORK, "recorded_videos"))
live_db = os.path.join(REPO, "recorded_videos", "recordings.db")
if os.path.exists(live_db):  # timeline/events data to look at (clips themselves won't play)
    src = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    src.backup(sqlite3.connect(os.path.join(WORK, "recorded_videos", "recordings.db")))
live_household = os.path.join(REPO, "recorded_videos", "household.db")
if os.path.exists(live_household):  # vehicles + photos for the Vehicles page
    src = sqlite3.connect(f"file:{live_household}?mode=ro", uri=True)
    src.backup(sqlite3.connect(os.path.join(WORK, "recorded_videos", "household.db")))
    shutil.copytree(os.path.join(REPO, "recorded_videos", ".household"), os.path.join(WORK, "recorded_videos", ".household"),
                    ignore=shutil.ignore_patterns("presence.json*"))
os.chdir(WORK)

from sentinel.web import auth as auth_mod  # noqa: E402

auth_mod.WebAuthManager.validate_session = lambda self, token, ip: auth_mod.SessionRecord(
    username="harness", client_ip=ip, created=time.monotonic(), last_seen=time.monotonic()
)

from sentinel.engine import Engine  # noqa: E402
from sentinel.settings.manager import SettingsManager  # noqa: E402

sm = SettingsManager(settings_file="settings.json")
sm.settings["web"].update({"enabled": True, "port": 18091, "bind_host": "127.0.0.1"})
sm.settings["telegram_enabled"] = False  # never answer or alert from the harness
sm.settings["recording_retention_enabled"] = False
sm.settings.update(json.loads(os.environ.get("HARNESS_SETTINGS") or "{}"))  # e.g. '{"weather_location": "44883"}'
engine = Engine(sm)
engine.start()
print(f"harness on http://127.0.0.1:18091 (work dir {WORK})", flush=True)
timer = threading.Timer(float(sys.argv[1]) if len(sys.argv) > 1 else 120.0, engine.stop)
timer.start()
try:
    engine.wait()
finally:
    timer.cancel()
    engine.stop()
    time.sleep(3)  # let camera/HD threads wind down
    from sentinel.logging_utils import app_logger

    app_logger.log_path = os.devnull  # stragglers must not write anywhere real
    shutil.rmtree(WORK, ignore_errors=True)  # includes the copied settings (credentials)
