import unittest

from sentinel.web.bind_util import bind_host_allowed


class TestBindHost(unittest.TestCase):
    def test_localhost_ok(self) -> None:
        ok, _ = bind_host_allowed("127.0.0.1", "192.168.0.0/24")
        self.assertTrue(ok)

    def test_camera_subnet_refused(self) -> None:
        ok, msg = bind_host_allowed("192.168.0.50", "192.168.0.0/24")
        self.assertFalse(ok)
        self.assertIn("Refusing", msg)


if __name__ == "__main__":
    unittest.main()
