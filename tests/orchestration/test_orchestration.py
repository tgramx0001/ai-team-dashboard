import unittest
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import main
import store


class TestOrchestrationContext(unittest.TestCase):
    def test_context_log_lifecycle(self):
        """ContextLog model adds and renders stages cleanly."""
        task = {"id": "t1", "stages": [{"role": "Architect", "name": "Plan", "output": "Architecture specs defined."}]}
        ctx = main.ContextLog(task)
        ctx.add("goal", "Build flexible workspace")
        ctx.add_stage_output(0)

        self.assertEqual(len(ctx.entries), 2)
        rendered = ctx.render()
        self.assertIn("Build flexible workspace", rendered)
        self.assertIn("Architecture specs defined.", rendered)

    def test_reconcile_interrupted_tasks(self):
        """Dangling 'running' tasks get marked as interrupted on startup."""
        dummy_tasks = {
            "task_1": {"status": "running", "title": "Test 1"},
            "task_2": {"status": "completed", "title": "Test 2"}
        }
        interrupted = store.reconcile_interrupted(dummy_tasks)
        self.assertIn("task_1", interrupted)
        self.assertEqual(dummy_tasks["task_1"]["status"], "interrupted")
        self.assertEqual(dummy_tasks["task_2"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
