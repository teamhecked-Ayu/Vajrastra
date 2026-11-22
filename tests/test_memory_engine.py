"""Tests for the episodic ledger, keyword index and skill store."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import (  # noqa: E402
    MemoryEngine,
    normalize_signature,
    signature_hash,
)
from core.security_guard import SecurityViolation  # noqa: E402

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/home/ana/project/app.py", line 42, in <module>\n'
    "    main()\n"
    "ValueError: portfolio weights must sum to 1.0\n"
)


class SignatureTests(unittest.TestCase):
    def test_normalises_paths_and_line_numbers(self) -> None:
        variant = TRACEBACK.replace("/home/ana/project/app.py", "/tmp/x/app.py").replace(
            "line 42", "line 4242"
        )
        self.assertEqual(signature_hash(TRACEBACK), signature_hash(variant))

    def test_ansi_escapes_removed(self) -> None:
        dirty = "\x1b[31mError: boom\x1b[0m"
        self.assertNotIn("\x1b", normalize_signature(dirty))

    def test_different_failures_differ(self) -> None:
        self.assertNotEqual(signature_hash("ValueError: a"), signature_hash("KeyError: b"))


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.memory = MemoryEngine(self.root)

    def tearDown(self) -> None:
        self.memory.close()
        self._tmp.cleanup()

    def test_record_and_recall_by_signature(self) -> None:
        lesson = self.memory.record_lesson(
            TRACEBACK,
            root_cause="weights were not normalised before the sum check",
            verified_patch="total = sum(w); w = [x / total for x in w]",
            experiment_metrics={"loss": 0.12, "epochs": 3},
            tags=["python", "numeric"],
            verified=True,
        )
        self.assertTrue(lesson.solved)
        self.assertEqual(lesson.success_count, 1)

        recall = self.memory.get_by_signature(TRACEBACK.replace("line 42", "line 7"))
        self.assertIsNotNone(recall)
        assert recall is not None
        self.assertEqual(recall.id, lesson.id)
        self.assertEqual(dict(recall.experiment_metrics)["loss"], 0.12)
        self.assertIn("numeric", recall.tags)

    def test_merge_keeps_verified_patch(self) -> None:
        self.memory.record_lesson(TRACEBACK, verified_patch="patch-v1", verified=True)
        merged = self.memory.record_lesson(TRACEBACK, root_cause="normalise weights")
        self.assertEqual(merged.verified_patch, "patch-v1")
        self.assertEqual(merged.root_cause, "normalise weights")

        updated = self.memory.record_lesson(TRACEBACK, verified_patch="patch-v2", verified=True)
        self.assertEqual(updated.verified_patch, "patch-v2")
        self.assertEqual(updated.success_count, 2)

    def test_keyword_search(self) -> None:
        self.memory.record_lesson(TRACEBACK, root_cause="weights not normalised", tags=["numeric"])
        self.memory.record_lesson("sqlite3.OperationalError: database is locked", root_cause="busy timeout")
        hits = self.memory.search("normalised weights")
        self.assertTrue(hits)
        self.assertIn("weights", hits[0].error_signature)

    def test_search_falls_back_to_recent_for_empty_query(self) -> None:
        self.memory.record_lesson("ValueError: a")
        self.assertEqual(len(self.memory.search("")), 1)

    def test_unresolved_and_stats(self) -> None:
        solved = self.memory.record_lesson("KeyError: 'a'", verified_patch="use .get", verified=True)
        self.memory.record_lesson("OSError: disk full")
        self.memory.record_hit(solved.id, resolved=True)

        open_items = self.memory.unresolved()
        self.assertEqual(len(open_items), 1)
        self.assertIn("disk full", open_items[0].error_signature)

        stats = self.memory.stats()
        self.assertEqual(stats["lessons"], 2)
        self.assertEqual(stats["verified_patches"], 1)
        self.assertEqual(stats["total_hits"], 1)
        self.assertEqual(stats["skills"], 0)

    def test_tags_counter(self) -> None:
        self.memory.record_lesson("A: one", tags=["sqlite", "concurrency"])
        self.memory.record_lesson("B: two", tags=["sqlite"])
        tags = dict(self.memory.all_tags())
        self.assertEqual(tags["sqlite"], 2)


class SkillStoreTests(unittest.TestCase):
    SAFE = '"""Backoff helper."""\n\n\ndef backoff(attempt: int) -> float:\n    return min(2 ** attempt, 30)\n'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.memory = MemoryEngine(self.root)

    def tearDown(self) -> None:
        self.memory.close()
        self._tmp.cleanup()

    def test_save_load_list_and_search(self) -> None:
        skill = self.memory.skills.save_skill(
            "Exponential Backoff",
            self.SAFE,
            description="retry helper for locked sqlite databases",
            tags=["retry", "sqlite"],
        )
        self.assertEqual(skill.slug, "exponential-backoff")
        self.assertTrue((self.root / "memory_bank" / "skills" / "exponential-backoff.py").is_file())

        loaded = self.memory.skills.load_skill("exponential-backoff")
        self.assertIn("backoff", loaded.code)
        self.assertEqual(self.memory.skills.count(), 1)

        by_tag = self.memory.skills.list_skills(tag="retry")
        self.assertEqual(len(by_tag), 1)
        self.assertEqual(self.memory.skills.search_skills("sqlite")[0].slug, "exponential-backoff")

    def test_dangerous_skill_is_rejected(self) -> None:
        with self.assertRaises(SecurityViolation):
            self.memory.skills.save_skill("Bad Helper", 'import os\nos.system("id")\n')
        self.assertEqual(self.memory.skills.count(), 0)

    def test_delete_skill(self) -> None:
        self.memory.skills.save_skill("Temp", self.SAFE)
        self.assertTrue(self.memory.skills.delete_skill("temp"))
        self.assertEqual(self.memory.skills.count(), 0)

    def test_snapshot_round_trip(self) -> None:
        self.memory.record_lesson(TRACEBACK, verified_patch="normalise", verified=True)
        self.memory.skills.save_skill("Backoff", self.SAFE, description="retry helper")
        snapshot = self.memory.export_snapshot()
        self.assertEqual(len(snapshot["lessons"]), 1)
        self.assertEqual(len(snapshot["skills"]), 1)

        with tempfile.TemporaryDirectory() as other:
            with MemoryEngine(Path(other)) as clone:
                counts = clone.import_snapshot(snapshot)
                self.assertEqual(counts["lessons"], 1)
                self.assertEqual(counts["skills"], 1)
                self.assertEqual(clone.stats()["lessons"], 1)
                self.assertEqual(clone.skills.count(), 1)


if __name__ == "__main__":
    unittest.main()
