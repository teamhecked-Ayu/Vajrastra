"""Tests for core.agent_loop (no network, no LLM, temp workspaces only)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def make_loop(self, script, **kwargs):
        return AgentLoop(
            self.root,
            runner=scripted_runner(script),
            command_runner=kwargs.pop("command_runner", lambda argv: (0, "ok")),
            **kwargs,
        )

    # ------------------------------------------------------------- happy path
    def test_finish_immediately(self) -> None:
        loop = self.make_loop(
            [{"tool": "finish", "args": {"summary": "nothing to do"}, "reason": "trivial"}]
        )
        run = loop.run("say hello")
        self.assertTrue(run.finished)
        self.assertTrue(run.verified)
        self.assertEqual(run.summary, "nothing to do")
        self.assertEqual(run.steps[0].tool, "finish")

    def test_read_then_finish(self) -> None:
        (self.root / "note.txt").write_text("remember this", encoding="utf-8")
        loop = self.make_loop(
            [
                {"tool": "read_file", "args": {"path": "note.txt"}},
                {"tool": "finish", "args": {"summary": "read it"}},
            ]
        )
        run = loop.run("read note.txt")
        self.assertTrue(run.verified)
        self.assertIn("remember this", run.steps[0].output)

    def test_write_code_goes_through_firewall(self) -> None:
        loop = self.make_loop(
            [
                {
                    "tool": "write_code",
                    "args": {"path": "helper.py", "code": "def add(a, b):\n    return a + b\n"},
                },
                {"tool": "finish", "args": {"summary": "wrote helper"}},
            ]
        )
        run = loop.run("add a helper")
        self.assertTrue(run.verified)
        self.assertTrue((self.root / "helper.py").is_file())

    def test_write_code_rejected_by_firewall_records_failure(self) -> None:
        loop = self.make_loop(
            [
                {
                    "tool": "write_code",
                    "args": {"path": "evil.py", "code": "import os\nos.system('rm -rf /')\n"},
                },
                {"tool": "write_code", "args": {"path": "good.py", "code": "x = 1\n"}},
                {"tool": "finish", "args": {"summary": "recovered"}},
            ]
        )
        run = loop.run("try something shady")
        self.assertTrue(run.verified)
        self.assertFalse(run.steps[0].ok)
        self.assertTrue(run.steps[1].ok)
        self.assertFalse((self.root / "evil.py").exists())

    def test_write_code_refused_inside_core(self) -> None:
        (self.root / "core").mkdir()
        loop = self.make_loop(
            [
                {
                    "tool": "write_code",
                    "args": {"path": "core/hacked.py", "code": "x = 1\n"},
                },
                {"tool": "finish", "args": {"summary": "gave up on core"}},
            ]
        )
        run = loop.run("hack the core")
        self.assertFalse(run.steps[0].ok)
        self.assertIn("protected", run.steps[0].output.lower())

    # ----------------------------------------------------------------- safety
    def test_path_escape_rejected(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "read_file", "args": {"path": "../../etc/passwd"}},
                {"tool": "finish", "args": {"summary": "done"}},
            ]
        )
        run = loop.run("escape the sandbox")
        self.assertFalse(run.steps[0].ok)
        self.assertIn("escapes the workspace", run.steps[0].output)

    def test_run_command_allow_list(self) -> None:
        calls = []

        def fake_command(argv):
            calls.append(list(argv))
            return 0, "All tests passed"

        loop = self.make_loop(
            [
                {"tool": "run_command", "args": {"argv": ["python", "-m", "unittest", "discover"]}},
                {"tool": "finish", "args": {"summary": "tests green"}},
            ],
            command_runner=fake_command,
        )
        run = loop.run("run the tests")
        self.assertTrue(run.verified)
        self.assertEqual(calls, [["python", "-m", "unittest", "discover"]])

    def test_run_command_blocks_arbitrary_shell(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "run_command", "args": {"argv": ["python", "-c", "print(1)"]}},
                {"tool": "finish", "args": {"summary": "done"}},
            ]
        )
        run = loop.run("run a sneaky command")
        self.assertFalse(run.steps[0].ok)
        self.assertIn("allow-list", run.steps[0].output)

    def test_run_command_failure_is_step_failure(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "run_command", "args": {"argv": ["python", "-m", "unittest"]}},
                {"tool": "finish", "args": {"summary": "done"}},
            ],
            command_runner=lambda argv: (1, "AssertionError: boom"),
        )
        run = loop.run("run tests")
        self.assertFalse(run.steps[0].ok)
        self.assertTrue(run.steps[1].ok)
        self.assertTrue(run.verified)  # loop recovered and finished

    # ---------------------------------------------------------------- memory
    def test_recall_lesson_hits_ledger(self) -> None:
        loop = AgentLoop(self.root, runner=scripted_runner([]))
        loop.memory.record_lesson(
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
            root_cause="missing cast",
            verified_patch="int(x)",
            verified=True,
        )
        loop2 = self.make_loop(
            [
                {
                    "tool": "recall_lesson",
                    "args": {"signature": "TypeError: unsupported operand type(s) for +: 'int' and 'str'"},
                },
                {"tool": "finish", "args": {"summary": "found it"}},
            ]
        )
        loop2.memory = loop.memory
        run = loop2.run("this again?")
        self.assertTrue(run.steps[0].verified)
        self.assertIn("missing cast", run.steps[0].output)

    # -------------------------------------------------------------- resilience
    def test_crash_mid_run_is_resumable(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "read_file", "args": {"path": "note.txt"}},
                {"tool": "read_file", "args": {"path": "note.txt"}},
            ]
        )
        (self.root / "note.txt").write_text("data", encoding="utf-8")

        calls = {"count": 0}

        def exploding_reader(args):
            calls["count"] += 1
            if calls["count"] == 1:
                return "data", True
            raise RuntimeError("simulated crash after checkpoint")

        loop._tool_read_file = exploding_reader  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            loop.run("fragile task")
        sessions = loop.sessions.list_sessions()
        self.assertEqual(len(sessions), 1)
        state = loop.sessions.load(sessions[0]["session_id"])
        self.assertEqual(state.status, "failed")
        # one checkpoint from the successful step, one from the crash recorder
        self.assertEqual(len(state.checkpoints), 2)

        resumed_loop = self.make_loop(
            [{"tool": "finish", "args": {"summary": "resumed fine"}}]
        )
        resumed_loop.sessions = loop.sessions
        run = resumed_loop.run("continue", session=state)
        self.assertTrue(run.finished)
        self.assertEqual(run.summary, "resumed fine")

    def test_planner_unavailable_pauses_with_resting_status(self) -> None:
        def failing_runner(prompt: str):
            raise RuntimeError("RESOURCE_EXHAUSTED on every model")

        loop = AgentLoop(self.root, runner=failing_runner)
        run = loop.run("do something")
        self.assertFalse(run.finished)
        self.assertIn("RESOURCE_EXHAUSTED", run.summary)
        state = loop.sessions.load(run.session_id)
        self.assertEqual(state.status, "resting")
        self.assertGreaterEqual(len(state.checkpoints), 1)

    def test_step_retry_counter_reported(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "read_file", "args": {"path": "missing.py"}},
                {"tool": "read_file", "args": {"path": "missing.py"}},
                {"tool": "read_file", "args": {"path": "missing.py"}},
                {"tool": "finish", "args": {"summary": "gave up reading"}},
            ]
        )
        run = loop.run("read a ghost")
        self.assertEqual(run.steps[0].meta["retries"], 1)
        self.assertEqual(run.steps[1].meta["retries"], 2)
        self.assertEqual(run.steps[2].meta["retries"], 3)

    def test_unknown_tool_recorded_not_fatal(self) -> None:
        loop = self.make_loop(
            [
                {"tool": "delete_everything", "args": {}},
                {"tool": "finish", "args": {"summary": "done"}},
            ]
        )
        run = loop.run("try a bogus tool")
        self.assertFalse(run.steps[0].ok)
        self.assertIn("unknown tool", run.steps[0].output)
        self.assertTrue(run.finished)

    def test_max_steps_caps_run(self) -> None:
        script = [{"tool": "read_file", "args": {"path": "note.txt"}}] * 10
        (self.root / "note.txt").write_text("data", encoding="utf-8")
        loop = self.make_loop(script, max_steps=3)
        run = loop.run("never ending reads")
        self.assertFalse(run.finished)
        self.assertEqual(len(run.steps), 3)

    def test_no_runner_and_no_router_raises(self) -> None:
        loop = AgentLoop(self.root)
        with self.assertRaises(AgentLoopError):
            loop.run("anything")


if __name__ == "__main__":
    unittest.main()
