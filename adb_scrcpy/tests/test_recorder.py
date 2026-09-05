import json
import tempfile
import unittest
from pathlib import Path

from adb_scrcpy.recorder import MouseGesture, RecordedWorkflow, classify_gesture
from adb_scrcpy.workflow_spec import build_steps, load_spec


class RecorderTests(unittest.TestCase):
    def test_classifies_tap_long_press_and_swipe(self):
        self.assertEqual(classify_gesture((10, 10), (12, 11), 100).kind, "tap")
        self.assertEqual(classify_gesture((10, 10), (11, 11), 700).kind, "long_press")
        self.assertEqual(classify_gesture((10, 10), (100, 50), 400).kind, "swipe")

    def test_saves_replayable_gestures_and_text(self):
        workflow = RecordedWorkflow(metadata={"serial": "SERIAL", "width": 1240, "height": 2772, "rotation": 0})
        workflow.add_gesture(MouseGesture("tap", 10, 20), now=1.0)
        workflow.add_gesture(MouseGesture("swipe", 10, 20, 100, 200, 500), now=1.5)
        workflow.add_text("hello", now=2.0)
        with tempfile.TemporaryDirectory() as directory:
            path = workflow.save(Path(directory) / "recorded.json", "com.example.app")
            spec = load_spec(path)
            steps = build_steps(spec)
        self.assertEqual(len(steps), 5)  # tap, wait, swipe, wait, text
        self.assertEqual(spec["package"], "com.example.app")
        self.assertEqual(spec["recording"]["width"], 1240)
        self.assertEqual(spec["steps"][0]["action"]["type"], "tap")

    def test_checkpoint_is_replayable_screenshot_step(self):
        workflow = RecordedWorkflow()
        workflow.add_checkpoint("before_battle")
        self.assertEqual(workflow.steps[0]["kind"], "screenshot")
        self.assertEqual(workflow.steps[0]["screenshot_name"], "before_battle")


if __name__ == "__main__":
    unittest.main()
