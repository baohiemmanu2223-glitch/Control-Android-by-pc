import subprocess
import unittest
from unittest.mock import MagicMock, patch

from adb_scrcpy.scrcpy_manager import ScrcpyAlreadyRunningError, ScrcpyManager, ScrcpyProcessError


class ScrcpyManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = ScrcpyManager("scrcpy.exe", startup_timeout=0, stop_timeout=0.1)

    def test_build_profiles_and_safe_options(self):
        low_latency = self.manager.build_command("SERIAL", "low-latency", clipboard_autosync=False, rotation=1)
        self.assertEqual(low_latency[:3], ["scrcpy.exe", "-s", "SERIAL"])
        self.assertIn("--no-audio", low_latency)
        self.assertIn("--no-clipboard-autosync", low_latency)
        self.assertIn("--rotation=1", low_latency)
        recording = self.manager.build_command("SERIAL", "recording", record_path=r"out\capture.mp4")
        self.assertIn("--record", recording)

    def test_recording_requires_path_and_validates_rotation(self):
        with self.assertRaises(ValueError):
            self.manager.build_command("SERIAL", "recording")
        with self.assertRaises(ValueError):
            self.manager.build_command("SERIAL", rotation=4)

    @patch("adb_scrcpy.scrcpy_manager.subprocess.Popen")
    def test_start_reuses_running_session_and_stop_terminates(self, popen):
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 123
        popen.return_value = process
        first = self.manager.start("SERIAL")
        second = self.manager.start("SERIAL")
        self.assertIs(first, second)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.manager.status("SERIAL"), "running")
        self.assertTrue(self.manager.stop("SERIAL"))
        process.terminate.assert_called_once()

    @patch("adb_scrcpy.scrcpy_manager.subprocess.Popen")
    def test_can_reject_duplicate_session(self, popen):
        process = MagicMock()
        process.poll.return_value = None
        popen.return_value = process
        self.manager.start("SERIAL")
        with self.assertRaises(ScrcpyAlreadyRunningError):
            self.manager.start("SERIAL", reuse_existing=False)

    @patch("adb_scrcpy.scrcpy_manager.subprocess.Popen")
    def test_raises_when_process_crashes_at_start(self, popen):
        process = MagicMock()
        process.poll.return_value = 1
        process.returncode = 1
        popen.return_value = process
        with self.assertRaises(ScrcpyProcessError):
            self.manager.start("SERIAL")


if __name__ == "__main__":
    unittest.main()
