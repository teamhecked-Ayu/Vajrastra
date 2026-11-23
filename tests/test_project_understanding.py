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

    def test_multi_repository_task_retrieval_benchmark(self):
        """Measure required-path recall on three distinct repository layouts."""
        cases = [
            {
                "files": {
                    "pyproject.toml": "[project]\nname='api-sample'\n",
                    "api/handlers.py": (
                        "from api.validators import validate\n"
                        "class RequestHandler:\n"
                        "    def handle(self, request):\n"
                        "        return validate(request)\n"
                    ),
                    "api/validators.py": "def validate(value):\n    return value\n",
                    "tests/test_handlers.py": "def test_handler():\n    assert True\n",
                },
                "task": "update RequestHandler validation behavior",
                "expected": {
                    "api/handlers.py", "api/validators.py",
                    "tests/test_handlers.py", "pyproject.toml",
                },
            },
            {
                "files": {
                    "setup.cfg": "[metadata]\nname = store-sample\n",
                    "database/store.py": (
                        "from database.schema import Record\n"
                        "class Store:\n"
                        "    def commit(self, record: Record):\n"
                        "        return record\n"
                    ),
                    "database/schema.py": "class Record:\n    pass\n",
                    "tests/test_store.py": "def test_store():\n    assert True\n",
                },
                "task": "change Store commit transaction behavior",
                "expected": {
                    "database/store.py", "database/schema.py",
                    "tests/test_store.py", "setup.cfg",
                },
            },
            {
                "files": {
                    "requirements.txt": "requests\n",
                    "cli/commands.py": (
                        "def sync_command(remote):\n"
                        "    return remote.sync()\n"
                    ),
                    "cli/transport.py": "def connect():\n    return None\n",
                    "tests/test_commands.py": "def test_sync_command():\n    assert True\n",
                },
                "task": "modify sync command behavior",
                "expected": {
                    "cli/commands.py", "tests/test_commands.py", "requirements.txt",
                },
            },
        ]
        recalls = []
        for case in cases:
            with self.subTest(task=case["task"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for rel, source in case["files"].items():
                    target = root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(source, encoding="utf-8")
                index = ProjectIndex.build(root, "benchmark-sha")
                context = index.task_context(
                    case["task"], current_commit_sha="benchmark-sha"
                )
                selected = set(context["selected_files"])
                expected = case["expected"]
                recall = len(expected & selected) / len(expected)
                recalls.append(recall)
                self.assertEqual(
                    expected & selected, expected,
                    "missing expected context paths: " + repr(expected - selected),
                )
        self.assertEqual(sum(recalls) / len(recalls), 1.0)

    def test_context_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            index = ProjectIndex.build(root, "abc123")
            with self.assertRaises(ProjectIndexError):
                index.task_context("service", current_commit_sha="abc123", max_files=0)


if __name__ == "__main__":
    unittest.main()
