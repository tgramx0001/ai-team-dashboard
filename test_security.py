import os
import unittest
from fastapi.testclient import TestClient
import main
from main import app, sanitize_path, extract_code_files
from fastapi import HTTPException

client = TestClient(app, headers={"Authorization": f"Bearer {main.AUTH_TOKEN}"} if main.AUTH_TOKEN else {})

class TestSecurityAndFeatures(unittest.TestCase):

    def test_sanitize_path_valid(self):
        base = os.path.dirname(os.path.abspath(main.__file__))
        p = sanitize_path(os.path.join(base, "main.py"), base_dir=base)
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(p.startswith(base))

    def test_sanitize_path_traversal(self):
        base = os.path.dirname(os.path.abspath(main.__file__))
        with self.assertRaises(HTTPException) as ctx:
            sanitize_path(os.path.join(base, "../../etc/passwd"), base_dir=base)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_sanitize_path_null_byte(self):
        base = os.path.dirname(os.path.abspath(main.__file__))
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

    def test_extract_code_files_nested_backticks(self):
        text_with_nested = """
### FILE: README.md
```markdown
# Documentation
Example usage:
```python
x = 10
print(x)
```
End of section.
```

### FILE: script.py
```python
print("done")
```
"""
        files = extract_code_files(text_with_nested)
        f_map = {f["path"]: f for f in files}
        self.assertIn("README.md", f_map)
        self.assertIn("script.py", f_map)
        self.assertIn("print(x)", f_map["README.md"]["content"])
        self.assertIn("End of section.", f_map["README.md"]["content"])
        self.assertEqual(f_map["script.py"]["content"].strip(), 'print("done")')

    def test_workspace_diff(self):
        res = client.post("/api/workspace/diff", json={
            "path": os.path.dirname(os.path.abspath(main.__file__)),
            "rel_path": "test_diff_temp.txt",
            "new_content": "Line 1\nLine 2\n"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("additions", data)
        self.assertIn("deletions", data)
        self.assertIn("diff_lines", data)

class TestPhase0WorkspaceBoundary(unittest.TestCase):
    """WORKSPACE_ROOT no longer defaults to the whole $HOME (Phase 0)."""

    def test_primary_root_allowed(self):
        p = sanitize_path(os.path.join(main.ALLOWED_ROOT, "probe.txt"))
        self.assertTrue(p.startswith(main.ALLOWED_ROOT))

    def test_extra_root_allowed(self):
        if not main.EXTRA_ROOTS:
            self.skipTest("no WORKSPACE_EXTRA_ROOTS configured")
        p = sanitize_path(os.path.join(main.EXTRA_ROOTS[0], "probe.txt"))
        self.assertTrue(p.startswith(main.EXTRA_ROOTS[0]))

    def test_outside_every_root_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            sanitize_path("/etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_home_dir_directly_rejected(self):
        home_file = os.path.join(os.path.expanduser("~"), "somefile.txt")
        roots = [main.ALLOWED_ROOT] + main.EXTRA_ROOTS
        if any(os.path.abspath(home_file).startswith(r) for r in roots):
            self.skipTest("home dir is inside a configured root")
        with self.assertRaises(HTTPException):
            sanitize_path(home_file)


class TestPhase0Auth(unittest.TestCase):

    def test_bearer_token_enforced_when_configured(self):
        old = main.AUTH_TOKEN
        try:
            main.AUTH_TOKEN = "unit-test-token"
            denied = client.get("/api/system/status")
            self.assertEqual(denied.status_code, 401)
            allowed = client.get(
                "/api/system/status",
                headers={"Authorization": "Bearer unit-test-token"},
            )
            self.assertEqual(allowed.status_code, 200)
        finally:
            main.AUTH_TOKEN = old


class TestPhase0Config(unittest.TestCase):

    def test_workspace_presets_endpoint(self):
        res = client.get("/api/workspace/presets")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("default", data)
        self.assertTrue(data["presets"])
        roots = [main.ALLOWED_ROOT] + main.EXTRA_ROOTS
        for item in data["presets"]:
            self.assertIn("path", item)
            self.assertIn("label", item)
            self.assertTrue(
                any(os.path.abspath(item["path"]).startswith(r) for r in roots),
                f"preset path outside workspace roots: {item['path']}",
            )

    def test_system_status_has_no_hardcoded_host(self):
        res = client.get("/api/system/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertNotIn("tailscale_ip", data)
        self.assertIn("public_host", data)
        self.assertIn("default_workspace", data)

    def test_model_switch_is_dashboard_scoped_by_default(self):
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp()
        hermes_cfg = os.path.join(tmpdir, "config.yaml")
        with open(hermes_cfg, "w", encoding="utf-8") as f:
            f.write(
                "model:\n"
                "  default: hermes-model-x\n"
                "  provider: custom\n"
                "  base_url: http://127.0.0.1:20128/v1\n"
            )
        override_file = os.path.join(tmpdir, "model_override")

        old_override = main.MODEL_OVERRIDE_FILE
        old_hermes = main.HERMES_CONFIG_FILE
        old_env_model = os.environ.pop("LLM_MODEL", None)
        try:
            main.MODEL_OVERRIDE_FILE = override_file
            main.HERMES_CONFIG_FILE = hermes_cfg
            def _read(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read()

            before = _read(hermes_cfg)

            scope = main.update_hermes_model("dash-model-1")
            self.assertEqual(scope, "dashboard")
            # global side effect must NOT happen on the default path
            self.assertEqual(_read(hermes_cfg), before)
            self.assertEqual(main.get_llm_config()[1], "dash-model-1")

            info = main.get_hermes_model_info()
            self.assertEqual(info["scope"], "dashboard")
            self.assertEqual(info["hermes_default"], "hermes-model-x")

            # explicit global write still available
            scope = main.update_hermes_model("hermes-model-2", apply_globally=True)
            self.assertEqual(scope, "hermes")
            self.assertIn("hermes-model-2", _read(hermes_cfg))
            self.assertFalse(os.path.exists(override_file))
        finally:
            main.MODEL_OVERRIDE_FILE = old_override
            main.HERMES_CONFIG_FILE = old_hermes
            if old_env_model is not None:
                os.environ["LLM_MODEL"] = old_env_model
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
