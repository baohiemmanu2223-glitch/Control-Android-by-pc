import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_scrcpy.adb_client import AdbClient


class FileOperationTests(unittest.TestCase):
    def setUp(self):
        self.client = AdbClient("SERIAL", "adb.exe")

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_push_uses_serial_and_absolute_destination(self, run):
        run.return_value = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.txt"
            source.write_text("data")
            self.client.push(source, "/sdcard/Download/file.txt")
        self.assertEqual(run.call_args.args[0][-2:], [str(source), "/sdcard/Download/file.txt"])

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_pull_and_install_commands(self, run):
        run.return_value = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            apk.write_bytes(b"not-real-apk")
            self.client.pull("/sdcard/a.txt", Path(directory) / "a.txt")
            self.client.install(apk)
        self.assertIn("pull", run.call_args_list[0].args[0])
        self.assertIn("install", run.call_args_list[1].args[0])

    def test_rejects_relative_device_paths_and_non_apk(self):
        with self.assertRaises(ValueError):
            self.client.push(__file__, "relative/path")
        with self.assertRaises(ValueError):
            self.client.pull("relative/path", "out")


if __name__ == "__main__":
    unittest.main()
