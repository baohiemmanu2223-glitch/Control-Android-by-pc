import io
import unittest
from unittest.mock import MagicMock

from PIL import Image

from adb_scrcpy.device_health import DeviceHealthMonitor
from adb_scrcpy.geometry import GeometryProvider, ScreenGeometry, parse_density, parse_rotation, parse_wm_size
from adb_scrcpy.metrics import FpsMeter, LatencyProbe
from adb_scrcpy.recognition_pipeline import RecognitionPipeline


class Phase5ReliabilityTests(unittest.TestCase):
    def test_geometry_parsers_and_provider(self):
        self.assertEqual(parse_wm_size("Physical size: 1240x2772"), (1240, 2772))
        self.assertEqual(parse_density("Physical density: 560"), 560)
        self.assertEqual(parse_rotation("0\n"), 0)
        client = MagicMock()
        client.shell.side_effect = ["Physical size: 1240x2772", "Physical density: 560", "0"]
        geometry = GeometryProvider(client).read()
        self.assertEqual((geometry.width, geometry.height, geometry.density), (1240, 2772, 560))
        self.assertEqual(ScreenGeometry(100, 200, rotation=1).normalize_point(10, 20, (100, 200)), (90, 20))

    def test_health_reports_lock_and_stopped_app(self):
        client = MagicMock(serial="SERIAL")
        client.shell.side_effect = ["1", "isStatusBarKeyguard=true", "", ""]
        report = DeviceHealthMonitor(client, "com.example.app").assess()
        self.assertFalse(report.safe)
        self.assertIn("screen_locked", report.blockers)
        self.assertIn("app_not_running", report.blockers)

    def test_health_reports_permission_controller_popup(self):
        client = MagicMock(serial="SERIAL")
        client.shell.side_effect = ["1", "mCurrentFocus=Window{permissioncontroller}", "", "1234"]
        report = DeviceHealthMonitor(client, "com.example.app").assess()
        self.assertTrue(report.permission_popup)
        self.assertIn("permission_popup", report.blockers)

    def test_health_falls_back_to_foreground_activity_when_pidof_fails(self):
        client = MagicMock(serial="SERIAL")
        client.shell.side_effect = [
            "1",
            "mCurrentFocus=Window{normal}",
            "",
            RuntimeError("pidof unavailable"),
            "topResumedActivity=ActivityRecord{ u0 com.example.app/.Main}",
        ]
        report = DeviceHealthMonitor(client, "com.example.app").assess()
        self.assertTrue(report.app_running)
        self.assertNotIn("app_state_unknown", report.blockers)

    def test_unknown_app_query_is_warning_not_blocker(self):
        client = MagicMock(serial="SERIAL")
        client.shell.side_effect = ["1", "mCurrentFocus=Window{normal}", "", RuntimeError("pidof unavailable"), RuntimeError("activity unavailable")]
        report = DeviceHealthMonitor(client, "com.example.app").assess()
        self.assertIsNone(report.app_running)
        self.assertTrue(report.safe)

    def test_latency_and_fps_primitives(self):
        observations = iter([0, 1])
        sample = LatencyProbe(lambda: next(observations)).measure(lambda: None, timeout=0.1, poll_interval=0)
        self.assertTrue(sample.changed)
        meter = FpsMeter()
        meter.frame(1.0)
        meter.frame(1.1)
        meter.frame(1.2)
        self.assertAlmostEqual(meter.fps(), 10.0)

    def test_pipeline_returns_result_when_image_is_invalid(self):
        client = MagicMock(serial="SERIAL")
        client.screencap.return_value = b"not-an-image"
        result = RecognitionPipeline(client, ScreenGeometry(10, 10)).template("missing.png")
        self.assertFalse(result.found)
        self.assertEqual(result.method, "template")
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
