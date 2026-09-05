import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.device_manager import Device
from adb_scrcpy.gui import DeviceDashboard


TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "config.example.toml")


class GuiScrcpyTests(unittest.TestCase):
    def setUp(self):
        self.poll = patch.object(DeviceDashboard, "_poll_devices")
        self.poll.start()
        self.window = DeviceDashboard(TEST_CONFIG)
        device = Device("SERIAL", "device", model="Model")
        self.window._known_devices = {"SERIAL": device}
        self.window._select_device("SERIAL")

    def tearDown(self):
        self.window._on_close()
        self.poll.stop()

    def _run_inline(self):
        def submit(action, success, _label):
            success(action())
        self.window._submit = submit

    def test_open_passes_selected_profile_options_and_tracks_session(self):
        self._run_inline()
        process = MagicMock()
        process.pid = 101
        process.poll.return_value = None
        session = type("Session", (), {"process": process, "serial": "SERIAL"})()
        self.window.scrcpy_manager.start = MagicMock(return_value=session)
        self.window.scrcpy_profile.set("manual")
        self.window.scrcpy_audio.set(True)
        self.window.scrcpy_clipboard.set(True)
        self.window.scrcpy_stay_awake.set(True)

        self.window._open_scrcpy()

        self.window.scrcpy_manager.start.assert_called_once_with(
            "SERIAL", "manual", audio=True, clipboard_autosync=True, stay_awake=True
        )
        self.assertIn("SERIAL", self.window._active_scrcpy_serials)
        self.assertEqual(str(self.window.stop_button["state"]), "normal")

    def test_stop_cleans_tracked_session(self):
        self._run_inline()
        self.window._active_scrcpy_serials.add("SERIAL")
        self.window.scrcpy_manager.stop = MagicMock(return_value=True)

        self.window._stop_scrcpy()

        self.window.scrcpy_manager.stop.assert_called_once_with("SERIAL")
        self.assertNotIn("SERIAL", self.window._active_scrcpy_serials)

    def test_stop_scrcpy_stops_active_recorder(self):
        self._run_inline()
        self.window.scrcpy_manager.stop = MagicMock(return_value=True)
        self.window._stop_recording = MagicMock()
        self.window._recorder = MagicMock()
        self.window._stop_scrcpy()
        self.window._stop_recording.assert_called_once_with(save=False)

    def test_header_button_names_save_action_while_recording(self):
        self.window._recorder = MagicMock()
        self.window._select_device("SERIAL")
        self.assertEqual(self.window.quick_record_button["text"], "Stop & Save")


if __name__ == "__main__":
    unittest.main()
