import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

from core.project_index import ProjectIndex, build_index

SAMPLE = """import os
import json

def add(a, b):
    return a + b

class Greeter:
    def greet(self, name):
        return 'hi ' + name
"""


class TestProjectIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "pkg").mkdir()
        (root / "pkg" / "mod.py").write_text(SAMPLE)
        (root / "pyproject.toml").write_text("[project]\nname='x'\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_symbols_kinds_imports_manifests(self):
        idx = build_index(self.tmp.name, commit_sha="abc123")
        kinds = {s.name: s.kind for s in idx.symbols}
        self.assertEqual(kinds["add"], "function")
        self.assertEqual(kinds["Greeter"], "class")
        self.assertEqual(kinds["greet"], "method")
        self.assertIn("pkg/mod.py", idx.files_importing("os"))
        self.assertIn("pkg/mod.py", idx.files_importing("json"))
        self.assertIn("pyproject.toml", idx.manifests)
        self.assertEqual(idx.commit_sha, "abc123")
        self.assertFalse(idx.truncated)

    def test_relevant_files_and_persistence_roundtrip(self):
        idx = build_index(self.tmp.name)
        self.assertIn("pkg/mod.py", idx.relevant_files(["add"]))
        p = Path(self.tmp.name) / "idx.json"
        idx.save(p)
        loaded = ProjectIndex.load(p)
        self.assertEqual(loaded.find_symbol("add")[0].file, "pkg/mod.py")
        self.assertEqual(loaded.files_importing("json"), ["pkg/mod.py"])

    def test_truncation_flagged_honestly(self):
        idx = build_index(self.tmp.name, max_files=0)
        self.assertTrue(idx.truncated)
        self.assertEqual(idx.files, [])


if __name__ == "__main__":
    unittest.main()
