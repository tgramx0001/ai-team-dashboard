import unittest
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient
import main

client = TestClient(main.app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"} if main.AUTH_TOKEN else {})


class TestApiSmoke(unittest.TestCase):
    def test_index_serves_html_and_scripts(self):
        """Index page must load and reference modular scripts."""
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("AI Team Workstation", res.text)
        self.assertIn("/static/js/app.js", res.text)
        self.assertIn("/static/js/chat.js", res.text)

    def test_static_js_app_served(self):
        """Static file app.js is served properly."""
        res = client.get("/static/js/app.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("AI_TEAM_AUTH_KEY", res.text)

    def test_static_js_chat_served(self):
        """Static file chat.js is served properly."""
        res = client.get("/static/js/chat.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("toggleChat", res.text)

    def test_system_status_api(self):
        """System status endpoint works."""
        res = client.get("/api/system/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("router_ok", data)
        self.assertIn("model", data)
        self.assertIn("default_workspace", data)


if __name__ == "__main__":
    unittest.main()
