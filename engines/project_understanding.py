"""Bounded repository inventory and Python project understanding.

This module uses only the Python standard library. It never executes project
code. Indexes are tied to the caller-supplied Git commit SHA; consumers must
provide the current SHA again before using task context.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


DEFAULT_EXCLUDES = (
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "env", "node_modules", "dist", "build",
    ".tox", ".nox", ".idea", ".vscode",
)
MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "poetry.lock", "uv.lock",
    "environment.yml", "environment.yaml", "package.json", "Cargo.toml",
    "go.mod", "Gemfile", "pom.xml", "build.gradle", "Makefile",
    "CMakeLists.txt", "Dockerfile", "docker-compose.yml",
}
CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "mypy.ini",
    ".ruff.toml", "ruff.toml", "requirements.txt", "requirements-dev.txt",
    "Pipfile", "environment.yml", "environment.yaml", "Makefile",
    "Dockerfile", "docker-compose.yml", ".gitignore",
}
ENTRYPOINT_NAMES = {
    "__main__.py", "main.py", "app.py", "cli.py", "manage.py", "wsgi.py",
    "asgi.py", "server.py",
}
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 8_000_000
MAX_FILES = 20_000
MAX_PYTHON_FILES = 2_000
MAX_SYMBOLS_PER_FILE = 2_000
MAX_IMPORTS_PER_FILE = 500
MAX_CONTEXT_FILES = 24
MAX_CONTEXT_CHARS = 48_000


class ProjectIndexError(ValueError):
    """Raised when an index cannot safely be built or used."""


class StaleProjectIndexError(ProjectIndexError):
    """Raised when the index commit differs from the selected repository."""


@dataclass(frozen=True)
class IndexLimits:
    max_files: int = MAX_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_python_files: int = MAX_PYTHON_FILES
    max_symbols_per_file: int = MAX_SYMBOLS_PER_FILE
    max_imports_per_file: int = MAX_IMPORTS_PER_FILE

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or value <= 0:
                raise ProjectIndexError(f"{name} must be a positive integer")


@dataclass
class FileRecord:
    path: str
    suffix: str
    size_bytes: int
    category: str


@dataclass
class PythonFileRecord:
    path: str
    sha256: str
    symbols: List[Dict[str, object]] = field(default_factory=list)
    imports: List[Dict[str, object]] = field(default_factory=list)
    syntax_error: Optional[str] = None
    truncated: bool = False


@dataclass
class ProjectIndex:
    root: str
    commit_sha: str
    inventory: List[FileRecord]
    python_files: Dict[str, PythonFileRecord]
    manifests: List[str]
    configs: List[str]
    entry_points: List[str]
    tests: List[str]
    import_edges: Dict[str, List[str]]
    limits: Dict[str, int]
    warnings: List[str] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def build(
        cls,
        root: Path | str,
        commit_sha: str,
        *,
        limits: Optional[IndexLimits] = None,
        excludes: Sequence[str] = DEFAULT_EXCLUDES,
    ) -> "ProjectIndex":
        """Index a workspace without importing or executing its source files."""
        base = Path(root).expanduser().resolve()
        if not base.is_dir():
            raise ProjectIndexError(f"workspace is not a directory: {base}")
        if not isinstance(commit_sha, str) or not commit_sha.strip():
            raise ProjectIndexError("commit_sha must be a non-empty Git revision")
        active_limits = limits or IndexLimits()
        active_limits.validate()

        inventory: List[FileRecord] = []
        py_records: Dict[str, PythonFileRecord] = {}
        manifests: List[str] = []
        configs: List[str] = []
        entry_points: List[str] = []
        tests: List[str] = []
        warnings: List[str] = []
        total_bytes = 0
        python_count = 0

        for path in _walk_files(base, excludes):
            if len(inventory) >= active_limits.max_files:
                warnings.append(f"file limit reached ({active_limits.max_files})")
                break
            try:
                resolved = path.resolve()
                resolved.relative_to(base)
                size = path.stat().st_size
            except (OSError, ValueError):
                warnings.append(f"skipped unreadable or out-of-root path: {_relative(path, base)}")
                continue

            rel = _relative(path, base)
            suffix = path.suffix.lower()
            category = _file_category(path, rel)
            inventory.append(FileRecord(rel, suffix, size, category))
            if path.name in MANIFEST_NAMES:
                manifests.append(rel)
            if path.name in CONFIG_NAMES or rel.startswith(".github/"):
                configs.append(rel)
            if path.name in ENTRYPOINT_NAMES or rel in {"vajrastra.py", "manage.py"}:
                entry_points.append(rel)
            if _is_test_path(rel):
                tests.append(rel)

            if suffix != ".py":
                continue
            if python_count >= active_limits.max_python_files:
                warnings.append(f"Python file limit reached ({active_limits.max_python_files})")
                continue
            if size > active_limits.max_file_bytes:
                warnings.append(f"skipped oversized Python file: {rel} ({size} bytes)")
                continue
            if total_bytes + size > active_limits.max_total_bytes:
                warnings.append(f"Python byte budget reached before: {rel}")
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.append(f"could not read {rel}: {exc}")
                continue
            total_bytes += size
            python_count += 1
            py_records[rel] = _index_python(
                rel, source, active_limits.max_symbols_per_file,
                active_limits.max_imports_per_file,
            )

        edges = _resolve_import_edges(py_records)
        return cls(
            root=str(base),
            commit_sha=commit_sha.strip(),
            inventory=inventory,
            python_files=py_records,
            manifests=sorted(set(manifests)),
            configs=sorted(set(configs)),
            entry_points=sorted(set(entry_points)),
            tests=sorted(set(tests)),
            import_edges=edges,
            limits=asdict(active_limits),
            warnings=warnings,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "root": self.root,
            "commit_sha": self.commit_sha,
            "inventory": [asdict(item) for item in self.inventory],
            "python_files": {key: asdict(value) for key, value in self.python_files.items()},
            "manifests": list(self.manifests),
            "configs": list(self.configs),
            "entry_points": list(self.entry_points),
            "tests": list(self.tests),
            "import_edges": {key: list(value) for key, value in self.import_edges.items()},
            "limits": dict(self.limits),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ProjectIndex":
        """Load a JSON-compatible index and validate its basic shape."""
        if payload.get("schema_version") != 1:
            raise ProjectIndexError("unsupported project index schema")
        try:
            inventory = [FileRecord(**item) for item in payload["inventory"]]  # type: ignore[arg-type]
            py_files = {
                str(key): PythonFileRecord(**value)
                for key, value in payload["python_files"].items()  # type: ignore[union-attr]
            }
            return cls(
                root=str(payload["root"]),
                commit_sha=str(payload["commit_sha"]),
                inventory=inventory,
                python_files=py_files,
                manifests=list(payload["manifests"]),
                configs=list(payload["configs"]),
                entry_points=list(payload["entry_points"]),
                tests=list(payload["tests"]),
                import_edges={str(k): list(v) for k, v in payload["import_edges"].items()},
                limits=dict(payload["limits"]),
                warnings=list(payload.get("warnings", [])),
                schema_version=1,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProjectIndexError(f"invalid project index payload: {exc}") from exc

    def task_context(
        self,
        instruction: str,
        *,
        current_commit_sha: str,
        max_files: int = MAX_CONTEXT_FILES,
        max_chars: int = MAX_CONTEXT_CHARS,
    ) -> Dict[str, object]:
        """Select source, tests and config relevant to a task.

        The returned package contains paths and index facts, not source contents.
        The caller reads only the selected files from its trusted workspace.
        """
        if current_commit_sha != self.commit_sha:
            raise StaleProjectIndexError(
                f"stale project index: indexed {self.commit_sha}, current {current_commit_sha}"
            )
        if not instruction or not instruction.strip():
            raise ProjectIndexError("instruction must be non-empty")
        if max_files <= 0 or max_chars <= 0:
            raise ProjectIndexError("context limits must be positive")

        tokens = _terms(instruction)
        scores: Dict[str, int] = {}
        for path, record in self.python_files.items():
            text = path.lower().replace("/", " ").replace("_", " ").replace("-", " ")
            symbols = " ".join(
                str(item.get("name", "")) + " " + str(item.get("kind", ""))
                for item in record.symbols
            ).lower()
            imported = " ".join(
                str(item.get("module", "")) + " " + " ".join(item.get("names", []))
                for item in record.imports
            ).lower()
            haystack = f"{text} {symbols} {imported}"
            score = sum(3 for token in tokens if token in text)
            score += sum(2 for token in tokens if token in symbols)
            score += sum(1 for token in tokens if token in imported)
            if score:
                scores[path] = score

        selected: List[str] = []
        for path in sorted(scores, key=lambda item: (-scores[item], item)):
            _add_unique(selected, path)
        # Include one-hop local dependencies of the most relevant modules.
        for path in list(selected[:max_files]):
            for dependency in self.import_edges.get(path, []):
                if dependency in self.python_files:
                    _add_unique(selected, dependency)
        # Tests are selected by filename/path relation to selected modules.
        selected_stems = {
            Path(path).stem.removeprefix("test_").lower() for path in selected
        }
        for test_path in self.tests:
            stem = Path(test_path).stem.removeprefix("test_").lower()
            if stem in selected_stems or any(stem in item or item in stem for item in selected_stems):
                _add_unique(selected, test_path)

        # Core project files are always candidates for multi-file planning.
        for path in self.manifests + self.configs:
            _add_unique(selected, path)
        if not selected:
            selected.extend(self.entry_points[:2])
        selected = selected[:max_files]

        descriptions: List[Dict[str, object]] = []
        used_chars = 0
        for path in selected:
            record = self.python_files.get(path)
            detail = {
                "path": path,
                "score": scores.get(path, 0),
                "symbols": record.symbols[:80] if record else [],
                "imports": record.imports[:80] if record else [],
                "is_test": path in self.tests,
                "is_config": path in self.configs or path in self.manifests,
            }
            rendered_size = len(json.dumps(detail, ensure_ascii=False))
            if used_chars + rendered_size > max_chars:
                break
            descriptions.append(detail)
            used_chars += rendered_size

        return {
            "commit_sha": self.commit_sha,
            "instruction": instruction,
            "selected_files": [item["path"] for item in descriptions],
            "files": descriptions,
            "manifests": self.manifests,
            "configs": self.configs,
            "entry_points": self.entry_points,
            "tests": self.tests,
            "warnings": self.warnings,
            "limits": {"max_files": max_files, "max_chars": max_chars},
        }

    def fingerprint(self) -> str:
        """Stable digest of the serialized index, excluding filesystem timestamps."""
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _walk_files(root: Path, excludes: Sequence[str]) -> Iterable[Path]:
    excluded = tuple(excludes)
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in excluded and not any(fnmatch.fnmatch(name, pattern) for pattern in excluded)
        )
        for name in sorted(files):
            if name in excluded or any(fnmatch.fnmatch(name, pattern) for pattern in excluded):
                continue
            path = Path(current) / name
            if path.is_symlink():
                continue
            yield path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_category(path: Path, rel: str) -> str:
    if _is_test_path(rel):
        return "test"
    if path.name in MANIFEST_NAMES:
        return "manifest"
    if path.name in CONFIG_NAMES or rel.startswith(".github/"):
        return "config"
    if path.suffix.lower() == ".py":
        return "python"
    if path.suffix.lower() in {".md", ".rst", ".txt"}:
        return "documentation"
    if path.suffix.lower() in {".yml", ".yaml", ".toml", ".ini", ".cfg", ".json"}:
        return "config"
    return "other"


def _is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    name = parts[-1]
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _index_python(
    path: str, source: str, max_symbols: int, max_imports: int
) -> PythonFileRecord:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    record = PythonFileRecord(path=path, sha256=digest)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        record.syntax_error = f"{exc.msg} (line {exc.lineno})"
        return record

    symbols: List[Dict[str, object]] = []
    imports: List[Dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append({
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "parent": _parent_name(tree, node),
            })
        elif isinstance(node, ast.Import):
            imports.append({
                "module": "",
                "names": [alias.name for alias in node.names],
                "level": 0,
                "line": node.lineno,
            })
        elif isinstance(node, ast.ImportFrom):
            imports.append({
                "module": node.module or "",
                "names": [alias.name for alias in node.names],
                "level": node.level,
                "line": node.lineno,
            })
    symbols.sort(key=lambda item: (int(item["line"]), str(item["name"])))
    imports.sort(key=lambda item: int(item["line"]))
    if len(symbols) > max_symbols:
        symbols = symbols[:max_symbols]
        record.truncated = True
    if len(imports) > max_imports:
        imports = imports[:max_imports]
        record.truncated = True
    record.symbols = symbols
    record.imports = imports
    return record


def _parent_name(tree: ast.AST, target: ast.AST) -> Optional[str]:
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    current = parents.get(target)
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def _resolve_import_edges(records: Mapping[str, PythonFileRecord]) -> Dict[str, List[str]]:
    modules: Dict[str, str] = {}
    for path in records:
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        modules[module] = path
    edges: Dict[str, List[str]] = {}
    for path, record in records.items():
        found: Set[str] = set()
        package = path[:-3].rsplit("/", 1)[0] if "/" in path else ""
        for item in record.imports:
            module = str(item.get("module") or "")
            level = int(item.get("level") or 0)
            candidates: List[str] = []
            if level:
                package_parts = package.split("/") if package else []
                keep = max(0, len(package_parts) - level + 1)
                prefix = ".".join(package_parts[:keep])
                target = ".".join(part for part in (prefix, module.replace("/", ".")) if part)
                candidates.append(target)
            elif module:
                candidates.append(module)
            for alias in item.get("names", []):
                if module and alias != "*":
                    candidates.append(".".join(part for part in (module, str(alias)) if part))
            for candidate in candidates:
                if candidate in modules and modules[candidate] != path:
                    found.add(modules[candidate])
        edges[path] = sorted(found)
    return edges


def _terms(value: str) -> Set[str]:
    return {
        token for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", value.lower())
        if token not in {"the", "and", "for", "with", "from", "that", "this", "into"}
    }


def _add_unique(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)
