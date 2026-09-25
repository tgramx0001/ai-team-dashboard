import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Configure test environment before importing main
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "test_ai_team_phase4.db")
os.environ["AI_TEAM_DB"] = TEST_DB_PATH
os.environ["AI_TEAM_AUTH_TOKEN"] = "test-phase4-token"

# Create a clean mock workspace inside ~/projects
WORKSPACE_BASE = os.path.abspath(os.path.expanduser("~/projects"))
TEST_WORKSPACE_DIR = os.path.join(WORKSPACE_BASE, "phase4_test_repo")
os.environ["WORKSPACE_ROOT"] = WORKSPACE_BASE
os.environ["DEFAULT_WORKSPACE"] = TEST_WORKSPACE_DIR

import main
import store
from fastapi.testclient import TestClient

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"})
unauth_client = TestClient(main.app)


class TestPhase4TerminalAndGit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        store._initialized_dbs.clear()
        store.init_db(force=True)
        store.seed_agents_from_file()

        # Create temporary git workspace
        if os.path.exists(TEST_WORKSPACE_DIR):
            shutil.rmtree(TEST_WORKSPACE_DIR)
        os.makedirs(TEST_WORKSPACE_DIR, exist_ok=True)

        # Initialize git repo in TEST_WORKSPACE_DIR
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=TEST_WORKSPACE_DIR, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=TEST_WORKSPACE_DIR, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=TEST_WORKSPACE_DIR, check=True)

        # Initial commit
        readme_path = os.path.join(TEST_WORKSPACE_DIR, "README.md")
        with open(readme_path, "w") as f:
            f.write("# Phase 4 Test\nLine 1\nLine 2\n")
        subprocess.run(["git", "add", "README.md"], cwd=TEST_WORKSPACE_DIR, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=TEST_WORKSPACE_DIR, check=True)

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

    def test_terminal_requires_auth(self):
        """Terminal execution requires valid Authorization header."""
        res = unauth_client.post("/api/workspace/terminal", json={"command": "echo 'hi'", "path": TEST_WORKSPACE_DIR})
        self.assertEqual(res.status_code, 401)

    def test_terminal_normal_execution(self):
        """Execute a basic shell command inside workspace and return structured output."""
        res = client.post("/api/workspace/terminal", json={
            "command": "echo 'Phase 4 works' && pwd",
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["exit_code"], 0)
        self.assertIn("Phase 4 works", data["stdout"])
        self.assertEqual(data["cwd"], TEST_WORKSPACE_DIR)
        self.assertFalse(data["timed_out"])
        self.assertGreater(data["duration_ms"], 0)

    def test_terminal_cwd_boundary_enforcement(self):
        """Primary Security: Terminal must reject cwd outside ALLOWED_ROOTS (e.g. /etc or ~ root)."""
        # 1. System directory
        res = client.post("/api/workspace/terminal", json={"command": "ls", "path": "/etc"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Akses direktori di luar batas", res.json()["detail"])

        # 2. Directory traversal attempt
        res = client.post("/api/workspace/terminal", json={"command": "ls", "path": f"{TEST_WORKSPACE_DIR}/../../../../etc"})
        self.assertEqual(res.status_code, 400)

        # 3. Non-existent path
        res = client.post("/api/workspace/terminal", json={"command": "ls", "path": f"{TEST_WORKSPACE_DIR}/nonexistent_12345"})
        self.assertEqual(res.status_code, 400)

    def test_terminal_timeout_and_process_cleanup(self):
        """Long-running commands must be killed upon reaching timeout with timed_out flag."""
        res = client.post("/api/workspace/terminal", json={
            "command": "sleep 10",
            "path": TEST_WORKSPACE_DIR,
            "timeout": 1
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["timed_out"])
        self.assertEqual(data["exit_code"], -1)
        self.assertIn("timed out after 1 seconds", data["stderr"])

    def test_terminal_output_size_truncation(self):
        """Huge terminal output is truncated at 100,000 characters."""
        # Print 120k characters
        res = client.post("/api/workspace/terminal", json={
            "command": "python3 -c \"import sys; sys.stdout.buffer.write(b'A' * 120000)\"",
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("Output truncated: exceeded 100000 chars", data["stdout"])

    def test_terminal_masks_sensitive_secrets(self):
        """API keys, Auth tokens, and standard secret tokens are masked in output."""
        res = client.post("/api/workspace/terminal", json={
            "command": "echo 'Auth token: test-phase4-token and key sk-1234567890abcdef1234567890'",
            "path": TEST_WORKSPACE_DIR
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertNotIn("test-phase4-token", data["stdout"])
        self.assertIn("[REDACTED_SECRET]", data["stdout"])
        self.assertIn("[REDACTED_API_KEY]", data["stdout"])

    def test_git_status_clean_repo(self):
        """Git status on clean repo returns clean=True and valid branch."""
        res = client.get(f"/api/workspace/git/status?path={TEST_WORKSPACE_DIR}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["is_git"])
        self.assertEqual(data["branch"], "main")
        self.assertTrue(data["clean"])
        self.assertEqual(data["status_lines"], [])

    def test_git_status_and_diff_with_modifications(self):
        """Modifying a tracked file reflects in git status and read-only unified diff."""
        readme_path = os.path.join(TEST_WORKSPACE_DIR, "README.md")
        with open(readme_path, "a") as f:
            f.write("Line 3 modified by Phase 4\n")

        # 1. Test git status
        res = client.get(f"/api/workspace/git/status?path={TEST_WORKSPACE_DIR}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["is_git"])
        self.assertFalse(data["clean"])
        self.assertTrue(any("README.md" in line for line in data["status_lines"]))

        # 2. Test git diff full repo
        res_diff = client.get(f"/api/workspace/git/diff?path={TEST_WORKSPACE_DIR}")
        self.assertEqual(res_diff.status_code, 200)
        diff_data = res_diff.json()
        self.assertTrue(diff_data["is_git"])
        self.assertFalse(diff_data["is_empty"])
        self.assertGreaterEqual(diff_data["files_changed"], 1)
        self.assertGreaterEqual(diff_data["additions"], 1)
        self.assertIn("Line 3 modified by Phase 4", diff_data["raw_diff"])

        # 3. Test git diff single file
        res_single = client.get(f"/api/workspace/git/diff?path={TEST_WORKSPACE_DIR}&file_path=README.md")
        self.assertEqual(res_single.status_code, 200)
        single_data = res_single.json()
        self.assertIn("Line 3 modified by Phase 4", single_data["raw_diff"])

        # Revert changes to keep repo clean
        import subprocess
        subprocess.run(["git", "checkout", "--", "README.md"], cwd=TEST_WORKSPACE_DIR, check=True)

    def test_git_endpoints_non_git_workspace(self):
        """Git endpoints on non-git directory gracefully return is_git=False."""
        temp_dir = os.path.join(WORKSPACE_BASE, "temp_non_git")
        os.makedirs(temp_dir, exist_ok=True)
        try:
            res_status = client.get(f"/api/workspace/git/status?path={temp_dir}")
            self.assertEqual(res_status.status_code, 200)
            self.assertFalse(res_status.json()["is_git"])

            res_diff = client.get(f"/api/workspace/git/diff?path={temp_dir}")
            self.assertEqual(res_diff.status_code, 200)
            self.assertFalse(res_diff.json()["is_git"])
            self.assertTrue(res_diff.json()["is_empty"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_git_endpoints_boundary_enforcement(self):
        """Git endpoints reject path traversal or locations outside ALLOWED_ROOTS."""
        res = client.get("/api/workspace/git/status?path=/etc")
        self.assertEqual(res.status_code, 400)

        res_diff = client.get("/api/workspace/git/diff?path=/etc")
        self.assertEqual(res_diff.status_code, 400)

        # File path traversal inside repo
        res_file_traversal = client.get(f"/api/workspace/git/diff?path={TEST_WORKSPACE_DIR}&file_path=../../../../etc/passwd")
        self.assertEqual(res_file_traversal.status_code, 400)

    def test_hermes_skills_and_sessions_parity(self):
        """Hermes status, skills, and model switching endpoints remain fully functional."""
        res = client.get("/api/hermes/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("skills_count", data)
        self.assertIn("active_model", data)

        res_skills = client.get("/api/hermes/skills")
        self.assertEqual(res_skills.status_code, 200)
        self.assertIsInstance(res_skills.json().get("skills"), list)


if __name__ == "__main__":
    unittest.main()
