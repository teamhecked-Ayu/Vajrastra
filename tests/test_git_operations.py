import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.git_operations import GitOperations

GIT = shutil.which("git")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(GIT, "git not available in this environment")
class TestGitOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root,
                       check=True, capture_output=True)
        (self.root / "a.txt").write_text("one\n")
        _git(self.root, "add", ".")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@example.com",
             "commit", "-m", "init")
        self.ops = GitOperations(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        change = self.ops.create_change("work/x", {"a.txt": "two\n"}, actor="agent")
        self.assertFalse(change.approved)
        self.assertIn("a.txt", change.files)
        self.assertIn("--- a/a.txt", change.diff)
        self.assertEqual((self.root / "a.txt").read_text(), "one\n")

    def test_approved_write_stays_off_main(self):
        change = self.ops.create_change("work/x", {"a.txt": "two\n"}, actor="agent",
                                        approve=lambda c: True)
        self.assertTrue(change.approved)
        self.assertEqual(_git(self.root, "branch", "--show-current"), "work/x")
        self.assertEqual(_git(self.root, "show", "main:a.txt"), "one")
        self.assertEqual(_git(self.root, "show", "work/x:a.txt"), "two")

    def test_refuses_default_branch_writes(self):
        with self.assertRaises(ValueError):
            self.ops.create_change("main", {"a.txt": "x\n"}, actor="agent")

    def test_read_file_blocks_escape(self):
        with self.assertRaises(PermissionError):
            self.ops.read_file("../outside.txt")

    def test_read_file_respects_size_limit(self):
        with self.assertRaises((ValueError, PermissionError)):
            self.ops.read_file("a.txt", max_bytes=1)

    def test_push_and_pr_requires_approval(self):
        result = self.ops.push_and_pr("work/x", "title", "body", actor="agent")
        self.assertTrue(result.startswith("dry-run"))

    def test_rollback_gated_and_works(self):
        dry = self.ops.rollback("work/x", actor="agent")
        self.assertTrue(dry.startswith("dry-run"))
        self.ops.create_change("work/x", {"a.txt": "two\n"}, actor="agent",
                               approve=lambda c: True)
        msg = self.ops.rollback("work/x", actor="agent", approve=lambda p: True)
        self.assertIn("rolled back", msg)
        self.assertEqual(_git(self.root, "show", "work/x:a.txt"), "one")


if __name__ == "__main__":
    unittest.main()
