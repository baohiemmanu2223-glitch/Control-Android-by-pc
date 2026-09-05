import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.device_manager import Device
from adb_scrcpy.gui import DeviceDashboard


TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "config.example.toml")


class Phase7RegressionTests(unittest.TestCase):
    def test_connection_state_matrix_and_sixteen_device_grid(self):
        app = DeviceDashboard
        with patch.object(app, "_poll_devices"), patch.object(app, "_poll_grid_thumbnails"):
            window = app(TEST_CONFIG)
            self.assertEqual(window.device_manager._parse_device_line("A device").state, "device")
            self.assertEqual(window.device_manager._parse_device_line("B offline").state, "offline")
            self.assertEqual(window.device_manager._parse_device_line("C unauthorized").state, "unauthorized")
            devices = [Device(f"SERIAL-{index:02d}", "device", model="Model") for index in range(16)]
            window._update_devices(devices)
            self.assertEqual(len(window._visible_devices()), 16)
            self.assertEqual(window._grid_columns(16), 4)
            window._selected_serials = {device.serial for device in devices}
            self.assertEqual(len(window._selected_serials), 16)
            window._on_close()

    def test_game_safe_mode_stops_managed_activity_and_resumes(self):
        app = DeviceDashboard
        with patch.object(app, "_poll_devices"), patch("adb_scrcpy.gui.messagebox.askyesno", return_value=True):
            window = app(TEST_CONFIG)
            window.scrcpy_manager.stop_all = MagicMock()
            window.device_manager.stop_server = MagicMock(return_value="")
            window.device_manager.reconnect = MagicMock(return_value="")
            window._submit = lambda action, success, _label, *_args: success(action())
            window._enter_game_safe_mode()
            self.assertTrue(window._game_safe_mode)
            window.scrcpy_manager.stop_all.assert_called_once_with()
            window.device_manager.stop_server.assert_called_once_with()
            self.assertEqual(window.game_safe_button["text"], "Resume controller")
            window._resume_game_safe_mode()
            self.assertFalse(window._game_safe_mode)
            window.device_manager.reconnect.assert_called_once_with()
            self.assertEqual(window.game_safe_button["text"], "Game safe mode")
            window._on_close()


if __name__ == "__main__":
    unittest.main()
