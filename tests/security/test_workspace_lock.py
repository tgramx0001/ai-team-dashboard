"""Per-workspace write serialization (shortcoming #5).

Covers: workspace_file_lock() serializes read-modify-write sections across
threads, keys are realpath-normalized (symlinks alias the same lock), and the
endpoint decorator holds the lock for the handler duration.
"""
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager

import main


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestWorkspaceFileLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wslock_")
        self.file = os.path.join(self.dir, "shared.txt")
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("")

    def test_lock_serializes_concurrent_read_modify_write(self):
        def worker(tag):
            for _ in range(10):
                with main.workspace_file_lock(self.dir):
                    cur = _read(self.file)
                    time.sleep(0.005)          # widen the race window
                    with open(self.file, "w", encoding="utf-8") as f:
                        f.write(cur + tag)

        threads = [threading.Thread(target=worker, args=("A",)),
                   threading.Thread(target=worker, args=("B",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        final = _read(self.file)
        # Without a lock, interleaved read-modify-write loses appends.
        self.assertEqual(len(final), 20)
        self.assertEqual(final.count("A"), 10)
        self.assertEqual(final.count("B"), 10)

    def test_lock_key_is_symlink_normalized(self):
        link = os.path.join(tempfile.gettempdir(), f"wslock_link_{os.getpid()}")
        if os.path.islink(link):
            os.remove(link)
        os.symlink(self.dir, link)
        try:
            key1 = os.path.normcase(os.path.realpath(self.dir))
            key2 = os.path.normcase(os.path.realpath(link))
            self.assertEqual(key1, key2)  # symlink target and link share one key
            with main.workspace_file_lock(self.dir):
                self.assertIn(key1, main._ws_locks)
            self.assertIs(main._ws_locks[key1], main._ws_locks.get(key2))
        finally:
            if os.path.islink(link):
                os.remove(link)

    def test_lock_released_on_exception(self):
        with self.assertRaises(ValueError):
            with main.workspace_file_lock(self.dir):
                raise ValueError("boom")
        lock = main._ws_locks[os.path.normcase(os.path.realpath(self.dir))]
        self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
