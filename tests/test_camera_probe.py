"""Tests for adding cameras of other brands: stream-path guessing and the Test connection probe."""

import _test_env  # noqa: F401  (isolates the event log)

import hashlib
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from sentinel.engine import camera_probe
from sentinel.recordings.hd_recorder import main_stream_url


class TestMainStreamGuess(unittest.TestCase):
    def test_brands(self):
        cases = {
            "rtsp://u:p@h:554/h264/ch1/sub/av_stream": "rtsp://u:p@h:554/h264/ch1/main/av_stream",
            "rtsp://h/cam/realmonitor?channel=1&subtype=1": "rtsp://h/cam/realmonitor?channel=1&subtype=0",
            "rtsp://h:554/Streaming/Channels/102": "rtsp://h:554/Streaming/Channels/101",
            "rtsp://h:554/Streaming/Channels/202": "rtsp://h:554/Streaming/Channels/201",
            "rtsp://u:p@h:554/h264Preview_01_sub": "rtsp://u:p@h:554/h264Preview_01_main",
            "rtsp://h:554/media/video2": "rtsp://h:554/media/video1",
            "rtsp://h:554/stream2": "rtsp://h:554/stream1",
            "rtsp://h:88/videoSub": "rtsp://h:88/videoMain",
        }
        for sub, main in cases.items():
            self.assertEqual(main_stream_url(sub), main, sub)

    def test_leaves_lookalikes_alone(self):
        self.assertIsNone(main_stream_url("rtsp://192.168.0.102:554/live"))  # 102 in the address, not the path
        self.assertIsNone(main_stream_url("rtsp://h:5102/live"))
        self.assertIsNone(main_stream_url("rtsp://h/live/ch0"))


class _FakeCamera(threading.Thread):
    """Minimal RTSP server: digest login admin/secret, stream only at /good."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if not data:
                        break
                    text = data.decode()
                    url = text.split()[1]
                    cseq = re.search(r"CSeq: (\d+)", text).group(1)
                    auth = re.search(r"Authorization: Digest (.*)", text)
                    status = "401 Unauthorized"
                    if auth:
                        p = dict(re.findall(r'(\w+)="([^"]*)"', auth.group(1)))
                        ha1 = hashlib.md5(b"admin:cam:secret").hexdigest()
                        ha2 = hashlib.md5(f"DESCRIBE:{url}".encode()).hexdigest()
                        good = p.get("response") == hashlib.md5(f"{ha1}:n1:{ha2}".encode()).hexdigest()
                        status = ("200 OK" if url.endswith("/good") else "404 Not Found") if good else "401 Unauthorized"
                    extra = 'WWW-Authenticate: Digest realm="cam", nonce="n1"\r\n' if status.startswith("401") else ""
                    conn.sendall(f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())

    def close(self):
        self.sock.close()


class TestProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cam = _FakeCamera()
        cls.cam.start()

    @classmethod
    def tearDownClass(cls):
        cls.cam.close()

    def test_status_codes(self):
        port = self.cam.port
        self.assertEqual(camera_probe.rtsp_status("127.0.0.1", port, "/good", "admin", "secret"), 200)
        self.assertEqual(camera_probe.rtsp_status("127.0.0.1", port, "/good", "admin", "wrong"), 401)
        self.assertEqual(camera_probe.rtsp_status("127.0.0.1", port, "/nope", "admin", "secret"), 404)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        self.assertIsNone(camera_probe.rtsp_status("127.0.0.1", closed, "/good", "admin", "secret", timeout=1))

    def test_plain_english_results(self):
        port = self.cam.port
        probe = lambda path, pw, main=None: camera_probe.probe_camera(  # noqa: E731
            "127.0.0.1", port, path, "admin", pw, "rtsp://x", main)
        self.assertIn("rejected the username or password", probe("/good", "wrong")["message"])
        self.assertIn("no video at that stream path", probe("/nope", "secret")["message"])
        with mock.patch.object(camera_probe, "grab_frame", return_value=None):
            self.assertIn("couldn't be decoded", probe("/good", "secret")["message"])
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        with mock.patch.object(camera_probe, "grab_frame", return_value=frame):
            ok = probe("/good", "secret", f"rtsp://127.0.0.1:{port}/good")
            self.assertTrue(ok["ok"])
            self.assertIn("1280×720", ok["message"])
            self.assertTrue(ok["hd"])
            self.assertTrue(ok["jpeg"].startswith(b"\xff\xd8"))
            self.assertFalse(probe("/good", "secret", f"rtsp://127.0.0.1:{port}/nope")["hd"])
            self.assertIn("normal quality", probe("/good", "secret", None)["hd_message"])
        with mock.patch.object(camera_probe, "grab_frame", return_value=np.zeros((1440, 2560, 3), dtype=np.uint8)):
            heavy = probe("/good", "secret")
            self.assertTrue(heavy["ok"] and heavy["heavy"])
            self.assertIn("high-res main stream", heavy["message"])


class TestAddCameraForm(unittest.TestCase):
    def setUp(self):
        from sentinel.engine import Engine
        from sentinel.settings.manager import SettingsManager

        self.dir = tempfile.mkdtemp(prefix="sentinel-addcam-")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.engine = Engine(SettingsManager(settings_file=os.path.join(self.dir, "settings.json")))

    def tearDown(self):
        for p in list(self.engine.cameras.values()):
            p.stop(timeout=2)
        os.chdir(self.old_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_validation(self):
        spec = self.engine._camera_spec
        self.assertIsNone(spec({"host": "bad host!"})[0])
        self.assertIsNone(spec({"host": "10.0.0.5", "port": 99999})[0])
        self.assertIsNone(spec({"host": "10.0.0.5", "path": "no-slash"})[0])
        self.assertIsNone(spec({"host": "10.0.0.5", "path": "/ok", "main_path": "/has space"})[0])
        fields, _ = spec({"host": "10.0.0.5", "path": "/cam/realmonitor?channel=1&subtype=1",
                          "username": "ad min", "password": "p@ss"})
        self.assertEqual(fields["source"], "rtsp://ad%20min:p%40ss@10.0.0.5:554/cam/realmonitor?channel=1&subtype=1")

    def test_manual_high_res_path_is_kept(self):
        with mock.patch("sentinel.engine.camera.CameraPipeline.start"):
            ok, key = self.engine.add_camera({"host": "10.0.0.7", "path": "/live/sub", "main_path": "/live/main",
                                              "username": "u", "password": "p", "name": "Shed"})
        self.assertTrue(ok)
        profile = next(iter(self.engine.settings["camera_profiles"].values()))
        self.assertEqual(profile["record_source"], "rtsp://u:p@10.0.0.7:554/live/main")
        self.assertEqual(self.engine.cameras[key].hd_source(), "rtsp://u:p@10.0.0.7:554/live/main")

    def test_test_camera_reports_form_errors(self):
        self.assertEqual(self.engine.test_camera({"host": ""}), {"ok": False, "message": "enter the camera's IP address or hostname"})


if __name__ == "__main__":
    unittest.main()
