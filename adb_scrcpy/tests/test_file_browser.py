import unittest
from unittest.mock import patch

from adb_scrcpy.adb_client import AdbClient


class FileBrowserTests(unittest.TestCase):
    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_list_files_uses_common_media_roots(self, run):
        run.return_value = type("Result", (), {"stdout": "/sdcard/DCIM/photo.jpg\n/sdcard/Pictures/a.png\n", "stderr": "", "returncode": 0})()
        files = AdbClient("SERIAL", "adb.exe").list_files()
        self.assertEqual(files, ["/sdcard/DCIM/photo.jpg", "/sdcard/Pictures/a.png"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("find" in args for args in commands))
        self.assertTrue(any("/sdcard/DCIM" in args for args in commands))
        self.assertTrue(any("head" in args for args in commands))
