import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.cli import main
from adb_scrcpy.device_manager import Device


READY = Device("SERIAL", "device", "product", "Model", "device", "1")


class CliScrcpyTests(unittest.TestCase):
    @patch("adb_scrcpy.cli.ScrcpyManager")
    @patch("adb_scrcpy.cli.DeviceManager")
    def test_scrcpy_detach_writes_registry(self, manager_class, scrcpy_class):
        manager_class.return_value.get.return_value = READY
        process = MagicMock()
        process.pid = 456
        process.poll.return_value = None
        scrcpy_class.return_value.start.return_value = type("Session", (), {"process": process, "command": ("scrcpy.exe", "-s", "SERIAL"), "running": True})()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[device]\nserial="SERIAL"\npackage="com.example.app"\n[tools]\nscrcpy_path="scrcpy.exe"\n[runtime]\nartifacts_dir="' + directory.replace('\\', '/') + '"\n', encoding="utf-8")
            code = main(["scrcpy", "--config", str(config), "--detach", "--json"])
            registry = Path(directory) / "SERIAL" / "scrcpy-session.json"
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(registry.read_text())["pid"], 456)

    @patch("adb_scrcpy.cli.subprocess.run")
    def test_stop_targets_registry_pid(self, run):
        run.return_value = type("Result", (), {"returncode": 0, "stderr": ""})()
        with tempfile.TemporaryDirectory() as directory:
            registry_dir = Path(directory) / "SERIAL"
            registry_dir.mkdir()
            (registry_dir / "scrcpy-session.json").write_text('{"serial":"SERIAL","pid":456}', encoding="utf-8")
            config = Path(directory) / "config.toml"
            config.write_text('[device]\nserial="SERIAL"\npackage="com.example.app"\n[runtime]\nartifacts_dir="' + directory.replace('\\', '/') + '"\n', encoding="utf-8")
            code = main(["stop", "--config", str(config), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "456", "/T", "/F"])

    @patch("adb_scrcpy.cli.subprocess.run")
    def test_stop_is_idempotent_when_pid_already_gone(self, run):
        run.return_value = type("Result", (), {"returncode": 128, "stderr": "ERROR: The process \"456\" not found."})()
        with tempfile.TemporaryDirectory() as directory:
            registry_dir = Path(directory) / "SERIAL"
            registry_dir.mkdir()
            (registry_dir / "scrcpy-session.json").write_text('{"serial":"SERIAL","pid":456}', encoding="utf-8")
            config = Path(directory) / "config.toml"
            config.write_text('[device]\nserial="SERIAL"\npackage="com.example.app"\n[runtime]\nartifacts_dir="' + directory.replace('\\', '/') + '"\n', encoding="utf-8")
            self.assertEqual(main(["stop", "--config", str(config), "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
