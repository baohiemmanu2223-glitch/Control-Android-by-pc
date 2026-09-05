import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.gui import DeviceDashboard


TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "config.example.toml")


class GuiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.poll = patch.object(DeviceDashboard, "_poll_devices")
        self.poll.start()
        self.window = DeviceDashboard(TEST_CONFIG)

    def tearDown(self):
        self.window._on_close()
        self.poll.stop()

    def test_runs_dry_run_workflow_and_writes_report(self):
        client = MagicMock()
        client.serial = "SERIAL"
        client.shell.return_value = "34"
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "workflow.json"
            workflow.write_text(json.dumps({
                "steps": [
                    {"name": "tap", "kind": "action", "action": {"type": "tap", "x": 10, "y": 20}},
                    {"name": "sdk", "kind": "assert", "condition": {"type": "shell_equals", "args": ["getprop"], "equals": "34"}},
                ]
            }), encoding="utf-8")
            session = Path(directory) / "session"
            session.mkdir()
            self.window.workflow_path.set(str(workflow))
            self.window.dry_run_var.set(True)
            self.window._gui_client = MagicMock(return_value=client)
            self.window._gui_session_dir = MagicMock(return_value=session)
            self.window._submit = lambda action, success, _label, *_args: success(action())
            self.window._run_workflow_gui()
            report = json.loads((session / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["simulated_actions"], 1)
        client.tap.assert_not_called()

    def test_runs_dry_run_workflow_for_requested_repeat_count(self):
        client = MagicMock()
        client.serial = "SERIAL"
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "repeat.json"
            workflow.write_text(json.dumps({"steps": [{"name": "tap", "kind": "action", "action": {"type": "tap", "x": 10, "y": 20}}]}), encoding="utf-8")
            session = Path(directory) / "session"
            session.mkdir()
            self.window.workflow_path.set(str(workflow))
            self.window.workflow_repeat_var.set(3)
            self.window.dry_run_var.set(True)
            self.window._gui_client = MagicMock(return_value=client)
            self.window._gui_session_dir = MagicMock(return_value=session)
            self.window._submit = lambda action, success, _label, *_args: success(action())
            self.window._run_workflow_gui()
            report = json.loads((session / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["repeat_count"], 3)
        self.assertEqual(report["loops_completed"], 3)
        self.assertEqual(report["simulated_actions"], 3)
        client.tap.assert_not_called()

    def test_automation_workspace_exposes_run_editor_and_queue(self):
        self.assertEqual(self.window.automation_scope["text"], "Select a device")
        self.assertEqual(self.window.workflow_steps["height"], 3)
        self.assertEqual(self.window.queue_tree["height"], 2)
        self.assertEqual(self.window.automation_input_hint["text"], "Simulation only")
        self.window.dry_run_var.set(False)
        self.window.confirm_var.set(True)
        self.window._update_input_state()
        self.assertEqual(self.window.run_workflow_button["text"], "Run live")
        self.assertEqual(self.window.automation_input_hint["text"], "Live input enabled")
        self.window._select_route("Automation")
        self.assertEqual(self.window.tabs.tab(self.window.tabs.select(), "text"), "Workflow")
        self.assertEqual(self.window._sidebar_buttons["Automation"]["style"], "NavActive.TButton")

    def test_workflow_target_package_is_optional_and_per_workflow(self):
        self.assertIsNone(self.window._resolve_workflow_package({"variables": {}}))
        self.window.workflow_package_var.set("")
        self.assertEqual(self.window._resolve_workflow_package({"variables": {"PACKAGE": "com.example.other"}}), "com.example.other")
        self.window.workflow_package_var.set("")
        self.assertEqual(self.window._resolve_workflow_package({"package": "com.example.recorded"}), "com.example.recorded")
        self.window.workflow_package_var.set("com.example.manual")
        self.assertEqual(self.window._resolve_workflow_package({"variables": {"PACKAGE": "com.example.other"}}), "com.example.manual")
        self.window.workflow_package_var.set("bad package")
        with self.assertRaises(ValueError):
            self.window._resolve_workflow_package({"variables": {}})


if __name__ == "__main__":
    unittest.main()
