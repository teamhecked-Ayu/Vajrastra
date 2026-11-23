from __future__ import annotations

import json
import unittest
from pathlib import Path


class UpstreamBenchmarkManifestTests(unittest.TestCase):
    def test_cases_are_pinned_and_have_buggy_and_fixed_commits(self):
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "debugging" / "upstream_cases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["dataset"]["commit"], "316b95e2353ecda832bad9b42f86fa7c2fcec8ac")
        self.assertGreaterEqual(len(data["cases"]), 2)
        for case in data["cases"]:
            self.assertEqual(len(case["buggy_commit"]), 40)
            self.assertEqual(len(case["fixed_commit"]), 40)
            self.assertNotEqual(case["buggy_commit"], case["fixed_commit"])
            self.assertTrue(case["test_command"])
            self.assertTrue(case["changed_files"])

    def test_upstream_runner_has_no_implicit_network_or_install_step(self):
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "debugging" / "run_upstream_benchmark.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("git", source)
        self.assertIn("subprocess.run", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("git clone", source)


if __name__ == "__main__":
    unittest.main()
