import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from core.context_engine import ContextEngine


class TestContextEngine(unittest.TestCase):
    def test_drops_low_priority_sections_to_fit_budget(self):
        eng = ContextEngine(budget_chars=2000)
        files = [(f"f{i}.py", "x" * 500) for i in range(5)]
        pkg = eng.build("sys", "do the task", selected_files=files,
                        lessons=["lesson " * 100])
        self.assertLessEqual(pkg.total_chars, 2000)
        self.assertTrue(pkg.fits)
        self.assertTrue(pkg.truncation_notes)

    def test_system_and_request_always_survive(self):
        eng = ContextEngine(budget_chars=5000)
        pkg = eng.build("s" * 3000, "req")
        names = [s.name for s in pkg.sections]
        self.assertIn("system", names)
        self.assertIn("request", names)

    def test_tiny_budget_rejected(self):
        with self.assertRaises(ValueError):
            ContextEngine(budget_chars=100)

    def test_render_contains_sections(self):
        eng = ContextEngine()
        pkg = eng.build("sys", "req", selected_files=[("a.py", "code")])
        out = pkg.render()
        self.assertIn("[system]", out)
        self.assertIn("[file:a.py]", out)


if __name__ == "__main__":
    unittest.main()
