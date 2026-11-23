"""Code-architect tests: red/blue loop, firewall, sandbox, honest verification.

The model is stubbed with a scripted sequence; subprocess checks run for real in
throw-away directories.
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
from engines.code_architect import ArchitectError, CodeArchitect  # noqa: E402

CALC = "def add(a, b):\n    return a + b\n"
CALC_BROKEN = "def add(a, b):\n    return a - b\n"
TEST_CALC = (
    "import unittest\n"
    "from calc import add\n\n\n"
    "class CalcTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(1, 2), 3)\n\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
ATTACK = {
    "summary": "one boundary attack",
    "attacks": [
        {"name": "negative", "code": "from calc import add\nassert add(-1, 1) == 0\n"}
    ],
}
NO_ATTACKS = {"summary": "nothing to add", "attacks": []}


def bundle(files, tests=None, summary="implemented") -> dict:
    return {
        "summary": summary,
        "files": [{"path": path, "content": content} for path, content in files.items()],
        "tests": [
            {"path": path, "content": content} for path, content in (tests or {}).items()
        ],
    }


class SequenceRouter:
    """Router stand-in returning pre-scripted JSON documents in order."""

    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def generate_json(self, prompt, *, system=None, **kwargs):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("architect asked for more replies than scripted")
        payload = self.responses.pop(0)
        return payload, ModelResponse(
            text=json.dumps(payload), model="stub-model", tier="deep"
        )


class ArchitectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine = MemoryEngine(self.root)
        self.guard = SecurityGuard(self.root)
        self._architect = None

    def tearDown(self) -> None:
        if self._architect is not None:
            self._architect.close()
        self.engine.close()
        self._tmp.cleanup()

    def architect(self, router, rounds: int = 2) -> CodeArchitect:
        self._architect = CodeArchitect(
            self.root,
            router=router,
            guard=self.guard,
            memory=self.engine,
            rounds=rounds,
        )
        return self._architect

    @property
    def out(self) -> Path:
        return self.root / "out"


class BuildTests(ArchitectTestCase):
    def test_verified_build_writes_files_and_lesson(self) -> None:
        router = SequenceRouter([bundle({"calc.py": CALC}, {"test_calc.py": TEST_CALC}), ATTACK])
        result = self.architect(router).build("add two numbers", out=str(self.out))
        self.assertTrue(result.verified, result.failures)
        self.assertEqual(result.rounds, 1)
        self.assertIn("calc.py", result.written)
        self.assertIn("test_calc.py", result.written)
        self.assertEqual((self.out / "calc.py").read_text(encoding="utf-8"), CALC)
        self.assertIn("one boundary attack", result.red_summary)
        lesson = self.engine.get(result.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertTrue(lesson.solved)

    def test_failing_tests_trigger_a_blue_patch(self) -> None:
        router = SequenceRouter(
            [
                bundle({"calc.py": CALC_BROKEN}, {"test_calc.py": TEST_CALC}),
                ATTACK,
                bundle({"calc.py": CALC}, {"test_calc.py": TEST_CALC}, summary="fixed"),
                ATTACK,
            ]
        )
        result = self.architect(router).build("add two numbers", out=str(self.out))
        self.assertTrue(result.verified, result.failures)
        self.assertEqual(result.rounds, 2)
        self.assertIn("unit tests failed", router.prompts[2])
        self.assertIn("repair round 2", router.prompts[2])
        self.assertEqual((self.out / "calc.py").read_text(encoding="utf-8"), CALC)

    def test_firewall_rejection_forces_a_repair(self) -> None:
        unsafe = 'import os\nos.system("id")\n'
        router = SequenceRouter(
            [
                bundle({"bad.py": unsafe}, {"test_bad.py": "import bad\n"}),
                bundle({"bad.py": CALC}, {"test_bad.py": TEST_CALC.replace("calc", "bad")}),
                NO_ATTACKS,
            ]
        )
        result = self.architect(router).build("unsafe module", out=str(self.out))
        self.assertTrue(result.verified, result.failures)
        self.assertIn("firewall rejected", router.prompts[1])
        self.assertIn("process_execution", router.prompts[1])
        self.assertNotIn("os.system", (self.out / "bad.py").read_text(encoding="utf-8"))

    def test_unsafe_red_attack_is_never_executed(self) -> None:
        unsafe_attack = {
            "summary": "hostile",
            "attacks": [{"name": "hostile", "code": 'import os\nos.system("id")\n'}],
        }
        router = SequenceRouter(
            [bundle({"calc.py": CALC}, {"test_calc.py": TEST_CALC}), unsafe_attack]
        )
        result = self.architect(router).build("add two numbers", out=str(self.out))
        self.assertTrue(result.verified, result.failures)
        self.assertEqual(result.failures, ())
        self.assertTrue((self.out / "calc.py").is_file())

    def test_rounds_exhausted_reports_failures_without_writing(self) -> None:
        router = SequenceRouter(
            [
                bundle({"calc.py": CALC_BROKEN}, {"test_calc.py": TEST_CALC}),
                NO_ATTACKS,
                bundle({"calc.py": CALC_BROKEN}, {"test_calc.py": TEST_CALC}),
                NO_ATTACKS,
            ]
        )
        result = self.architect(router, rounds=2).build("add two numbers", out=str(self.out))
        self.assertFalse(result.verified)
        self.assertTrue(result.failures)
        self.assertEqual(result.written, ())
        self.assertFalse((self.out / "calc.py").exists())

    def test_non_python_build_is_not_claimed_as_verified(self) -> None:
        router = SequenceRouter(
            [
                {
                    "summary": "js module",
                    "files": [{"path": "app.js", "content": "function add(a, b) { return a + b; }\n"}],
                    "tests": [],
                },
                {"summary": "advisory", "attacks": [{"name": "edges", "code": ""}]},
            ]
        )
        result = self.architect(router).build(
            "add two numbers", language="javascript", out=str(self.out)
        )
        self.assertFalse(result.verified)
        self.assertTrue(any("no execution harness" in note for note in result.notes))
        self.assertEqual(result.written, ())

    def test_path_traversal_is_refused(self) -> None:
        router = SequenceRouter(
            [bundle({"../escape.py": "VALUE = 1\n"}, {"test_escape.py": "import escape\n"})]
        )
        with self.assertRaises(ArchitectError):
            self.architect(router).build("escape", out=str(self.out))

    def test_bundle_without_files_is_refused(self) -> None:
        router = SequenceRouter([{"summary": "nothing", "files": [], "tests": []}])
        with self.assertRaises(ArchitectError):
            self.architect(router).build("nothing", out=str(self.out))

    def test_no_model_is_a_clear_error(self) -> None:
        self._architect = CodeArchitect(
            self.root, router=None, guard=self.guard, memory=self.engine
        )
        with self.assertRaises(ArchitectError):
            self._architect.build("anything", out=str(self.out))


if __name__ == "__main__":
    unittest.main()
