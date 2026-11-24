"""
This PC as a Spotify speaker, through Spotify's own web player (setting spotify_speaker_kind = "web").

Why: since November 2026 Spotify refuses the open-source players (spotifyd, librespot, go-librespot) the key to
each song ("refused the audio key"): the phone says it's playing, nothing sounds, and it skips from song to
song. Spotify's own web player isn't refused. So this keeps a Firefox window open on open.spotify.com on a
screen of its own that nobody sees (xvfb-run); it shows up in the Spotify apps as WEB_NAME.

Its sound: Firefox plays into a PipeWire/PulseAudio sink of its own (SINK, made here if it's missing), and a
`parec | aplay` pipe carries that into the shared ALSA mixer as "sentinel_music", so the voice assistant talks
over it and turns it down exactly as with spotifyd (alsa-sentinel.conf). Firefox mustn't play straight into
the sound card: PipeWire would take the card for itself and silence everything else.

Sign-in is Spotify's web sign-in, kept in the Firefox profile PROFILE (owner-only, not in git). It can't be done
from the Settings page: once, on the PC's own screen, run tools/spotify_web_login.sh and sign in (see Help).

A web player that has sat silent for hours can drop out of Spotify's list of speakers, so while nothing is
playing Firefox is restarted every REFRESH_HOURS.
"""

import collections
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.audio.alert_system import alsa_env
from sentinel.logging_utils import app_logger

WEB_NAME = "Web Player (Firefox)"  # what Spotify calls it; it can't be renamed
DIR = ".spotify-web"
PROFILE = os.path.join(DIR, "profile")
SIGNED_IN = os.path.join(DIR, "signed_in")  # left by tools/spotify_web_login.sh
SINK = "sentinel_spotify"
URL = "https://open.spotify.com/"
NEEDS = ("firefox", "xvfb-run", "pactl", "parec", "aplay")
REFRESH_HOURS = 6
RESTART_DELAYS = (5, 15, 60, 300)
# Firefox settings the player needs: protected playback on, and sound without a click first.
PREFS = {
    "media.eme.enabled": True, "media.gmp-widevinecdm.enabled": True, "media.gmp-widevinecdm.visible": True,
    "media.autoplay.default": 0, "media.autoplay.blocking_policy": 0,
    "browser.shell.checkDefaultBrowser": False, "browser.aboutwelcome.enabled": False,
    "browser.sessionstore.resume_from_crash": False, "browser.sessionstore.max_resumed_crashes": 0,
    "datareporting.policy.dataSubmissionEnabled": False, "toolkit.telemetry.reportingpolicy.firstRun": False,
}


def write_prefs() -> None:
    os.makedirs(PROFILE, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    with open(os.path.join(PROFILE, "user.js"), "w") as f:
        for key, value in PREFS.items():
            f.write(f'user_pref("{key}", {str(value).lower() if isinstance(value, bool) else value});\n')


def session_env() -> Dict[str, str]:
    """The service has no login session of its own: point the sound programs at the user's PipeWire."""
    env = alsa_env()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


class SpotifyWebSpeaker:
    """Same shape as SpotifyPlayer (spotify_player.py), so the engine and the Settings page use either."""

    def __init__(self, settings: Dict[str, Any], popen: Callable = subprocess.Popen,
                 run: Callable = subprocess.run, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._popen, self._run, self._clock = popen, run, clock
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._browser: Optional[subprocess.Popen] = None
        self._bridge: Optional[subprocess.Popen] = None
        self._output: collections.deque = collections.deque(maxlen=30)
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""

    # ------------------------------------------------------------ state
    @staticmethod
    def installed() -> bool:
        return all(shutil.which(name) for name in NEEDS)

    @staticmethod
    def signed_in() -> bool:
        return os.path.isfile(SIGNED_IN)

    def running(self) -> bool:
        return all(p is not None and p.poll() is None for p in (self._browser, self._bridge))

    def status(self) -> Dict[str, Any]:
        return {"installed": self.installed(), "signed_in": self.signed_in(), "running": self.running(),
                "signing_in": False, "name": WEB_NAME, "error": self.last_error, "kind": "web"}

    # ------------------------------------------------------------ running it
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._supervise, name="spotify-web-speaker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        self._end_all()

    def restart(self) -> None:
        self._end_all()
        self._wake.set()

    def settings_changed(self) -> None:
        pass  # (the web player has no name of ours to change)

    def _pactl(self, *args: str) -> str:
        done = self._run(["pactl", *args], capture_output=True, text=True, timeout=10, env=session_env())
        if done.returncode != 0:
            raise OSError((done.stderr or "pactl failed").strip()[:200])
        return done.stdout

    def _ensure_sink(self) -> None:
        if any(line.split("\t")[1:2] == [SINK] for line in self._pactl("list", "short", "sinks").splitlines()):
            return
        self._pactl("load-module", "module-null-sink", f"sink_name={SINK}",
                    "sink_properties=device.description=SentinelSpotify", "rate=48000", "channels=2")

    def playing(self) -> bool:
        """Is anything sounding on our sink right now? (so a refresh never cuts a song off)"""
        try:
            sinks = {line.split("\t")[0]: line.split("\t")[1] for line in self._pactl("list", "short", "sinks").splitlines()
                     if line.count("\t") >= 1}
            block: List[str] = []
            for line in self._pactl("list", "sink-inputs").splitlines() + ["Sink Input #end"]:
                if line.startswith("Sink Input #"):
                    text = "\n".join(block)
                    sink = next((part.split(":", 1)[1].strip() for part in block if part.strip().startswith("Sink:")), "")
                    if sinks.get(sink) == SINK and "Corked: no" in text:
                        return True
                    block = []
                block.append(line)
        except (OSError, subprocess.SubprocessError, IndexError):
            return True  # can't tell: leave it be
        return False

    def _start_both(self) -> None:
        write_prefs()
        self._ensure_sink()
        env = session_env()
        # one shell so the two ends of the pipe live and die together
        self._bridge = self._popen(
            ["sh", "-c", f"exec parec --device={SINK}.monitor --format=s16le --rate=48000 --channels=2 "
                         f"--latency-msec=100 | aplay -q -D sentinel_music -f S16_LE -r 48000 -c 2"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            start_new_session=True)
        browser_env = dict(env, PULSE_SINK=SINK, MOZ_ENABLE_WAYLAND="0")
        browser_env.pop("DISPLAY", None)
        self._browser = self._popen(
            ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24 -nolisten tcp", "firefox", "--no-remote",
             "--profile", os.path.abspath(PROFILE), "--kiosk", URL],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=browser_env,
            start_new_session=True)

    def _supervise(self) -> None:
        failures = 0
        while not self._stopped.is_set():
            if not (self.installed() and self.signed_in()):
                self._wake.wait(30)
                self._wake.clear()
                continue
            started = self._clock()
            try:
                self._start_both()
                app_logger.log("info", "spotify_speaker_started", name=WEB_NAME, kind="web",
                               component="spotify", action="speaker")
                self.last_error = ""
                why = self._watch(started)
            except (OSError, subprocess.SubprocessError) as e:
                why = str(e)
            self._end_all()
            if self._stopped.is_set():
                return
            refresh = why == "refresh"
            failures = 0 if refresh or self._clock() - started > 600 else failures + 1
            if not refresh:
                self.last_error = why
            app_logger.log("info" if refresh or not why else "warning", "spotify_speaker_exited", reason=why or "asked",
                           kind="web", component="spotify", action="speaker")
            self._wake.wait(RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)] if failures else 1)
            self._wake.clear()

    def _watch(self, started: float) -> str:
        """Wait until something stops (what, in words), a restart is asked for (""), or a refresh is due."""
        while not self._stopped.is_set():
            if self._wake.wait(20):
                return ""
            if self._browser is None or self._browser.poll() is not None:
                return "Firefox (Spotify's web player) stopped"
            if self._bridge is None or self._bridge.poll() is not None:
                return "the sound pipe to the speaker stopped"
            if self._clock() - started > REFRESH_HOURS * 3600 and not self.playing():
                return "refresh"
        return ""

    def _end_all(self) -> None:
        for proc in (self._browser, self._bridge):
            self._end(proc)
        self._browser = self._bridge = None

    @staticmethod
    def _end(proc: Optional[subprocess.Popen]) -> None:
        """Each runs in a session of its own (xvfb-run and the pipe have children): end the whole group."""
        if proc is None or proc.poll() is not None:
            return
        import signal

        for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    # ------------------------------------------------------------ sign-in
    def begin_login(self) -> str:
        raise RuntimeError("Spotify's web player is signed in on the PC's own screen: run "
                           "tools/spotify_web_login.sh there once (see Help).")

    def finish_login(self, url: str) -> Tuple[bool, str]:
        return False, "sign in on the PC's own screen with tools/spotify_web_login.sh"

    def sign_out(self) -> None:
        self._end_all()
        shutil.rmtree(PROFILE, ignore_errors=True)
        try:
            os.remove(SIGNED_IN)
        except FileNotFoundError:
            pass
        self._wake.set()
