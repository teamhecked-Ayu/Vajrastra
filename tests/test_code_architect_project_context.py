"""Integration tests for project context in the CodeArchitect prompts."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from engines.code_architect import CodeArchitect


class CodeArchitectProjectContextTests(unittest.TestCase):
    def test_blue_and_repair_prompts_receive_indexed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "catalog.py").write_text(
                "class Catalog:\n    def lookup(self, sku):\n        return sku\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Vajrastra Tests"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

            architect = CodeArchitect.__new__(CodeArchitect)
            architect.root = root
            rendered, note = architect._prepare_project_context(
                "change Catalog lookup behavior", "python"
            )
            architect._project_context_text = rendered
            self.assertIn("project context attached", note)
            self.assertIn("def lookup", rendered)
            self.assertIn("def lookup", architect._blue_prompt(
                "change Catalog lookup behavior", "python", round_index=1
            ))
            patch_prompt = architect._patch_prompt(
                "change Catalog lookup behavior", "python",
                {"src/catalog.py": "class Catalog: pass"},
                {"tests/test_catalog.py": "def test_catalog(): pass"},
                ["test failed"], "edge case", 1,
            )
            self.assertIn("def lookup", patch_prompt)


if __name__ == "__main__":
    unittest.main()
