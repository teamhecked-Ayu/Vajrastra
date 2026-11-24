from __future__ import annotations

import unittest

from engines.language_capabilities import ToolCapability, capability_report, detect_language_capabilities


class LanguageCapabilityTests(unittest.TestCase):
    def test_python_capability_uses_standard_library(self):
        report = detect_language_capabilities()
        self.assertIn("python", report)
        python = report["python"]
        self.assertEqual(python.parser, "stdlib ast")
        self.assertIn("unittest", python.test_runner)

    def test_missing_compilers_are_reported_as_missing(self):
        report = detect_language_capabilities()
        self.assertIn("go", report)
        self.assertEqual(report["go"].language, "go")
        self.assertIsInstance(report["go"].notes, tuple)

    def test_report_is_json_serializable(self):
        import json
        encoded = json.dumps(capability_report())
        self.assertIn('"python"', encoded)
        self.assertIn('"languages"', encoded)

    def test_capability_dict_contains_evidence_fields(self):
        item = ToolCapability("example", parser="parser-x")
        self.assertEqual(item.as_dict()["parser"], "parser-x")
        self.assertIsNone(item.as_dict()["compiler"])


if __name__ == "__main__":
    unittest.main()
