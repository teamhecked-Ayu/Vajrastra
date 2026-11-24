import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

from engines.auto_test_gen import generate_candidates, run_candidates

GOOD = """
def add(a, b):
    return a + b

def explode(x):
    return 1 / 0
"""


class TestAutoTestGen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mod.py"
        self.path.write_text(GOOD)

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates_generated_and_labeled(self):
        cands = generate_candidates(self.path, observed_bug="add returns wrong sum")
        names = {c.name for c in cands}
        self.assertIn("test_add_smoke", names)
        self.assertIn("test_explode_smoke", names)
        self.assertTrue(all(c.origin == "generated" for c in cands))
        add_cand = next(c for c in cands if c.name == "test_add_smoke")
        self.assertIn("regression intent", add_cand.source)

    def test_execution_reports_honestly(self):
        cands = generate_candidates(self.path)
        reports = {r.candidate: r for r in run_candidates(cands, self.path)}
        self.assertTrue(reports["test_add_smoke"].ran)
        self.assertTrue(reports["test_add_smoke"].passed)
        self.assertFalse(reports["test_add_smoke"].flaky)
        self.assertTrue(reports["test_explode_smoke"].ran)
        self.assertFalse(reports["test_explode_smoke"].passed)
        self.assertIn("ZeroDivisionError", reports["test_explode_smoke"].detail)
        self.assertTrue(all(r.origin == "generated" for r in reports.values()))

    def test_no_candidates_reports_nothing_ran(self):
        empty = Path(self.tmp.name) / "empty.py"
        empty.write_text("X = 1\n")
        reports = run_candidates([], empty)
        self.assertFalse(reports[0].ran)
        self.assertFalse(reports[0].passed)
        self.assertIn("nothing ran", reports[0].detail)


if __name__ == "__main__":
    unittest.main()
