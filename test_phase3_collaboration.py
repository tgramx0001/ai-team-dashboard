import asyncio
import json
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ["AI_TEAM_AUTH_TOKEN"] = "test-phase3-token"
os.environ["WORKSPACE_ROOT"] = os.path.abspath(os.path.expanduser("~/projects"))
os.environ["DEFAULT_WORKSPACE"] = os.environ["WORKSPACE_ROOT"]

import main
import store
from fastapi.testclient import TestClient

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"} if main.AUTH_TOKEN else {})


class TestPhase3Collaboration(unittest.TestCase):
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

        # Update permission via API
        res_update = client.post("/api/agents/Researcher/permissions", json={
            "permissions": {"filesystem": {"read": True, "write": False}, "web": True, "shell": True}
        })
        self.assertEqual(res_update.status_code, 200)
        self.assertTrue(store.check_agent_permission("Researcher", "shell"))
        # Revert back
        client.post("/api/agents/Researcher/permissions", json={
            "permissions": {"filesystem": {"read": True, "write": False}, "web": True, "shell": False}
        })

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
        # 1. Create a session starting with Hermes
        res_sess = client.post("/api/chat/sessions", json={"title": "Team Session"})
        self.assertEqual(res_sess.status_code, 200)
        sess_id = res_sess.json()["session"]["id"]

        # Mock LLM stream generator
        async def mock_stream(messages, model=None, temperature=0.3):
            yield 'data: {"chunk": "Riset: "}\n\n'
            yield 'data: {"chunk": "Berikut temuan awal."}\n\n'

        # 2. Invoke Researcher explicitly
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

        # 3. Verify session active agents now has Hermes + Researcher
        res_agents = client.get(f"/api/chat/sessions/{sess_id}/agents")
        self.assertEqual(res_agents.status_code, 200)
        team = res_agents.json()["active_agents"]
        self.assertIn("Hermes", team)
        self.assertIn("Researcher", team)

        # 4. Invoke Coder via @mention in user prompt
        with patch("main._stream_chat_llm", side_effect=mock_stream):
            res_mention = client.post("/api/chat", json={
                "session_id": sess_id,
                "message": "@coder bagaimana implementasi clustering di python?"
            })
            self.assertEqual(res_mention.status_code, 200)
            self.assertIn("Coder", res_mention.text)

        # Verify Coder joined the active team
        res_team2 = client.get(f"/api/chat/sessions/{sess_id}/agents")
        team2 = res_team2.json()["active_agents"]
        self.assertIn("Coder", team2)

        # Clean up
        store.delete_chat_session(sess_id)

    def test_collaborative_retry_loop_on_qa_failed(self):
        """Verify dynamic auto-fix cycle triggers when QA outputs VERDICT: NEEDS_REVISION."""
        task_id = "test-auto-fix-flow"
        main.tasks_store[task_id] = {
            "id": task_id,
            "title": "Auto Fix Collaborative Task",
            "prompt": "Buat fungsi login",
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

        call_count = {"Coder": 0, "QA": 0}

        async def mock_call_llm(system, user, temperature=0.2):
            if "Lead Full-Stack Developer" in system or "Coder" in system:
                call_count["Coder"] += 1
                return "### FILE: auth.py\n```python\ndef login(): pass\n```"
            elif "Senior QA" in system or "QA" in system:
                call_count["QA"] += 1
                if call_count["QA"] == 1:
                    # First QA audit fails -> triggers auto-fix
                    return "Ditemukan celah: null pointer. VERDICT: NEEDS_REVISION"
                else:
                    # Second QA audit passes
                    return "Perbaikan berhasil. VERDICT: PASSED"
            return "OK"

        with patch("main.call_llm", side_effect=mock_call_llm):
            asyncio.run(main.execute_pipeline(task_id))

        t = main.tasks_store.get(task_id)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["current_fix_loop"], 1)

        # Coder called twice (initial + fix cycle), QA called twice (initial + re-verification)
        self.assertEqual(call_count["Coder"], 2)
        self.assertEqual(call_count["QA"], 2)

        # Verify structured REQUEST_CHANGE and TEST_PASSED messages exist
        msgs = store.list_messages(task_id)
        kinds = [m["kind"] for m in msgs]
        self.assertIn("REQUEST_CHANGE", kinds)
        self.assertIn("TEST_PASSED", kinds)

        # Clean up
        main.tasks_store.pop(task_id, None)
        store.purge_task(task_id)

    def test_approval_state_pause_and_resume(self):
        """Verify task pauses when approval required, and resumes when approved."""
        task_id = "test-approval-task"
        main.tasks_store[task_id] = {
            "id": task_id,
            "title": "Approval Test",
            "prompt": "Arsitektur sistem",
            "preset_id": "standard",
            "status": "queued",
            "require_approval": True,
            "stages": [
                {
                    "role": "Architect",
                    "name": "System Architect",
                    "system": "Kamu adalah System Architect",
                    "temperature": 0.1,
                    "status": "waiting",
                    "output": "",
                    "error": None
                }
            ]
        }

        async def run_pipeline_with_approval():
            async def mock_call(sys, u, temperature=0.1):
                return "Desain arsitektur database selesai."
            
            with patch("main.call_llm", side_effect=mock_call):
                # Start pipeline as background coroutine
                pipe_task = asyncio.create_task(main.execute_pipeline(task_id))
                # Wait briefly until it enters waiting_approval
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    if main.tasks_store[task_id].get("status") == "waiting_approval":
                        break
                
                # Check that it paused
                self.assertEqual(main.tasks_store[task_id]["status"], "waiting_approval")

                # Approve via endpoint
                res = client.post(f"/api/tasks/{task_id}/approve", json={"action": "approve", "feedback": "Disetujui"})
                self.assertEqual(res.status_code, 200)

                # Wait for pipeline to finish
                await pipe_task

        asyncio.run(run_pipeline_with_approval())

        t = main.tasks_store.get(task_id)
        self.assertEqual(t["status"], "completed")

        # Clean up
        main.tasks_store.pop(task_id, None)
        store.purge_task(task_id)


if __name__ == "__main__":
    unittest.main()
