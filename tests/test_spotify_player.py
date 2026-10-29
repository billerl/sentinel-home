"""This PC as a Spotify speaker: spotifyd's config, sign-in relay and supervision (no real spotifyd)."""

import _test_env  # noqa: F401  (isolates the event log)

import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from sentinel.audio import spotify_player
from sentinel.audio.spotify_player import SpotifyPlayer, speaker_name


class FakeProc:
    def __init__(self, lines, code=0):
        self.stdout = io.StringIO("".join(lines))
        self.code, self.done = code, False
        self.args = None

    def poll(self):
        return self.code if self.done else None

    def wait(self, timeout=None):
        self.done = True
        return self.code

    def terminate(self):
        self.done, self.code = True, -15

    kill = terminate


class TestSpotifyPlayer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-spotifyd-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        os.makedirs("bin")
        with open(spotify_player.BIN, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(spotify_player.BIN, 0o755)
        self.spawned = []

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def player(self, procs, settings=None):
        def popen(args, **kw):
            proc = procs.pop(0)
            proc.args, proc.env = args, kw.get("env")
            self.spawned.append(proc)
            return proc
        return SpotifyPlayer(settings if settings is not None else {}, popen=popen)

    def test_name_is_cleaned(self):
        self.assertEqual(speaker_name({}), "Sentinel")
        self.assertEqual(speaker_name({"spotify_speaker_name": ' Office "PC"; '}), "Office PC")

    def test_config_plays_through_the_shared_music_device(self):
        p = self.player([FakeProc([])], {"spotify_speaker_name": "Office"})
        p._spawn(["--no-daemon"])
        with open(spotify_player.CONFIG) as f:
            conf = f.read()
        for line in ('device = "sentinel_music"', 'device_name = "Office"', "disable_discovery = true",
                     'volume_controller = "softvol"'):
            self.assertIn(line, conf)
        self.assertEqual(oct(os.stat(spotify_player.CACHE_DIR).st_mode & 0o777), "0o700")
        args = self.spawned[0].args
        self.assertEqual(args[1:], ["--no-daemon", "--config-path", os.path.abspath(spotify_player.CONFIG)])

    def test_sign_in_relays_the_pasted_address(self):
        url = "https://accounts.spotify.com/authorize?client_id=x&redirect_uri=http%3A%2F%2F127.0.0.1%3A8898%2Flogin"
        login = FakeProc([f"Browse to: {url}\n"])
        login.wait = lambda timeout=None: login.code if login.done else time.sleep(5)  # waits for the redirect
        p = self.player([login])
        self.assertEqual(p.begin_login(), url)
        self.assertEqual(self.spawned[0].args[1:4], ["authenticate", "--oauth-port", "8898"])
        self.assertEqual(p.finish_login("http://127.0.0.1:8898/login?state=s"), (False, "couldn't find the code in that address"))

        def deliver(addr, timeout):
            self.assertEqual(addr, "http://127.0.0.1:8898/login?code=abc&state=s")
            with open(spotify_player.CREDENTIALS, "w") as f:
                f.write("{}")
            login.done = True
            return mock.MagicMock()
        with mock.patch("urllib.request.urlopen", side_effect=deliver):
            self.assertEqual(p.finish_login("http://127.0.0.1:8898/login?code=abc&state=s"), (True, "connected"))
        self.assertTrue(p.status()["signed_in"])

    def test_runs_only_when_signed_in_and_restarts_after_a_crash(self):
        p = self.player([FakeProc(["boom\n"], code=1)] + [FakeProc([], code=0) for _ in range(50)])
        self.assertEqual(p.status()["running"], False)
        p.start()
        time.sleep(0.2)
        self.assertEqual(self.spawned, [])  # not signed in: nothing runs
        os.makedirs(spotify_player.CACHE_DIR)
        with open(spotify_player.CREDENTIALS, "w") as f:
            f.write("{}")
        with mock.patch.object(spotify_player, "RESTART_DELAYS", (0.05,)):
            p._wake.set()
            for _ in range(50):
                if len(self.spawned) >= 2:
                    break
                time.sleep(0.05)
        p.stop()
        self.assertGreaterEqual(len(self.spawned), 2)
        self.assertIn("alsa-sentinel.conf", self.spawned[0].env.get("ALSA_CONFIG_PATH", ""))
        self.assertEqual(p.last_error, "")  # the second run ended cleanly

    def test_sign_out_removes_the_credentials(self):
        os.makedirs(spotify_player.CACHE_DIR)
        with open(spotify_player.CREDENTIALS, "w") as f:
            f.write("{}")
        p = self.player([])
        p.sign_out()
        self.assertFalse(p.signed_in())


class TestDucking(unittest.TestCase):
    def test_music_turns_down_while_the_assistant_talks(self):
        from sentinel.audio.alert_system import AudioAlertSystem

        audio = AudioAlertSystem.__new__(AudioAlertSystem)
        with mock.patch("subprocess.run") as run:
            audio.busy = True
            audio.busy = True  # no second call
            audio.busy = False
        levels = [c.args[0][-1] for c in run.call_args_list]
        self.assertEqual(levels, ["60%", "100%"])
        self.assertEqual(run.call_args.args[0][:5], ["amixer", "-q", "-c", "0", "sset"])


if __name__ == "__main__":
    unittest.main()
