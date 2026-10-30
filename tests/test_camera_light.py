"""Tests for the camera white light Sentinel switches on for cars at night."""

import _test_env  # noqa: F401  (isolates the event log and the camera logins)

import json
import os
import shutil
import tempfile
import types
import unittest

import numpy as np

from sentinel.engine import camera_light
from sentinel.engine.camera_light import LIGHT_SECONDS, CameraLights

IR = ('<SupplementLight><supplementLightMode>eventIntelligence</supplementLightMode>'
      '<whiteLightTime>60</whiteLightTime></SupplementLight>')


class FakeCamera:
    def __init__(self, setting=IR):
        self.setting = setting
        self.calls = []
        self.fail = False

    def __call__(self, host, method, body):
        self.calls.append(method)
        if self.fail:
            raise OSError("no route to host")
        if method == "PUT":
            self.setting = body
        return self.setting


class TestCameraLights(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-light-")
        self.admin = os.path.join(self.dir, "camera_admin.json")
        with open(self.admin, "w") as f:
            json.dump({"192.168.0.9": {"user": "admin", "password": "x"}}, f)
        self.state = os.path.join(self.dir, "light.json")
        self.now = 1000.0
        self.cam = FakeCamera()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lights(self):
        return CameraLights(self.admin, self.state, clock=lambda: self.now, http=self.cam)

    def tick(self, lights):
        lights._wake.set()
        # one round of the worker loop, without its thread
        now = self.now
        wanted = {h for h, t in lights._until.items() if t > now}
        for host in wanted - set(lights._lit):
            lights._turn_on(host)
        for host in set(lights._lit) - wanted:
            lights._turn_off(host)

    def test_on_for_a_car_then_back_to_the_cameras_own_setting(self):
        lights = self.lights()
        self.assertFalse(lights.available("192.168.0.10"))  # no admin login: never touched
        lights.request("192.168.0.9")
        self.tick(lights)
        self.assertIn("colorVuWhiteLight", self.cam.setting)
        self.assertTrue(lights.is_on("192.168.0.9"))
        self.assertTrue(os.path.exists(self.state))  # remembered in case Sentinel stops now
        self.now += LIGHT_SECONDS - 10
        lights.request("192.168.0.9")  # another car: keep it on
        self.now += 20
        self.tick(lights)
        self.assertTrue(lights.is_on("192.168.0.9"))
        self.now += LIGHT_SECONDS
        self.tick(lights)
        self.assertEqual(self.cam.setting, IR)
        self.assertFalse(lights.is_on("192.168.0.9"))
        self.assertFalse(os.path.exists(self.state))

    def test_white_all_night_is_left_alone(self):
        self.cam.setting = IR.replace("eventIntelligence", "colorVuWhiteLight")
        lights = self.lights()
        lights.request("192.168.0.9")
        self.tick(lights)
        self.assertEqual(self.cam.calls, ["GET"])
        self.assertFalse(lights.is_on("192.168.0.9"))

    def test_left_on_by_a_stop_is_put_back_on_start(self):
        lights = self.lights()
        lights.request("192.168.0.9")
        self.tick(lights)  # ... and Sentinel is killed here
        again = self.lights()
        again._restore_leftovers()
        self.assertEqual(self.cam.setting, IR)
        self.assertFalse(os.path.exists(self.state))

    def test_unreachable_camera_doesnt_break_anything(self):
        self.cam.fail = True
        lights = self.lights()
        lights.request("192.168.0.9")
        self.tick(lights)
        self.assertFalse(lights.is_on("192.168.0.9"))
        self.assertEqual(lights._until, {})

    def test_tests_never_see_the_real_logins(self):
        self.assertNotEqual(os.path.abspath(camera_light.ADMIN_FILE), os.path.abspath("camera_admin.json"))
        self.assertFalse(CameraLights().available("192.168.0.9"))


class TestLightForCars(unittest.TestCase):
    """The Garage pipeline asks for the light only for a car moving in the dark."""

    def setUp(self):
        from sentinel.engine.camera import CameraPipeline
        self.asked = []
        lights = types.SimpleNamespace(available=lambda h: True, request=self.asked.append)
        self.pipe = types.SimpleNamespace(host="192.168.0.9", _detections=types.SimpleNamespace(lights=lights))
        self.call = lambda tracks, frame: CameraPipeline._light_for_cars(self.pipe, tracks, frame, lambda b: True)

    @staticmethod
    def car(moved):
        return types.SimpleNamespace(group="vehicle", box=(100, 100, 300, 200), vehicle_moved=lambda w: moved)

    def test_only_moving_cars_after_dark(self):
        night = np.full((720, 1280, 3), 50, np.uint8)
        day = np.zeros((720, 1280, 3), np.uint8); day[:] = (60, 140, 90)
        self.call([self.car(False)], night)
        self.assertEqual(self.asked, [])  # a parked car
        self.call([self.car(True)], day)
        self.assertEqual(self.asked, [])  # daylight: no need
        self.call([self.car(True)], night)
        self.assertEqual(self.asked, ["192.168.0.9"])


class TestColourUnderTheLight(unittest.TestCase):
    def test_lit_frames_count_lamp_frames_dont(self):
        from sentinel.engine.household import crop_colour
        dark_colour = np.zeros((360, 640, 3), np.uint8); dark_colour[:] = (90, 40, 10)  # dark, blue car
        self.assertIsNone(crop_colour(dark_colour, (10, 10, 100, 80)))  # lamp light: not trusted
        self.assertIsNotNone(crop_colour(dark_colour, (10, 10, 100, 80), lit=True))  # our white light
        grey = np.full((360, 640, 3), 90, np.uint8)
        self.assertIsNone(crop_colour(grey, (10, 10, 100, 80), lit=True))  # still infrared


if __name__ == "__main__":
    unittest.main()
