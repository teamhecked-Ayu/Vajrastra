"""End-to-end tests for the CLI and REPL (everything runs in a temp root)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vajrastra  # noqa: E402

CLEAN_ENV_KEYS = (
    "VAJRASTRA_EXECUTION_MODE",
    "VAJRASTRA_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {"VAJRASTRA_ROOT": str(self.root)},
            clear=False,
        )
        self._env.start()
        for key in CLEAN_ENV_KEYS:
            if key != "VAJRASTRA_ROOT":
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = vajrastra.main(argv)
        return code, out.getvalue(), err.getvalue()


class CommandTests(CliTestCase):
    def test_memory_stats(self) -> None:
        code, out, _ = self.run_cli(["memory", "stats"])
        self.assertEqual(code, 0)
        self.assertIn("lessons", out)
        self.assertTrue((self.root / "memory_bank" / "lessons.sqlite").is_file())

    def test_memory_search_empty(self) -> None:
        code, out, _ = self.run_cli(["memory", "search", "dataset drift"])
        self.assertEqual(code, 0)
        self.assertIn("results", out)

    def test_build_local_plan(self) -> None:
        code, out, _ = self.run_cli(["build", "add retry logic to the sqlite loader"])
        self.assertEqual(code, 0)
        self.assertIn("local_native", out)
        plans = list((self.root / "memory_bank" / "plans").glob("*.json"))
        self.assertEqual(len(plans), 1)
        self.assertIn("steps", json.loads(plans[0].read_text(encoding="utf-8")))

    def test_build_heavy_queues_colab_job(self) -> None:
        os.environ["VAJRASTRA_EXECUTION_MODE"] = "cloud"
        code, out, _ = self.run_cli(["build", "train a small gpu model on the dataset"])
        self.assertEqual(code, 0)
        self.assertIn("cloud_bridge", out)
        jobs = list((self.root / "memory_bank" / "bridge" / "outbox").glob("*.json"))
        self.assertEqual(len(jobs), 1)
        payload = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["target"], "google_colab")
        self.assertEqual(payload["task_class"], "training")

    def test_research_writes_brief(self) -> None:
        code, out, _ = self.run_cli(["research", "sqlite locking"])
        self.assertEqual(code, 0)
        briefs = list((self.root / "memory_bank" / "research").glob("*.md"))
        self.assertEqual(len(briefs), 1)
        self.assertIn("sqlite locking", briefs[0].read_text(encoding="utf-8"))

    def test_fix_flags_unsafe_file(self) -> None:
        target = self.root / "bad.py"
        target.write_text('import os\nos.system("id")\n', encoding="utf-8")
        code, out, _ = self.run_cli(["fix", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("process_execution", out)

    def test_fix_accepts_traceback_text(self) -> None:
        code, out, _ = self.run_cli(["fix", "ValueError: weights must sum to 1.0"])
        self.assertEqual(code, 0)
        self.assertIn("signature", out)

    def test_evolve_reports_integrity(self) -> None:
        code, out, _ = self.run_cli(["evolve"])
        self.assertEqual(code, 0)
        self.assertIn("integrity", out)
        self.assertIn("immutable", out)
        self.assertTrue(list((self.root / "memory_bank" / "evolution").glob("*.md")))

    def test_sync_exports_snapshot(self) -> None:
        code, out, _ = self.run_cli(["sync"])
        self.assertEqual(code, 0)
        snapshots = list((self.root / "memory_bank" / "sync").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)

    def test_sync_import_round_trip(self) -> None:
        self.run_cli(["sync"])
        source = next((self.root / "memory_bank" / "sync").glob("*.json"))
        with tempfile.TemporaryDirectory() as other:
            copy = Path(other) / "snapshot.json"
            copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            code, out, _ = self.run_cli(["sync", "--import", str(copy)])
        self.assertEqual(code, 0)
        self.assertIn("imported", out)

    def test_missing_prompt_is_usage_error(self) -> None:
        code, _, err = self.run_cli(["build"])
        self.assertEqual(code, 2)
        self.assertTrue(err)


class ReplTests(CliTestCase):
    def test_repl_runs_command_and_exits(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch(
            "builtins.input", side_effect=["memory stats", "help", "exit"]
        ):
            code = vajrastra.main([])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertEqual(vajrastra.REPL_PROMPT, "vajrastra> ")
        self.assertIn("Vajrastra", text)
        self.assertIn("lessons", text)

    def test_repl_exits_on_eof(self) -> None:
        with redirect_stdout(io.StringIO()), mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(vajrastra.main([]), 0)


if __name__ == "__main__":
    unittest.main()
