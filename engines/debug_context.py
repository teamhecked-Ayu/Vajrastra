"""Static project dependency evidence for debugging prompts.

Only Python import syntax is inspected. No project modules are imported or run.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Mapping, Set, Tuple


def _module_name(path: str) -> str:
    value = Path(path).with_suffix("").as_posix().replace("/", ".")
    return value[:-9] if value.endswith(".__init__") else value


def build_dependency_context(sources: Mapping[str, str]) -> Dict[str, Tuple[str, ...]]:
    """Map each Python source path to its directly imported local source paths."""
    modules = {_module_name(path): path for path in sources if path.endswith(".py")}
    result: Dict[str, Set[str]] = {path: set() for path in modules.values()}
    for path, source in sources.items():
        if path not in result:
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        package = _module_name(path).rsplit(".", 1)[0] if "." in _module_name(path) else ""
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    prefix = package.split(".")
                    keep = max(0, len(prefix) - node.level + 1)
                    base = ".".join(prefix[:keep] + ([base] if base else []))
                if base:
                    imported.append(base)
                imported.extend(
                    (base + "." + alias.name).strip(".")
                    for alias in node.names if alias.name != "*"
                )
            for name in imported:
                candidate = modules.get(name)
                if candidate and candidate != path:
                    result[path].add(candidate)
                else:
                    # Resolve a package import to its source module when exact
                    # symbol imports do not map to a file path.
                    parts = name.split(".")
                    for end in range(len(parts), 0, -1):
                        candidate = modules.get(".".join(parts[:end]))
                        if candidate and candidate != path:
                            result[path].add(candidate)
                            break
    return {path: tuple(sorted(paths)) for path, paths in result.items() if paths}
