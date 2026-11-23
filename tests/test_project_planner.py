"""Tests for safe project source materialization."""

import tempfile
import unittest
from pathlib import Path

from engines.project_understanding import ProjectIndex, ProjectIndexError
from engines.project_planner import render_project_context


class ProjectPlannerContextTests(unittest.TestCase):
    def make_workspace(self, root: Path) -> None:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text("[project]\nname='planner-fixture'\n", encoding="utf-8")
        (root / "src" / "billing.py").write_text(
            "class BillingService:\n"
            "    def calculate_invoice(self, amount):\n"
            "        return amount * 1.18\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_billing.py").write_text(
            "def test_billing():\n    assert True\n", encoding="utf-8"
        )

    def test_materializes_selected_source_tests_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_workspace(root)
            index = ProjectIndex.build(root, "fixture-revision")
            rendered, context = render_project_context(
                index,
                "change BillingService invoice calculation",
                current_commit_sha="fixture-revision",
            )
            self.assertIn("--- src/billing.py ---", rendered)
            self.assertIn("def calculate_invoice", rendered)
            self.assertIn("--- tests/test_billing.py ---", rendered)
            self.assertIn("--- pyproject.toml ---", rendered)
            self.assertIn("src/billing.py", context["selected_files"])

    def test_rejects_source_changed_after_indexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_workspace(root)
            index = ProjectIndex.build(root, "fixture-revision")
            (root / "src" / "billing.py").write_text(
                "class BillingService:\n    pass\n", encoding="utf-8"
            )
            with self.assertRaises(ProjectIndexError):
                render_project_context(
                    index,
                    "change BillingService invoice calculation",
                    current_commit_sha="fixture-revision",
                )

    def test_rejects_stale_commit_before_reading_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_workspace(root)
            index = ProjectIndex.build(root, "old-revision")
            with self.assertRaises(ProjectIndexError):
                render_project_context(
                    index,
                    "change billing",
                    current_commit_sha="new-revision",
                )


if __name__ == "__main__":
    unittest.main()
