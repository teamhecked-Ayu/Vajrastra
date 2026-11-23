from __future__ import annotations

import unittest

from engines.debug_context import build_dependency_context


class DependencyContextTests(unittest.TestCase):
    def test_local_imports_are_resolved_without_importing_modules(self):
        sources = {
            "app.py": "from services.auth import verify\nimport helpers\n",
            "services/__init__.py": "",
            "services/auth.py": "def verify(): return True\n",
            "helpers.py": "VALUE = 1\n",
            "unrelated.py": "VALUE = 2\n",
        }
        context = build_dependency_context(sources)
        self.assertEqual(
            context["app.py"],
            ("helpers.py", "services/auth.py"),
        )
        self.assertNotIn("unrelated.py", context.get("app.py", ()))

    def test_syntax_error_is_skipped(self):
        context = build_dependency_context({
            "broken.py": "from helpers import value\ndef bad(:\n",
            "helpers.py": "value = 1\n",
        })
        self.assertNotIn("broken.py", context)

    def test_non_python_sources_are_ignored(self):
        context = build_dependency_context({
            "main.js": "import './helper.js';",
            "helper.js": "export const value = 1;",
        })
        self.assertEqual(context, {})


if __name__ == "__main__":
    unittest.main()
