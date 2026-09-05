import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from adb_scrcpy.recognition import ScreenGeometry, TemplateMatcher, normalize_image
from adb_scrcpy.workflow import WorkflowContext, WorkflowRunner, WorkflowStep


class WorkflowTests(unittest.TestCase):
    def test_runs_steps_and_stops_with_artifact(self):
        client = MagicMock()
        client.save_screenshot.side_effect = lambda path: Path(path)
        with tempfile.TemporaryDirectory() as directory:
            context = WorkflowContext(client, Path(directory))
            seen = []
            condition = lambda ctx: True
            result = WorkflowRunner(context).run([
                WorkflowStep("tap", "action", action=lambda ctx: seen.append("tap")),
                WorkflowStep("ready", "wait", condition=condition, timeout=1),
                WorkflowStep("capture", "screenshot", screenshot_name="state/unsafe"),
                WorkflowStep("halt", "stop"),
                WorkflowStep("never", "action", action=lambda ctx: seen.append("never")),
            ])
        self.assertEqual(result.status, "stopped")
        self.assertEqual(seen, ["tap"])
        self.assertEqual(len(result.steps), 4)
        self.assertEqual(client.save_screenshot.call_count, 1)

    def test_assert_failure_is_reported(self):
        context = WorkflowContext(MagicMock())
        result = WorkflowRunner(context).run([WorkflowStep("bad", "assert", condition=lambda ctx: False, retries=1)])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps[0].attempts, 2)
        self.assertIn("thất bại", result.steps[0].error)

    def test_stop_request_interrupts_wait_step(self):
        context = WorkflowContext(MagicMock())
        def request_stop(ctx):
            ctx.stop_requested = True
            return False
        result = WorkflowRunner(context).run([WorkflowStep("wait", "wait", condition=request_stop, timeout=1, poll_interval=0.001)])
        self.assertEqual(result.status, "stopped")

    def test_runner_emits_step_progress_callbacks(self):
        events = []
        step = WorkflowStep("one", "action", action=lambda ctx: None)
        runner = WorkflowRunner(
            WorkflowContext(MagicMock()),
            on_step_start=lambda current, index, total: events.append(("start", current.name, index, total)),
            on_step_result=lambda current, result, index, total: events.append(("result", current.name, result.status, index, total)),
        )
        self.assertTrue(runner.run([step]).ok)
        self.assertEqual(events, [("start", "one", 1, 1), ("result", "one", "passed", 1, 1)])


class RecognitionTests(unittest.TestCase):
    def test_normalizes_rotation_and_size(self):
        image = Image.new("RGB", (4, 2), "red")
        normalized = normalize_image(image, ScreenGeometry(8, 8, rotation=1))
        self.assertEqual(normalized.size, (8, 8))

    def test_template_match_finds_embedded_pattern(self):
        source = Image.new("RGB", (40, 30), "black")
        pattern = Image.new("RGB", (6, 5), "white")
        for x in range(6):
            for y in range(5):
                if (x + y) % 2:
                    pattern.putpixel((x, y), (20, 80, 220))
        source.paste(pattern, (12, 9))
        def png(image):
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            return stream.getvalue()
        result = TemplateMatcher(0.99).match(png(source), png(pattern))
        self.assertTrue(result.found)
        self.assertEqual(result.center, (15, 11))


if __name__ == "__main__":
    unittest.main()
