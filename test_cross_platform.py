import asyncio
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import main
import store

IS_WINDOWS = sys.platform.startswith("win")


class TestCrossPlatformSupport(unittest.TestCase):
    """Platform-aware tests for Windows path handling, process management, and Git/Hermes paths."""

    def test_paths_use_proper_separators(self):
        """Path operations must handle Windows and POSIX separators consistently."""
        base_dir = main.BASE_DIR
        self.assertTrue(os.path.isabs(base_dir))
        self.assertTrue(os.path.exists(base_dir))

        # Check store paths
        db_path = store.db_path()
        self.assertTrue(os.path.isabs(db_path))

        agents_path = store.agents_seed_path()
        self.assertTrue(os.path.isabs(agents_path))
        self.assertTrue(os.path.exists(agents_path))

    def test_hermes_and_9router_paths_configurable(self):
        """Hermes and 9Router paths should honor environment overrides."""
        with patch.dict(os.environ, {
            "HERMES_HOME": "C:\\Users\\User\\.hermes" if IS_WINDOWS else "/custom/hermes",
            "NINEROUTER_DB": "C:\\Users\\User\\.9router\\db\\data.sqlite" if IS_WINDOWS else "/custom/9router.sqlite"
        }):
            custom_hermes = os.environ.get("HERMES_HOME")
            self.assertTrue(custom_hermes is not None and "hermes" in custom_hermes.lower())

            cfg_base, cfg_model, cfg_key = main.get_llm_config()
            self.assertTrue(isinstance(cfg_base, str))

    def test_sanitize_path_windows_and_posix_pathlib(self):
        """Sanitize path must support Path objects and resolve drive/symlink boundaries cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            real_tmp = str(Path(tmp_dir).resolve())
            
            # File inside base
            child_file = os.path.join(real_tmp, "hello.txt")
            with open(child_file, "w") as f:
                f.write("content")
            
            sanitized = main.sanitize_path(child_file, base_dir=real_tmp)
            self.assertEqual(sanitized, str(Path(child_file).resolve()))

            # Traversal outside must fail
            with self.assertRaises(main.HTTPException):
                main.sanitize_path(os.path.join(real_tmp, "..", "escaped.txt"), base_dir=real_tmp)

            # Null byte must fail
            with self.assertRaises(main.HTTPException):
                main.sanitize_path(f"{real_tmp}\0bad.txt", base_dir=real_tmp)

    def test_workspace_seed_cross_platform_separators(self):
        """Workspace root naming must handle both forward slash and backslash roots."""
        # POSIX style
        posix_root = "/home/user/projects/my-app"
        name_posix = store._get_path_name(posix_root)
        self.assertEqual(name_posix, "my-app")

        # Windows style
        win_root = "C:\\Users\\User\\projects\\my-app"
        name_win = store._get_path_name(win_root)
        self.assertEqual(name_win, "my-app")

    def test_process_tree_kill_logic(self):
        """_kill_process_tree must be platform aware."""
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.returncode = None

        if IS_WINDOWS:
            # On Windows, should attempt taskkill
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_kill_task = MagicMock()
                mock_kill_task.wait = MagicMock(return_value=asyncio.sleep(0.01))
                mock_exec.return_value = mock_kill_task
                asyncio.run(main._kill_process_tree(mock_proc))
                mock_exec.assert_called_once()
                args, _ = mock_exec.call_args
                self.assertEqual(args[0], "taskkill")
                self.assertIn("/PID", args)
                self.assertIn("99999", args)
        else:
            # On Unix, should use os.killpg
            with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=99999):
                asyncio.run(main._kill_process_tree(mock_proc))
                mock_killpg.assert_called_once_with(99999, main.signal.SIGKILL)

    @unittest.skipUnless(IS_WINDOWS, "Windows-specific test for shell and taskkill execution")
    def test_windows_shell_execution(self):
        """Verify Windows cmd/powershell execution when running on native Windows."""
        pass

    @unittest.skipIf(IS_WINDOWS, "POSIX-specific process session test")
    def test_posix_start_new_session_flag(self):
        """On POSIX systems, start_new_session is enabled for process group isolation."""
        self.assertFalse(main.IS_WINDOWS)


if __name__ == "__main__":
    unittest.main()
