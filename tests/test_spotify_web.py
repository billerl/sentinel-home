"""This PC as a Spotify speaker through Spotify's web player: what it starts and how it's watched (no real Firefox)."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from sentinel.audio import spotify_web
from sentinel.audio.spotify_player import SpotifyPlayer, make_speaker, speaker_name
from sentinel.audio.spotify_web import SINK, WEB_NAME, SpotifyWebSpeaker

SINKS = "47\talsa_output.pci.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
OURS = "1246\tsentinel_spotify\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n"
INPUTS = ("Sink Input #796\n\tDriver: PipeWire\n\tSink: 47\n\tCorked: no\n"
          "Sink Input #1287\n\tDriver: PipeWire\n\tSink: 1246\n\tCorked: {corked}\n\tapplication.name = \"Firefox\"\n")


class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class TestSpotifyWebSpeaker(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-spotify-web-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.calls, self.started = [], []
        self.sinks, self.corked = SINKS, "yes"

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_pactl(self, args, **kwargs):
        self.calls.append(args[1:])
        if args[1] == "load-module":
            self.sinks += OURS
        out = self.sinks if args[1:] == ["list", "short", "sinks"] else INPUTS.format(corked=self.corked)
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    def popen(self, args, **kwargs):
        self.started.append((args, kwargs))
        return FakeProc()

    def speaker(self, clock=lambda: 0.0):
        return SpotifyWebSpeaker({"spotify_speaker_kind": "web"}, popen=self.popen, run=self.run_pactl, clock=clock)

    def test_the_setting_chooses_the_kind_and_the_name(self):
        self.assertIsInstance(make_speaker({}), SpotifyPlayer)
        self.assertIsInstance(make_speaker({"spotify_speaker_kind": "web"}), SpotifyWebSpeaker)
        self.assertEqual(speaker_name({"spotify_speaker_name": "Den"}), "Den")
        self.assertEqual(speaker_name({"spotify_speaker_kind": "web", "spotify_speaker_name": "Den"}), WEB_NAME)

    def test_starts_the_sink_the_pipe_and_a_hidden_firefox(self):
        sp = self.speaker()
        sp._start_both()
        self.assertIn("load-module", [c[0] for c in self.calls])  # the sink wasn't there yet
        (pipe, pipe_kw), (browser, browser_kw) = self.started
        self.assertIn(f"parec --device={SINK}.monitor", pipe[2])
        self.assertIn("aplay -q -D sentinel_music", pipe[2])  # into the shared mixer, never the card itself
        self.assertEqual(browser[0], "xvfb-run")
        self.assertIn(os.path.abspath(spotify_web.PROFILE), browser)
        self.assertEqual(browser_kw["env"]["PULSE_SINK"], SINK)
        self.assertNotIn("DISPLAY", browser_kw["env"])
        self.assertTrue(browser_kw["env"]["XDG_RUNTIME_DIR"])
        with open(os.path.join(spotify_web.PROFILE, "user.js")) as f:
            prefs = f.read()
        self.assertIn('user_pref("media.eme.enabled", true);', prefs)
        self.assertIn('user_pref("media.autoplay.default", 0);', prefs)
        self.assertTrue(sp.running())
        self.calls.clear()
        sp._ensure_sink()  # there now: not made twice
        self.assertNotIn("load-module", [c[0] for c in self.calls])

    def test_playing_is_only_our_sink_uncorked(self):
        sp = self.speaker()
        self.sinks += OURS
        self.assertFalse(sp.playing())  # (the other stream, on another sink, doesn't count)
        self.corked = "no"
        self.assertTrue(sp.playing())
        with mock.patch.object(sp, "_pactl", side_effect=OSError("no sound server")):
            self.assertTrue(sp.playing())  # can't tell: don't interrupt

    def watch(self, sp):
        sp._wake = mock.Mock(wait=mock.Mock(return_value=False))
        return sp._watch(0.0)

    def test_watch_notices_what_stopped_and_refreshes_only_in_silence(self):
        now = [0.0]
        sp = self.speaker(clock=lambda: now[0])
        self.sinks += OURS
        sp._start_both()
        sp._browser.code = 1
        self.assertIn("Firefox", self.watch(sp))
        sp._browser.code, sp._bridge.code = None, 1
        self.assertIn("pipe", self.watch(sp))
        sp._bridge.code = None
        now[0] = spotify_web.REFRESH_HOURS * 3600 + 1
        self.assertEqual(self.watch(sp), "refresh")
        self.corked = "no"  # a song is on: left alone until it isn't
        sp._wake = mock.Mock(wait=mock.Mock(side_effect=[False, False, True]))
        self.assertEqual(sp._watch(0.0), "")

    def test_signed_in_is_the_marker_the_sign_in_script_leaves(self):
        sp = self.speaker()
        self.assertFalse(sp.signed_in())
        os.makedirs(spotify_web.DIR)
        open(spotify_web.SIGNED_IN, "w").close()
        self.assertTrue(sp.signed_in())
        self.assertEqual(sp.status()["kind"], "web")
        self.assertEqual(sp.status()["name"], WEB_NAME)
        with self.assertRaises(RuntimeError):
            sp.begin_login()
        sp.sign_out()
        self.assertFalse(sp.signed_in())


if __name__ == "__main__":
    unittest.main()
