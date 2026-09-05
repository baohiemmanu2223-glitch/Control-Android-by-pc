import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.config import RuntimeConfig
from adb_scrcpy.safety import DryRunResult, EmergencyStop, SafeAdbClient, SafetyController
from adb_scrcpy.workflow import WorkflowContext, WorkflowRunner, WorkflowStep


class Phase5Tests(unittest.TestCase):
    def test_config_loads_and_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[device]\nserial="SERIAL"\npackage="com.example.app"\n[video]\nmax_fps=60\n', encoding="utf-8")
            config = RuntimeConfig.from_toml(path)
        self.assertEqual(config.serial, "SERIAL")
        self.assertEqual(config.package, "com.example.app")
        self.assertEqual(config.retention_days, 30)

    def test_dry_run_skips_mutating_adb(self):
        safety = SafetyController(dry_run=True)
        client = SafeAdbClient("SERIAL", adb_path="adb", safety=safety)
        with patch("adb_scrcpy.adb_client.subprocess.run") as run:
            result = client.tap(1, 2)
        self.assertIsInstance(result, DryRunResult)
        run.assert_not_called()

    def test_emergency_stop_blocks_next_action(self):
        safety = SafetyController()
        client = SafeAdbClient("SERIAL", safety=safety)
        safety.request_stop()
        with self.assertRaises(EmergencyStop):
            client.keyevent("KEYCODE_BACK")

    def test_failure_scenarios_are_reported(self):
        client = MagicMock()
        # Cable lost, lock screen, permission popup and app closed are all fail-closed.
        for scenario in ("cable-lost", "screen-locked", "permission-popup", "app-closed"):
            with self.subTest(scenario=scenario):
                context = WorkflowContext(client)
                result = WorkflowRunner(context).run([
                    WorkflowStep(scenario, "wait", condition=lambda ctx: False, timeout=0.01, poll_interval=0.001),
                ])
                self.assertEqual(result.status, "failed")
                self.assertIn("timeout", result.steps[0].error)


if __name__ == "__main__":
    unittest.main()
