import asyncio
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch, MagicMock

import httpx
from fastapi.testclient import TestClient

import main
from main import app, _build_chat_system_prompt
import store

client = TestClient(app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"} if main.AUTH_TOKEN else {})

class TestPhase2Verification(unittest.TestCase):

    def setUp(self):
        store.init_db()

    def test_browser_uses_fetch_not_eventsource(self):
        """Verify chat JS uses fetch + ReadableStream, NOT native EventSource (which is GET-only)."""
        chat_js_path = os.path.join(main.BASE_DIR, "static", "js", "chat.js")
        with open(chat_js_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Chat send block must use fetch with POST
        self.assertIn("res.body.getReader()", content)
        self.assertIn("ReadableStream", content)
        # Native EventSource must not be used for /api/chat
        self.assertNotIn("new EventSource('/api/chat'", content)
        self.assertNotIn('new EventSource("/api/chat"', content)

    def test_chat_session_and_message_persistence(self):
        """Verify chat sessions and messages persist in SQLite store."""
        sid = store.create_chat_session("Test Session 1", workspace_root="/tmp", model="bai")
        self.assertIsNotNone(sid)

        sess = store.get_chat_session(sid)
        self.assertEqual(sess["title"], "Test Session 1")
        self.assertEqual(sess["model"], "bai")

        mid1 = store.add_chat_message(sid, "user", "Hello Hermes!")
        mid2 = store.add_chat_message(sid, "assistant", "Hello human!", model="bai")

        messages = store.list_chat_messages(sid)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "Hello Hermes!")
        self.assertEqual(messages[1]["content"], "Hello human!")

        # Verify recent context helper produces correct format for LLM
        ctx = store.get_recent_chat_context(sid, n_turns=5)
        self.assertEqual(len(ctx), 2)
        self.assertEqual(ctx[0]["role"], "user")
        self.assertEqual(ctx[1]["role"], "assistant")

        # Clean up
        store.delete_chat_session(sid)
        self.assertIsNone(store.get_chat_session(sid))

    def test_workspace_and_skills_context_injection(self):
        """Verify _build_chat_system_prompt injects workspace repo map, rules, and skills."""
        here = main.BASE_DIR
        prompt = _build_chat_system_prompt(workspace_root=here, skills=["vibe-coding"])

        # Must include identity
        self.assertIn("Hermes", prompt)
        # Must include workspace path and skeleton
        self.assertIn(f"=== WORKSPACE: {here} ===", prompt)
        # Must include AGENTS.md rules
        self.assertIn("=== AGENTS.md ===", prompt)
        self.assertIn("Autonomous Multi-Agent Workstation", prompt)
        # Must include requested skill SOP
        self.assertIn("=== HERMES SKILL: vibe-coding ===", prompt)
        self.assertIn("smallest safe change", prompt.lower())

    def test_chat_session_crud_endpoints(self):
        """Verify /api/chat/sessions endpoints."""
        res = client.post("/api/chat/sessions", json={"title": "Test CRUD", "model": "deepseek-coder"})
        self.assertEqual(res.status_code, 200)
        sess = res.json()["session"]
        sid = sess["id"]
        self.assertEqual(sess["title"], "Test CRUD")

        # List
        res_list = client.get("/api/chat/sessions")
        self.assertEqual(res_list.status_code, 200)
        ids = [s["id"] for s in res_list.json()["sessions"]]
        self.assertIn(sid, ids)

        # Delete
        res_del = client.delete(f"/api/chat/sessions/{sid}")
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json()["status"], "ok")

    def test_chat_streaming_incremental(self):
        """Verify POST /api/chat genuinely yields incremental SSE chunks, not full buffered text."""
        chunks_seen = []

        # Mock _stream_chat_llm to simulate 3 distinct chunks over time
        async def fake_stream(messages, model=None, temperature=0.3):
            for word in ["Halo ", "dunia ", "Hermes!"]:
                yield f"data: {json.dumps({'chunk': word})}\n\n"
            yield f"data: {json.dumps({'done': True, 'full_content': 'Halo dunia Hermes!', 'model': model or 'bai'})}\n\n"

        with patch("main._stream_chat_llm", side_effect=fake_stream):
            with client.stream("POST", "/api/chat", json={"message": "Tes streaming"}) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
                sid = response.headers.get("x-chat-session-id")
                self.assertIsNotNone(sid)

                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if "chunk" in data:
                            chunks_seen.append(data["chunk"])

        # Genuinely received incremental pieces
        self.assertEqual(chunks_seen, ["Halo ", "dunia ", "Hermes!"])

        # Check that message was persisted in DB
        msgs = store.list_chat_messages(sid)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Tes streaming")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Halo dunia Hermes!")
        store.delete_chat_session(sid)

    def test_dashboard_model_override_respected(self):
        """Verify model parameter in request overrides default model."""
        captured_model = []

        async def fake_stream(messages, model=None, temperature=0.3):
            captured_model.append(model)
            yield f"data: {json.dumps({'done': True, 'full_content': 'OK', 'model': model})}\n\n"

        with patch("main._stream_chat_llm", side_effect=fake_stream):
            res = client.post("/api/chat", json={"message": "cek model", "model": "custom-gpt-5"})
            self.assertEqual(res.status_code, 200)

        self.assertEqual(captured_model, ["custom-gpt-5"])

    def test_existing_task_regression(self):
        """Verify Phase 1 task endpoints and security sanitization still work without regression."""
        # 1. Traversal blocked
        with self.assertRaises(main.HTTPException):
            main.sanitize_path("../../etc/shadow")

        # 2. Workspace info
        res_info = client.get("/api/workspace/info")
        self.assertEqual(res_info.status_code, 200)
        self.assertTrue(res_info.json()["exists"])

        # 3. Presets — returns a list directly
        res_presets = client.get("/api/presets")
        self.assertEqual(res_presets.status_code, 200)
        self.assertIsInstance(res_presets.json(), list)

        # 4. Workstation sessions
        res_sess = client.get("/api/workstation/sessions")
        self.assertEqual(res_sess.status_code, 200)

    def test_auto_pilot_pipeline_execution(self):
        """End-to-end execution of auto preset task, verifying parse_orchestrator_plan is defined and runs."""
        task_id = "test-auto-pipeline-run"
        main.tasks_store[task_id] = {
            "id": task_id,
            "title": "Test Auto Task",
            "prompt": "Buat fungsi kalkulator sederhana",
            "preset_id": "auto",
            "status": "queued",
            "stages": [
                {
                    "role": "Orchestrator",
                    "name": "AI Team Orchestrator",
                    "system": "Kamu adalah AI Team Orchestrator",
                    "temperature": 0.1,
                    "status": "waiting",
                    "output": "",
                    "error": None
                }
            ]
        }

        # Mock call_llm: Orchestrator returns a plan, Coder returns code, QA passes
        async def mock_call(system, user, temperature=0.2):
            if "Orchestrator" in system:
                return """{
  "task_type": "FEATURE_DEV",
  "target_scope": "Kalkulator sederhana",
  "selected_roles": ["Coder", "QA"],
  "bypassed_roles": ["Architect", "UI/UX"],
  "allowed_files": ["calc.py"],
  "forbidden_files": ["main.py"]
}"""
            elif "Lead Full-Stack Developer" in system or "Coder" in system:
                return "Implementasi kalkulator:\n### FILE: calc.py\n```python\ndef add(a, b):\n    return a + b\n```"
            elif "Senior QA" in system or "QA" in system:
                return "Kode bersih. VERDICT: PASSED"
            return "OK"

        with patch("main.call_llm", side_effect=mock_call):
            asyncio.run(main.execute_pipeline(task_id))

        t = main.tasks_store.get(task_id)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "completed")
        # Verify stages were dynamically populated by parse_orchestrator_plan
        stage_roles = [s["role"] for s in t["stages"]]
        self.assertIn("Coder", stage_roles)
        self.assertIn("QA", stage_roles)
        # Verify code extraction succeeded
        self.assertIn("calc.py", [f["path"] for f in t.get("extracted_files", [])])

        # Clean up
        main.tasks_store.pop(task_id, None)
        store.purge_task(task_id)

if __name__ == "__main__":
    unittest.main()
