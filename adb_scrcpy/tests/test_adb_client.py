import subprocess
import unittest
from unittest.mock import patch

from adb_scrcpy.adb_client import AdbClient, AdbCommandError


def completed(stdout="ok", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class AdbClientTests(unittest.TestCase):
    def setUp(self):
        self.client = AdbClient("SERIAL", adb_path="adb.exe")

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_uses_serial_and_no_shell(self, run):
        run.return_value = completed("ok")
        result = self.client.tap(10, 20)
        self.assertTrue(result.ok)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["adb.exe", "-s", "SERIAL", "shell", "input", "tap", "10", "20"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_text_redacted_in_log_and_spaces_encoded(self, run):
        run.return_value = completed("ok")
        with self.assertLogs("adb_scrcpy.adb", level="INFO") as logs:
            self.client.text("secret value")
        self.assertIn("<redacted>", logs.output[0])
        self.assertNotIn("secret", logs.output[0])
        self.assertEqual(run.call_args.args[0][-1], "secret%svalue")

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_read_operation_retries_connection_error(self, run):
        run.side_effect = [completed("", "device offline", 1), completed("value")]
        self.assertEqual(self.client.shell("getprop", "ro.build.version.sdk"), "value")
        self.assertEqual(run.call_count, 2)

    @patch("adb_scrcpy.adb_client.subprocess.run")
    def test_input_operation_does_not_retry(self, run):
        run.return_value = completed("", "device offline", 1)
        with self.assertRaises(AdbCommandError):
            self.client.tap(1, 2)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
