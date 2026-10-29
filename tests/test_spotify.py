"""Spotify: spoken/typed music commands, PKCE sign-in, token refresh and what "play X" picks."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from unittest import mock

from sentinel.engine.spotify import SpotifyService, parse_music_command


class TestParse(unittest.TestCase):
    def test_commands(self):
        cases = {
            "Play some jazz.": ("play", "some jazz"),
            "play Tom Petty": ("play", "tom petty"),
            "put on free fallin' on the TV": ("play", "free fallin' @ tv"),
            "play on the TV": ("move", "tv"),
            "Pause": ("pause", ""), "stop the music": ("pause", ""), "pause the music please": ("pause", ""),
            "resume": ("resume", ""), "play music": ("resume", ""), "keep playing": ("resume", ""),
            "skip": ("next", ""), "next song": ("next", ""), "skip this song": ("next", ""),
            "previous song": ("previous", ""), "go back": ("previous", ""),
            "volume up": ("volume", "+"), "turn it down a bit": ("volume", "-"), "louder": ("volume", "+"),
            "volume 40": ("volume", "40"), "set the volume to fifty percent": ("volume", "50"),
            "What's playing?": ("now", ""), "what song is this": ("now", ""),
            "Place on Tom Petty.": ("play", "tom petty"), "Place some jazz": ("play", "some jazz"),
            "Play Spotify on Sentinel.": ("move", "sentinel"), "play spotify here": ("move", "here"),
            "play music in here": ("move", "here"), "play Tom Petty on this speaker": ("play", "tom petty @ this speaker"),
        }
        for text, want in cases.items():
            self.assertEqual(parse_music_command(text), want, text)

    def test_not_music(self):
        for text in ("who's home", "did anyone come by today", "what's the weather", "stop",
                     "what's on the cameras", "enable messages", "play it", "what time is it"):
            if text == "stop":
                self.assertEqual(parse_music_command(text), ("pause", ""))  # engine decides (cancel vs pause)
                continue
            self.assertIsNone(parse_music_command(text), text)


class FakeSpotify:
    """Records calls; answers like the Web API."""

    def __init__(self):
        self.calls = []
        self.devices = [{"id": "tv1", "name": "Fire TV Living Room", "type": "TV", "is_active": False},
                        {"id": "ph1", "name": "Luke's iPhone", "type": "Smartphone", "is_active": False}]
        self.search = {}
        self.player = {}
        self.token_status = 200

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        path = urllib.parse.urlparse(url).path
        if url.startswith("https://accounts.spotify.com/api/token"):
            form = urllib.parse.parse_qs(body.decode())
            if self.token_status != 200:
                return self.token_status, json.dumps({"error": "invalid_grant"}).encode()
            out = {"access_token": "acc-" + form["grant_type"][0], "expires_in": 3600}
            if form["grant_type"][0] == "authorization_code":
                out["refresh_token"] = "ref-1"
            return 200, json.dumps(out).encode()
        if path == "/v1/me/player/devices":
            return 200, json.dumps({"devices": self.devices}).encode()
        if path == "/v1/search":
            return 200, json.dumps(self.search).encode()
        if path == "/v1/me/player" and method == "GET":
            return (200, json.dumps(self.player).encode()) if self.player else (204, b"")
        return 204, b""


def _artist(name, uri):
    return {"name": name, "uri": uri}


def _track(name, artist, uri):
    return {"name": name, "uri": uri, "artists": [{"name": artist}]}


def _playlist(name, uri, owner="someone"):
    return {"name": name, "uri": uri, "owner": {"id": owner}}


class TestService(unittest.TestCase):
    def setUp(self):
        self.http = FakeSpotify()
        self.saved = 0
        self.settings = {"spotify_client_id": "bb8771b512a74eec910fbae26e4026d9"}
        self.sp = SpotifyService(self.settings, self.save, http=self.http)
        self.sp._start_listener = lambda: None  # no real port 8888 in tests

    def save(self):
        self.saved += 1

    def connect(self):
        url = self.sp.begin_login()
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        return self.sp.finish_login(f"http://127.0.0.1:8888/callback?code={'c' * 30}&state={state}")

    def last_play(self):
        method, url, _h, body = [c for c in self.http.calls if "/me/player/play" in c[1]][-1]
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query), json.loads(body or b"{}")

    def test_pkce_sign_in(self):
        url = self.sp.begin_login()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:8888/callback"])
        self.assertNotIn("client_secret", url)
        ok, _ = self.sp.finish_login(f"http://127.0.0.1:8888/callback?code={'c' * 30}&state={q['state'][0]}")
        self.assertTrue(ok)
        self.assertEqual(self.settings["spotify_refresh_token"], "ref-1")
        self.assertEqual(self.saved, 1)
        form = urllib.parse.parse_qs(self.http.calls[-1][3].decode())
        self.assertTrue(43 <= len(form["code_verifier"][0]) <= 128)

    def test_sign_in_rejects_wrong_state_and_reuse(self):
        self.sp.begin_login()
        ok, msg = self.sp.finish_login(f"http://127.0.0.1:8888/callback?code={'c' * 30}&state=other")
        self.assertFalse(ok)
        self.assertTrue(self.connect()[0])
        ok, msg = self.sp.finish_login(f"http://127.0.0.1:8888/callback?code={'c' * 30}")
        self.assertFalse(ok)
        self.assertIn("expired", msg)

    def test_revoked_refresh_token_disconnects(self):
        self.connect()
        self.sp._access = ("", 0.0)
        self.http.token_status = 400
        self.assertIn("connecting again", self.sp.command("pause"))
        self.assertFalse(self.sp.connected())

    def test_play_artist_on_preferred_device_when_nothing_active(self):
        self.connect()
        self.settings["spotify_device"] = "Fire TV Living Room"
        self.http.search = {"artists": {"items": [_artist("Tom Petty", "spotify:artist:tp")]},
                            "tracks": {"items": [_track("Free Fallin'", "Tom Petty", "spotify:track:ff")]}}
        said = self.sp.command("play", "tom petty")
        params, body = self.last_play()
        self.assertEqual(params["device_id"], ["tv1"])
        self.assertEqual(body, {"context_uri": "spotify:artist:tp"})
        self.assertEqual(said, "Playing Tom Petty on Fire TV Living Room.")

    def test_some_genre_plays_a_playlist_not_spotifys_own(self):
        self.connect()
        self.http.devices[1]["is_active"] = True
        self.http.search = {"playlists": {"items": [None, _playlist("Jazz Classics", "spotify:playlist:sp", "spotify"),
                                                    _playlist("Smooth Jazz", "spotify:playlist:sj")]},
                            "tracks": {"items": [_track("Jazz (We've Got)", "A Tribe Called Quest", "spotify:track:x")]}}
        said = self.sp.command("play", "some jazz")
        params, body = self.last_play()
        self.assertEqual(body, {"context_uri": "spotify:playlist:sj"})
        self.assertEqual(params["device_id"], ["ph1"])  # the active one
        self.assertEqual(said, "Playing a jazz playlist.")

    def test_song_name_plays_the_track(self):
        self.connect()
        self.http.search = {"tracks": {"items": [_track("Free Fallin'", "Tom Petty", "spotify:track:ff")]},
                            "playlists": {"items": [_playlist("Free Fallin' covers", "spotify:playlist:c")]}}
        self.sp.command("play", "free fallin'")
        self.assertEqual(self.last_play()[1], {"uris": ["spotify:track:ff"]})

    def test_named_device(self):
        self.connect()
        self.http.search = {"tracks": {"items": [_track("Song", "Band", "spotify:track:s")]}}
        self.sp.command("play", "song @ tv")
        self.assertEqual(self.last_play()[0]["device_id"], ["tv1"])
        self.assertIn("can't see a Spotify speaker called kitchen", self.sp.command("play", "song @ kitchen"))

    def test_this_speaker_means_the_pc(self):
        self.connect()
        self.http.devices.append({"id": "pc1", "name": "Sentinel", "type": "Speaker", "is_active": False})
        for where in ("here", "this speaker", "the computer", "sentinel"):
            self.assertEqual(self.sp.command("move", where), "Playing on Sentinel.", where)
        self.settings["spotify_speaker_name"] = "Office"
        self.http.devices[-1]["name"] = "Office"
        self.assertEqual(self.sp.command("move", "the pc"), "Playing on Office.")

    def test_speaker_list(self):
        self.connect()
        self.assertEqual(self.sp.speaker_list(), "The Spotify speakers I can see are Fire TV Living Room and Luke's iPhone.")
        self.http.devices = self.http.devices[:1]
        self.assertEqual(self.sp.speaker_list(), "The only Spotify speaker I can see is Fire TV Living Room.")
        self.http.devices = []
        self.assertIn("can't see any", self.sp.speaker_list())

    def test_no_devices_says_what_to_do(self):
        self.connect()
        self.http.devices = []
        self.assertIn("Open Spotify on the TV", self.sp.command("play", "jazz"))

    def test_volume_steps_from_current(self):
        self.connect()
        self.http.player = {"is_playing": True, "item": {"name": "S", "artists": [{"name": "A"}]},
                            "device": {"name": "Fire TV", "volume_percent": 50}}
        self.assertEqual(self.sp.command("volume", "+"), "Volume 65.")
        self.assertEqual(self.sp.command("volume", "-"), "Volume 35.")
        self.assertEqual(self.sp.command("volume", "150"), "Volume 100.")

    def test_now_playing_and_not_connected(self):
        self.assertEqual(self.sp.now_playing(), {"connected": False, "configured": True})
        self.connect()
        self.assertFalse(self.sp.now_playing()["active"] if "active" in self.sp.now_playing() else False)
        self.http.player = {"is_playing": False, "item": {"name": "Free Fallin'", "artists": [{"name": "Tom Petty"}],
                                                          "album": {"images": [{"url": "big"}, {"url": "mid"}, {"url": "small"}]}},
                            "device": {"name": "Fire TV"}}
        now = self.sp.now_playing(fresh=True)
        self.assertEqual((now["track"], now["artist"], now["art"], now["playing"]), ("Free Fallin'", "Tom Petty", "mid", False))
        self.assertEqual(self.sp.command("now"), "Free Fallin' by Tom Petty is paused, on Fire TV.")

    def test_refresh_token_never_in_now_playing(self):
        self.connect()
        self.http.player = {"is_playing": True, "item": {"name": "S", "artists": []}, "device": {}}
        self.assertNotIn("ref-1", json.dumps(self.sp.now_playing(fresh=True)))


class TestEngineMusic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-spotify-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        manager.settings["volume"] = 0
        self.engine = Engine(manager)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_voice_music_goes_to_spotify_when_connected(self):
        with mock.patch.object(self.engine.spotify, "connected", return_value=True), \
                mock.patch.object(self.engine.spotify, "command", return_value="Playing a jazz playlist.") as cmd:
            reply = self.engine.answer_voice("Play some jazz.", lambda s: None)
        cmd.assert_called_once_with("play", "play some jazz".split(" ", 1)[1])
        self.assertEqual(reply.text, "Playing a jazz playlist.")

    def test_unknown_speaker_offers_the_list(self):
        sp = self.engine.spotify
        with mock.patch.object(sp, "connected", return_value=True), \
                mock.patch.object(sp, "command", return_value="I can't see a Spotify speaker called kitchen."), \
                mock.patch.object(sp, "speaker_list", return_value="The only Spotify speaker I can see is Sentinel."):
            reply = self.engine.answer_voice("Play Spotify on the kitchen.", lambda s: None)
            self.assertEqual(reply.text, "I can't see a Spotify speaker called kitchen. Do you want to hear the list of speakers?")
            self.assertEqual(reply.confirm(), "The only Spotify speaker I can see is Sentinel.")

    def test_volume_words(self):
        from sentinel.engine.engine import parse_volume_command

        cases = {"turn the PC volume down": ("pc", "-"), "turn the computer up a bit": ("pc", "+"),
                 "PC volume 40": ("pc", "40"), "set the p c volume to fifty percent": ("pc", "50"),
                 "turn down the speakers": ("pc", "-"), "make the computer louder": ("pc", "+"),
                 "speak up": ("voice", "+"), "talk quieter": ("voice", "-"), "you're too loud": ("voice", "-"),
                 "set your volume to 30": ("voice", "30"),
                 "turn it down": None, "volume 40": None, "turn the music down": None}
        for text, want in cases.items():
            self.assertEqual(parse_volume_command(text), want, text)

    def test_pc_and_voice_volume_by_voice(self):
        with mock.patch("sentinel.audio.alert_system.pc_volume", return_value=64), \
                mock.patch("sentinel.audio.alert_system.set_pc_volume", return_value=True) as setter:
            self.assertEqual(self.engine.answer_voice("Turn the PC volume down.", lambda s: None).text, "PC volume 54 percent.")
            setter.assert_called_once_with(54)
        self.assertEqual(self.engine.settings["pc_volume"], 54)
        self.engine.settings["volume"] = 0.5
        self.assertEqual(self.engine.answer_voice("Speak up.", lambda s: None).text, "Okay, my voice is at 60 percent.")
        self.assertEqual(self.engine.answer_voice("Set your volume to 5", lambda s: None).text, "Okay, my voice is at 10 percent.")
        self.assertEqual(self.engine.settings["volume"], 0.1)

    def test_stop_is_cancel_when_not_connected(self):
        self.assertEqual(self.engine.answer_voice("Stop.", lambda s: None).text, "Okay.")
        self.assertIn("isn't connected", self.engine.answer_voice("play some jazz", lambda s: None).text)

    def test_client_id_validated(self):
        ok, msg = self.engine.update_settings({"spotify_client_id": "not-an-id"})
        self.assertFalse(ok)
        ok, _ = self.engine.update_settings({"spotify_client_id": "BB8771B512A74EEC910FBAE26E4026D9"})
        self.assertTrue(ok)
        self.assertEqual(self.engine.settings["spotify_client_id"], "bb8771b512a74eec910fbae26e4026d9")
        self.assertNotIn("spotify_refresh_token", self.engine.settings_view()["values"])


if __name__ == "__main__":
    unittest.main()
