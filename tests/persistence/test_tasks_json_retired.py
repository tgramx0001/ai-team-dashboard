"""tasks.json retirement (shortcoming #6): SQLite is the sole writer.

Covers: save_tasks/save_single_task no longer touch the JSON mirror, the
one-time import migration still works, and GET /api/tasks/export serves an
explicit JSON dump on demand.
"""
import json
import os
import sys
import tempfile
import unittest

if "main" not in sys.modules:
    os.environ["AI_TEAM_DB"] = os.path.join(tempfile.gettempdir(), "test_ai_team_tasksjson.db")

import main  # noqa: E402
import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"})


class TestTasksJsonRetired(unittest.TestCase):
    def tearDown(self):
        # Never leave the probe task behind in whichever DB is active.
        try:
            conn = store.connect()
            with conn:
                conn.execute("DELETE FROM stages WHERE task_id = ?", ("probe-retired",))
                conn.execute("DELETE FROM tasks WHERE id = ?", ("probe-retired",))
            conn.close()
        except Exception:
            pass

    def test_save_tasks_no_longer_writes_json_mirror(self):
        # The write-through mirror function itself is gone from store.py...
        self.assertFalse(hasattr(store, "export_tasks_json"))
        # ...and saving tasks must not modify the legacy JSON file on disk.
        mirror = store.JSON_BACKUP_PATH
        before = None
        if os.path.exists(mirror):
            with open(mirror, "rb") as f:
                before = f.read()
        main.save_tasks()
        main.tasks_store["probe-retired"] = {"id": "probe-retired", "title": "x", "status": "queued",
                                             "created_at": 1, "stages": []}
        main.save_single_task("probe-retired")
        main.tasks_store.pop("probe-retired", None)
        if os.path.exists(mirror):
            with open(mirror, "rb") as f:
                self.assertEqual(f.read(), before, "tasks.json must not be rewritten")

    def test_one_time_import_migration_still_available(self):
        tmp = os.path.join(tempfile.gettempdir(), "legacy_tasks_migration.json")
        legacy = {"legacy-1": {"id": "legacy-1", "title": "old", "status": "queued", "stages": []}}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        loaded = store.import_tasks_from_json(tmp)
        self.assertEqual(loaded["legacy-1"]["title"], "old")
        os.remove(tmp)

    def test_export_endpoint_serves_json_dump(self):
        res = client.get("/api/tasks/export")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("tasks", body)
        self.assertIn("exported_at", body)
        self.assertIsInstance(body["tasks"], list)
        self.assertIn("attachment", res.headers.get("content-disposition", ""))


if __name__ == "__main__":
    unittest.main()
