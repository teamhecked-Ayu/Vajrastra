from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engines.project_understanding import ProjectIndex


class ProjectTermNormalizationTests(unittest.TestCase):
    def test_singular_task_terms_match_plural_module_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
            (root / "src" / "requests").mkdir(parents=True)
            (root / "src" / "requests" / "sessions.py").write_text(
                "class Session: pass\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_requests.py").write_text(
                "def test_session_cookie(): pass\n", encoding="utf-8"
            )
            index = ProjectIndex.build(root, commit_sha="test-commit")
            context = index.task_context(
                "change session cookie persistence in requests",
                current_commit_sha="test-commit",
                max_files=8,
            )
            self.assertIn("src/requests/sessions.py", context["selected_files"])
            self.assertIn("tests/test_requests.py", context["selected_files"])


if __name__ == "__main__":
    unittest.main()
