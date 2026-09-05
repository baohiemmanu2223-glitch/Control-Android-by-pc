import unittest

from adb_scrcpy.device_manager import Device, DeviceManager


class DeviceParsingTests(unittest.TestCase):
    def test_parses_ready_device_and_metadata(self):
        device = DeviceManager._parse_device_line(
            "SERIAL123 device product:DemoProduct model:DemoModel device:DemoDevice transport_id:1"
        )
        self.assertEqual(device, Device("SERIAL123", "device", "DemoProduct", "DemoModel", "DemoDevice", "1"))
        self.assertTrue(device.ready)

    def test_classifies_offline_and_unauthorized(self):
        self.assertEqual(DeviceManager._parse_device_line("abc offline").state, "offline")
        self.assertEqual(DeviceManager._parse_device_line("def unauthorized").state, "unauthorized")

    def test_classifies_no_permissions(self):
        self.assertEqual(DeviceManager._parse_device_line("abc no permissions").state, "no permissions")


if __name__ == "__main__":
    unittest.main()
