import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.cli import EXIT_DEVICE_ERROR, main
from adb_scrcpy.device_manager import Device
from adb_scrcpy.workflow import WorkflowContext, WorkflowRunner
from adb_scrcpy.workflow_spec import build_steps, has_mutating_actions, load_spec


class WorkflowSpecTests(unittest.TestCase):
    def _write(self, directory, payload):
        path = Path(directory) / "workflow.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_expands_variables_and_executes_dry_run_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, {
                "variables": {"X": "10", "PACKAGE": "com.example.app"},
                "steps": [
                    {"name": "tap", "kind": "action", "action": {"type": "tap", "x": "${X}", "y": 20}},
                    {"name": "installed", "kind": "assert", "condition": {"type": "package_installed", "package": "${PACKAGE}"}},
                ],
            })
            spec = load_spec(path)
            self.assertTrue(has_mutating_actions(spec))
            client = MagicMock()
            client.shell.return_value = "package:/data/app/example"
            context = WorkflowContext(client, Path(directory), {"dry_run": True, "workflow_base_dir": directory})
            result = WorkflowRunner(context).run(build_steps(spec))
        self.assertTrue(result.ok)
        client.tap.assert_not_called()
        self.assertEqual(context.data["dry_run_actions"][0]["x"], "10")

    def test_live_action_uses_health_guard_and_before_after_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = load_spec(self._write(directory, {"steps": [{"name": "tap", "kind": "action", "action": {"type": "tap", "x": 1, "y": 2}}]}))
            client = MagicMock(serial="SERIAL")
            client.save_screenshot.side_effect = lambda path: Path(path)
            guard = MagicMock()
            context = WorkflowContext(client, Path(directory), {"capture_actions": True, "health_guard": guard, "workflow_base_dir": directory})
            result = WorkflowRunner(context).run(build_steps(spec))
        self.assertTrue(result.ok)
        guard.ensure_safe.assert_called_once()
        self.assertEqual(client.save_screenshot.call_count, 2)
        self.assertEqual(len(context.data["action_artifacts"]), 1)

    @patch("adb_scrcpy.cli.AdbClient")
    @patch("adb_scrcpy.cli.DeviceManager")
    def test_cli_writes_failure_report_and_screenshot(self, manager_class, client_class):
        ready = Device("SERIAL", "device")
        manager_class.return_value.get.return_value = ready
        client_class.return_value.serial = "SERIAL"
        client_class.return_value.shell.return_value = "33"
        def save(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PNG")
            return path
        client_class.return_value.save_screenshot.side_effect = save
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[device]\nserial="SERIAL"\npackage="com.example.app"\n[runtime]\nartifacts_dir="' + directory.replace('\\', '/') + '"\n', encoding="utf-8")
            workflow = self._write(directory, {"steps": [{"name": "sdk", "kind": "assert", "condition": {"type": "shell_equals", "args": ["getprop"], "equals": "34"}}]})
            code = main(["workflow", "--config", str(config), str(workflow), "--json"])
            reports = list(Path(directory).glob("SERIAL/*/result.json"))
        self.assertEqual(code, EXIT_DEVICE_ERROR)
        self.assertEqual(len(reports), 1)


if __name__ == "__main__":
    unittest.main()
