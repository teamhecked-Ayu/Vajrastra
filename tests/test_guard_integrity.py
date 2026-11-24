import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile
import unittest
from pathlib import Path

from core._guard_integrity import IntegrityGuard


class TestIntegrityGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "core").mkdir()
        (root / "engines").mkdir()
        (root / "vajrastra.py").write_text("print('cli')\n")
        (root / "core" / "agent_loop.py").write_text("X = 1\n")
        (root / "core" / "session_manager.py").write_text("Y = 1\n")
        (root / "engines" / "e.py").write_text("Z = 1\n")
        self.guard = IntegrityGuard(root)

    def tearDown(self):
        os.environ.pop("VAJASTRA_MAINTAINER", None)
        self.tmp.cleanup()

    def _seal(self):
        os.environ["VAJASTRA_MAINTAINER"] = "tester"
        return self.guard.reseal(reason="initial seal")

    def test_verify_ok_after_seal_with_full_coverage(self):
        self._seal()
        report = self.guard.verify()
        self.assertTrue(report["ok"], report)
        targets = self.guard.manifest_targets()
        self.assertIn("core/agent_loop.py", targets)
        self.assertIn("core/session_manager.py", targets)
        self.assertIn("engines/e.py", targets)

    def test_unexpected_protected_file_fails(self):
        self._seal()
        (Path(self.tmp.name) / "core" / "new_module.py").write_text("N = 1\n")
        report = self.guard.verify()
        self.assertFalse(report["ok"])
        self.assertIn("core/new_module.py", report["unexpected"])

    def test_changed_file_fails(self):
        self._seal()
        (Path(self.tmp.name) / "core" / "agent_loop.py").write_text("X = 2\n")
        self.assertFalse(self.guard.verify()["ok"])

    def test_missing_file_fails(self):
        self._seal()
        (Path(self.tmp.name) / "engines" / "e.py").unlink()
        self.assertFalse(self.guard.verify()["ok"])

    def test_reseal_requires_maintainer_identity(self):
        os.environ.pop("VAJASTRA_MAINTAINER", None)
        with self.assertRaises(PermissionError):
            self.guard.reseal(reason="sneaky self-reseal")

    def test_reseal_requires_reason(self):
        os.environ["VAJASTRA_MAINTAINER"] = "tester"
        with self.assertRaises(PermissionError):
            self.guard.reseal(reason="   ")

    def test_reseal_writes_audit_entry(self):
        self._seal()
        log = Path(self.tmp.name) / "security_reseal_audit.jsonl"
        self.assertTrue(log.is_file())
        self.assertIn("tester", log.read_text())

    def test_patch_gate_blocks_trust_root(self):
        self._seal()
        with self.assertRaises(PermissionError):
            self.guard.assert_writable("core/agent_loop.py")
        self.guard.assert_writable("tests/test_x.py")  # outside trust root


if __name__ == "__main__":
    unittest.main()
