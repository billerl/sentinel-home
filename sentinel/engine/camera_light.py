"""
Camera white light for cars at night. In infrared the blue Edge and the grey Fusion look the
same; under the camera's own white light their paint shows (tested 2026-10-29: colour about
three seconds after asking). When a car moves in the driveway after dark, the light goes on
for LIGHT_SECONDS (long enough for the presence check to look twice) and then back to the
camera's own setting.

Changing the light needs the camera's admin login, kept in camera_admin.json (git-ignored,
owner-only; {"<camera ip>": {"user": ..., "password": ...}}): Sentinel's own camera login may
only watch. A camera the user already set to white light all night is left alone. If Sentinel
stops with a light on, it's put back on the next start (STATE_FILE). A camera whose login says
"car_light": false is only used for signals.

Signals (2026-11-17): a camera that listens for "Hey Jarvis" (sentinel/audio/camera_ear.py) has no
speaker, so it answers with this light: on while it listens, then one flash for done, two for
"didn't understand" or "not from out here", then back to the camera's own setting.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

from sentinel.logging_utils import app_logger

ADMIN_FILE = "camera_admin.json"
STATE_FILE = ".camera_light_on.json"  # camera ip -> its light setting before Sentinel turned it on
LIGHT_SECONDS = 150.0
WHITE = "colorVuWhiteLight"
DARK = "close"
FLASH_SECONDS = 0.45  # on, then off for as long: two flashes read as two (the camera switches in ~0.15 s)
_PATH = "/ISAPI/Image/channels/1/supplementLight"


def _mode(xml: str) -> str:
    start = xml.find("<supplementLightMode>")
    end = xml.find("</supplementLightMode>")
    return xml[start + len("<supplementLightMode>"):end] if start >= 0 and end > start else ""


class CameraLights:
    def __init__(self, admin_path: Optional[str] = None, state_path: Optional[str] = None,
                 clock: Callable[[], float] = time.time, http: Optional[Callable] = None) -> None:
        admin_path = admin_path or ADMIN_FILE  # (read at call time: the tests point these elsewhere)
        self.state_path = state_path or STATE_FILE
        self._clock = clock
        self._http = http or self._request  # (host, method, body or None) -> response text
        self._logins: Dict[str, Dict[str, str]] = {}
        try:
            with open(admin_path) as f:
                data = json.load(f)
            self._logins = {h: v for h, v in data.items() if isinstance(v, dict) and v.get("user")}
        except (OSError, ValueError):
            pass
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._until: Dict[str, float] = {}  # wanted on until (camera thread asks, worker acts)
        self._lit: Dict[str, str] = {}  # host -> original setting, while Sentinel has the light on
        self._signalled: Dict[str, str] = {}  # host -> original setting, while it shows it's listening
        self._sleep = time.sleep
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ used by the cameras
    def available(self, host: str) -> bool:
        """For cars at night."""
        return host in self._logins and self._logins[host].get("car_light", True) is not False

    def can_signal(self, host: str) -> bool:
        return host in self._logins

    # ------------------------------------------------------------ signals (a camera that listens)
    def listening(self, host: str) -> bool:
        """Light on: "I'm listening". False when it can't (no login, the camera refused, a car light is on)."""
        if not self.can_signal(host) or host in self._lit or host in self._signalled:
            return False
        try:
            original = self._http(host, "GET", None)
            self._signalled[host] = original
            self._save()
            if _mode(original) != WHITE:
                self._http(host, "PUT", original.replace(f">{_mode(original)}<", f">{WHITE}<", 1))
            return True
        except (OSError, urllib.error.URLError, ValueError) as e:
            self._signalled.pop(host, None)
            self._save()
            app_logger.log("warning", "camera_light_failed", camera_ip=host, error_message=str(e),
                           component="camera_light", action="signal")
            return False

    def answer(self, host: str, flashes: int) -> None:
        """After listening: off, then this many flashes, then the camera's own setting again. Blocks ~1-2 s."""
        original = self._signalled.get(host)
        if original is None:
            return
        mode = _mode(original)
        dark, lit = original.replace(f">{mode}<", f">{DARK}<", 1), original.replace(f">{mode}<", f">{WHITE}<", 1)
        try:
            self._http(host, "PUT", dark)
            for _ in range(max(0, flashes)):
                self._sleep(FLASH_SECONDS)
                self._http(host, "PUT", lit)
                self._sleep(FLASH_SECONDS)
                self._http(host, "PUT", dark)
            self._sleep(FLASH_SECONDS)
        except (OSError, urllib.error.URLError, ValueError) as e:
            app_logger.log("warning", "camera_light_failed", camera_ip=host, error_message=str(e),
                           component="camera_light", action="signal")
        finally:
            try:
                self._http(host, "PUT", original)
                self._signalled.pop(host, None)
                self._save()
            except (OSError, urllib.error.URLError, ValueError):
                pass  # (still in the state file: put back on the next start)

    def is_on(self, host: str) -> bool:
        """Sentinel's white light is on: colours in this camera's pictures are the real ones."""
        return host in self._lit

    def request(self, host: str) -> None:
        """A car is moving here in the dark: light on (or keep it on) for LIGHT_SECONDS. Never blocks."""
        if not self.available(host):
            return
        with self._lock:
            self._until[host] = self._clock() + LIGHT_SECONDS
        self._wake.set()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self._logins or self._thread is not None:
            return
        self._restore_leftovers()
        self._thread = threading.Thread(target=self._run, name="camera-lights", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        for host in list(self._lit):
            self._turn_off(host)
        for host, original in list(self._signalled.items()):
            try:
                self._http(host, "PUT", original)
                self._signalled.pop(host, None)
                self._save()
            except (OSError, urllib.error.URLError, ValueError):
                pass

    def _run(self) -> None:
        while not self._stopped.is_set():
            now = self._clock()
            with self._lock:
                wanted = {h for h, t in self._until.items() if t > now}
            for host in wanted - set(self._lit):
                self._turn_on(host)
            for host in set(self._lit) - wanted:
                self._turn_off(host)
            self._wake.wait(5)
            self._wake.clear()

    # ------------------------------------------------------------ the camera
    def _turn_on(self, host: str) -> None:
        try:
            original = self._http(host, "GET", None)
            if _mode(original) == WHITE:
                with self._lock:
                    self._until.pop(host, None)  # already white all night (the user's choice)
                return
            self._lit[host] = original
            self._save()
            self._http(host, "PUT", original.replace(f">{_mode(original)}<", f">{WHITE}<", 1))
            app_logger.log("info", "camera_light_on", camera_ip=host, component="camera_light", action="on")
        except (OSError, urllib.error.URLError, ValueError) as e:
            self._lit.pop(host, None)
            self._save()
            with self._lock:
                self._until.pop(host, None)  # don't hammer a camera that refuses; next car tries again
            app_logger.log("warning", "camera_light_failed", camera_ip=host, error_message=str(e),
                           component="camera_light", action="on")

    def _turn_off(self, host: str) -> None:
        original = self._lit.get(host)
        if original is None:
            return
        try:
            self._http(host, "PUT", original)
            self._lit.pop(host, None)
            self._save()
            app_logger.log("info", "camera_light_off", camera_ip=host, component="camera_light", action="off")
        except (OSError, urllib.error.URLError, ValueError) as e:
            app_logger.log("warning", "camera_light_failed", camera_ip=host, error_message=str(e),
                           component="camera_light", action="off")  # stays in _lit: tried again next round

    def _restore_leftovers(self) -> None:
        try:
            with open(self.state_path) as f:
                leftovers = json.load(f)
        except (OSError, ValueError):
            return
        self._lit.update({h: x for h, x in leftovers.items() if self.can_signal(h)})
        for host in list(self._lit):
            self._turn_off(host)

    def _save(self) -> None:
        try:
            if self._lit or self._signalled:
                tmp = self.state_path + ".tmp"
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                    json.dump({**self._signalled, **self._lit}, f)
                os.replace(tmp, self.state_path)
            elif os.path.exists(self.state_path):
                os.remove(self.state_path)
        except OSError:
            pass

    def ensure_sound(self, host: str) -> bool:
        """The camera's full-quality stream carries its microphone's sound (off as these cameras come). Only the
        stream: recordings stay silent (the HD recorder takes the picture alone). True when it does."""
        if not self.can_signal(host):
            return False
        path = "/ISAPI/Streaming/channels/101"
        try:
            xml = self._request(host, "GET", None, path)
            audio = xml[xml.find("<Audio>"):xml.find("</Audio>")]
            if "<enabled>true</enabled>" in audio:
                return True
            if "<enabled>false</enabled>" not in audio:
                return False
            fixed = xml.replace(audio, audio.replace("<enabled>false</enabled>", "<enabled>true</enabled>", 1), 1)
            self._request(host, "PUT", fixed, path)
            app_logger.log("info", "camera_sound_on", camera_ip=host, component="camera_light", action="sound")
            return True
        except (OSError, urllib.error.URLError, ValueError) as e:
            app_logger.log("warning", "camera_sound_failed", camera_ip=host, error_message=str(e)[:200],
                           component="camera_light", action="sound")
            return False

    def _request(self, host: str, method: str, body: Optional[str], path: str = _PATH) -> str:
        login = self._logins[host]
        base = f"http://{host}/"
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, base, login["user"], login.get("password", ""))
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(passwords))
        req = urllib.request.Request(base.rstrip("/") + path, data=body.encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/xml"})
        with opener.open(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", "replace")
        if method == "PUT" and "<statusCode>1<" not in text:
            raise ValueError("camera refused: " + text[:200])
        return text
