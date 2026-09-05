import json
import unittest
from io import StringIO
from unittest.mock import patch

from adb_scrcpy.cli import EXIT_DEVICE_ERROR, EXIT_USAGE, main
from adb_scrcpy.device_manager import Device, DeviceStateError


READY = Device("SERIAL", "device", "product", "Model", "device", "1")


class CliTests(unittest.TestCase):
    @patch("adb_scrcpy.cli.DeviceManager")
    def test_devices_json(self, manager_class):
        manager_class.return_value.list_devices.return_value = [READY]
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["devices", "--adb", "adb.exe", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["devices"][0]["serial"], "SERIAL")

    @patch("adb_scrcpy.cli.DeviceManager")
    def test_check_returns_nonzero_for_unready_device(self, manager_class):
        manager_class.return_value.get.side_effect = DeviceStateError("SERIAL=unauthorized")
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["check", "--serial", "SERIAL", "--json"])
        self.assertEqual(code, EXIT_DEVICE_ERROR)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    @patch("adb_scrcpy.cli.AdbClient")
    @patch("adb_scrcpy.cli.DeviceManager")
    def test_check_json_includes_android_properties(self, manager_class, client_class):
        manager_class.return_value.get.return_value = READY
        client_class.return_value.shell.side_effect = ["14", "34"]
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["check", "--serial", "SERIAL", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["android_version"], "14")
        self.assertEqual(payload["sdk"], "34")

    @patch("adb_scrcpy.cli.DeviceManager")
    @patch("adb_scrcpy.cli.AdbClient")
    def test_tap_requires_confirm_and_dry_run_does_not_call_adb(self, client_class, manager_class):
        manager_class.return_value.get.return_value = READY
        client_class.return_value._argv.side_effect = lambda args: ["adb.exe", "-s", "SERIAL", *args]
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["tap", "--serial", "SERIAL", "10", "20", "--dry-run", "--json"])
        self.assertEqual(code, 0)
        client_class.return_value.tap.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["tap", "--serial", "SERIAL", "10", "20", "--json"])
        self.assertEqual(code, EXIT_USAGE)

    @patch("adb_scrcpy.cli.DeviceManager")
    @patch("adb_scrcpy.cli.AdbClient")
    def test_screenshot_saves_to_session_artifact(self, client_class, manager_class):
        manager_class.return_value.get.return_value = READY
        client_class.return_value.serial = "SERIAL"
        def save(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PNG")
            return path
        client_class.return_value.save_screenshot.side_effect = save
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["screenshot", "--serial", "SERIAL", "--out", "screen.png", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["bytes"], 3)

    @patch("adb_scrcpy.cli.DeviceManager")
    @patch("adb_scrcpy.cli.AdbClient")
    def test_shell_returns_command_status(self, client_class, manager_class):
        manager_class.return_value.get.return_value = READY
        client_class.return_value.serial = "SERIAL"
        client_class.return_value.run.return_value = type("Result", (), {"ok": True, "stdout": "14", "stderr": "", "returncode": 0})()
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["shell", "--serial", "SERIAL", "--json", "getprop", "ro.build.version.sdk"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["stdout"], "14")


if __name__ == "__main__":
    unittest.main()
