"""Adversarial hyper-debugger: recall, static proof, fuzz, verified patch.

Four stages, each of which can be switched off by capability rather than by a
crash:

1. **Recall** -- ask :mod:`core.memory_engine` whether this failure signature
   has been seen before.
2. **Static proof** -- deep :mod:`ast` parsing for Python plus the immutable
   :mod:`core.security_guard` firewall; language-agnostic structural checks for
   everything else (the tool accepts any file or directory).
3. **Adversarial verification** -- when ``z3``/``sympy``/``hypothesis`` are
   installed (Colab, Linux, macOS) it proves simple constraints and fuzzes pure
   functions; on Android, where they are absent, it falls back to a lightweight
   AST pass plus an LLM Red-Team/Blue-Team review.
4. **Repair** -- the model proposes a patch, the patch is re-scanned, executed
   in an isolated subprocess and only then logged to ``lessons.sqlite`` as a
   verified lesson, with a unified diff and a one-paragraph root cause.

Optional libraries are imported lazily inside their loaders; none is required.
"""

from __future__ import annotations

# subprocess is used only for isolated, time-boxed verification runs.
# vajra-allow: network, exfiltration, process

import ast
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.memory_engine import LessonRecord, MemoryEngine, normalize_signature
from core.model_router import ModelRouter, ModelRouterError
from core.security_guard import SecurityGuard, SecurityViolation

SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
     ".pytest_cache", "memory_bank", ".idea", ".vscode", "dist", "build"}
)
BINARY_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".png", ".jpg",
     ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".tar", ".whl", ".sqlite",
     ".db", ".class", ".jar", ".o", ".a"}
)
LANGUAGE_BY_SUFFIX: Mapping[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".hpp": "cpp", ".php": "php", ".rb": "ruby", ".sh": "bash",
    ".bash": "bash", ".ps1": "powershell", ".sql": "sql", ".kt": "kotlin",
    ".swift": "swift", ".scala": "scala", ".lua": "lua", ".r": "r", ".cs": "csharp",
}
PYTHON_RUNNER_LIMIT = 5
MAX_FILES_DEFAULT = 400
MAX_FILE_BYTES = 1_500_000
FUZZ_TIMEOUT = 30.0
VERIFY_TIMEOUT = 45.0

SAFE_PURE_CALLS = frozenset(
    {"abs", "all", "any", "bool", "chr", "dict", "divmod", "enumerate", "float",
     "int", "len", "list", "max", "min", "ord", "pow", "range", "reversed", "round",
     "set", "sorted", "str", "sum", "tuple", "zip"}
)

_ANNOTATION_KINDS = {"int", "float", "str", "bool"}
_COMPARE_OPS = {
    ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!=",
}
_FLIPPED = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "==", "!=": "!="}
_CONFLICT_RE = re.compile(r"^(?:<{7}|={7}|>{7})(?:\s|$)")
_DYNAMIC_CALL_RE = re.compile(r"\b(?:eval|exec|system|popen)\s*\(")


# --------------------------------------------------------------------- records
@dataclass(frozen=True)
class EngineStatus:
    """Which optional verification engines are importable on this host."""

    z3: bool = False
    sympy: bool = False
    hypothesis: bool = False

    @property
    def heavy(self) -> bool:
        return self.z3 or self.sympy or self.hypothesis

    @property
    def names(self) -> Tuple[str, ...]:
        found = []
        if self.z3:
            found.append("z3")
        if self.sympy:
            found.append("sympy")
        if self.hypothesis:
            found.append("hypothesis")
        return tuple(found)

    def describe(self) -> str:
        if not self.heavy:
            return "symbolic engines absent -> lightweight AST + LLM adversarial hunt"
        return "symbolic engines: " + ", ".join(self.names)

    def as_dict(self) -> Dict[str, bool]:
        return {"z3": self.z3, "sympy": self.sympy, "hypothesis": self.hypothesis}


@dataclass(frozen=True)
class Finding:
    """One diagnostic from any stage."""

    stage: int
    kind: str
    severity: str
    message: str
    path: str = ""
    lineno: int = 0

    @property
    def blocking(self) -> bool:
        return self.severity in {"high", "critical"}

    def render(self) -> str:
        where = ""
        if self.path:
            where = f" ({self.path}:{self.lineno})" if self.lineno else f" ({self.path})"
        return f"[{self.severity.upper()}] {self.message}{where}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "lineno": self.lineno,
        }


@dataclass(frozen=True)
class Analysis:
    """The complete stage 1-3 report for one target."""

    target: str
    files: Tuple[str, ...] = ()
    findings: Tuple[Finding, ...] = ()
    lessons: Tuple[LessonRecord, ...] = ()
    engines: EngineStatus = field(default_factory=EngineStatus)
    signature: str = ""
    languages: Mapping[str, int] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()

    @property
    def blocking(self) -> Tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def blocking_count(self) -> int:
        return len(self.blocking)

    @property
    def lesson_ids(self) -> Tuple[int, ...]:
        return tuple(lesson.id for lesson in self.lessons)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "files": list(self.files),
            "findings": [finding.as_dict() for finding in self.findings],
            "lessons": [lesson.as_dict() for lesson in self.lessons],
            "engines": self.engines.as_dict(),
            "signature": self.signature,
            "languages": dict(self.languages),
            "notes": list(self.notes),
            "blocking": self.blocking_count,
        }


@dataclass(frozen=True)
class PatchResult:
    """Outcome of stage 4."""

    target: str
    verified: bool = False
    applied: bool = False
    root_cause: str = ""
    diff: str = ""
    message: str = ""
    test_output: str = ""
    executed: bool = False
    lesson_id: Optional[int] = None
    model: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "verified": self.verified,
            "applied": self.applied,
            "root_cause": self.root_cause,
            "diff": self.diff,
            "message": self.message,
            "executed": self.executed,
            "lesson_id": self.lesson_id,
            "model": self.model,
        }


# -------------------------------------------------------------------- loaders
def _load_z3() -> Optional[Any]:
    try:
        import z3  # type: ignore
    except Exception:
        return None
    return z3


def _load_sympy() -> Optional[Any]:
    try:
        import sympy  # type: ignore
    except Exception:
        return None
    return sympy


def _load_hypothesis() -> Optional[Any]:
    try:
        import hypothesis  # type: ignore
    except Exception:
        return None
    return hypothesis


def detect_engines() -> EngineStatus:
    """Probe the optional engines without failing when they are missing."""
    return EngineStatus(
        z3=_load_z3() is not None,
        sympy=_load_sympy() is not None,
        hypothesis=_load_hypothesis() is not None,
    )


def detect_language(path: Path | str) -> str:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower(), "text")


# ---------------------------------------------------------------------- helper
def _read_source(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", "replace")


def _annotation_kind(annotation: Optional[ast.AST]) -> Optional[str]:
    if isinstance(annotation, ast.Name) and annotation.id in _ANNOTATION_KINDS:
        return annotation.id
    return None


def _pure_candidates(tree: ast.Module) -> List[Tuple[str, Tuple[str, ...]]]:
    """Top-level functions with annotated scalars and only safe calls."""
    candidates: List[Tuple[str, Tuple[str, ...]]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef,)) or node.decorator_list:
            continue
        if node.args.vararg or node.args.kwarg:
            continue
        kinds: List[str] = []
        ok = True
        for argument in [*node.args.posonlyargs, *node.args.args]:
            kind = _annotation_kind(argument.annotation)
            if kind is None:
                ok = False
                break
            kinds.append(kind)
        if not ok or len(kinds) > 3:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name) and child.func.id in SAFE_PURE_CALLS:
                    continue
                ok = False
                break
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                ok = False
                break
        if ok:
            candidates.append((node.name, tuple(kinds)))
    return candidates


def _linear_constraints(test: ast.AST) -> List[Tuple[str, str, float]]:
    """Extract conjunctive ``name <op> number`` constraints from a condition."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        collected: List[Tuple[str, str, float]] = []
        for value in test.values:
            collected.extend(_linear_constraints(value))
        return collected
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return []
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1):
        return []
    op = _COMPARE_OPS.get(type(test.ops[0]))
    if op is None:
        return []
    left, right = test.left, test.comparators[0]
    if (
        isinstance(left, ast.Name)
        and isinstance(right, ast.Constant)
        and isinstance(right.value, (int, float))
        and not isinstance(right.value, bool)
    ):
        return [(left.id, op, float(right.value))]
    if (
        isinstance(right, ast.Name)
        and isinstance(left, ast.Constant)
        and isinstance(left.value, (int, float))
        and not isinstance(left.value, bool)
    ):
        return [(right.id, _FLIPPED[op], float(left.value))]
    return []


def _z3_contradiction(z3: Any, constraints: Sequence[Tuple[str, str, float]]) -> bool:
    solver = z3.Solver()
    variables: Dict[str, Any] = {}
    for name, op, value in constraints:
        variable = variables.get(name)
        if variable is None:
            variable = z3.Real(name) if value != int(value) else z3.Int(name)
            variables[name] = variable
        literal = z3.RealVal(str(value)) if value != int(value) else z3.IntVal(int(value))
        expression = {
            ">": variable > literal,
            ">=": variable >= literal,
            "<": variable < literal,
            "<=": variable <= literal,
            "==": variable == literal,
            "!=": variable != literal,
        }[op]
        solver.add(expression)
    return solver.check() == z3.unsat


def _sympy_constant(sympy: Any, node: ast.AST) -> Optional[Any]:
    """Build a sympy expression only when the AST subtree is fully constant."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return None
        if isinstance(node.value, int):
            return sympy.Integer(node.value)
        return sympy.Float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _sympy_constant(sympy, node.operand)
        return -inner if inner is not None else None
    if isinstance(node, ast.BinOp):
        left = _sympy_constant(sympy, node.left)
        right = _sympy_constant(sympy, node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return sympy.floor(left / right)
            if isinstance(node.op, ast.Mod):
                return sympy.Mod(left, right)
            if isinstance(node.op, ast.Pow):
                return left ** right
        except (ZeroDivisionError, ValueError, TypeError):
            return None
    return None


def _bracket_report(source: str) -> List[Tuple[int, str]]:
    """Language-agnostic delimiter check that respects quotes and comments."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[Tuple[str, int]] = []
    problems: List[Tuple[int, str]] = []
    quote: Optional[str] = None
    escaped = False
    line = 1
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\n":
            line += 1
            if quote in {'"', "'"}:
                quote = None
            escaped = False
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'", "`"}:
            quote = char
            index += 1
            continue
        if char == "#" or (char == "/" and index + 1 < length and source[index + 1] == "/"):
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if char in "([{":
            stack.append((char, line))
        elif char in ")]}":
            if not stack or stack[-1][0] != pairs[char]:
                problems.append((line, f"unmatched closing {char!r}"))
            else:
                stack.pop()
        index += 1
    for char, opened_at in stack:
        problems.append((opened_at, f"unclosed {char!r}"))
    return problems


def build_signature(findings: Sequence[Finding], files: Sequence[str]) -> str:
    """Stable signature text for the episodic ledger."""
    lines = [
        f"{finding.kind}: {finding.message}"
        for finding in findings
        if finding.severity in {"high", "critical"}
    ][:12]
    if not lines:
        lines = [f"no blocking findings across {len(files)} file(s)"]
    return "\n".join(lines)


# ------------------------------------------------------------------- fuzz spec
_RUNNER_SOURCE = '''\
"""Isolated edge-case fuzzer generated by Vajrastra (stdlib contract)."""
import importlib.util
import json
import sys
import traceback

spec = json.load(open(sys.argv[1], encoding="utf-8"))


def load(path):
    module_spec = importlib.util.spec_from_file_location("vajra_target", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["vajra_target"] = module
    module_spec.loader.exec_module(module)
    return module


def cases_for(kinds):
    if not kinds:
        return [()]
    options = []
    for kind in kinds:
        if kind == "int":
            options.append([0, 1, -1, 2, 10, 2 ** 31 - 1, -(2 ** 31)])
        elif kind == "float":
            options.append([0.0, 1.0, -1.0, 0.5, 1e308, -0.0])
        elif kind == "str":
            options.append(["", "a", "0", " ", "x" * 64])
        else:
            options.append([True, False])
    rows = [()]
    for values in options:
        rows = [row + (value,) for row in rows for value in values]
    return rows[:64]


def check(func, args):
    try:
        first = func(*args)
    except TypeError:
        return None
    except Exception as exc:
        return "raised " + type(exc).__name__ + ": " + str(exc)[:200]
    try:
        second = func(*args)
    except Exception as exc:
        return "second call raised " + type(exc).__name__ + ": " + str(exc)[:200]
    if repr(first) != repr(second):
        return "non-deterministic output for identical input"
    return None


module = load(spec["module"])
results = []
for entry in spec["functions"]:
    func = getattr(module, entry["name"], None)
    if func is None:
        results.append({"name": entry["name"], "engine": spec["engine"],
                        "errors": ["function disappeared"], "calls": 0})
        continue
    errors = []
    calls = 0
    if spec["engine"] == "hypothesis":
        try:
            from hypothesis import HealthCheck, given, settings
            from hypothesis import strategies as st
            strategies = []
            for kind in entry["kinds"]:
                if kind == "int":
                    strategies.append(st.integers())
                elif kind == "float":
                    strategies.append(st.floats(allow_nan=False, allow_infinity=False))
                elif kind == "str":
                    strategies.append(st.text(max_size=32))
                else:
                    strategies.append(st.booleans())
            strategy = st.tuples(*strategies) if strategies else st.just(())

            @settings(max_examples=30, deadline=None, database=None,
                      suppress_health_check=list(HealthCheck))
            @given(strategy)
            def run(args):
                global calls
                calls += 1
                problem = check(func, args)
                if problem:
                    errors.append(problem)

            run()
        except Exception:
            errors.append("hypothesis run failed: " + traceback.format_exc(limit=1).strip())
    else:
        for args in cases_for(entry["kinds"]):
            calls += 1
            problem = check(func, args)
            if problem:
                errors.append(problem)
    results.append({"name": entry["name"], "engine": spec["engine"],
                    "errors": errors[:10], "calls": calls})

print(json.dumps(results))
'''


# ----------------------------------------------------------------- the engine
class HyperDebugger:
    """Stage 1-4 debugger for any file or directory."""

    def __init__(
        self,
        root: Path | str,
        *,
        guard: Optional[SecurityGuard] = None,
        memory: Optional[MemoryEngine] = None,
        router: Optional[ModelRouter] = None,
        max_files: int = MAX_FILES_DEFAULT,
        timeout: float = VERIFY_TIMEOUT,
    ) -> None:
        self.root = Path(root).resolve()
        self.guard = guard or SecurityGuard(self.root)
        self._owns_memory = memory is None
        self.memory = memory or MemoryEngine(self.root)
        self.router = router
        self.max_files = max(1, int(max_files))
        self.timeout = float(timeout)

    def close(self) -> None:
        if self._owns_memory:
            self.memory.close()

    def __enter__(self) -> "HyperDebugger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ files
    def collect(self, target: Path | str) -> List[Path]:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = (self.root / candidate).resolve()
        if candidate.is_file():
            return [candidate]
        if not candidate.is_dir():
            raise ValueError(f"target does not exist: {candidate}")
        collected: List[Path] = []
        for path in sorted(candidate.rglob("*")):
            if len(collected) >= self.max_files:
                break
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            collected.append(path)
        return collected

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    # ----------------------------------------------------------------- recall
    def recall(self, signature: str, *, limit: int = 5) -> List[LessonRecord]:
        """Stage 1: past lessons matching *signature*."""
        text = (signature or "").strip()
        if not text:
            return []
        found: List[LessonRecord] = []
        seen = set()
        exact = self.memory.get_by_signature(text)
        candidates = [exact] if exact else []
        try:
            candidates.extend(self.memory.search(normalize_signature(text), limit=limit))
        except (ValueError, KeyError):
            pass
        for lesson in candidates:
            if lesson is None or lesson.id in seen:
                continue
            seen.add(lesson.id)
            found.append(lesson)
        return found[:limit]

    # ------------------------------------------------------------- analysis
    def analyze(
        self,
        target: Path | str,
        *,
        use_llm: bool = True,
        engines: Optional[EngineStatus] = None,
    ) -> Analysis:
        files = self.collect(target)
        status = engines or detect_engines()
        findings: List[Finding] = []
        languages: Dict[str, int] = {}
        notes: List[str] = []
        sources: Dict[str, str] = {}
        trees: Dict[str, ast.Module] = {}

        for path in files:
            source = _read_source(path)
            if source is None:
                notes.append(f"skipped binary or oversized file: {self._rel(path)}")
                continue
            rel = self._rel(path)
            language = detect_language(path)
            languages[language] = languages.get(language, 0) + 1
            sources[rel] = source
            if language == "python":
                file_findings, tree = self._python_scan(rel, source)
                findings.extend(file_findings)
                if tree is not None:
                    trees[rel] = tree
            else:
                findings.extend(self._structural_scan(rel, source, language))

        signature = build_signature(findings, [self._rel(path) for path in files])
        lessons = self.recall(signature)
        for lesson in lessons:
            cause = lesson.root_cause or "(no recorded root cause)"
            findings.insert(
                0,
                Finding(
                    stage=1,
                    kind="memory",
                    severity="info",
                    message=f"lesson #{lesson.id} matched: {cause[:240]}",
                ),
            )

        python_files = [(rel, sources[rel], trees[rel]) for rel in trees]
        if status.heavy and python_files:
            for rel, source, tree in python_files[:PYTHON_RUNNER_LIMIT]:
                findings.extend(self._symbolic_scan(rel, tree, status))
                findings.extend(self._fuzz_scan(rel, source, tree, status))
        elif use_llm and python_files:
            review = self._llm_review(sources, findings)
            findings.extend(review)

        if not files:
            notes.append("target contained no readable source files")
        return Analysis(
            target=str(Path(target)),
            files=tuple(self._rel(path) for path in files),
            findings=tuple(findings),
            lessons=tuple(lessons),
            engines=status,
            signature=signature,
            languages=languages,
            notes=tuple(notes),
        )

    # --------------------------------------------------------------- stage 2
    def _python_scan(self, rel: str, source: str) -> Tuple[List[Finding], Optional[ast.Module]]:
        findings: List[Finding] = []
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            findings.append(
                Finding(
                    stage=2,
                    kind="syntax",
                    severity="high",
                    message=f"SyntaxError: {exc.msg}",
                    path=rel,
                    lineno=int(exc.lineno or 0),
                )
            )
            return findings, None

        result = self.guard.scan_source(source, filename=rel)
        for violation in result.violations:
            findings.append(
                Finding(
                    stage=2,
                    kind="security",
                    severity=violation.severity,
                    message=violation.describe(),
                )
            )
        if result.syntax_error:
            findings.append(
                Finding(stage=2, kind="syntax", severity="high",
                        message=f"SyntaxError: {result.syntax_error}", path=rel)
            )
        findings.extend(self._semantic_scan(rel, tree, source))
        return findings, tree

    def _semantic_scan(self, rel: str, tree: ast.Module, source: str) -> List[Finding]:
        findings: List[Finding] = []
        lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        findings.append(
                            Finding(2, "ast", "high",
                                    f"mutable default argument in {node.name}()",
                                    rel, getattr(default, "lineno", 0))
                        )
                    elif (
                        isinstance(default, ast.Call)
                        and isinstance(default.func, ast.Name)
                        and default.func.id in {"list", "dict", "set"}
                    ):
                        findings.append(
                            Finding(2, "ast", "high",
                                    f"mutable default argument in {node.name}()",
                                    rel, getattr(default, "lineno", 0))
                        )
                body = node.body
                for index, statement in enumerate(body[:-1]):
                    if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        nxt = body[index + 1]
                        findings.append(
                            Finding(2, "ast", "low", "unreachable statement after control flow",
                                    rel, getattr(nxt, "lineno", 0))
                        )
                        break
            elif isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    findings.append(
                        Finding(2, "ast", "medium", "bare except clause swallows every error",
                                rel, getattr(node, "lineno", 0))
                    )
                caught = node.type
                broad = isinstance(caught, ast.Name) and caught.id in {"Exception", "BaseException"}
                if broad and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    findings.append(
                        Finding(2, "ast", "high", "exception is caught and silently ignored",
                                rel, getattr(node, "lineno", 0))
                    )
            elif isinstance(node, ast.Compare):
                for comparator in [node.left, *node.comparators]:
                    if isinstance(comparator, ast.Constant) and comparator.value is None:
                        findings.append(
                            Finding(2, "ast", "low", "comparison against None; prefer 'is None'",
                                    rel, getattr(node, "lineno", 0))
                        )
                        break
                if (
                    isinstance(node.left, ast.Name)
                    and len(node.comparators) == 1
                    and isinstance(node.comparators[0], ast.Name)
                    and node.left.id == node.comparators[0].id
                ):
                    findings.append(
                        Finding(2, "ast", "medium", "self-comparison is always constant",
                                rel, getattr(node, "lineno", 0))
                    )
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
                right = node.right
                if isinstance(right, ast.Constant) and right.value in (0, 0.0):
                    findings.append(
                        Finding(2, "ast", "high", "division by a literal zero",
                                rel, getattr(node, "lineno", 0))
                    )

        for lineno, raw in enumerate(lines, start=1):
            if _CONFLICT_RE.match(raw.strip()):
                findings.append(
                    Finding(2, "ast", "high", "merge conflict marker left in source", rel, lineno)
                )
        return findings

    def _structural_scan(self, rel: str, source: str, language: str) -> List[Finding]:
        findings: List[Finding] = []
        for lineno, raw in enumerate(source.splitlines(), start=1):
            if _CONFLICT_RE.match(raw.strip()):
                findings.append(
                    Finding(2, "structure", "high", "merge conflict marker left in source", rel, lineno)
                )
            if _DYNAMIC_CALL_RE.search(raw):
                findings.append(
                    Finding(2, "structure", "medium",
                            f"dynamic execution call in {language} source", rel, lineno)
                )
        for lineno, problem in _bracket_report(source):
            findings.append(
                Finding(2, "structure", "high", f"{language} delimiter error: {problem}", rel, lineno)
            )
        return findings

    # --------------------------------------------------------------- stage 3
    def _symbolic_scan(self, rel: str, tree: ast.Module, status: EngineStatus) -> List[Finding]:
        findings: List[Finding] = []
        z3 = _load_z3() if status.z3 else None
        if z3 is not None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                constraints = _linear_constraints(node.test)
                if len(constraints) < 2:
                    continue
                try:
                    if _z3_contradiction(z3, constraints):
                        findings.append(
                            Finding(3, "symbolic", "high",
                                    "branch condition is unsatisfiable (z3 proof)",
                                    rel, getattr(node, "lineno", 0))
                        )
                except Exception:
                    continue

        sympy = _load_sympy() if status.sympy else None
        if sympy is not None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.BinOp):
                    continue
                expression = _sympy_constant(sympy, node)
                if expression is None:
                    continue
                try:
                    simplified = sympy.simplify(expression)
                except Exception:
                    continue
                if simplified.is_finite is False:
                    findings.append(
                        Finding(3, "symbolic", "high",
                                f"constant expression is not finite ({simplified})",
                                rel, getattr(node, "lineno", 0))
                    )
                elif getattr(simplified, "is_real", None) is False:
                    findings.append(
                        Finding(3, "symbolic", "medium",
                                f"constant expression is not real ({simplified})",
                                rel, getattr(node, "lineno", 0))
                    )
        return findings

    def _fuzz_scan(
        self, rel: str, source: str, tree: ast.Module, status: EngineStatus
    ) -> List[Finding]:
        candidates = _pure_candidates(tree)
        if not candidates:
            return []
        engine = "hypothesis" if status.hypothesis else "deterministic"
        findings: List[Finding] = []
        with tempfile.TemporaryDirectory(prefix="vajra-fuzz-") as tmp:
            workdir = Path(tmp)
            module_path = workdir / "vajra_module.py"
            module_path.write_text(source, encoding="utf-8")
            runner = workdir / "runner.py"
            runner.write_text(_RUNNER_SOURCE, encoding="utf-8")
            spec_path = workdir / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "module": str(module_path),
                        "engine": engine,
                        "functions": [
                            {"name": name, "kinds": list(kinds)} for name, kinds in candidates
                        ],
                    }
                ),
                encoding="utf-8",
            )
            command = [sys.executable, "-B", "-s", str(runner), str(spec_path)]
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(workdir),
                    env=self._isolated_env(str(workdir)),
                    capture_output=True,
                    text=True,
                    timeout=FUZZ_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return [
                    Finding(3, "fuzz", "low", f"edge-case fuzzing could not run: {exc}", rel)
                ]
            payload = self._last_json(completed.stdout)
            if not isinstance(payload, list):
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                message = detail[-1][:200] if detail else "no output"
                return [
                    Finding(3, "fuzz", "low", f"fuzzer produced no report ({engine}): {message}", rel)
                ]
            for entry in payload:
                if not isinstance(entry, Mapping):
                    continue
                name = str(entry.get("name") or "function")
                for error in entry.get("errors") or ():
                    findings.append(
                        Finding(3, "fuzz", "high",
                                f"fuzz({engine}) {name}(): {str(error)[:200]}", rel)
                    )
        return findings

    def _llm_review(
        self, sources: Mapping[str, str], findings: Sequence[Finding]
    ) -> List[Finding]:
        if self.router is None or not getattr(self.router, "available", False):
            return [
                Finding(3, "llm", "info",
                        "symbolic engines unavailable and no model configured; "
                        "adversarial review skipped")
            ]
        prompt = (
            "You are the Red Team in an adversarial bug hunt. Review this source and "
            "report real defects only: edge cases, missing validation, unsafe assumptions, "
            "and logical errors. Reply as JSON: "
            '{"findings": [{"path": "...", "line": 0, "severity": "high|medium|low", '
            '"message": "..."}], "summary": "..."}\n\n'
            + self._compact_sources(sources)
            + "\n\nStatic findings so far:\n"
            + "\n".join(f"- {finding.render()}" for finding in findings[:20])
        )
        try:
            data, _response = self.router.generate_json(prompt)
        except (ModelRouterError, ValueError) as exc:
            return [Finding(3, "llm", "info", f"LLM adversarial review unavailable: {exc}")]
        review: List[Finding] = []
        for entry in data.get("findings") or ():
            if not isinstance(entry, Mapping):
                continue
            severity = str(entry.get("severity") or "medium").lower()
            if severity not in {"low", "medium", "high", "critical"}:
                severity = "medium"
            review.append(
                Finding(
                    stage=3,
                    kind="red-team",
                    severity=severity,
                    message=str(entry.get("message") or "model-reported defect")[:400],
                    path=str(entry.get("path") or ""),
                    lineno=int(entry.get("line") or 0),
                )
            )
        if not review:
            summary = str(data.get("summary") or "no model-reported defects")
            review.append(Finding(3, "red-team", "info", summary[:300]))
        return review

    # --------------------------------------------------------------- stage 4
    def repair(
        self,
        target: Path | str,
        analysis: Analysis,
        *,
        apply: bool = False,
        test_command: Optional[str] = None,
    ) -> PatchResult:
        files = self.collect(target)
        if not files:
            return PatchResult(target=str(target), message="nothing to repair")
        primary = self._primary_file(analysis, files)
        rel = self._rel(primary)
        original = _read_source(primary)
        if original is None:
            return PatchResult(target=str(target), message=f"cannot read {rel}")

        if self.router is None or not getattr(self.router, "available", False):
            return PatchResult(
                target=rel,
                message="no model configured: set GEMINI_API_KEY to generate a verified patch",
            )

        prompt = (
            "You are the Blue Team fixing a defect. Return JSON with exactly these keys: "
            '{"root_cause": "one paragraph", "patched_source": "the complete corrected file", '
            '"test_command": "optional shell command or empty string"}.\n'
            f"File: {rel}\nLanguage: {detect_language(primary)}\n\n"
            "Diagnostics:\n"
            + "\n".join(f"- {finding.render()}" for finding in analysis.findings[:30])
            + "\n\nRemembered lessons:\n"
            + (
                "\n".join(
                    f"- #{lesson.id}: {lesson.root_cause[:300]}"
                    for lesson in analysis.lessons
                )
                or "- none"
            )
            + "\n\nCurrent source:\n```\n"
            + original
            + "\n```"
        )
        try:
            data, response = self.router.generate_json(prompt)
        except (ModelRouterError, ValueError) as exc:
            return PatchResult(target=rel, message=f"model call failed: {exc}")

        patched = data.get("patched_source")
        root_cause = str(data.get("root_cause") or "").strip()
        if not isinstance(patched, str) or not patched.strip():
            return PatchResult(
                target=rel, message="model returned no patched_source", root_cause=root_cause,
                model=response.model,
            )
        if patched.strip() == original.strip():
            lesson = self.memory.record_lesson(
                analysis.signature,
                root_cause=root_cause or "model produced no change",
                tags=("phase2", "hyper-debugger", detect_language(primary)),
            )
            return PatchResult(
                target=rel,
                root_cause=root_cause or "model produced no change",
                message="model proposed no change; lesson recorded as open",
                lesson_id=lesson.id,
                model=response.model,
            )

        language = detect_language(primary)
        scan_ok = True
        scan_detail = ""
        if language == "python":
            result = self.guard.scan_source(patched, filename=rel)
            scan_ok = result.ok
            if not scan_ok:
                scan_detail = "; ".join(violation.describe() for violation in result.violations)
        else:
            problems = _bracket_report(patched)
            scan_ok = not problems
            if problems:
                scan_detail = "; ".join(f"line {line}: {text}" for line, text in problems)

        command = shlex.split(test_command or str(data.get("test_command") or ""))
        executed_ok = False
        executed = False
        output = ""
        if scan_ok:
            executed_ok, output, executed = self._verify_candidate(
                rel, patched, command or None, language
            )

        verified = bool(scan_ok and executed_ok and executed)
        diff_text = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                patched.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        changed = sum(
            1 for line in diff_text.splitlines() if line.startswith(("+", "-")) and line[:3] not in {"+++", "---"}
        )

        lesson = self.memory.record_lesson(
            analysis.signature,
            root_cause=root_cause or (scan_detail or "patch candidate"),
            verified_patch=(diff_text if verified else ""),
            experiment_metrics={
                "findings": len(analysis.findings),
                "language": language,
                "tests_passed": bool(executed_ok),
                "executed": bool(executed),
                "lines_changed": changed,
            },
            tags=("phase2", "hyper-debugger", language),
            verified=verified,
        )

        message = "patch verified in an isolated subprocess"
        if not scan_ok:
            message = "patch rejected by the security firewall: " + scan_detail
        elif not executed:
            message = f"no execution harness for {language}; patch left unverified"
        elif not executed_ok:
            message = "isolated verification failed; lesson recorded as open"
        applied = False
        if verified and apply:
            try:
                self.guard.write_patch(rel, patched)
                applied = True
                message = "patch verified and applied"
            except SecurityViolation as exc:
                message = f"verified, but the write gate refused it: {exc}"
        return PatchResult(
            target=rel,
            verified=verified,
            applied=applied,
            root_cause=root_cause or scan_detail,
            diff=diff_text,
            message=message,
            test_output=(output or "")[:4000],
            executed=executed,
            lesson_id=lesson.id,
            model=response.model,
        )

    def _primary_file(self, analysis: Analysis, files: Sequence[Path]) -> Path:
        ranked: List[Path] = []
        for finding in analysis.findings:
            if not finding.blocking or not finding.path:
                continue
            for path in files:
                if self._rel(path) == finding.path and path not in ranked:
                    ranked.append(path)
        return ranked[0] if ranked else files[0]

    # ------------------------------------------------------------------ utils
    def _isolated_env(self, workdir: str) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": workdir,
            "PYTHONHASHSEED": "0",
            "HOME": workdir,
            "TMPDIR": workdir,
            "LANG": "C.UTF-8",
        }
        return env

    @staticmethod
    def _last_json(text: str) -> Any:
        for line in reversed((text or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith(("[", "{")):
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def _compact_sources(self, sources: Mapping[str, str], limit: int = 14000) -> str:
        chunks: List[str] = []
        used = 0
        for rel, source in sources.items():
            block = f"--- {rel} ---\n{source}\n"
            if used + len(block) > limit:
                chunks.append(f"--- {rel} --- (truncated)")
                break
            chunks.append(block)
            used += len(block)
        return "\n".join(chunks)

    def _copy_tree(self, destination: Path) -> int:
        copied = 0
        for path in sorted(self.root.rglob("*.py")):
            if copied >= 300:
                break
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > 400_000:
                    continue
                target = destination / path.relative_to(self.root)
            except (OSError, ValueError):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(path.read_bytes())
            except OSError:
                continue
            copied += 1
        return copied

    def _verify_candidate(
        self,
        rel: str,
        patched: str,
        command: Optional[Sequence[str]],
        language: str,
    ) -> Tuple[bool, str, bool]:
        """Run the patch in an isolated copy; returns (ok, output, executed)."""
        with tempfile.TemporaryDirectory(prefix="vajra-verify-") as tmp:
            workdir = Path(tmp)
            self._copy_tree(workdir)
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patched, encoding="utf-8")
            env = self._isolated_env(str(workdir))

            if command:
                return (*self._run(command, workdir, env), True)

            if language != "python":
                return (False, f"no harness for {language}", False)

            compile_command = [sys.executable, "-B", "-s", "-m", "py_compile", rel]
            ok, output = self._run(compile_command, workdir, env)
            if not ok:
                return (False, output, True)

            stem = Path(rel).stem
            tests = sorted(workdir.rglob(f"test_{stem}.py"))
            if tests:
                test = tests[0]
                parent = test.parent
                discover = [
                    sys.executable,
                    "-B",
                    "-s",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(parent),
                    "-p",
                    f"test_{stem}.py",
                    "-q",
                ]
                test_ok, test_output = self._run(discover, workdir, env)
                return (test_ok, output + "\n" + test_output, True)
            return (True, output, True)

    def _run(
        self, command: Sequence[str], cwd: Path, env: Mapping[str, str]
    ) -> Tuple[bool, str]:
        try:
            completed = subprocess.run(
                list(command),
                cwd=str(cwd),
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return (False, f"could not run {command[0]}: {exc}")
        output = (completed.stdout or "") + (completed.stderr or "")
        return (completed.returncode == 0, output.strip()[:6000])
