"""Interactive terminal: stdin injection, PTY sessions, WebSocket streaming
(shortcomings #2/#3 coverage for routes_tools)."""
import os
import sys
import unittest

if "main" not in sys.modules:
    import tempfile
    os.environ["AI_TEAM_DB"] = os.path.join(tempfile.gettempdir(), "test_ai_team_terminalws.db")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"})
IS_POSIX = os.name == "posix"


def _ws_run(ws, command, stdin_lines=(), timeout=30, path=None):
    """Run one command over the WS session; return (output_text, exit_payload)."""
    payload = {"type": "run", "command": command, "timeout": timeout}
    if path:
        payload["path"] = path
    ws.send_json(payload)
    for line in stdin_lines:
        ws.send_json({"type": "input", "data": line if line.endswith("\n") else line + "\n"})
    text = ""
    while True:
        msg = ws.receive_json()
        if msg["type"] == "output":
            text = msg["text"]          # server sends full snapshots, not deltas
        elif msg["type"] == "error":
            raise AssertionError(f"WS error: {msg.get('detail')}")
        elif msg["type"] == "exit":
            return text, msg


class TestTerminalStdinAndPty(unittest.TestCase):
    def test_post_stdin_injection(self):
        res = client.post("/api/workspace/terminal", json={
            "command": "read x && echo got:$x",
            "stdin": "hello-from-stdin\n",
            "timeout": 10,
        })
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["exit_code"], 0, data)
        self.assertIn("got:hello-from-stdin", data["stdout"])

    @unittest.skipUnless(IS_POSIX, "PTY is POSIX-only; Windows uses pipes")
    def test_post_pty_mode_reports_tty(self):
        res = client.post("/api/workspace/terminal", json={
            "command": "test -t 1 && echo TTYOK",
            "pty": True,
            "timeout": 10,
        })
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data["pty"], data)
        self.assertEqual(data["exit_code"], 0, data)
        self.assertIn("TTYOK", data["stdout"])

    def test_post_default_keeps_pipe_contract(self):
        res = client.post("/api/workspace/terminal", json={
            "command": "echo to-stdout; echo to-stderr >&2",
            "timeout": 10,
        })
        data = res.json()
        self.assertFalse(data["pty"])
        self.assertIn("to-stdout", data["stdout"])
        self.assertIn("to-stderr", data["stderr"])   # streams stay separated


class TestTerminalWebSocket(unittest.TestCase):
    def test_auth_rejects_bad_token(self):
        with client.websocket_connect("/api/workspace/terminal/ws") as ws:
            ws.send_json({"type": "auth", "token": "definitely-wrong"})
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()

    def test_run_streams_output_and_exit(self):
        with client.websocket_connect("/api/workspace/terminal/ws") as ws:
            ws.send_json({"type": "auth", "token": main.AUTH_TOKEN})
            self.assertEqual(ws.receive_json()["type"], "ready")
            text, exit_msg = _ws_run(ws, "echo hello-ws")
        self.assertIn("hello-ws", text)
        self.assertEqual(exit_msg["code"], 0)

    def test_interactive_stdin_roundtrip(self):
        with client.websocket_connect("/api/workspace/terminal/ws") as ws:
            ws.send_json({"type": "auth", "token": main.AUTH_TOKEN})
            self.assertEqual(ws.receive_json()["type"], "ready")
            text, exit_msg = _ws_run(ws, "read x && echo got:$x", stdin_lines=["ping"])
        self.assertIn("got:ping", text)
        self.assertEqual(exit_msg["code"], 0)

    def test_cwd_rejects_outside_workspace(self):
        with client.websocket_connect("/api/workspace/terminal/ws") as ws:
            ws.send_json({"type": "auth", "token": main.AUTH_TOKEN})
            self.assertEqual(ws.receive_json()["type"], "ready")
            ws.send_json({"type": "run", "command": "echo x", "path": "/etc", "timeout": 10})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")


if __name__ == "__main__":
    unittest.main()
