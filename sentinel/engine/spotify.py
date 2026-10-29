"""
Spotify (Premium): play, pause, skip and volume on the household's Spotify devices
(the Fire TV's Spotify app, phones, computers) by voice and from the web pages.

Sentinel only sends commands through Spotify's Web API; the music plays on the device.
Sign-in is Authorization Code with PKCE: the app's Client ID is public and there is no
client secret. The refresh token is kept in settings.json (owner-only, never sent to the
browser). The redirect is http://127.0.0.1:8888/callback: on the PC itself a small
one-shot listener catches it; from any other device the person pastes the address the
browser ended up on (the page fails to load, but the code is in its address).
"""

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
NO_SUCH_SPEAKER = "I can't see a Spotify speaker called "  # the voice assistant offers the list after it
# What people call the PC's own Spotify speaker (spotify_player.py) besides its name.
_THIS_PC = re.compile(r"here|(this|the|that)? ?(speaker|computer|pc|desktop|office computer)")
NOW_PLAYING_SECONDS = 3.0  # shared by every open page (Fire TV, phones)
LOGIN_SECONDS = 15 * 60


class SpotifyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers, **{"User-Agent": "SentinelHome"}))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", (text or "").lower())).strip()


# ------------------------------------------------------------ spoken / typed commands
_NUMBERS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100, "max": 100}


def parse_music_command(text: str) -> Optional[Tuple[str, str]]:
    """("play", "jazz"), ("pause", ""), ("volume", "40" / "+" / "-"), ... or None if it isn't about music."""
    t = _words(text)
    t = re.sub(r"^(please |can you |could you |would you )", "", t)
    t = re.sub(r" please$", "", t)
    # Whisper's usual mishearings of "play" from across the room ("Place on Tom Petty", 2026-10-27)
    # ("on" there is the misheard "play"'s tail, not "on the TV": "Place on Tom Petty" = play Tom Petty)
    t = re.sub(r"^(place|plays|played|playing|blade|plate) (on (?!(the )?(tv|television)\b))?", "play ", t)
    if re.fullmatch(r"(pause|stop)( the| this)?( music| song| spotify| playing| it)?|(be )?quiet|shut up|"
                    r"mute( the music)?|turn (off|the music off) the music|turn the music off", t):
        return ("pause", "")
    if re.fullmatch(r"(resume|unpause|continue|keep playing)( the)?( music| song| playing| spotify)?|"
                    r"play( the)? music|(start|turn on)( the)? music|play spotify", t):
        return ("resume", "")
    if re.fullmatch(r"(next|skip)( this)?( one| song| track)?|skip( it)?|next please", t):
        return ("next", "")
    if re.fullmatch(r"(previous|last|go back)( one| song| track)?|(play )?(the )?(previous|last) (song|track)( again)?", t):
        return ("previous", "")
    if re.search(r"\b(what'?s|what is|what song is|who is|who'?s) (this|playing|this song|singing)\b|"
                 r"\bwhat song\b|\bname of this song\b", t):
        return ("now", "")
    m = re.fullmatch(r"(?:turn |set )?(?:the )?(?:music |spotify )?volume (?:to |at )?(\d{1,3}|[a-z ]+?)(?: percent)?", t)
    if m and (m.group(1).isdigit() or m.group(1) in _NUMBERS):
        return ("volume", str(min(100, int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1)])))
    if re.fullmatch(r"(turn (it|the music|the volume) up|(music |volume )?up|louder|volume up|"
                    r"turn up( the)?( music| volume)?)( a (bit|little))?", t):
        return ("volume", "+")
    if re.fullmatch(r"(turn (it|the music|the volume) down|(music |volume )?down|quieter|softer|volume down|"
                    r"turn down( the)?( music| volume)?)( a (bit|little))?", t):
        return ("volume", "-")
    m = re.fullmatch(r"(?:play|put on)(?: the)? (?:music |spotify |it )?(?:on (?:the )?(.+)|(?:right |in )?(here))", t)
    if m:
        return ("move", m.group(1) or m.group(2))
    m = re.fullmatch(r"(?:play|put on|listen to|i want to hear|let'?s hear)(?: me)? (.+?)"
                     r"(?: on spotify)?(?: on (?:the )?([a-z' ]+))?", t)
    if m and m.group(1) not in ("it", "that", "this"):
        query = m.group(1)
        if m.group(2):
            query += f" @ {m.group(2)}"
        return ("play", query)
    return None


class SpotifyService:
    def __init__(self, settings: Dict[str, Any], save: Callable[[], None], http: Callable = _http) -> None:
        self.settings = settings
        self._save = save
        self._http = http
        self._lock = threading.Lock()
        self._access = ("", 0.0)  # (token, expires at)
        self._pending: Optional[Dict[str, Any]] = None  # PKCE sign-in in progress
        self._now: Tuple[float, Dict[str, Any]] = (0.0, {})
        self._listener: Optional[http.server.HTTPServer] = None
        self.last_error = ""

    # ------------------------------------------------------------ sign-in
    def client_id(self) -> str:
        return str(self.settings.get("spotify_client_id") or "").strip()

    def connected(self) -> bool:
        return bool(self.client_id() and self.settings.get("spotify_refresh_token"))

    def begin_login(self) -> str:
        """Spotify's sign-in page address; also starts the PC-only callback listener."""
        if not self.client_id():
            raise SpotifyError(400, "enter the Spotify Client ID first")
        verifier = secrets.token_urlsafe(64)  # 86 characters; PKCE allows 43-128
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        with self._lock:
            self._pending = {"verifier": verifier, "state": state, "at": time.time()}
        self._start_listener()
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id(), "response_type": "code", "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state,
        })

    def finish_login(self, url_or_code: str) -> Tuple[bool, str]:
        """Accepts the address the browser was sent to (or just its code)."""
        text = (url_or_code or "").strip()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query) if "?" in text else {}
        if query.get("error"):
            return False, f"Spotify said: {query['error'][0]}"
        code = (query.get("code") or [text if re.fullmatch(r"[\w-]{20,}", text) else ""])[0]
        state = (query.get("state") or [""])[0]
        with self._lock:
            pending = self._pending
            if not pending or time.time() - pending["at"] > LOGIN_SECONDS:
                return False, "that sign-in has expired; press Connect Spotify again"
            if state and state != pending["state"]:
                return False, "that address is from a different sign-in; press Connect Spotify again"
            if not code:
                return False, "couldn't find the code in that address"
            self._pending = None
        status, raw = self._http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode({
                                     "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                                     "client_id": self.client_id(), "code_verifier": pending["verifier"],
                                 }).encode())
        data = self._json(raw)
        if status != 200 or not data.get("refresh_token"):
            return False, f"Spotify didn't accept the sign-in ({data.get('error_description') or data.get('error') or status})"
        self._store_tokens(data)
        app_logger.log("info", "spotify_connected", component="spotify", action="login")
        return True, "connected"

    def disconnect(self) -> None:
        with self._lock:
            self.settings["spotify_refresh_token"] = ""
            self._access = ("", 0.0)
            self._now = (0.0, {})
        self._save()

    def _start_listener(self) -> None:
        """One-shot http://127.0.0.1:8888/callback, for signing in from the PC's own browser."""
        if self._listener is not None:
            return
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                if not self.path.startswith("/callback"):
                    self.send_error(404)
                    return
                ok, message = service.finish_login("http://127.0.0.1:8888" + self.path)
                body = ("<h2>Spotify is connected to Sentinel.</h2><p>You can close this tab.</p>" if ok else
                        f"<h2>Spotify sign-in didn't work</h2><p>{message}</p>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                if ok:
                    threading.Thread(target=service._stop_listener, daemon=True).start()

            def log_message(self, *args):
                pass

        try:
            self._listener = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
        except OSError:
            return  # port busy: the paste-the-address route still works
        threading.Thread(target=self._listener.serve_forever, name="spotify-login", daemon=True).start()
        threading.Timer(LOGIN_SECONDS, self._stop_listener).start()

    def _stop_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.shutdown()
            listener.server_close()

    # ------------------------------------------------------------ API plumbing
    @staticmethod
    def _json(raw: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _store_tokens(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._access = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
            if data.get("refresh_token") and data["refresh_token"] != self.settings.get("spotify_refresh_token"):
                self.settings["spotify_refresh_token"] = data["refresh_token"]  # Spotify may rotate it
                changed = True
            else:
                changed = False
        if changed:
            self._save()

    def _token(self) -> str:
        token, expires = self._access
        if token and time.time() < expires:
            return token
        if not self.connected():
            raise SpotifyError(401, "Spotify isn't connected yet (Settings → Spotify)")
        status, raw = self._http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode({
                                     "grant_type": "refresh_token", "client_id": self.client_id(),
                                     "refresh_token": self.settings.get("spotify_refresh_token"),
                                 }).encode())
        data = self._json(raw)
        if status != 200 or not data.get("access_token"):
            if data.get("error") == "invalid_grant":  # revoked or expired: sign in again
                self.disconnect()
                raise SpotifyError(401, "Spotify needs connecting again (Settings → Spotify)")
            raise SpotifyError(status or 502, "couldn't reach Spotify")
        self._store_tokens(data)
        return self._access[0]

    def api(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
            body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = API_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self._token()}"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        elif method in ("PUT", "POST"):
            headers["Content-Length"] = "0"
        status, raw = self._http(method, url, headers, payload)
        if status == 401:  # token expired early: once more with a fresh one
            self._access = ("", 0.0)
            headers["Authorization"] = f"Bearer {self._token()}"
            status, raw = self._http(method, url, headers, payload)
        data = self._json(raw)
        if status >= 400:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise SpotifyError(status, err.get("reason") or err.get("message") or f"Spotify error {status}")
        return data

    # ------------------------------------------------------------ what's playing
    def now_playing(self, fresh: bool = False) -> Dict[str, Any]:
        if not self.connected():
            return {"connected": False, "configured": bool(self.client_id())}
        at, cached = self._now
        if not fresh and time.time() - at < NOW_PLAYING_SECONDS:
            return cached
        try:
            data = self.api("GET", "/me/player", {"additional_types": "episode"})
        except SpotifyError as e:
            return {"connected": self.connected(), "configured": True, "error": str(e)}
        item = data.get("item") or {}
        artists = item.get("artists") or ([item.get("show")] if item.get("show") else [])
        images = (item.get("album") or item.get("show") or {}).get("images") or []
        device = data.get("device") or {}
        now = {
            "connected": True, "configured": True, "active": bool(item),
            "playing": bool(data.get("is_playing")),
            "track": item.get("name") or "", "artist": ", ".join(a.get("name", "") for a in artists if a),
            "art": (images[-2] if len(images) > 1 else images[0])["url"] if images else "",
            "device": device.get("name") or "", "volume": device.get("volume_percent"),
            "progress_ms": data.get("progress_ms") or 0, "duration_ms": item.get("duration_ms") or 0,
        }
        self._now = (time.time(), now)
        return now

    def devices(self) -> List[Dict[str, Any]]:
        return [d for d in self.api("GET", "/me/player/devices").get("devices") or [] if not d.get("is_restricted")]

    def speaker_list(self) -> str:
        """The speakers Spotify can see right now, as a sentence to say."""
        try:
            names = [str(d.get("name")) for d in self.devices() if d.get("name")]
        except SpotifyError as e:
            return "Spotify didn't answer: " + str(e)
        if not names:
            return "I can't see any Spotify speakers right now. Open Spotify on one first."
        if len(names) == 1:
            return f"The only Spotify speaker I can see is {names[0]}."
        return f"The Spotify speakers I can see are {', '.join(names[:-1])} and {names[-1]}."

    # ------------------------------------------------------------ controls
    def _pick_device(self, wanted: str = "") -> Optional[Dict[str, Any]]:
        """By (part of) its name; else the active one; else the preferred one; else the first."""
        devices = self.devices()
        if not devices:
            return None
        if wanted:
            if _THIS_PC.fullmatch(_words(wanted)):
                wanted = str(self.settings.get("spotify_speaker_name") or "Sentinel")
            w = _words(wanted).replace("tv", "").strip() or "tv"
            for d in devices:
                name = _words(d.get("name"))
                if w in name or wanted.lower() in ("tv", "the tv", "television") and d.get("type") == "TV":
                    return d
            return None
        preferred = _words(str(self.settings.get("spotify_device") or ""))
        return (next((d for d in devices if d.get("is_active")), None)
                or next((d for d in devices if preferred and _words(d.get("name")) == preferred), None)
                or devices[0])

    def _search(self, query: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(what to play, how to describe it). "some jazz" -> a playlist; an artist's name -> the artist."""
        q = re.sub(r"^(some|a little|a bit of|any|the) ", "", query).strip()
        q = re.sub(r" (music|songs|tunes)$", "", q) if not re.match(r"^(songs|music) by ", q) else q
        by = re.match(r"^(?:songs |music |something )?by (.+)$", q)
        if by:
            q = by.group(1)
        data = self.api("GET", "/search", {"q": q, "type": "artist,track,playlist,album", "limit": 5})
        artists = [a for a in (data.get("artists") or {}).get("items") or [] if a]
        tracks = [t for t in (data.get("tracks") or {}).get("items") or [] if t]
        albums = [a for a in (data.get("albums") or {}).get("items") or [] if a]
        # Spotify's own editorial playlists aren't playable by new apps (Nov 2024 API change).
        playlists = [p for p in (data.get("playlists") or {}).get("items") or []
                     if p and (p.get("owner") or {}).get("id") != "spotify"]
        wq = _words(q)
        artist = next((a for a in artists if _words(a.get("name")) == wq), None)
        if artist or (by and artists):
            artist = artist or artists[0]
            return {"context_uri": artist["uri"]}, artist["name"]
        album = next((a for a in albums if _words(a.get("name")) == wq), None)
        if album:
            return {"context_uri": album["uri"]}, f"{album['name']} by {album['artists'][0]['name']}"
        track = next((t for t in tracks if _words(t.get("name")) == wq or wq.startswith(_words(t.get("name")))), None)
        genre_like = len(wq.split()) <= 3 and not track
        if playlists and (genre_like or query.startswith(("some ", "a little ", "a bit of "))):
            p = playlists[0]
            return {"context_uri": p["uri"]}, f"a {q} playlist"
        track = track or (tracks[0] if tracks else None)
        if track:
            return {"uris": [track["uri"]]}, f"{track['name']} by {track['artists'][0]['name']}"
        if playlists:
            return {"context_uri": playlists[0]["uri"]}, playlists[0].get("name") or q
        return None, q

    def command(self, action: str, arg: str = "") -> str:
        """Do it; returns a short sentence to say or show."""
        try:
            return self._command(action, arg)
        except SpotifyError as e:
            self.last_error = str(e)
            app_logger.log("warning", "spotify_failed", action_name=action, status=e.status, error_message=str(e),
                           component="spotify", action="command")
            if action == "pause" and e.status in (403, 404):  # already paused / nothing playing
                return "Nothing is playing."
            if e.status == 404 or "NO_ACTIVE_DEVICE" in str(e):
                return "I can't find a Spotify speaker that's on. Open Spotify on the TV or your phone, then ask again."
            if e.status == 403 and "PREMIUM" in str(e).upper():
                return "Spotify says that needs Premium on the signed-in account."
            if e.status == 401:
                return str(e)[:1].upper() + str(e)[1:] + "."
            return "Spotify didn't do that: " + str(e)
        finally:
            self._now = (0.0, {})

    def _command(self, action: str, arg: str) -> str:
        if action == "now":
            now = self.now_playing(fresh=True)
            if not now.get("active"):
                return "Nothing is playing on Spotify."
            return (f"This is {now['track']} by {now['artist']}" if now["playing"] else
                    f"{now['track']} by {now['artist']} is paused") + (f", on {now['device']}." if now["device"] else ".")
        if action == "pause":
            self.api("PUT", "/me/player/pause")
            return "Paused."
        if action == "resume":
            self.api("PUT", "/me/player/play")
            return "Playing."
        if action == "next":
            self.api("POST", "/me/player/next")
            return "Skipped."
        if action == "previous":
            self.api("POST", "/me/player/previous")
            return "Going back."
        if action == "volume":
            current = self.now_playing(fresh=True).get("volume")
            if arg in "+-":
                if current is None:
                    return "That speaker doesn't let me change its volume."
                level = max(0, min(100, int(current) + (15 if arg == "+" else -15)))
            else:
                level = max(0, min(100, int(arg)))
            self.api("PUT", "/me/player/volume", {"volume_percent": level})
            return f"Volume {level}."
        if action == "move":
            device = self._pick_device(arg)
            if device is None:
                return f"{NO_SUCH_SPEAKER}{arg}."
            self.api("PUT", "/me/player", body={"device_ids": [device["id"]], "play": True})
            return f"Playing on {device['name']}."
        if action == "play":
            query, _, where = arg.partition(" @ ")
            device = self._pick_device(where)
            if device is None:
                return (f"{NO_SUCH_SPEAKER}{where}." if where else
                        "I can't find a Spotify speaker that's on. Open Spotify on the TV or your phone, then ask again.")
            what, described = self._search(query)
            if what is None:
                return f"I couldn't find {query} on Spotify."
            self.api("PUT", "/me/player/play", {"device_id": device["id"]}, body=what)
            app_logger.log("info", "spotify_play", query=query[:100], device=device.get("name"),
                           component="spotify", action="play")
            return f"Playing {described}" + ("" if device.get("is_active") else f" on {device['name']}") + "."
        raise SpotifyError(400, f"unknown action {action}")
