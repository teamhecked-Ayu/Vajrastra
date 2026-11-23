"""Tests for bounded, commit-aware project understanding."""
import json
import tempfile
import unittest
from pathlib import Path

from engines.project_understanding import (
    IndexLimits,
    ProjectIndex,
    ProjectIndexError,
    StaleProjectIndexError,
)


class ProjectUnderstandingTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text(
            "[project]\nname='sample'\n", encoding="utf-8"
        )
        (root / "src" / "service.py").write_text(
            "from .helpers import normalize\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return normalize('x')\n",
            encoding="utf-8",
        )
        (root / "src" / "helpers.py").write_text(
            "def normalize(value):\n    return value.strip()\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_service.py").write_text(
            "def test_service():\n    assert True\n", encoding="utf-8"
        )
        (root / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "secret.py").write_text("not indexed", encoding="utf-8")

    def test_inventory_symbols_imports_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "abc123")
            self.assertIn("pyproject.toml", index.manifests)
            self.assertIn("main.py", index.entry_points)
            self.assertIn("tests/test_service.py", index.tests)
            service = index.python_files["src/service.py"]
            self.assertIn("Service", [item["name"] for item in service.symbols])
            self.assertIn("run", [item["name"] for item in service.symbols])
            self.assertIn("src/helpers.py", index.import_edges["src/service.py"])
            self.assertNotIn(".git/secret.py", [item.path for item in index.inventory])

    def test_task_context_includes_related_test_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "abc123")
            context = index.task_context(
                "change Service run behavior", current_commit_sha="abc123"
            )
            selected = context["selected_files"]
            self.assertIn("src/service.py", selected)
            self.assertIn("tests/test_service.py", selected)
            self.assertIn("pyproject.toml", selected)

    def test_stale_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "oldsha")
            with self.assertRaises(StaleProjectIndexError):
                index.task_context("change service", current_commit_sha="newsha")

    def test_index_round_trip_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "abc123")
            restored = ProjectIndex.from_dict(json.loads(json.dumps(index.to_dict())))
            self.assertEqual(index.fingerprint(), restored.fingerprint())

    def test_file_and_byte_limits_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(
                root, "abc123",
                limits=IndexLimits(max_files=2, max_file_bytes=10,
                                   max_total_bytes=10, max_python_files=1,
                                   max_symbols_per_file=1, max_imports_per_file=1),
            )
            self.assertLessEqual(len(index.inventory), 2)
            self.assertTrue(index.warnings)

    def test_invalid_limits_and_empty_revision_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            with self.assertRaises(ProjectIndexError):
                ProjectIndex.build(root, "")
            with self.assertRaises(ProjectIndexError):
                ProjectIndex.build(root, "abc", limits=IndexLimits(max_files=0))

    def test_syntax_error_is_recorded_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            index = ProjectIndex.build(root, "abc123")
            self.assertIsNotNone(index.python_files["broken.py"].syntax_error)

    def test_context_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "abc123")
            with self.assertRaises(ProjectIndexError):
                index.task_context("service", current_commit_sha="abc123", max_files=0)


if __name__ == "__main__":
    unittest.main()
