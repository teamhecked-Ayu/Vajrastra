"""Tests for core.agent_loop (no network, no LLM, temp workspaces only)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_loop import AgentLoop, AgentLoopError


def scripted_runner(script):
    """Return a planner that replays the given decisions one at a time."""
    queue = list(script)

    def runner(prompt: str):
        if not queue:
            raise AssertionError("planner was asked for more steps than scripted")
        return queue.pop(0)

    return runner


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
