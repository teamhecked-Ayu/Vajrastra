"""Bounded project index: files, symbols, imports, manifests.

Indexes Python first (AST-based), records the commit SHA the index was
built from so stale indexes can be detected, and enforces file-count and
byte limits so a huge tree fails honestly (truncated=True) instead of
silently dropping work.
"""

from __future__ import annotations

import ast
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

INDEX_VERSION = 1
IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}
MAX_FILE_BYTES = 512 * 1024
MANIFEST_NAMES = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")


@dataclass
class Symbol:
    name: str
    kind: str            # function | class | method
    file: str
    line: int


@dataclass
class ProjectIndex:
    index_version: int
    commit_sha: str
    generated_at: float
    files: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)
    manifests: list[str] = field(default_factory=list)
    truncated: bool = False

    def find_symbol(self, name: str) -> list[Symbol]:
        return [s for s in self.symbols if s.name == name]

    def files_importing(self, module: str) -> list[str]:
        return list(self.imports.get(module, []))

    def relevant_files(self, keywords: list[str], limit: int = 8) -> list[str]:
        scored: dict[str, int] = {}
        kws = [k.lower() for k in keywords if k]
        for s in self.symbols:
            score = sum(1 for k in kws if k in s.name.lower())
            if score:
                scored[s.file] = scored.get(s.file, 0) + score
        for f in self.files:
            score = sum(2 for k in kws if k in Path(f).name.lower())
            if score:
                scored[f] = scored.get(f, 0) + score
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        return [f for f, _ in ranked[:limit]]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectIndex":
        d = dict(d)
        d["symbols"] = [Symbol(**s) for s in d.get("symbols", [])]
        return cls(**d)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "ProjectIndex":
        return cls.from_dict(json.loads(Path(path).read_text()))


class _Collector(ast.NodeVisitor):
    def __init__(self, rel: str, idx: ProjectIndex):
        self.rel = rel
        self.idx = idx
        self._class_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.idx.symbols.append(Symbol(node.name, "class", self.rel, node.lineno))
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func(node)

    def _func(self, node) -> None:
        kind = "method" if self._class_depth else "function"
        self.idx.symbols.append(Symbol(node.name, kind, self.rel, node.lineno))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.idx.imports.setdefault(a.name.split(".")[0], []).append(self.rel)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.idx.imports.setdefault(node.module.split(".")[0], []).append(self.rel)


def build_index(root: str | Path, commit_sha: str = "unknown",
                max_files: int = 5000) -> ProjectIndex:
    root = Path(root)
    idx = ProjectIndex(index_version=INDEX_VERSION, commit_sha=commit_sha,
                       generated_at=time.time())
    count = 0
    for p in sorted(root.rglob("*")):
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if p.name in MANIFEST_NAMES:
            idx.manifests.append(rel)
        if p.suffix != ".py":
            continue
        count += 1
        if count > max_files or p.stat().st_size > MAX_FILE_BYTES:
            idx.truncated = True
            continue
        idx.files.append(rel)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        _Collector(rel, idx).visit(tree)
    for mod in idx.imports:
        idx.imports[mod] = sorted(set(idx.imports[mod]))
    return idx
