"""
This PC as a Spotify speaker. Runs spotifyd (built on librespot), which shows up in Spotify Connect
as "Sentinel" (setting spotify_speaker_name), so "play Tom Petty on Sentinel" and the Spotify apps can
play on the PC's line-out jack. Its music goes through the shared ALSA mixer (alsa-sentinel.conf) so
the voice assistant can talk over it, and is turned down while it does (AudioAlertSystem.busy).

spotifyd isn't packaged for Ubuntu: the release binary goes in bin/spotifyd (not in git; see readme).
Sign-in is Spotify's own, through `spotifyd authenticate`, which saves reusable credentials in
.spotifyd/credentials.json (owner-only, not in git); discovery on the network is off, so the
firewall needs no changes. The sign-in redirect is http://127.0.0.1:8898/login: on the PC spotifyd's
own listener catches it; from another device the person pastes the address and Sentinel forwards it.
"""

import collections
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.audio.alert_system import alsa_env
from sentinel.logging_utils import app_logger

BIN = os.path.join("bin", "spotifyd")
CACHE_DIR = ".spotifyd"
CONFIG = os.path.join(CACHE_DIR, "spotifyd.conf")
CREDENTIALS = os.path.join(CACHE_DIR, "credentials.json")
OAUTH_PORT = 8898  # Sentinel's own Spotify sign-in uses 8888
LOGIN_SECONDS = 15 * 60
RESTART_DELAYS = (5, 15, 60, 300)  # after it stops by itself, growing while it keeps failing


def speaker_name(settings: Dict[str, Any]) -> str:
    name = re.sub(r"[^\w' -]", "", str(settings.get("spotify_speaker_name") or "")).strip()
    return name[:40] or "Sentinel"


class SpotifyPlayer:
    def __init__(self, settings: Dict[str, Any], popen: Callable = subprocess.Popen) -> None:
        self.settings = settings
        self._popen = popen
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._login: Optional[subprocess.Popen] = None
        self._login_at = 0.0
        self._output: collections.deque = collections.deque(maxlen=30)  # spotifyd's last lines, for errors
        self._thread: Optional[threading.Thread] = None
        self._running_name = ""
        self.last_error = ""

    # ------------------------------------------------------------ state
    @staticmethod
    def installed() -> bool:
        return os.path.isfile(BIN) and os.access(BIN, os.X_OK)

    @staticmethod
    def signed_in() -> bool:
        return os.path.isfile(CREDENTIALS)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        return {"installed": self.installed(), "signed_in": self.signed_in(), "running": self.running(),
                "signing_in": self._login is not None and self._login.poll() is None,
                "name": speaker_name(self.settings), "error": self.last_error}

    # ------------------------------------------------------------ running it
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._supervise, name="spotify-speaker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        self._end(self._login)
        self._end(self._proc)

    def restart(self) -> None:
        """New name (or new sign-in): spotifyd reads its settings only when it starts."""
        self._end(self._proc)
        self._wake.set()

    def settings_changed(self) -> None:
        if self.running() and self._running_name != speaker_name(self.settings):
            self.restart()

    def _write_config(self) -> None:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        os.chmod(CACHE_DIR, 0o700)
        values = {
            "backend": "alsa", "device": "sentinel_music", "device_name": speaker_name(self.settings),
            "device_type": "speaker", "volume_controller": "softvol", "bitrate": 320, "initial_volume": 70,
            "cache_path": os.path.abspath(CACHE_DIR), "no_audio_cache": True, "disable_discovery": True,
        }
        lines = ["[global]"] + [f"{k} = {json.dumps(v)}" for k, v in values.items()]  # JSON strings are TOML strings
        with open(CONFIG, "w") as f:
            f.write("\n".join(lines) + "\n")

    def _spawn(self, args: List[str]) -> subprocess.Popen:
        self._write_config()
        return self._popen([os.path.abspath(BIN), "--config-path", os.path.abspath(CONFIG)] + args,
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           env=alsa_env())

    def _supervise(self) -> None:
        failures = 0
        while not self._stopped.is_set():
            if not (self.installed() and self.signed_in()):
                self._wake.wait(30)
                self._wake.clear()
                continue
            started = time.time()
            try:
                with self._lock:
                    self._proc = self._spawn(["--no-daemon"])
                    self._running_name = speaker_name(self.settings)
                app_logger.log("info", "spotify_speaker_started", name=speaker_name(self.settings),
                               component="spotify", action="speaker")
                for line in self._proc.stdout:
                    self._output.append(line.rstrip()[:300])
                code = self._proc.wait()
            except OSError as e:
                code = -1
                self._output.append(str(e))
            if self._stopped.is_set():
                return
            failures = 0 if time.time() - started > 600 else failures + 1
            self.last_error = "" if code in (0, -15) else (self._output[-1] if self._output else f"exit {code}")
            app_logger.log("info" if code in (0, -15) else "warning", "spotify_speaker_exited", code=code,
                           last_output=list(self._output)[-5:], component="spotify", action="speaker")
            self._wake.wait(RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)] if failures else 1)
            self._wake.clear()

    @staticmethod
    def _end(proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ------------------------------------------------------------ sign-in
    def begin_login(self) -> str:
        """Spotify's sign-in address for this speaker (spotifyd waits for the redirect meanwhile)."""
        if not self.installed():
            raise RuntimeError("spotifyd isn't installed (bin/spotifyd)")
        self._end(self._login)
        proc = self._spawn(["authenticate", "--oauth-port", str(OAUTH_PORT)])
        found: List[str] = []
        got = threading.Event()

        def read() -> None:
            for line in proc.stdout:
                self._output.append(line.rstrip()[:300])
                m = re.search(r"https://accounts\.spotify\.com/authorize\S+", line)
                if m and not found:
                    found.append(m.group(0))
                    got.set()
            got.set()
            if proc.wait() == 0 and self.signed_in():
                app_logger.log("info", "spotify_speaker_signed_in", component="spotify", action="speaker")
                self.last_error = ""
                self.restart()

        threading.Thread(target=read, name="spotify-speaker-login", daemon=True).start()
        if not got.wait(15) or not found:
            self._end(proc)
            raise RuntimeError("spotifyd didn't give a sign-in address: " + (self._output[-1] if self._output else "no output"))
        self._login, self._login_at = proc, time.time()
        timer = threading.Timer(LOGIN_SECONDS, self._end, (proc,))
        timer.daemon = True
        timer.start()
        return found[0]

    def finish_login(self, url: str) -> Tuple[bool, str]:
        """The address the browser ended up on (from another device): hand it to spotifyd's listener."""
        parts = urllib.parse.urlparse((url or "").strip())
        query = urllib.parse.parse_qs(parts.query)
        if query.get("error"):
            return False, f"Spotify said: {query['error'][0]}"
        if not query.get("code"):
            return False, "couldn't find the code in that address"
        if self._login is None or self._login.poll() is not None:
            return False, "that sign-in has ended; press Connect this PC again"
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{OAUTH_PORT}{parts.path or '/login'}?{parts.query}",
                                        timeout=10) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as e:
            return False, f"spotifyd didn't take it ({e})"
        for _ in range(30):
            if self._login.poll() is not None:
                break
            time.sleep(0.5)
        if self._login.poll() == 0 and self.signed_in():
            return True, "connected"
        return False, "Spotify didn't accept the sign-in: " + (self._output[-1] if self._output else "no answer")

    def sign_out(self) -> None:
        try:
            os.remove(CREDENTIALS)
        except FileNotFoundError:
            pass
        self.restart()
