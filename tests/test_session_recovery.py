"""Crash and partial-write recovery tests for SessionManager."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.session_manager import SessionError, SessionManager


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = SessionManager(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replays_journal_record_if_snapshot_write_was_interrupted(self):
        state = self.manager.create("recover me")
        snapshot = self.manager._state_path(state.session_id).read_bytes()
        self.manager.append(state, "tool", "completed output", step=4)
        self.manager._state_path(state.session_id).write_bytes(snapshot)

        recovered = self.manager.load(state.session_id)
        self.assertEqual(len(recovered.turns), 1)
        self.assertEqual(recovered.turns[0].content, "completed output")
        self.assertEqual(recovered.step, 4)
        self.assertEqual(recovered.meta["_journal_entries"], 1)

    def test_replays_checkpoint_after_snapshot_failure(self):
        state = self.manager.create("checkpoint recovery")
        snapshot = self.manager._state_path(state.session_id).read_bytes()
        state.step = 3
        self.manager.checkpoint(state, "after-step-3", verified=True)
        self.manager._state_path(state.session_id).write_bytes(snapshot)

        recovered = self.manager.load(state.session_id)
        self.assertEqual(recovered.checkpoints[0]["label"], "after-step-3")
        self.assertEqual(recovered.checkpoints[0]["step"], 3)

    def test_ignores_torn_final_journal_record(self):
        state = self.manager.create("torn write")
        journal = self.manager._journal_path(state.session_id)
        journal.write_bytes(b'{"kind":"user","content":"ok","step":1,"meta":{},"at":"now"}\n{"kind":')
        recovered = self.manager.load(state.session_id)
        self.assertEqual([turn.content for turn in recovered.turns], ["ok"])

    def test_rejects_malformed_complete_journal_record(self):
        state = self.manager.create("bad journal")
        self.manager._journal_path(state.session_id).write_text("{bad}\n", encoding="utf-8")
        with self.assertRaisesRegex(SessionError, "malformed journal record"):
            self.manager.load(state.session_id)

    def test_disk_full_append_does_not_mutate_session_state(self):
        state = self.manager.create("disk full")
        with mock.patch("core.session_manager.os.write", side_effect=OSError(28, "no space")):
            with self.assertRaisesRegex(SessionError, "cannot append"):
                self.manager.append(state, "user", "not durable")
        self.assertEqual(state.turns, [])
        self.assertEqual(state.meta["_journal_entries"], 0)

    def test_duplicate_replay_is_prevented_by_durable_sequence(self):
        state = self.manager.create("sequence")
        self.manager.append(state, "user", "once")
        loaded = self.manager.load(state.session_id)
        self.manager._persist(loaded)
        loaded_again = self.manager.load(state.session_id)
        self.assertEqual([turn.content for turn in loaded_again.turns], ["once"])

    def test_legacy_snapshot_does_not_duplicate_existing_journal(self):
        state = self.manager.create("legacy")
        self.manager.append(state, "user", "already in snapshot")
        snapshot = json.loads(self.manager._state_path(state.session_id).read_text(encoding="utf-8"))
        snapshot["meta"] = {}
        self.manager._state_path(state.session_id).write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        loaded = self.manager.load(state.session_id)
        self.assertEqual([turn.content for turn in loaded.turns], ["already in snapshot"])


if __name__ == "__main__":
    unittest.main()
