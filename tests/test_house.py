"""House page: cleaning the drawing and devices, the saved file, and its web routes."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from sentinel.engine.house import HouseLayout, clean_device, clean_drawing
from sentinel.settings.manager import SettingsManager

ROOMS = [
    {"id": "living", "name": "Living room", "r": [0, 0, 400, 300], "y": -1.3, "gaps": [[400, 0, 400, 100, "door"]]},
    {"id": "kitchen", "name": "Kitchen", "parts": [[400, 0, 700, 300]],
     "poly": [[400, 0], [700, 0], [700, 300], [400, 300]]},
    {"id": "up", "name": "Bedroom", "r": [0, 0, 400, 300], "level": 1},
]


class TestClean(unittest.TestCase):
    def test_rooms_kept_and_tidied(self):
        d = clean_drawing({"rooms": ROOMS, "features": [], "marks": []})
        self.assertEqual([r["id"] for r in d["rooms"]], ["living", "kitchen", "up"])
        living = d["rooms"][0]
        self.assertEqual(living["parts"], [[0, 0, 400, 300]])
        self.assertEqual(living["y"], -1.3)
        self.assertEqual(living["gaps"], [[400, 0, 400, 100, "door"]])
        self.assertEqual(d["rooms"][2]["level"], 1)

    def test_bad_rooms_dropped(self):
        d = clean_drawing({"rooms": ROOMS + [
            {"id": "x", "name": "", "r": [0, 0, 1, 1]},            # no name
            {"id": "Bad id!", "name": "Den", "r": [0, 0, 1, 1]},   # bad id
            {"id": "flat", "name": "Flat", "r": [5, 5, 5, 9]},     # no width
            {"id": "living", "name": "Again", "r": [0, 0, 1, 1]},  # duplicate id
            None, "room", 7]})
        self.assertEqual(len(d["rooms"]), 3)

    def test_needs_a_room(self):
        for raw in ({}, {"rooms": []}, {"rooms": "lots"}, [], None):
            with self.assertRaises(ValueError):
                clean_drawing(raw)

    def test_names_are_plain_text(self):
        d = clean_drawing({"rooms": [{"id": "a", "name": "Den\x00\n<b>", "r": [0, 0, 1, 1]}]})
        self.assertEqual(d["rooms"][0]["name"], "Den<b>")  # control characters gone; the page escapes the rest

    def test_features(self):
        d = clean_drawing({"rooms": ROOMS, "features": [
            {"type": "steps", "from": [0, 0], "to": [100, 0], "y0": -1.3, "y1": 0, "width": 40, "count": 6, "solid": True},
            {"type": "window", "a": [0, 0], "b": [50, 0], "door": True},
            {"type": "box", "at": [10, 10], "size": [20, 30], "h": 0.9, "color": "#C8955A"},
            {"type": "rail", "a": [400, 0], "b": [400, 100], "h": 1.0},
            {"type": "steps", "from": [0, 0], "to": [1, 0]},       # no heights
            {"type": "box", "at": [1, 1], "size": [0, 5], "h": 1},  # flat
            {"type": "laser", "a": [0, 0], "b": [1, 1]},
            {"type": "door", "a": [1, 1], "b": [1, 1]},            # zero length
            {"type": "box", "at": [1, 1], "size": [2, 2], "h": 1, "color": "red; background:url(x)"},
        ]})
        self.assertEqual([f["type"] for f in d["features"]], ["steps", "window", "box", "rail", "box"])
        self.assertTrue(d["features"][0]["solid"])
        self.assertEqual(d["features"][2]["color"], "#c8955a")
        self.assertNotIn("color", d["features"][4])

    def test_numbers_are_bounded(self):
        d = clean_drawing({"rooms": [{"id": "a", "name": "A", "r": [0, 0, 1e9, 10], "y": 1e9}]})
        self.assertEqual(d["rooms"][0]["parts"][0][2], 10000.0)
        self.assertEqual(d["rooms"][0]["y"], 20.0)
        with self.assertRaises(ValueError):
            clean_drawing({"rooms": [{"id": "a", "name": "A", "r": [0, 0, True, 10]}]})

    def test_device(self):
        dev = clean_device({"kind": "switch", "ref": "kasa-9c532256c25f-1", "name": "Patio", "at": [10, 20], "level": 1, "turn": 45})
        self.assertEqual(dev["id"], "switch:kasa-9c532256c25f-1")
        self.assertEqual((dev["at"], dev["level"], dev["turn"]), ([10, 20], 1, 45))
        self.assertEqual(clean_device({"kind": "camera", "ref": "192.168.0.10", "at": [0, 0]})["id"], "camera:192.168.0.10")
        # a remote taught to the BroadLink goes by its name (spaces allowed there only)
        self.assertEqual(clean_device({"kind": "remote", "ref": "Bedroom Ceiling Fan", "at": [5, 5]})["id"], "remote:Bedroom Ceiling Fan")
        for bad in ({"kind": "toaster", "ref": "a", "at": [0, 0]}, {"kind": "sensor", "ref": "a b", "at": [0, 0]},
                    {"kind": "remote", "ref": "fan<script>", "at": [0, 0]}, {"kind": "remote", "ref": "", "at": [0, 0]},
                    {"kind": "sensor", "ref": "8d24", "at": [0]}, {"kind": "sensor", "ref": None, "at": [0, 0]}, None):
            with self.assertRaises(ValueError):
                clean_device(bad)


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-house-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_empty_then_saved(self):
        house = HouseLayout(self.dir)
        self.assertEqual(house.view(), {"drawing": None, "devices": []})
        self.assertEqual(house.set_drawing({"rooms": ROOMS}), (True, "saved 3 rooms"))
        self.assertEqual(house.place({"kind": "sensor", "ref": "8d24", "at": [100, 100]}), (True, "placed"))
        again = HouseLayout(self.dir)
        self.assertEqual(len(again.view()["drawing"]["rooms"]), 3)
        self.assertEqual(again.view()["devices"][0]["id"], "sensor:8d24")

    def test_move_and_remove(self):
        house = HouseLayout(self.dir)
        house.place({"kind": "sensor", "ref": "8d24", "at": [100, 100]})
        house.place({"kind": "sensor", "ref": "8d24", "at": [200, 50]})
        self.assertEqual([d["at"] for d in house.view()["devices"]], [[200, 50]])
        self.assertEqual(house.remove("sensor:8d24"), (True, "removed"))
        self.assertEqual(house.remove("sensor:8d24"), (False, "it isn't on the map"))
        self.assertEqual(house.remove(None)[0], False)

    def test_renamed_remote_keeps_its_place(self):
        house = HouseLayout(self.dir)
        house.place({"kind": "remote", "ref": "Fan", "name": "Fan", "at": [100, 100], "level": 1})
        house.rename_ref("remote", "Fan", "Bedroom fan")
        house.rename_ref("remote", "Nothing", "Else")  # (not on the map: nothing happens)
        dev = HouseLayout(self.dir).view()["devices"][0]
        self.assertEqual((dev["id"], dev["ref"], dev["at"], dev["level"]), ("remote:Bedroom fan", "Bedroom fan", [100, 100], 1))

    def test_bad_drawing_keeps_the_old_one(self):
        house = HouseLayout(self.dir)
        house.set_drawing({"rooms": ROOMS})
        self.assertFalse(house.set_drawing({"rooms": []})[0])
        self.assertEqual(len(house.view()["drawing"]["rooms"]), 3)

    def test_new_drawing_keeps_devices(self):
        house = HouseLayout(self.dir)
        house.set_drawing({"rooms": ROOMS})
        house.place({"kind": "camera", "ref": "192.168.0.10", "at": [0, 0]})
        house.set_drawing({"rooms": ROOMS[:1]})
        self.assertEqual(len(house.view()["devices"]), 1)

    def test_broken_file(self):
        with open(os.path.join(self.dir, "house.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(HouseLayout(self.dir).view(), {"drawing": None, "devices": []})
        with open(os.path.join(self.dir, "house.json"), "w") as f:
            json.dump({"rooms": "x", "devices": [{"kind": "sensor", "ref": "8d24", "at": [1, 2]}, "junk"]}, f)
        view = HouseLayout(self.dir).view()
        self.assertIsNone(view["drawing"])
        self.assertEqual(len(view["devices"]), 1)


class TestWeb(unittest.TestCase):
    def setUp(self):
        import socket

        import bcrypt

        from sentinel.web.frame_broker import FrameBroker
        from sentinel.web.server import WebServer

        self.dir = tempfile.mkdtemp(prefix="sentinel-house-web-")
        self.house = HouseLayout(self.dir)
        self.house.set_drawing({"rooms": ROOMS})
        manager = SettingsManager(settings_file=os.path.join(self.dir, "settings.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        manager.settings["web"].update({"enabled": True, "port": port})
        manager.settings["web"]["auth"].update({"username": "alex", "password_hash": bcrypt.hashpw(
            b"pw", bcrypt.gensalt(rounds=4)).decode()})
        self.broker = FrameBroker()
        self.broker.publish("rtsp://u:secret@10.0.0.5/s", "Front", np.zeros((2, 2, 3), dtype=np.uint8))
        engine = mock.Mock(house=self.house)
        engine.house_live.return_value = {"now": 1.0, "paths": [], "boards": [], "switches": []}
        self.server = WebServer(manager, self.broker, engine=engine)
        self.assertTrue(self.server.start())
        self.server._auth.validate_session = lambda token, ip: mock.Mock(username="alex")
        self.client = self.server._app.test_client()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body, header=True):
        return self.client.post(path, json=body, headers={"X-Sentinel-Request": "1"} if header else {})

    def test_page_and_drawing(self):
        self.assertEqual(self.client.get("/house").status_code, 200)
        self.assertEqual(len(self.client.get("/api/house").get_json()["drawing"]["rooms"]), 3)

    def test_live_cameras_by_address_not_url(self):
        cams = self.client.get("/api/house/live").get_json()["cameras"]
        self.assertEqual([(c["id"], c["name"]) for c in cams], [("10.0.0.5", "Front")])
        self.assertEqual(cams[0]["web"], self.broker.list_cameras()[0][0])
        self.assertNotIn("secret", json.dumps(cams))

    def test_place_and_remove(self):
        r = self.post("/api/house/device", {"kind": "camera", "ref": "10.0.0.5", "at": [50, 50]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["devices"][0]["id"], "camera:10.0.0.5")
        self.assertEqual(self.post("/api/house/device", {"kind": "camera", "ref": "10.0.0.5", "at": "here"}).status_code, 400)
        self.assertEqual(self.post("/api/house/device", {"id": "camera:10.0.0.5", "remove": True}).get_json()["devices"], [])

    def test_writes_need_the_header(self):
        self.assertEqual(self.post("/api/house/device", {"kind": "sensor", "ref": "8d24", "at": [1, 1]}, header=False).status_code, 400)
        self.assertEqual(self.house.view()["devices"], [])

    def test_replace_drawing(self):
        self.assertEqual(self.post("/api/house/drawing", {"rooms": ROOMS[:2]}).status_code, 200)
        self.assertEqual(len(self.house.view()["drawing"]["rooms"]), 2)
        self.assertEqual(self.post("/api/house/drawing", {"rooms": []}).status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TestHouseLive(unittest.TestCase):
    def test_remotes_and_heater_in_the_poll(self):
        from sentinel.engine.engine import Engine

        fake = mock.Mock()
        fake.wifi_motion.view.return_value = {"now": 1.0, "sensors": [], "boards": []}
        fake.switches.view.return_value = {"switches": []}
        fake.remotes.view.return_value = {"connected": True, "remotes": [
            {"name": "Heater", "buttons": [{"name": "Power", "kind": "ir"}, {"name": "Temp up", "kind": "ir"}]}]}
        fake.heater.view.return_value = {"enabled": True, "target_f": 68, "room_f": 70.2, "celsius": False,
                                         "buttons": {"remote": "Heater", "up": "Temp up", "down": "Temp down"}}
        live = Engine.house_live(fake)
        self.assertEqual(live["remotes"], [{"id": "Heater", "name": "Heater", "online": True, "buttons": ["Power", "Temp up"]}])
        self.assertEqual(live["heater"], {"enabled": True, "target_f": 68, "room_f": 70.2, "celsius": False, "remote": "Heater"})
        fake.heater.view.return_value["buttons"] = None  # no heater remote found yet
        self.assertIsNone(Engine.house_live(fake)["heater"]["remote"])
