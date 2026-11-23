"""Tests for core.session_manager."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory_engine import MemoryEngine  # noqa: F401  (import guard: package wiring)
from core.session_manager import (
    DEFAULT_COMPACTION_THRESHOLD,
    SessionError,
    SessionManager,
    Turn,
    extract_key_lines,
)


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = SessionManager(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --------------------------------------------------------------- creation
    def test_create_and_load_roundtrip(self) -> None:
        state = self.manager.create("fix the login bug in app.py")
        loaded = self.manager.load(state.session_id)
        self.assertEqual(loaded.session_id, state.session_id)
        self.assertEqual(loaded.goal, "fix the login bug in app.py")
        self.assertEqual(loaded.status, "active")

    def test_load_missing_raises(self) -> None:
        with self.assertRaises(SessionError):
            self.manager.load("does-not-exist")

    def test_list_sessions_orders_by_update(self) -> None:
        first = self.manager.create("first task")
        second = self.manager.create("second task")
        rows = self.manager.list_sessions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["session_id"], second.session_id)
        self.assertEqual(rows[1]["session_id"], first.session_id)

    # ---------------------------------------------------------------- writing
    def test_append_turn_persists(self) -> None:
        state = self.manager.create("goal")
        self.manager.append(state, "user", "hello")
        loaded = self.manager.load(state.session_id)
        self.assertEqual(len(loaded.turns), 1)
        self.assertEqual(loaded.turns[0].kind, "user")
        journal = self.manager._journal_path(state.session_id)
        self.assertTrue(journal.is_file())

    def test_checkpoint_and_resume(self) -> None:
        state = self.manager.create("goal")
        for index in range(5):
            self.manager.append(state, "tool", f"output {index}", step=index)
        checkpoint = self.manager.checkpoint(state, "after-step-2", step=2, turn_index=3)
        self.manager.append(state, "tool", "doomed output", step=3)
        self.manager.resume_from(state, checkpoint)
        self.assertEqual(len(state.turns), 3)
        self.assertEqual(state.step, 2)
        # The journal on disk still holds every turn, including the rewound one.
        lines = self.manager._journal_path(state.session_id).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 7)  # 5 turns + 1 checkpoint + 1 rewound turn

    def test_finish_status_validation(self) -> None:
        state = self.manager.create("goal")
        with self.assertRaises(ValueError):
            self.manager.finish(state, "bogus")
        self.manager.finish(state, "resting")
        self.assertEqual(self.manager.load(state.session_id).status, "resting")

    # -------------------------------------------------------------- compaction
    def test_extract_key_lines_keeps_errors_and_patches(self) -> None:
        content = "all good\nNameError: name 'x' is not defined\nverbose detail\nverified patch applied"
        lines = extract_key_lines(content)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("NameError" in line for line in lines))

    def test_compact_deterministic_fold(self) -> None:
        state = self.manager.create("goal")
        for index in range(DEFAULT_COMPACTION_THRESHOLD + 10):
            self.manager.append(state, "tool", f"plain output {index}", step=index)
        report = self.manager.compact(state, threshold=DEFAULT_COMPACTION_THRESHOLD)
        self.assertEqual(report["method"], "deterministic")
        self.assertEqual(report["folded"], 10)
        self.assertEqual(report["remaining"], DEFAULT_COMPACTION_THRESHOLD)
        self.assertIn("folded 10 turn(s)", state.summary)

    def test_compact_model_summarizer_used_when_working(self) -> None:
        state = self.manager.create("goal")
        for index in range(DEFAULT_COMPACTION_THRESHOLD + 5):
            self.manager.append(state, "tool", f"output {index}", step=index)

        def summarizer(block: str) -> str:
            return "MODEL SUMMARY"

        report = self.manager.compact(state, summarize=summarizer, threshold=10)
        self.assertEqual(report["method"], "model")
        self.assertIn("MODEL SUMMARY", state.summary)

    def test_compact_model_failure_falls_back(self) -> None:
        state = self.manager.create("goal")
        for index in range(20):
            self.manager.append(state, "tool", "Error: boom", step=index)

        def broken(_block: str) -> str:
            raise RuntimeError("model down")

        report = self.manager.compact(state, summarize=broken, threshold=10)
        self.assertEqual(report["method"], "deterministic")
        self.assertIn("Error: boom", state.summary)

    def test_compact_noop_below_threshold(self) -> None:
        state = self.manager.create("goal")
        self.manager.append(state, "user", "short session")
        report = self.manager.compact(state)
        self.assertEqual(report["folded"], 0)

    def test_context_payload_budgeted(self) -> None:
        state = self.manager.create("the big goal")
        for index in range(30):
            self.manager.append(state, "tool", "x" * 2000, step=index)
        payload = self.manager.context_payload(state, budget_chars=5000)
        self.assertLessEqual(len(payload), 5000 + 2000)  # one line may overshoot
        self.assertIn("GOAL: the big goal", payload)
        self.assertIn("RECENT TURNS", payload)


if __name__ == "__main__":
    unittest.main()
