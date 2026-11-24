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
            self.assertTrue(case["buggy_failure_pattern"])

    def test_pysnooper_uses_python_2_and_test_helper_overlays(self):
        data = json.loads((BENCHMARK_DIR / "upstream_cases.json").read_text(encoding="utf-8"))
        case = next(item for item in data["cases"] if item["id"] == "pysnooper-1")
        self.assertEqual(case["python_version"], "2.7.18")
        self.assertEqual(
            case["test_overlays_from_fixed"],
            ["tests/test_chinese.py", "tests/utils.py"],
        )

    def test_case_filter_selects_only_requested_case_and_rejects_unknown_ids(self):
        cases = [{"id": "one"}, {"id": "two"}]
        self.assertEqual(select_cases(cases, ["two"]), [{"id": "two"}])
        self.assertEqual(select_cases(cases, []), cases)
        with self.assertRaises(ValueError):
            select_cases(cases, ["missing"])

    def test_test_overlay_copies_multiple_files_and_replaces_test_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buggy = root / "buggy"
            fixed = root / "fixed"
            (buggy / "tests").mkdir(parents=True)
            (fixed / "tests").mkdir(parents=True)
            (buggy / "tests" / "utils.py").write_text("old helper\n", encoding="utf-8")
            (fixed / "tests" / "test_regression.py").write_text("def test_bug(): pass\n", encoding="utf-8")
            (fixed / "tests" / "utils.py").write_text("fixed helper\n", encoding="utf-8")
            status = apply_test_overlay(
                {"test_overlays_from_fixed": ["tests/test_regression.py", "tests/utils.py"]},
                buggy, fixed,
            )
            self.assertEqual(status, "copied-from-fixed-revision")
            self.assertEqual(
                (buggy / "tests" / "test_regression.py").read_text(encoding="utf-8"),
                "def test_bug(): pass\n",
            )
            self.assertEqual(
                (buggy / "tests" / "utils.py").read_text(encoding="utf-8"),
                "fixed helper\n",
            )

    def test_test_overlay_rejects_parent_traversal_and_non_test_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for unsafe in ("../escape.py", "pysnooper/tracer.py"):
                with self.assertRaises(ValueError):
                    apply_test_overlay(
                        {"test_overlays_from_fixed": [unsafe]}, root / "buggy", root / "fixed"
                    )

    def test_upstream_runner_has_no_implicit_network_or_install_step(self):
        source = (BENCHMARK_DIR / "run_upstream_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("git", source)
        self.assertIn("subprocess.run", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("git clone", source)
        self.assertIn("--case", source)
        self.assertIn("buggy_failure_pattern", source)


if __name__ == "__main__":
    unittest.main()
