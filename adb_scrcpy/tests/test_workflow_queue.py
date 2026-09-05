import unittest

from adb_scrcpy.workflow import WorkflowContext
from adb_scrcpy.workflow_queue import WorkflowQueue


class WorkflowQueueTests(unittest.TestCase):
    def test_queue_reports_per_device_status(self):
        updates = []
        queue = WorkflowQueue(max_concurrency=2)
        queue.run(["A", "B"], lambda serial, control: f"{serial}.json", lambda item: updates.append((item.serial, item.status)))
        self.assertEqual({item.status for item in queue.items.values()}, {"passed"})
        self.assertIn(("A", "running"), updates)
        self.assertIn(("B", "passed"), updates)

    def test_stop_propagates_to_registered_context(self):
        queue = WorkflowQueue()
        context = WorkflowContext(client=None)  # type: ignore[arg-type]
        queue.control.register(context)
        queue.stop()
        self.assertTrue(context.stop_requested)
        self.assertFalse(context.pause_requested)


if __name__ == "__main__":
    unittest.main()
