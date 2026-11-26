"""Import first in test modules: sends app logs to a temp file, not the live log."""

import atexit
import os
import shutil
import tempfile

from sentinel.logging_utils import app_logger

_LOG_DIR = tempfile.mkdtemp(prefix="sentinel-test-log-")
app_logger.log_path = os.path.join(_LOG_DIR, "sentinel_events.log")
atexit.register(shutil.rmtree, _LOG_DIR, True)

# Never let a test switch a real camera's light (camera_admin.json holds real admin logins).
import sentinel.engine.camera_light as _camera_light  # noqa: E402

_camera_light.ADMIN_FILE = os.path.join(_LOG_DIR, "camera_admin.json")
_camera_light.STATE_FILE = os.path.join(_LOG_DIR, ".camera_light_on.json")

# Never read Sentinel's real Claude sign-in key (~/.config/sentinel/claude-token).
import sentinel.engine.assistant as _assistant  # noqa: E402

_assistant.TOKEN_FILE = os.path.join(_LOG_DIR, "claude-token")

# Never ask this PC's real offline AI or library (a test would wait half a minute for a real answer): port 9 is
# "discard", nothing answers there.
import sentinel.engine.offline as _offline  # noqa: E402

_offline.AI_URL = "http://127.0.0.1:9"
_offline.LIBRARY_URL = "http://127.0.0.1:9/library"
