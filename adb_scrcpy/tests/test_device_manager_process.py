import subprocess
import unittest
from unittest.mock import patch

from adb_scrcpy.device_manager import DeviceManager


class DeviceManagerProcessTests(unittest.TestCase):
    @patch("adb_scrcpy.device_manager.subprocess.run")
    def test_adb_poll_suppresses_console_window(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "List of devices attached\n", "")
        DeviceManager("adb.exe").list_devices()
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @patch("adb_scrcpy.device_manager.subprocess.run")
    def test_reconnect_restarts_only_adb_daemon(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        DeviceManager("adb.exe").reconnect()
        self.assertEqual([call.args[0] for call in run.call_args_list], [["adb.exe", "kill-server"], ["adb.exe", "start-server"]])

    @patch("adb_scrcpy.device_manager.subprocess.run")
    def test_stop_server_only_stops_local_adb_daemon(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        DeviceManager("adb.exe").stop_server()
        self.assertEqual([call.args[0] for call in run.call_args_list], [["adb.exe", "kill-server"]])


if __name__ == "__main__":
    unittest.main()
