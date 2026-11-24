from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine
from core.model_router import ModelResponse
from core.security_guard import SecurityGuard
from engines.hyper_debugger import EngineStatus, HyperDebugger


class RegressionRouter:
    available = True

    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, prompt, *, system=None, **kwargs):
        return self.payload, ModelResponse(
            text=json.dumps(self.payload), model="test-model", tier="deep"
        )


class GeneratedRegressionTest(unittest.TestCase):
    def test_generated_test_is_run_with_the_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "maths.py"
            target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            memory = MemoryEngine(root)
            debugger = HyperDebugger(root, guard=SecurityGuard(root), memory=memory)
            try:
                analysis = debugger.analyze(target, use_llm=False, engines=EngineStatus())
                debugger.router = RegressionRouter({
                    "root_cause": "subtraction used instead of addition",
                    "patched_source": "def add(a, b):\n    return a + b\n",
                    "regression_test": {
                        "path": "tests/test_regression_add.py",
                        "content": (
                            "import unittest\nfrom maths import add\n"
                            "class AddRegression(unittest.TestCase):\n"
                            "    def test_add(self):\n"
                            "        self.assertEqual(add(2, 2), 4)\n"
                        ),
                    },
                })
                result = debugger.repair(target, analysis)
                self.assertTrue(result.verified, result.message)
                self.assertTrue(result.regression_test_added)
                self.assertTrue(result.executed)
            finally:
                debugger.close()
                memory.close()

    def test_generated_test_cannot_escape_tests_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "maths.py"
            target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            memory = MemoryEngine(root)
            debugger = HyperDebugger(root, guard=SecurityGuard(root), memory=memory)
            try:
                analysis = debugger.analyze(target, use_llm=False, engines=EngineStatus())
                debugger.router = RegressionRouter({
                    "root_cause": "wrong operator",
                    "patched_source": "def add(a, b):\n    return a + b\n",
                    "regression_test": {
                        "path": "../escape.py",
                        "content": "VALUE = 1\n",
                    },
                })
                result = debugger.repair(target, analysis)
                self.assertFalse(result.verified)
                self.assertIn("path/content rejected", result.message)
                self.assertFalse((root.parent / "escape.py").exists())
            finally:
                debugger.close()
                memory.close()


if __name__ == "__main__":
    unittest.main()
