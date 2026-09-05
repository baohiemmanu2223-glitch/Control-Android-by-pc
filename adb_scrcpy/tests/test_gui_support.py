import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_scrcpy.device_manager import Device
from adb_scrcpy.gui import DeviceDetails, app_directory, state_label
from adb_scrcpy.workflow import StepResult, WorkflowStep


TEST_CONFIG = str(Path(__file__).resolve().parents[1] / "config.example.toml")


class GuiSupportTests(unittest.TestCase):
    def test_status_labels_are_actionable(self):
        self.assertEqual(state_label("device"), "Ready")
        self.assertEqual(state_label("unauthorized"), "Authorize USB debugging")
        self.assertEqual(state_label("offline"), "Offline")

    def test_device_details_preserves_metadata(self):
        details = DeviceDetails(Device("SERIAL", "device", model="Model"), "14", "34")
        self.assertEqual(details.device.serial, "SERIAL")
        self.assertEqual(details.android_version, "14")
        self.assertTrue(app_directory().is_dir())

    def test_gui_defaults_to_dry_run_and_exposes_adb_controls(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            self.assertTrue(window.dry_run_var.get())
            self.assertEqual(str(window.tap_button["state"]), "normal")
            self.assertEqual(window.quick_record_button["text"], "Record")
            self.assertEqual(window.shell_entry.get(), "getprop ro.build.version.sdk")
            self.assertIn("Last seen", window.detail_values)
            self.assertTrue(window.workflow_path.get().replace("\\", "/").endswith("adb_scrcpy/workflows/device_smoke.json") or window.workflow_path.get().replace("\\", "/").endswith("workflows/device_smoke.json"))
            self.assertEqual(window.run_workflow_button["text"], "Simulate")
            self.assertFalse(window.launch_app_var.get())
            self.assertTrue(window.devices_expanded)
            window._toggle_devices()
            self.assertFalse(window.devices_expanded)
            self.assertEqual(window.device_toggle["text"], "Expand")
            window._toggle_devices()
            self.assertTrue(window.devices_expanded)
            window.dry_run_var.set(False)
            window.confirm_var.set(True)
            window._update_input_state()
            self.assertEqual(window.run_workflow_button["text"], "Run live")
            self.assertIn("will be sent", window.workflow_hint["text"])
            window._on_close()

    def test_workflow_progress_table_updates(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            steps = [WorkflowStep("one", "action", action=lambda ctx: None), WorkflowStep("two", "assert", condition=lambda ctx: True)]
            window._prepare_workflow_progress(steps)
            window._workflow_step_started(steps[0], 1, 2)
            self.assertEqual(window.workflow_current["text"], "Running: one")
            result = StepResult("one", "action", "passed", 1, 0.25)
            window._workflow_step_finished(steps[0], result, 1, 2)
            self.assertEqual(window.workflow_steps.item(window._workflow_step_ids["one"], "values")[1], "Passed")
            self.assertEqual(window.workflow_counts["text"], "1/2 steps")
            window._on_close()

    def test_workflow_editor_loads_and_adds_step(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            self.assertGreater(len(window.editor_steps_data), 0)
            window.editor_tree.selection_set("0")
            window._editor_select(None)
            original = len(window.editor_steps_data)
            window._editor_add()
            self.assertEqual(len(window.editor_steps_data), original + 1)
            self.assertEqual(window.editor_steps_data[-1]["action"]["type"], "tap")
            window._on_close()

    def test_device_grid_adapts_thumbnail_profile(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            self.assertEqual(window._grid_profile(1), (320, 712, 2000))
            self.assertEqual(window._grid_profile(6), (220, 489, 5000))
            self.assertEqual(window._grid_profile(16), (180, 400, 10000))
            window._grid_canvas_width = 439
            self.assertEqual(window._grid_columns(16), 1)
            window._grid_canvas_width = 880
            self.assertEqual(window._grid_columns(16), 4)
            for count in (1, 4, 8, 16):
                devices = [Device(f"SERIAL-{index}", "device", model="Model") for index in range(count)]
                window._update_devices(devices)
                self.assertEqual(len(window.grid_inner.winfo_children()), count)
            window._on_close()

    def test_focus_view_tracks_selected_device(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._known_devices = {"SERIAL": Device("SERIAL", "device", model="Model")}
            window._last_seen["SERIAL"] = 1_700_000_000
            window._select_device("SERIAL")
            window._focus_grid_device("SERIAL")
            self.assertEqual(window.tabs.tab(window.tabs.select(), "text"), "Focus")
            self.assertIn("SERIAL", window.focus_title["text"])
            self.assertEqual(window.focus_info_values["Model"]["text"], "Model")
            window._on_close()

    def test_sidebar_routes_select_existing_views(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._select_route("Automation")
            self.assertEqual(window.tabs.tab(window.tabs.select(), "text"), "Workflow")
            self.assertEqual(window._sidebar_buttons["Automation"]["style"], "NavActive.TButton")
            window._select_route("Files & APK")
            self.assertEqual(window.tabs.tab(window.tabs.select(), "text"), "Files & APK")
            self.assertEqual(window._sidebar_buttons["Files & APK"]["style"], "NavActive.TButton")
            window._on_close()

    def test_queue_row_focuses_matching_device(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._known_devices = {"SERIAL": Device("SERIAL", "device", model="Model")}
            window._select_device("SERIAL")
            window.queue_tree.insert("", "end", iid="SERIAL", values=("SERIAL", "Queued", "", ""), tags=("queued",))
            window.queue_tree.selection_set("SERIAL")
            window._focus_queue_device()
            self.assertEqual(window._current_serial, "SERIAL")
            self.assertEqual(window.tabs.tab(window.tabs.select(), "text"), "Focus")
            window._on_close()

    def test_files_workspace_tracks_selected_device_scope(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._known_devices = {"SERIAL": Device("SERIAL", "device", model="Model")}
            window._select_device("SERIAL")
            self.assertIn("SERIAL", window.files_scope["text"])
            self.assertIn("Ready", window.files_scope["text"])
            window._select_device(None)
            self.assertIn("select a ready device", window.files_scope["text"])
            window._on_close()

    def test_files_workspace_separates_transfer_actions(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            self.assertEqual(len(window.file_actions.tabs()), 3)
            self.assertEqual([window.file_actions.tab(tab, "text") for tab in window.file_actions.tabs()], ["Push", "Pull", "Install APK"])
            window.file_actions.select(window.pull_file_info.winfo_parent())
            self.assertEqual(window.file_actions.tab(window.file_actions.select(), "text"), "Pull")
            window._on_close()

    def test_device_filter_and_multi_selection_scope(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"), patch.object(app, "_poll_grid_thumbnails"):
            window = app(TEST_CONFIG)
            devices = [Device("A", "device", model="One"), Device("B", "offline", model="Two")]
            window._update_devices(devices)
            window._device_meta["A"] = {"name": "Alpha", "group": "farm", "role": "qa", "location": "rack-1", "environment": "test", "tags": ["blue"]}
            window._grid_android["A"] = "14"
            window._grid_foreground["A"] = "com.example.game"
            window.filter_tag_var.set("blue")
            self.assertEqual([device.serial for device in window._visible_devices()], ["A"])
            window.filter_tag_var.set("")
            window.filter_android_var.set("14")
            self.assertEqual([device.serial for device in window._visible_devices()], ["A"])
            window.filter_android_var.set("All")
            window.filter_app_var.set("example.game")
            self.assertEqual([device.serial for device in window._visible_devices()], ["A"])
            window._select_visible_devices()
            self.assertEqual(window._selected_serials, {"A"})
            self.assertEqual(window.selection_summary["text"], "1 selected")
            window._clear_selection()
            self.assertEqual(window._selected_serials, set())
            window._on_close()

    def test_settings_preferences_and_log_filters(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._log("INFO  check")
            window._log("WARNING  review")
            window.log_severity_var.set("Warning")
            self.assertIn("review", window.activity.get("1.0", "end"))
            self.assertNotIn("check", window.activity.get("1.0", "end"))
            window.poll_seconds_var.set(4)
            window._save_preferences()
            self.assertEqual(window._saved_ui_state["poll_seconds"], 4)
            window._on_close()

    def test_theme_runtime_and_accessibility_defaults(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window.theme_var.set("Dark")
            self.assertEqual(window._style.lookup("App.TFrame", "background"), "#1c1c1e")
            self.assertEqual(window._style.lookup("PanelTitle.TLabel", "foreground"), "#f5f5f7")
            window.theme_var.set("Light")
            window.accent_var.set("invalid")
            self.assertEqual(window._theme_palette()["accent"], "#007AFF")
            self.assertEqual(window._style.lookup("App.TFrame", "background"), "#f5f5f7")
            for button in window._sidebar_buttons.values():
                self.assertEqual(button.winfo_class(), "TButton")
            window._on_close()

    def test_logs_expose_latest_existing_artifact(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            artifact = window.config.artifacts_dir / "test_phase5" / "result.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            window._log(f"WORKFLOW passed report={artifact}")
            self.assertEqual(str(window.open_log_artifact_button["state"]), "normal")
            self.assertEqual(window._latest_log_artifact, artifact)
            artifact.unlink()
            window._on_close()

    def test_recorder_timeline_edit_and_delete(self):
        app = __import__("adb_scrcpy.gui", fromlist=["DeviceDashboard"]).DeviceDashboard
        with patch.object(app, "_poll_devices"):
            window = app(TEST_CONFIG)
            window._recorder_steps = [{"name": "tap_1", "kind": "action", "action": {"type": "tap", "x": 1, "y": 2}}]
            window._refresh_recorder_events()
            window.recorder_events.selection_set("0")
            window._recorder_select(None)
            payload = json.loads(window.recorder_event_payload.get("1.0", "end"))
            payload["action"]["x"] = 99
            window.recorder_event_payload.delete("1.0", "end")
            window.recorder_event_payload.insert("1.0", json.dumps(payload))
            window._recorder_update_event()
            self.assertEqual(window._recorder_steps[0]["action"]["x"], 99)
            window._recorder_delete_event()
            self.assertEqual(window._recorder_steps, [])
            window._on_close()


if __name__ == "__main__":
    unittest.main()
