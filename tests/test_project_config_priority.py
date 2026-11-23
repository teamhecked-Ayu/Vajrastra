from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engines.project_understanding import ProjectIndex


class ProjectConfigPriorityTests(unittest.TestCase):
    def test_manifest_is_not_crowded_out_by_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
            (root / "service.py").write_text("def service_retry(): return True\n", encoding="utf-8")
            (root / "service_helpers.py").write_text("def helper(): return True\n", encoding="utf-8")
            index = ProjectIndex.build(root, commit_sha="test-commit")
            context = index.task_context(
                "change service retry logic",
                current_commit_sha="test-commit",
                max_files=2,
            )
            self.assertIn("pyproject.toml", context["selected_files"])


if __name__ == "__main__":
    unittest.main()
