import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Isolated test DB & environment
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "test_ai_team_collab.db")
os.environ["AI_TEAM_DB"] = TEST_DB_PATH
os.environ["AI_TEAM_AUTH_TOKEN"] = "test-phase3-token"
os.environ["WORKSPACE_ROOT"] = os.path.abspath(os.path.expanduser("~/projects"))
os.environ["DEFAULT_WORKSPACE"] = os.environ["WORKSPACE_ROOT"]

import main
import store
from fastapi.testclient import TestClient

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"} if main.AUTH_TOKEN else {})


class TestPhase3Collaboration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        store._initialized_dbs.clear()
        store.init_db(force=True)
        store.seed_agents_from_file()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("AI_TEAM_DB", None)
        store._initialized_dbs.clear()
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        store.init_db()
        store.seed_agents_from_file()

    def test_agent_registry_and_scoped_permissions(self):
        """Verify agents registry returns all roles with scoped permissions."""
        res = client.get("/api/agents")
        self.assertEqual(res.status_code, 200)
        agents = res.json()["agents"]
        
        # Verify key roles exist
        for role in ["Hermes", "Architect", "Coder", "QA", "Researcher", "Critic", "Tutor", "Data Analyst"]:
            self.assertIn(role, agents)

        # Scoped permissions validation
        self.assertTrue(store.check_agent_permission("Coder", "filesystem", "write"))
        self.assertTrue(store.check_agent_permission("Coder", "shell"))
        self.assertTrue(store.check_agent_permission("Coder", "git"))

        self.assertFalse(store.check_agent_permission("Architect", "filesystem", "write"))
        self.assertFalse(store.check_agent_permission("Architect", "shell"))

        self.assertTrue(store.check_agent_permission("Researcher", "web"))
        self.assertFalse(store.check_agent_permission("Researcher", "filesystem", "write"))

        self.assertTrue(store.check_agent_permission("QA", "shell"))
        self.assertFalse(store.check_agent_permission("QA", "filesystem", "write"))

        # Test endpoint check-permission
        res_chk_coder = client.post("/api/agents/Coder/check-permission", json={
            "capability": "filesystem", "subaction": "write"
        })
        self.assertEqual(res_chk_coder.status_code, 200)
        self.assertTrue(res_chk_coder.json()["allowed"])

        res_chk_arch = client.post("/api/agents/Architect/check-permission", json={
            "capability": "filesystem", "subaction": "write"
        })
        self.assertEqual(res_chk_arch.status_code, 200)
        self.assertFalse(res_chk_arch.json()["allowed"])

    def test_apply_files_enforces_permissions(self):
        """Verify apply_task_files raises 403 when Coder lacks filesystem write permission."""
        task_id = "test-perm-enforce"
        task_obj = {
            "id": task_id,
            "title": "Permission Enforcement Task",
            "prompt": "Test",
            "status": "completed",
            "working_directory": tempfile.gettempdir(),
            "extracted_files": [{"path": "dummy.txt", "content": "hello", "lines": 1}],
            "stages": []
        }
        main.tasks_store[task_id] = task_obj
        store.save_task(task_obj)

        # Revoke Coder write permission
        orig_perms = store.load_agents()["Coder"].get("permissions", {})
        client.post("/api/agents/Coder/permissions", json={
            "permissions": {"filesystem": {"read": True, "write": False}, "shell": False}
        })

        try:
            res_denied = client.post(f"/api/tasks/{task_id}/apply-files")
            self.assertEqual(res_denied.status_code, 403)
            self.assertIn("Izin ditolak", res_denied.json()["detail"])
        finally:
            # Restore original permissions
            client.post("/api/agents/Coder/permissions", json={"permissions": orig_perms})
            main.tasks_store.pop(task_id, None)
            store.purge_task(task_id)

    def test_structured_messages_crud(self):
        """Verify structured agent messaging (QUESTION, FINDING, REQUEST_CHANGE)."""
        task_id = "test-struct-task-1"
        task_obj = {
            "id": task_id,
            "title": "Structured Messaging Task",
            "prompt": "Test",
            "status": "running",
            "stages": []
        }
        main.tasks_store[task_id] = task_obj
        store.save_task(task_obj)

        # Post structured finding
        res_f = client.post(f"/api/tasks/{task_id}/messages", json={
            "role": "Researcher",
            "to_role": "all",
            "kind": "FINDING",
            "content": "Ditemukan 3 paper terkait sentiment analysis.",
            "meta": {"papers_count": 3}
        })
        self.assertEqual(res_f.status_code, 200)

        # Post structured question
        res_q = client.post(f"/api/tasks/{task_id}/messages", json={
            "role": "Coder",
            "to_role": "Architect",
            "kind": "QUESTION",
            "content": "Apakah harus menggunakan foreign key cascade pada tabel attendance?"
        })
        self.assertEqual(res_q.status_code, 200)

        # Query messages
        res_msgs = client.get(f"/api/tasks/{task_id}/messages")
        self.assertEqual(res_msgs.status_code, 200)
        msgs = res_msgs.json()["messages"]
        self.assertGreaterEqual(len(msgs), 2)

        # Filter by kind
        res_findings = client.get(f"/api/tasks/{task_id}/messages?kind=FINDING")
        self.assertEqual(res_findings.status_code, 200)
        f_msgs = res_findings.json()["messages"]
        self.assertTrue(all(m["kind"] == "FINDING" for m in f_msgs))

        # Clean up
        main.tasks_store.pop(task_id, None)
        store.purge_task(task_id)

    def test_chat_dynamic_agent_escalation(self):
        """Verify chat can dynamically switch to specialists (@researcher, @coder) and track team."""
        res_sess = client.post("/api/chat/sessions", json={"title": "Team Session"})
        self.assertEqual(res_sess.status_code, 200)
        sess_id = res_sess.json()["session"]["id"]

        captured_temp = []
        async def mock_stream(messages, model=None, temperature=0.3):
            captured_temp.append(temperature)
            yield 'data: {"chunk": "Respon: "}\n\n'
            yield 'data: {"chunk": "Berikut temuan awal."}\n\n'

        # 1. Invoke Researcher explicitly
        with patch("main._stream_chat_llm", side_effect=mock_stream):
            res_chat = client.post("/api/chat", json={
                "session_id": sess_id,
                "message": "Tolong carikan literatur algoritma clustering",
                "agent": "Researcher"
            })
            self.assertEqual(res_chat.status_code, 200)
            body = res_chat.text
            self.assertIn("agent.started", body)
            self.assertIn("Researcher", body)

        # Verify session active agents now has Hermes + Researcher
        res_agents = client.get(f"/api/chat/sessions/{sess_id}/agents")
        self.assertEqual(res_agents.status_code, 200)
        team = res_agents.json()["active_agents"]
        self.assertIn("Hermes", team)
        self.assertIn("Researcher", team)

        # 2. Invoke Coder via @mention in user prompt (even if UI sends default agent='Hermes')
        with patch("main._stream_chat_llm", side_effect=mock_stream):
            res_mention = client.post("/api/chat", json={
                "session_id": sess_id,
                "agent": "Hermes",
                "message": "@coder bagaimana implementasi clustering di python?"
            })
            self.assertEqual(res_mention.status_code, 200)
            self.assertIn('"agent": "Coder"', res_mention.text)

        # 3. Invoke Critic via natural language 'Tanya ke Critic'
        with patch("main._stream_chat_llm", side_effect=mock_stream):
            res_nl = client.post("/api/chat", json={
                "session_id": sess_id,
                "agent": "Hermes",
                "message": "Tanya ke Critic apakah ada celah pada rencana di atas"
            })
            self.assertEqual(res_nl.status_code, 200)
            self.assertIn('"agent": "Critic"', res_nl.text)

        # Clean up
        store.delete_chat_session(sess_id)

    def test_auto_fix_exhaustion_approval_gate(self):
        """Verify pipeline strictly pauses with waiting_approval when auto-fix loop limit is exhausted."""
        task_id = "test-gate-exhaustion"
        main.tasks_store[task_id] = {
            "id": task_id,
            "title": "Auto Fix Exhaustion Gate Test",
            "prompt": "Buat fungsi transfer",
            "preset_id": "standard",
            "status": "queued",
            "auto_fix_loops": 1,
            "current_fix_loop": 0,
            "stages": [
                {
                    "role": "Coder",
                    "name": "Lead Developer",
                    "system": "Kamu adalah Lead Full-Stack Developer",
                    "temperature": 0.1,
                    "status": "waiting",
                    "output": "",
                    "error": None
                },
                {
                    "role": "QA",
                    "name": "QA & Security Engineer",
                    "system": "Kamu adalah Senior QA",
                    "temperature": 0.1,
                    "status": "waiting",
                    "output": "",
                    "error": None
                }
            ]
        }
        store.save_task(main.tasks_store[task_id])

        async def run_pipeline_exhaustion():
            async def mock_call(sys, u, temperature=0.1):
                if "QA" in sys:
                    # Always fail QA
                    return "Bug ditemukan: race condition transfer saldo. VERDICT: NEEDS_REVISION"
                return "### FILE: transfer.py\n```python\ndef transfer(): pass\n```"

            with patch("main.call_llm", side_effect=mock_call):
                pipe_task = asyncio.create_task(main.execute_pipeline(task_id))
                
                # Wait until pipeline pauses at approval gate
                for _ in range(30):
                    await asyncio.sleep(0.05)
                    if main.tasks_store[task_id].get("status") == "waiting_approval":
                        break

                # Assert that it DID NOT fail open or jump to completed
                self.assertEqual(main.tasks_store[task_id]["status"], "waiting_approval")
                self.assertIn("QA Review Gate", main.tasks_store[task_id].get("waiting_stage_name", ""))

                # Now reject via approval endpoint
                res_reject = client.post(f"/api/tasks/{task_id}/approve", json={
                    "action": "reject", "feedback": "Dibatalkan karena bug belum terselesaikan."
                })
                self.assertEqual(res_reject.status_code, 200)

                await pipe_task

        asyncio.run(run_pipeline_exhaustion())

        t = main.tasks_store.get(task_id)
        self.assertEqual(t["status"], "cancelled")

        # Clean up
        main.tasks_store.pop(task_id, None)
        store.purge_task(task_id)


if __name__ == "__main__":
    unittest.main()