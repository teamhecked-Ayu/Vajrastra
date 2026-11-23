"""Hyper-debugger tests: AST, firewall, symbolic, fuzzing and repair.

LLM calls are stubbed; no test contacts the network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine  # noqa: E402
from core.model_router import ModelResponse  # noqa: E402
from core.security_guard import SecurityGuard  # noqa: E402
from engines.hyper_debugger import (  # noqa: E402
    EngineStatus,
    HyperDebugger,
    _linear_constraints,
    detect_engines,
)

import ast  # noqa: E402


class StubRouter:
    """Minimal router stand-in with a canned JSON reply."""

    available = True

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def generate_json(self, prompt, *, system=None, **kwargs):
        self.prompts.append(prompt)
        return self.payload, ModelResponse(
            text=json.dumps(self.payload), model="stub-model", tier="deep"
        )


class DebuggerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine = MemoryEngine(self.root)
        self.debugger = HyperDebugger(
            self.root,
            guard=SecurityGuard(self.root),
            memory=self.engine,
            router=None,
        )

    def tearDown(self) -> None:
        self.debugger.close()
        self.engine.close()
        self._tmp.cleanup()

    def write(self, name: str, source: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path


class StageOneAndTwoTests(DebuggerTestCase):
    def test_mutable_default_is_flagged(self) -> None:
        path = self.write("mod.py", "def collect(items=[]):\n    items.append(1)\n    return items\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.assertTrue(any(f.kind == "ast" for f in analysis.findings))
        self.assertGreaterEqual(analysis.blocking_count, 1)

    def test_silently_swallowed_exception_is_high(self) -> None:
        source = "def load():\n    try:\n        return 1\n    except Exception:\n        pass\n"
        analysis = self.debugger.analyze(self.write("swallow.py", source), engines=EngineStatus())
        self.assertTrue(any("silently ignored" in f.message for f in analysis.blocking))

    def test_firewall_finding_mentions_category(self) -> None:
        path = self.write("bad.py", 'import os\nos.system("id")\n')
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        rendered = "\n".join(f.render() for f in analysis.findings)
        self.assertIn("process_execution", rendered)
        self.assertGreaterEqual(analysis.blocking_count, 1)

    def test_syntax_error_becomes_signature(self) -> None:
        path = self.write("broken.py", "def broken(:\n    pass\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.assertTrue(any(f.kind == "syntax" for f in analysis.findings))
        self.assertIn("SyntaxError", analysis.signature)

    def test_non_python_structural_checks(self) -> None:
        path = self.write("app.js", "function broken() {\n  return 1;\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.assertTrue(any("delimiter" in f.message for f in analysis.findings))
        self.assertEqual(analysis.languages.get("javascript"), 1)

    def test_conflict_marker_is_blocking_in_any_language(self) -> None:
        path = self.write("notes.md", "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.assertTrue(any("conflict marker" in f.message for f in analysis.blocking))

    def test_recall_finds_a_recorded_lesson(self) -> None:
        self.engine.record_lesson(
            "KeyError: 'weights'",
            root_cause="weights column dropped",
            verified_patch="weights = frame['weights']",
            verified=True,
        )
        lessons = self.debugger.recall("KeyError: 'weights'")
        self.assertTrue(lessons)
        self.assertIn("weights", lessons[0].root_cause)

    def test_analyze_surfaces_memory_matches(self) -> None:
        self.engine.record_lesson(
            "ast: mutable default argument in f()",
            root_cause="shared mutable default",
            tags=("ast",),
        )
        path = self.write("f.py", "def f(items=[]):\n    return items\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.assertTrue(analysis.lesson_ids)
        self.assertTrue(any(f.kind == "memory" for f in analysis.findings))

    def test_directory_collection_skips_binary(self) -> None:
        self.write("a.py", "x = 1\n")
        (self.root / "blob.bin").write_bytes(b"\x00\x01binary")
        analysis = self.debugger.analyze(self.root, engines=EngineStatus())
        self.assertIn("a.py", analysis.files)
        self.assertNotIn("blob.bin", analysis.files)

    def test_engines_probe_returns_status(self) -> None:
        status = detect_engines()
        self.assertIsInstance(status, EngineStatus)


class StageThreeTests(DebuggerTestCase):
    def test_linear_constraints_extraction(self) -> None:
        tree = ast.parse("if x > 10 and x < 5:\n    pass\n")
        test = tree.body[0].test
        constraints = _linear_constraints(test)
        self.assertEqual(
            sorted(constraints), sorted([("x", ">", 10.0), ("x", "<", 5.0)])
        )

    def test_z3_reports_unsatisfiable_branch(self) -> None:
        if not detect_engines().z3:
            self.skipTest("z3 is not installed on this host")
        source = "def choose(x: int) -> int:\n    if x > 10 and x < 5:\n        return 1\n    return 0\n"
        path = self.write("branch.py", source)
        tree = ast.parse(source)
        findings = self.debugger._symbolic_scan("branch.py", tree, EngineStatus(z3=True))
        self.assertTrue(any("unsatisfiable" in f.message for f in findings))

    def test_sympy_reports_division_by_zero(self) -> None:
        if not detect_engines().sympy:
            self.skipTest("sympy is not installed on this host")
        source = "RATE = 1 / 0\n"
        path = self.write("rate.py", source)
        tree = ast.parse(source)
        findings = self.debugger._symbolic_scan("rate.py", tree, EngineStatus(sympy=True))
        self.assertTrue(any("not finite" in f.message for f in findings))

    def test_deterministic_fuzz_catches_zero_division(self) -> None:
        source = "def invert(x: int) -> int:\n    return 1 // x\n"
        tree = ast.parse(source)
        findings = self.debugger._fuzz_scan(
            "invert.py", source, tree, EngineStatus()
        )
        self.assertTrue(any("ZeroDivisionError" in f.message for f in findings))

    def test_fuzz_is_quiet_for_a_clean_pure_function(self) -> None:
        source = "def increment(x: int) -> int:\n    return x + 1\n"
        tree = ast.parse(source)
        findings = self.debugger._fuzz_scan(
            "increment.py", source, tree, EngineStatus()
        )
        self.assertEqual(findings, [])

    def test_llm_review_is_used_when_engines_are_missing(self) -> None:
        self.debugger.router = StubRouter(
            {
                "findings": [
                    {"path": "mod.py", "line": 2, "severity": "high", "message": "off-by-one"}
                ],
                "summary": "one defect",
            }
        )
        path = self.write("mod.py", "def f(x):\n    return x + 1\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus(), use_llm=True)
        self.assertTrue(any(f.kind == "red-team" for f in analysis.findings))

    def test_llm_review_absent_without_model(self) -> None:
        path = self.write("mod.py", "def f(x):\n    return x + 1\n")
        analysis = self.debugger.analyze(path, engines=EngineStatus(), use_llm=True)
        self.assertTrue(any(f.kind == "llm" for f in analysis.findings))


class RepairTests(DebuggerTestCase):
    def _buggy(self) -> Path:
        return self.write("maths.py", "def add(a, b):\n    return a - b\n")

    def test_repair_without_model_is_honest(self) -> None:
        path = self._buggy()
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        result = self.debugger.repair(path, analysis)
        self.assertFalse(result.verified)
        self.assertIn("GEMINI_API_KEY", result.message)

    def test_verified_repair_produces_diff_and_lesson(self) -> None:
        path = self._buggy()
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        patched = "def add(a, b):\n    return a + b\n"
        self.debugger.router = StubRouter(
            {"root_cause": "the operator was subtraction", "patched_source": patched}
        )
        result = self.debugger.repair(path, analysis)
        self.assertTrue(result.verified, result.message)
        self.assertTrue(result.executed)
        self.assertIn("+    return a + b", result.diff)
        self.assertEqual(result.root_cause, "the operator was subtraction")
        lesson = self.engine.get(result.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertTrue(lesson.solved)

    def test_apply_writes_the_verified_patch(self) -> None:
        path = self._buggy()
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        patched = "def add(a, b):\n    return a + b\n"
        self.debugger.router = StubRouter(
            {"root_cause": "wrong operator", "patched_source": patched}
        )
        result = self.debugger.repair(path, analysis, apply=True)
        self.assertTrue(result.applied, result.message)
        self.assertIn("a + b", path.read_text(encoding="utf-8"))

    def test_unsafe_patch_is_rejected_by_the_firewall(self) -> None:
        path = self._buggy()
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.debugger.router = StubRouter(
            {
                "root_cause": "bad idea",
                "patched_source": 'import os\nos.system("id")\n',
            }
        )
        result = self.debugger.repair(path, analysis)
        self.assertFalse(result.verified)
        self.assertIn("security firewall", result.message)

    def test_unchanged_patch_is_recorded_as_open(self) -> None:
        path = self._buggy()
        original = path.read_text(encoding="utf-8")
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        self.debugger.router = StubRouter(
            {"root_cause": "nothing to change", "patched_source": original}
        )
        result = self.debugger.repair(path, analysis)
        self.assertFalse(result.verified)
        self.assertIn("no change", result.message)
        lesson = self.engine.get(result.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertFalse(lesson.solved)

    def test_custom_test_command_is_executed(self) -> None:
        path = self._buggy()
        analysis = self.debugger.analyze(path, engines=EngineStatus())
        patched = "def add(a, b):\n    return a + b\n"
        self.debugger.router = StubRouter(
            {"root_cause": "wrong operator", "patched_source": patched}
        )
        command = f'"{sys.executable}" -c "import maths, sys; sys.exit(0 if maths.add(2, 2) == 4 else 1)"'
        result = self.debugger.repair(path, analysis, test_command=command)
        self.assertTrue(result.verified, result.message)
        self.assertTrue(result.executed)


if __name__ == "__main__":
    unittest.main()
