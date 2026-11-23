"""Adversarial Red-Team / Blue-Team software and algorithm builder.

The loop is intentionally adversarial:

* the **Blue** agent writes a modular implementation plus unit tests;
* the **Red** agent writes attacks -- edge cases, boundary values and exploit
  scripts -- and each attack is executed in an isolated subprocess;
* the Blue agent patches until the firewall, the compiler, the unit tests and
  every Red attack pass.

All generated code is scanned by :mod:`core.security_guard` before it is
written or executed, and subprocesses run with a sanitised environment, a
throw-away working directory and a hard timeout.
"""

from __future__ import annotations

# vajra-allow: network, exfiltration, process

import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.memory_engine import MemoryEngine, slugify
from core.model_router import ModelRouter, ModelRouterError
from core.security_guard import SecurityGuard, SecurityViolation

EXTENSION_BY_LANGUAGE: Mapping[str, str] = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "java": ".java",
    "go": ".go",
    "rust": ".rs",
    "c": ".c",
    "cpp": ".cpp",
    "csharp": ".cs",
    "php": ".php",
    "ruby": ".rb",
    "bash": ".sh",
    "sql": ".sql",
}
EXECUTABLE_LANGUAGES = frozenset({"python"})
MAX_BUNDLE_FILES = 12
MAX_ATTACKS = 6
BLUE_SYSTEM = (
    "You are the Blue Team: a senior engineer who writes small, modular, "
    "dependency-free code with complete unit tests. Reply with JSON only."
)
RED_SYSTEM = (
    "You are the Red Team: an adversarial reviewer who breaks implementations "
    "with edge cases, boundary values, malformed inputs and exploit attempts. "
    "Reply with JSON only."
)
JSON_REMINDER = "\n\nReturn only a single valid JSON object. No markdown, no prose."


class ArchitectError(RuntimeError):
    """Raised when the build loop cannot produce code."""


@dataclass(frozen=True)
class RedReport:
    """Outcome of one Red-Team round."""

    attacks: Tuple[str, ...] = ()
    summary: str = ""
    failures: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attacks": list(self.attacks),
            "summary": self.summary,
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class Evaluation:
    """Compiler + unit-test + firewall verdict for one bundle."""

    failures: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    executed: bool = False
    firewall_clean: bool = True


@dataclass(frozen=True)
class BuildResult:
    """Everything ``vajrastra build`` reports."""

    prompt: str
    language: str = "python"
    verified: bool = False
    rounds: int = 0
    summary: str = ""
    files: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()
    written: Tuple[str, ...] = ()
    failures: Tuple[str, ...] = ()
    red_summary: str = ""
    notes: Tuple[str, ...] = ()
    workspace: str = ""
    lesson_id: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "language": self.language,
            "verified": self.verified,
            "rounds": self.rounds,
            "summary": self.summary,
            "files": list(self.files),
            "tests": list(self.tests),
            "written": list(self.written),
            "failures": list(self.failures),
            "red_summary": self.red_summary,
            "notes": list(self.notes),
            "workspace": self.workspace,
            "lesson_id": self.lesson_id,
        }


def _delimiter_problems(source: str) -> List[str]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[str] = []
    quote: Optional[str] = None
    escaped = False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack[-1] != pairs[char]:
                return [f"unmatched closing {char!r}"]
            stack.pop()
    return [f"unclosed {char!r}" for char in stack]


class CodeArchitect:
    """Drive Blue and Red agents until the artifact survives every check."""

    def __init__(
        self,
        root: Path | str,
        *,
        router: Optional[ModelRouter] = None,
        guard: Optional[SecurityGuard] = None,
        memory: Optional[MemoryEngine] = None,
        rounds: int = 3,
        timeout: float = 90.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.router = router
        self.guard = guard or SecurityGuard(self.root)
        self._owns_memory = memory is None
        self.memory = memory or MemoryEngine(self.root)
        self.rounds = max(1, int(rounds))
        self.timeout = float(timeout)

    def close(self) -> None:
        if self._owns_memory:
            self.memory.close()

    def __enter__(self) -> "CodeArchitect":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------- build
    def build(
        self,
        prompt: str,
        *,
        language: str = "python",
        rounds: Optional[int] = None,
        out: Optional[str] = None,
        write: bool = True,
    ) -> BuildResult:
        text = (prompt or "").strip()
        if not text:
            raise ArchitectError("build prompt must not be empty")
        if self.router is None or not getattr(self.router, "available", False):
            raise ArchitectError(
                "no model configured: set GEMINI_API_KEY to generate verified code"
            )
        language = (language or "python").strip().lower()
        total_rounds = max(1, int(rounds or self.rounds))
        self._prompt_text = text

        draft = self._ask_json(
            self._blue_prompt(text, language, round_index=1), BLUE_SYSTEM
        )
        files, tests = self._normalise_bundle(draft, language)
        summary = str(draft.get("summary") or "").strip()
        red_summary = ""
        failures: List[str] = []
        notes: List[str] = []
        verified = False
        round_used = 0

        for round_index in range(1, total_rounds + 1):
            round_used = round_index
            evaluation = self._evaluate(files, tests, language)
            if evaluation.firewall_clean:
                red = self._red_team(files, tests, language, list(evaluation.failures))
            else:
                red = RedReport(
                    summary="Red Team skipped: the bundle failed the security firewall"
                )
            red_summary = red.summary or red_summary
            notes.extend(evaluation.notes)
            failures = [*evaluation.failures, *red.failures]
            if not failures and evaluation.executed:
                verified = True
                break
            if not failures:
                notes.append(
                    f"static checks passed, but no execution harness exists for {language}"
                )
                break
            if round_index == total_rounds:
                break
            patch = self._ask_json(
                self._patch_prompt(
                    text, language, files, tests, failures, red.summary, round_index
                ),
                BLUE_SYSTEM,
            )
            files, tests = self._normalise_bundle(
                patch, language, previous_files=files, previous_tests=tests
            )
            summary = str(patch.get("summary") or summary).strip()

        written: List[str] = []
        workspace = ""
        if write:
            target_dir = (
                Path(out)
                if out
                else self.root / "memory_bank" / "builds" / slugify(text)[:48]
            )
            workspace = str(target_dir)
            if verified:
                for rel, content in {**files, **tests}.items():
                    self.guard.write_patch(target_dir / rel, content)
                    written.append(rel)
            else:
                notes.append("artifacts were not written because verification did not pass")

        primary = next(iter(files.values()), "")
        lesson = self.memory.record_lesson(
            "build: " + text,
            root_cause=(summary or red_summary or "adversarial build")[:2000],
            verified_patch=(primary[:3000] if verified else ""),
            experiment_metrics={
                "rounds": round_used,
                "files": len(files),
                "tests": len(tests),
                "language": language,
                "verified": verified,
                "failures": len(failures),
            },
            tags=("phase2", "architect", language),
            verified=verified,
        )
        return BuildResult(
            prompt=text,
            language=language,
            verified=verified,
            rounds=round_used,
            summary=summary or red_summary,
            files=tuple(files),
            tests=tuple(tests),
            written=tuple(written),
            failures=tuple(failures[:10]),
            red_summary=red_summary,
            notes=tuple(dict.fromkeys(notes)),
            workspace=workspace,
            lesson_id=lesson.id,
        )

    # ------------------------------------------------------------------ agents
    def _ask_json(self, prompt: str, system: str, *, attempts: int = 2) -> Dict[str, Any]:
        last: Optional[Exception] = None
        if self.router is None:
            raise ArchitectError("no model configured")
        for attempt in range(attempts):
            try:
                data, _response = self.router.generate_json(
                    prompt if attempt == 0 else prompt + JSON_REMINDER, system=system
                )
                return data
            except (ModelRouterError, ValueError) as exc:
                last = exc
        raise ArchitectError(f"model did not return usable JSON: {last}")

    def _blue_prompt(self, prompt: str, language: str, *, round_index: int) -> str:
        return (
            f"Build this in {language}: {prompt}\n\n"
            "Rules: standard library only for Python; no network, no shell-out, no "
            "third-party imports; small modules with docstrings; one unit-test file "
            "per module. Reply as JSON:\n"
            '{"summary": "...", "files": [{"path": "module.py", "content": "..."}], '
            '"tests": [{"path": "test_module.py", "content": "..."}]}\n'
            f"Round {round_index}."
        )

    def _patch_prompt(
        self,
        prompt: str,
        language: str,
        files: Mapping[str, str],
        tests: Mapping[str, str],
        failures: Sequence[str],
        red_summary: str,
        round_index: int,
    ) -> str:
        return (
            f"Repair this {language} implementation of: {prompt}\n\n"
            "Every failure below must be fixed without weakening a test:\n"
            + "\n".join(f"- {failure[:500]}" for failure in failures[:10])
            + "\n\nRed-Team summary: "
            + (red_summary[:600] or "none")
            + "\n\nReturn the complete corrected bundle as JSON with the same shape: "
            '{"summary": "...", "files": [{"path": "...", "content": "..."}], '
            '"tests": [{"path": "...", "content": "..."}]}\n\n'
            + self._bundle_text(files, tests)
            + f"\n\nThis is repair round {round_index + 1}."
        )

    def _red_prompt(
        self,
        prompt: str,
        language: str,
        files: Mapping[str, str],
        tests: Mapping[str, str],
        failures: Sequence[str],
    ) -> str:
        return (
            f"Red-Team this {language} implementation of: {prompt}\n\n"
            "Write adversarial checks that a correct implementation must pass. "
            "Each attack is a standalone script that exits non-zero when it finds a "
            "defect. For Python, import the module directly and use plain 'assert'.\n"
            "Reply as JSON:\n"
            '{"summary": "...", "attacks": [{"name": "...", "code": "..."}]}\n\n'
            + (
                "Already failing checks:\n"
                + "\n".join(f"- {failure[:300]}" for failure in failures[:6])
                + "\n\n"
                if failures
                else ""
            )
            + self._bundle_text(files, tests)
        )

    @staticmethod
    def _bundle_text(files: Mapping[str, str], tests: Mapping[str, str]) -> str:
        chunks: List[str] = []
        for rel, content in {**files, **tests}.items():
            chunks.append(f"--- {rel} ---\n{content}\n")
        return "\n".join(chunks)[:18000]

    # -------------------------------------------------------------- normalising
    def _normalise_bundle(
        self,
        data: Mapping[str, Any],
        language: str,
        *,
        previous_files: Optional[Mapping[str, str]] = None,
        previous_tests: Optional[Mapping[str, str]] = None,
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        files = dict(previous_files or {})
        tests = dict(previous_tests or {})
        files.update(self._entries(data.get("files"), "files", language))
        test_entries = data.get("tests")
        if test_entries is None:
            test_entries = data.get("test_files")
        tests.update(self._entries(test_entries, "tests", language))
        if not files:
            raise ArchitectError("the Blue agent returned no implementation files")
        if len(files) + len(tests) > MAX_BUNDLE_FILES:
            raise ArchitectError(
                f"bundle too large ({len(files) + len(tests)} files, max {MAX_BUNDLE_FILES})"
            )
        return files, tests

    def _entries(self, raw: Any, label: str, language: str) -> Dict[str, str]:
        entries: Dict[str, str] = {}
        if raw is None:
            return entries
        if not isinstance(raw, list):
            raise ArchitectError(f"{label} must be a list of path/content objects")
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path") or "").strip().replace("\\", "/")
            content = entry.get("content")
            if not path or not isinstance(content, str) or not content.strip():
                continue
            self._validate_path(path, language)
            entries[path] = content
        return entries

    def _validate_path(self, path: str, language: str) -> None:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or path.startswith("/"):
            raise ArchitectError(f"unsafe generated path: {path!r}")
        if candidate.parts and candidate.parts[0] == "core":
            raise ArchitectError(f"refusing to generate into the immutable core: {path!r}")
        expected = EXTENSION_BY_LANGUAGE.get(language)
        if expected and candidate.suffix and candidate.suffix.lower() != expected:
            raise ArchitectError(
                f"{path!r} does not use the {language} extension {expected!r}"
            )

    # -------------------------------------------------------------- verification
    def _evaluate(
        self, files: Mapping[str, str], tests: Mapping[str, str], language: str
    ) -> Evaluation:
        failures: List[str] = []
        notes: List[str] = []
        firewall_clean = True

        for rel, content in files.items():
            if language in EXECUTABLE_LANGUAGES:
                result = self.guard.scan_source(content, filename=rel)
                if not result.ok:
                    firewall_clean = False
                    detail = "; ".join(
                        violation.describe() for violation in result.violations
                    )
                    failures.append(f"firewall rejected {rel}: {detail or result.syntax_error}")
            else:
                problems = _delimiter_problems(content)
                if problems:
                    failures.append(f"delimiter error in {rel}: {', '.join(problems)}")

        if language not in EXECUTABLE_LANGUAGES:
            notes.append(
                f"no execution harness for {language}; verification is static only"
            )
            return Evaluation(tuple(failures), tuple(notes), False, firewall_clean)

        if not firewall_clean:
            notes.append("execution skipped: the firewall rejected this bundle")
            return Evaluation(tuple(failures), tuple(notes), False, False)

        with tempfile.TemporaryDirectory(prefix="vajra-build-") as tmp:
            workdir = Path(tmp)
            for rel, content in {**files, **tests}.items():
                target = workdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            env = self._isolated_env(str(workdir))

            compile_command = [
                sys.executable, "-B", "-s", "-m", "py_compile", *files.keys(),
            ]
            ok, output = self._run(compile_command, workdir, env)
            if not ok:
                failures.append("compile failure: " + output)

            if tests:
                discover = [
                    sys.executable, "-B", "-s", "-m", "unittest", "discover",
                    "-s", ".", "-p", "test_*.py", "-q",
                ]
                tests_ok, test_output = self._run(discover, workdir, env)
                if not tests_ok:
                    failures.append("unit tests failed: " + test_output)
            else:
                notes.append("the Blue agent supplied no unit tests")

        return Evaluation(tuple(failures), tuple(notes), True, firewall_clean)

    def _red_team(
        self,
        files: Mapping[str, str],
        tests: Mapping[str, str],
        language: str,
        failures: Sequence[str],
    ) -> RedReport:
        prompt = self._red_prompt(self._last_prompt, language, files, tests, failures)
        try:
            data = self._ask_json(prompt, RED_SYSTEM)
        except ArchitectError as exc:
            return RedReport(summary=f"Red Team unavailable: {exc}")
        summary = str(data.get("summary") or "").strip()
        raw_attacks = data.get("attacks")
        if not isinstance(raw_attacks, list):
            return RedReport(summary=summary or "Red Team returned no attacks")

        attacks: List[str] = []
        broken: List[str] = []
        if language not in EXECUTABLE_LANGUAGES:
            for entry in raw_attacks[:MAX_ATTACKS]:
                if isinstance(entry, Mapping) and entry.get("name"):
                    attacks.append(str(entry["name"]))
            return RedReport(
                attacks=tuple(attacks),
                summary=summary or "attacks are advisory for this language",
            )

        with tempfile.TemporaryDirectory(prefix="vajra-red-") as tmp:
            workdir = Path(tmp)
            for rel, content in {**files, **tests}.items():
                target = workdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            env = self._isolated_env(str(workdir))
            for index, entry in enumerate(raw_attacks[:MAX_ATTACKS]):
                if not isinstance(entry, Mapping):
                    continue
                name = str(entry.get("name") or f"attack-{index + 1}")
                code = entry.get("code")
                if not isinstance(code, str) or not code.strip():
                    continue
                attacks.append(name)
                verdict = self.guard.scan_source(code, filename=f"attack_{index}.py")
                if not verdict.ok:
                    continue  # weakens nothing: unsafe attacks are never executed
                script = workdir / f"attack_{index}.py"
                script.write_text(code, encoding="utf-8")
                ok, output = self._run(
                    [sys.executable, "-B", "-s", str(script)], workdir, env
                )
                if not ok:
                    broken.append(f"red attack {name!r} broke the build: {output[:400]}")
        return RedReport(
            attacks=tuple(attacks), summary=summary, failures=tuple(broken)
        )

    # ------------------------------------------------------------------- utils
    @property
    def _last_prompt(self) -> str:
        return getattr(self, "_prompt_text", "")

    def _isolated_env(self, workdir: str) -> Dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": workdir,
            "PYTHONHASHSEED": "0",
            "HOME": workdir,
            "TMPDIR": workdir,
            "LANG": "C.UTF-8",
        }

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
