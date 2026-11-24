from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "debugging"
sys.path.insert(0, str(BENCHMARK_DIR))
from run_upstream_benchmark import apply_test_overlay, select_cases


class UpstreamBenchmarkManifestTests(unittest.TestCase):
    def test_cases_are_pinned_and_have_buggy_and_fixed_commits(self):
        path = BENCHMARK_DIR / "upstream_cases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["dataset"]["commit"], "316b95e2353ecda832bad9b42f86fa7c2fcec8ac")
        self.assertGreaterEqual(len(data["cases"]), 2)
        for case in data["cases"]:
            self.assertEqual(len(case["buggy_commit"]), 40)
            self.assertEqual(len(case["fixed_commit"]), 40)
            self.assertNotEqual(case["buggy_commit"], case["fixed_commit"])
            self.assertTrue(case["test_command"])
            self.assertTrue(case["changed_files"])

    def test_pysnooper_regression_test_is_declared_as_fixed_revision_overlay(self):
        data = json.loads((BENCHMARK_DIR / "upstream_cases.json").read_text(encoding="utf-8"))
        case = next(item for item in data["cases"] if item["id"] == "pysnooper-1")
        self.assertEqual(case["test_overlay_from_fixed"], "tests/test_chinese.py")

    def test_case_filter_selects_only_requested_case_and_rejects_unknown_ids(self):
        cases = [{"id": "one"}, {"id": "two"}]
        self.assertEqual(select_cases(cases, ["two"]), [{"id": "two"}])
        self.assertEqual(select_cases(cases, []), cases)
        with self.assertRaises(ValueError):
            select_cases(cases, ["missing"])

    def test_test_overlay_copies_fixed_test_into_buggy_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buggy = root / "buggy"
            fixed = root / "fixed"
            (buggy / "tests").mkdir(parents=True)
            (fixed / "tests").mkdir(parents=True)
            (fixed / "tests" / "test_regression.py").write_text("def test_bug(): pass\n", encoding="utf-8")
            status = apply_test_overlay(
                {"test_overlay_from_fixed": "tests/test_regression.py"}, buggy, fixed
            )
            self.assertEqual(status, "copied-from-fixed-revision")
            self.assertEqual(
                (buggy / "tests" / "test_regression.py").read_text(encoding="utf-8"),
                "def test_bug(): pass\n",
            )

    def test_test_overlay_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                apply_test_overlay(
                    {"test_overlay_from_fixed": "../escape.py"}, root / "buggy", root / "fixed"
                )

    def test_upstream_runner_has_no_implicit_network_or_install_step(self):
        source = (BENCHMARK_DIR / "run_upstream_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("git", source)
        self.assertIn("subprocess.run", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("git clone", source)
        self.assertIn("--case", source)


if __name__ == "__main__":
    unittest.main()
