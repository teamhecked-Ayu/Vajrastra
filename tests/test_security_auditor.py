import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

from engines.security_auditor import scan_path, summarize

DIRTY = """
import os
token = "AKIAIOSFODNN7EXAMPLE"
def bad():
    return eval("1+1")
"""


class TestSecurityAuditor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "dirty.py").write_text(DIRTY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_secrets_and_dangerous_calls_with_evidence(self):
        findings = scan_path(self.tmp.name)
        rules = {f.rule_id for f in findings}
        self.assertIn("aws_access_key", rules)
        self.assertIn("dangerous_call:eval", rules)
        for f in findings:
            self.assertTrue(f.evidence)
            self.assertEqual(f.path, "dirty.py")
        s = summarize(findings)
        self.assertGreaterEqual(s["high"], 2)

    def test_clean_tree_has_no_high_findings(self):
        root = Path(self.tmp.name)
        (root / "clean.py").write_text("def ok():\n    return 1 + 1\n")
        (root / "dirty.py").unlink()
        s = summarize(scan_path(self.tmp.name))
        self.assertEqual(s["high"], 0)
        self.assertEqual(s["total"], 0)

    def test_traversal_shape_flagged(self):
        root = Path(self.tmp.name)
        (root / "t.py").write_text('p = base + "../../etc/passwd"\n')
        (root / "dirty.py").unlink()
        findings = scan_path(self.tmp.name)
        self.assertIn("path_traversal_shape", {f.rule_id for f in findings})


if __name__ == "__main__":
    unittest.main()
