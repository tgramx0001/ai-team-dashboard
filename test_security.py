import os
import unittest
from fastapi.testclient import TestClient
from main import app, sanitize_path, extract_code_files
from fastapi import HTTPException

client = TestClient(app)

class TestSecurityAndFeatures(unittest.TestCase):

    def test_sanitize_path_valid(self):
        base = "/home/andreadst/projects/ai-team-dashboard"
        p = sanitize_path(os.path.join(base, "main.py"), base_dir=base)
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(p.startswith(base))

    def test_sanitize_path_traversal(self):
        base = "/home/andreadst/projects/ai-team-dashboard"
        with self.assertRaises(HTTPException) as ctx:
            sanitize_path(os.path.join(base, "../../etc/passwd"), base_dir=base)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_sanitize_path_null_byte(self):
        base = "/home/andreadst/projects/ai-team-dashboard"
        with self.assertRaises(HTTPException) as ctx:
            sanitize_path(os.path.join(base, "file\0.txt"), base_dir=base)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_extract_code_files_scope_matrix_whitelist_glob(self):
        sm = {
            "allowed_files": ["*.md", "src/*"],
            "forbidden_files": ["main.py", "secret.env"]
        }
        sample_text = """
### FILE: docs/readme.md
```markdown
# Readme
```

### FILE: main.py
```python
print("forbidden")
```

### FILE: other.py
```python
print("not allowed")
```
"""
        files = extract_code_files(sample_text, scope_matrix=sm)
        f_map = {f["path"]: f for f in files}

        self.assertIn("docs/readme.md", f_map)
        self.assertFalse(f_map["docs/readme.md"]["blocked"])

        self.assertIn("main.py", f_map)
        self.assertTrue(f_map["main.py"]["blocked"])
        self.assertIn("forbidden_files", f_map["main.py"]["blocked_reason"])

        self.assertIn("other.py", f_map)
        self.assertTrue(f_map["other.py"]["blocked"])
        self.assertIn("whitelist", f_map["other.py"]["blocked_reason"])

    def test_check_syntax_python(self):
        res_valid = client.post("/api/workspace/check-syntax", json={
            "rel_path": "app.py",
            "content": "def hello():\n    return 42\n"
        })
        self.assertEqual(res_valid.status_code, 200)
        self.assertTrue(res_valid.json()["valid"])

        res_invalid = client.post("/api/workspace/check-syntax", json={
            "rel_path": "app.py",
            "content": "def hello(\n"
        })
        self.assertEqual(res_invalid.status_code, 200)
        self.assertFalse(res_invalid.json()["valid"])
        self.assertEqual(res_invalid.json()["line"], 1)

    def test_check_syntax_javascript(self):
        res_valid = client.post("/api/workspace/check-syntax", json={
            "rel_path": "app.js",
            "content": "const msg = \")\"; console.log(msg);"
        })
        self.assertEqual(res_valid.status_code, 200)
        self.assertTrue(res_valid.json()["valid"])

        res_invalid = client.post("/api/workspace/check-syntax", json={
            "rel_path": "app.js",
            "content": "const msg = (\n"
        })
        self.assertEqual(res_invalid.status_code, 200)
        self.assertFalse(res_invalid.json()["valid"])

    def test_workspace_diff(self):
        res = client.post("/api/workspace/diff", json={
            "path": "/home/andreadst/projects/ai-team-dashboard",
            "rel_path": "test_diff_temp.txt",
            "new_content": "Line 1\nLine 2\n"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("additions", data)
        self.assertIn("deletions", data)
        self.assertIn("diff_lines", data)

if __name__ == "__main__":
    unittest.main()
