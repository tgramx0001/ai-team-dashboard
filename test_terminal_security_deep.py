import asyncio
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

# Configure test environment
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "test_ai_team_term_sec.db")
os.environ["AI_TEAM_DB"] = TEST_DB_PATH
os.environ["AI_TEAM_AUTH_TOKEN"] = "super-secret-auth-token-12345"
os.environ["LLM_API_KEY"] = "sk-llm-secret-key-998877665544332211"
os.environ["OPENAI_API_KEY"] = "sk-openai-secret-key-aabbccddeeffgghh"

WORKSPACE_BASE = os.path.abspath(os.path.expanduser("~/projects"))
TEST_WORKSPACE_DIR = os.path.join(WORKSPACE_BASE, "term_sec_repo")
os.environ["WORKSPACE_ROOT"] = WORKSPACE_BASE
os.environ["DEFAULT_WORKSPACE"] = TEST_WORKSPACE_DIR

import main
import store
from fastapi.testclient import TestClient

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"})


class TestTerminalDeepSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        store._initialized_dbs.clear()
        store.init_db(force=True)

        if os.path.exists(TEST_WORKSPACE_DIR):
            shutil.rmtree(TEST_WORKSPACE_DIR)
        os.makedirs(TEST_WORKSPACE_DIR, exist_ok=True)

        # Create symlink pointing OUTSIDE the workspace boundary to /etc
        cls.outside_symlink = os.path.join(TEST_WORKSPACE_DIR, "symlink_to_etc")
        try:
            os.symlink("/etc", cls.outside_symlink)
        except Exception:
            pass

        # Create nested workspace dir
        cls.nested_dir = os.path.join(TEST_WORKSPACE_DIR, "sub_project")
        os.makedirs(cls.nested_dir, exist_ok=True)

        # Create symlink inside pointing to nested dir (allowed)
        cls.valid_symlink = os.path.join(TEST_WORKSPACE_DIR, "symlink_to_sub")
        try:
            os.symlink(cls.nested_dir, cls.valid_symlink)
        except Exception:
            pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_WORKSPACE_DIR):
            try:
                shutil.rmtree(TEST_WORKSPACE_DIR)
            except Exception:
                pass
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def test_1_cwd_symlink_escape_blocked(self):
        """1. CWD enforcement cannot be bypassed through symlinks pointing outside workspace."""
        if not os.path.islink(self.outside_symlink):
            self.skipTest("Symlink creation not supported")
        res = client.post("/api/workspace/terminal", json={
            "command": "pwd",
            "path": self.outside_symlink
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("Akses direktori di luar batas", res.json()["detail"])

    def test_2_cwd_traversal_blocked(self):
        """2. CWD enforcement blocks relative traversal variations."""
        for bad_path in [
            f"{TEST_WORKSPACE_DIR}/../..",
            f"{TEST_WORKSPACE_DIR}/../../../etc",
            f"{TEST_WORKSPACE_DIR}/./../../",
            f"{TEST_WORKSPACE_DIR}/sub_project/../../../../home",
            "/tmp",
            "/var",
            "/root"
        ]:
            res = client.post("/api/workspace/terminal", json={
                "command": "pwd",
                "path": bad_path
            })
            self.assertEqual(res.status_code, 400, f"Failed to block: {bad_path}")

    def test_3_allowed_symlink_inside_boundary(self):
        """3. Symlink pointing to an allowed path inside ALLOWED_ROOTS is resolved safely."""
        if not os.path.islink(self.valid_symlink):
            self.skipTest("Valid symlink not created")
        res = client.post("/api/workspace/terminal", json={
            "command": "pwd",
            "path": self.valid_symlink
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["exit_code"], 0)
        # Resolved path should match real destination
        self.assertEqual(data["cwd"], os.path.realpath(self.nested_dir))

    def test_4_process_group_and_children_killed_on_timeout(self):
        """4. Timeout reliably kills the entire process group, including child/subshell processes."""
        # Use a distinctive process marker
        marker = "orphan_proc_marker_xyz"
        cmd = f"python3 -c \"import subprocess, time; subprocess.Popen(['sleep', '120']); time.sleep(120)\""
        t0 = time.time()
        res = client.post("/api/workspace/terminal", json={
            "command": cmd,
            "path": TEST_WORKSPACE_DIR,
            "timeout": 1
        })
        duration = time.time() - t0
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["timed_out"])
        self.assertEqual(data["exit_code"], -1)
        self.assertLess(duration, 5.0, f"Timeout took too long: {duration}s")

        # Check if sleep 120 survived
        import subprocess
        ps = subprocess.run(["pgrep", "-f", "sleep 120"], capture_output=True, text=True)
        self.assertEqual(ps.returncode, 1, f"Orphan process still alive: {ps.stdout}")

    def test_5_environment_secrets_sanitized(self):
        """5. Commands like `env` or `printenv` cannot leak server tokens or provider keys."""
        res = client.post("/api/workspace/terminal", json={
            "command": "env",
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        stdout = res.json()["stdout"]

        # Critical: Master token & API keys MUST NOT appear in env output
        self.assertNotIn("super-secret-auth-token-12345", stdout)
        self.assertNotIn("sk-llm-secret-key-998877665544332211", stdout)
        self.assertNotIn("sk-openai-secret-key-aabbccddeeffgghh", stdout)
        self.assertNotIn("AI_TEAM_AUTH_TOKEN=", stdout)
        self.assertNotIn("LLM_API_KEY=", stdout)
        self.assertNotIn("OPENAI_API_KEY=", stdout)

    def test_6_stdout_and_stderr_secret_masking(self):
        """6. Masking applies to both stdout and stderr for configured tokens and generic patterns."""
        cmd = (
            "echo 'out: AI_TEAM_AUTH_TOKEN=super-secret-auth-token-12345 Bearer abcdef1234567890abcdef' && "
            "echo 'err: sk-someRandomSecretKey20CharsHere123' >&2"
        )
        res = client.post("/api/workspace/terminal", json={
            "command": cmd,
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Check stdout masking
        self.assertNotIn("super-secret-auth-token-12345", data["stdout"])
        self.assertIn("[REDACTED_SECRET]", data["stdout"])
        self.assertIn("[REDACTED_TOKEN]", data["stdout"])

        # Check stderr masking
        self.assertNotIn("sk-someRandomSecretKey20CharsHere123", data["stderr"])
        self.assertIn("[REDACTED_API_KEY]", data["stderr"])

    def test_7_output_limits_enforced_on_both_stdout_and_stderr(self):
        """7. Output limits (100,000 chars) apply to both stdout and stderr independently."""
        cmd = (
            "python3 -c \"import sys; sys.stdout.buffer.write(b'O' * 120000); sys.stdout.flush(); sys.stderr.buffer.write(b'E' * 120000); sys.stderr.flush()\""
        )
        res = client.post("/api/workspace/terminal", json={
            "command": cmd,
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertIn("Output truncated: exceeded 100000 chars", data["stdout"])
        self.assertIn("Error output truncated: exceeded 100000 chars", data["stderr"])
        self.assertLessEqual(len(data["stdout"]), 100100)
        self.assertLessEqual(len(data["stderr"]), 100100)


if __name__ == "__main__":
    unittest.main()
