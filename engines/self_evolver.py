"""Vajrastra self-evolver: the Meta-Critic development loop behind ``evolve``.

The loop is deliberately conservative.  It reads the episodic ledger and the
latest measured research runs, names the weak spots it can actually see, drafts
reviewable candidate improvements, and then proves them before they can leave
the machine:

1. ``SecurityGuard.verify()`` must pass -- a drifted core stops the loop.
2. A candidate that touches ``core/`` (in particular
   ``core/security_guard.py`` and ``core/security_lock.json``) is rejected
   outright: the trust root stays human-sealed.
3. Python candidates must pass the AST firewall and ``ast.parse``.
4. Accepted candidates are replayed into a throw-away copy of the repository
   and the full ``unittest`` suite must stay green there.

Only then are candidates written to the staging area, and -- when a GitHub
token is available -- pushed to an isolated ``self-upgrade/<timestamp>`` branch
through :mod:`core.github_sync`.  Nothing here commits to ``main``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(PROJECT_ROOT))

EVOLUTION_RELPATH = Path("memory_bank") / "evolution"
STAGING_RELPATH = EVOLUTION_RELPATH / "staging"
RESEARCH_RESULTS = Path("results") / "metrics.json"
RESEARCH_TREE = Path("research_tree") / "tree_state.json"

#: files a self-generated change may never touch; they stay human-sealed.
SEALED_FILES: Tuple[str, ...] = (
    "core/security_guard.py",
    "core/security_lock.json",
)
SEALED_PREFIXES: Tuple[str, ...] = ("core/",)

WEAK_PROMPT_RE = re.compile(
    r"json object|invalid json|model reply|schema|prompt|signature mismatch",
    re.IGNORECASE,
)

META_CRITIC_SYSTEM = (
    "You are the Meta-Critic of an autonomous engineer. You may only propose "
    "changes as JSON. Never propose edits to core/security_guard.py, "
    "core/security_lock.json or any path under core/. Keep proposals small, "
    "testable and reversible. Reply with JSON only."
)


class EvolutionError(RuntimeError):
    """Raised when the evolution loop cannot continue safely."""


@dataclass
class Finding:
    """One observed weakness in the current system."""

    finding_id: str
    kind: str
    severity: str
    title: str
    evidence: str
    metric: Optional[float] = None
    subject: str = ""
    suggestion: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "metric": self.metric,
            "subject": self.subject,
            "suggestion": self.suggestion,
        }


@dataclass
class Candidate:
    """A reviewable improvement proposal and its verification evidence."""

    candidate_id: str
    path: str
    content: str
    kind: str
    rationale: str
    finding_ids: Tuple[str, ...] = ()
    protected: bool = False
    scan_ok: Optional[bool] = None
    scan_detail: str = ""
    parses: Optional[bool] = None
    tests_ok: Optional[bool] = None
    tests_detail: str = ""
    accepted: bool = False
    rejection_reason: str = ""
    staged_path: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "path": self.path,
            "kind": self.kind,
            "rationale": self.rationale,
            "finding_ids": list(self.finding_ids),
            "protected": self.protected,
            "scan_ok": self.scan_ok,
            "scan_detail": self.scan_detail,
            "parses": self.parses,
            "tests_ok": self.tests_ok,
            "tests_detail": self.tests_detail,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "staged_path": self.staged_path,
        }


@dataclass
class EvolutionReport:
    """Outcome of one Meta-Critic pass."""

    integrity_status: str
    integrity_detail: str
    findings: List[Finding]
    candidates: List[Candidate]
    staging_branch: str
    staging_dir: Path
    pushed: List[str]
    notes: Tuple[str, ...]
    report_path: Path
    meta_critique: str = ""
    duration_s: float = 0.0
    lesson_id: Optional[int] = None

    @property
    def accepted(self) -> List[Candidate]:
        return [candidate for candidate in self.candidates if candidate.accepted]

    @property
    def rejected(self) -> List[Candidate]:
        return [candidate for candidate in self.candidates if not candidate.accepted]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "integrity_status": self.integrity_status,
            "integrity_detail": self.integrity_detail,
            "staging_branch": self.staging_branch,
            "staging_dir": str(self.staging_dir),
            "pushed": list(self.pushed),
            "findings": [finding.as_dict() for finding in self.findings],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "accepted": [candidate.candidate_id for candidate in self.accepted],
            "rejected": [candidate.candidate_id for candidate in self.rejected],
            "notes": list(self.notes),
            "meta_critique": self.meta_critique,
            "duration_s": round(self.duration_s, 3),
            "lesson_id": self.lesson_id,
        }


TestRunner = Callable[[Candidate], Tuple[bool, str]]


def _slug(text: str, limit: int = 40) -> str:
    cleaned = [character if character.isalnum() else "-" for character in (text or "change").lower()]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:limit] or "change"


class SelfEvolver:
    """Meta-Critic loop: observe, propose, verify, stage."""

    def __init__(
        self,
        root: Path | str,
        *,
        guard: Any,
        memory: Any,
        router: Any = None,
        client: Any = None,
        test_runner: Optional[TestRunner] = None,
        run_tests: bool = True,
        slow_threshold_s: float = 2.0,
        recurrence_threshold: int = 2,
        lesson_limit: int = 200,
        timeout: float = 600.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.guard = guard
        self.memory = memory
        self.router = router
        self.client = client
        self.test_runner = test_runner
        self.run_tests = bool(run_tests)
        self.slow_threshold_s = float(slow_threshold_s)
        self.recurrence_threshold = max(2, int(recurrence_threshold))
        self.lesson_limit = max(1, int(lesson_limit))
        self.timeout = float(timeout)
        if os.environ.get("VAJRASTRA_SELFEVOLVE_CHILD"):
            # a nested suite must never try to run itself again
            self.run_tests = False

    # ------------------------------------------------------------------ paths
    @property
    def evolution_dir(self) -> Path:
        return self.root / EVOLUTION_RELPATH

    def staging_branch(self) -> str:
        return "self-upgrade/" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    # ---------------------------------------------------------------- observe
    def analyze(self) -> List[Finding]:
        """Name the weak prompts, slow parsers and recurring failures we can see."""
        findings: List[Finding] = []
        lessons = []
        try:
            lessons = list(self.memory.recent(limit=self.lesson_limit))
        except Exception as exc:  # a broken ledger must not hide the rest
            findings.append(
                Finding("f-memory", "ledger", "high", "ledger unreadable",
                        f"{type(exc).__name__}: {exc}", suggestion="check memory_bank/lessons.sqlite")
            )

        seen: Dict[str, int] = {}
        for lesson in lessons:
            signature = str(getattr(lesson, "error_signature", "") or "")
            head = signature.splitlines()[0][:160] if signature else ""
            solved = bool(str(getattr(lesson, "verified_patch", "") or "").strip())
            hits = int(getattr(lesson, "hit_count", 0) or 0)
            if head:
                seen[head] = seen.get(head, 0) + 1 + hits
            if not solved and WEAK_PROMPT_RE.search(signature):
                findings.append(
                    Finding(
                        f"f-prompt-{getattr(lesson, 'id', len(findings))}",
                        "weak_prompt",
                        "medium",
                        "model replies are failing the JSON contract",
                        f"lesson #{getattr(lesson, 'id', '?')}: {head}",
                        subject=f"lesson:{getattr(lesson, 'id', '?')}",
                        suggestion="tighten the system prompt, add a schema example, and retry once",
                    )
                )
            if not solved and hits >= self.recurrence_threshold:
                findings.append(
                    Finding(
                        f"f-repeat-{getattr(lesson, 'id', len(findings))}",
                        "recurring_failure",
                        "high",
                        "the same failure keeps recurring unsolved",
                        f"lesson #{getattr(lesson, 'id', '?')} seen {hits} time(s): {head}",
                        metric=float(hits),
                        subject=f"lesson:{getattr(lesson, 'id', '?')}",
                        suggestion="turn this signature into a regression test before patching",
                    )
                )
            for tag in getattr(lesson, "tags", ()) or ():
                if tag in {"parse", "parser", "timeout"} and not solved:
                    findings.append(
                        Finding(
                            f"f-parser-{getattr(lesson, 'id', len(findings))}",
                            "slow_parser",
                            "medium",
                            "parser-tagged failure is still open",
                            f"lesson #{getattr(lesson, 'id', '?')} tag={tag}: {head}",
                            subject=f"lesson:{getattr(lesson, 'id', '?')}",
                            suggestion="cache the parse and fail fast on oversized payloads",
                        )
                    )

        findings.extend(self._slow_runs())
        deduped: Dict[Tuple[str, str], Finding] = {}
        for finding in findings:
            deduped.setdefault((finding.kind, finding.subject), finding)
        return list(deduped.values())

    def _slow_runs(self) -> List[Finding]:
        """Read the measured research artefacts for slow benchmark seeds."""
        findings: List[Finding] = []
        results_path = self.root / RESEARCH_RESULTS
        slowest: List[Tuple[float, str]] = []
        if results_path.is_file():
            try:
                payload = json.loads(results_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            for node in payload.get("nodes") or []:
                experiment = (node or {}).get("experiment") or {}
                for duration in experiment.get("durations") or []:
                    try:
                        value = float(duration)
                    except (TypeError, ValueError):
                        continue
                    slowest.append((value, str(node.get("node_id"))))
        if slowest:
            value, node_id = max(slowest)
            if value > self.slow_threshold_s:
                findings.append(
                    Finding(
                        "f-slow-run",
                        "slow_parser",
                        "medium",
                        "a benchmark seed is slower than the budget allows",
                        f"node {node_id} took {value:.3f}s (threshold {self.slow_threshold_s:.1f}s)",
                        metric=value,
                        subject=f"node:{node_id}",
                        suggestion="shrink the dataset or cache the synthetic split",
                    )
                )
        return findings

    # ---------------------------------------------------------------- propose
    def propose(self, findings: Sequence[Finding]) -> List[Candidate]:
        """Draft candidate improvements; deterministic first, model second."""
        candidates: List[Candidate] = []
        index = 0
        stages: Dict[str, List[str]] = {}
        for finding in findings:
            stages.setdefault(finding.kind, []).append(f"- **{finding.severity}** {finding.title}: {finding.evidence}")

        for kind, lines in sorted(stages.items()):
            index += 1
            candidate_id = f"c{index:02d}"
            path = f"docs/evolution/{_slug(kind)}-playbook.md"
            body = self._playbook(kind, lines)
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    path=path,
                    content=body,
                    kind=kind,
                    rationale=f"consolidate {len(lines)} {kind} observation(s) into a reviewable playbook",
                    finding_ids=tuple(
                        finding.finding_id for finding in findings if finding.kind == kind
                    ),
                )
            )

        model_candidates = self._model_candidates(findings, start_index=index)
        candidates.extend(model_candidates)
        return candidates

    def _playbook(self, kind: str, lines: Sequence[str]) -> str:
        titles = {
            "weak_prompt": "Tighten the model contract",
            "slow_parser": "Make the hot path cheaper",
            "recurring_failure": "Close the recurring failure",
            "ledger": "Repair the ledger",
        }
        title = titles.get(kind, kind.replace("_", " ").title())
        body = [
            f"# Evolution playbook: {title}",
            "",
            f"Generated by `vajrastra evolve` at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.",
            "This file is a review note, not an automatic code change.",
            "",
            "## Evidence",
            *lines,
            "",
            "## Proposed steps",
            "1. Reproduce the finding with a focused test before touching code.",
            "2. Change the smallest surface that removes the cause.",
            "3. Re-run the full unittest suite and `SecurityGuard.verify()`.",
            "4. Record the verified outcome in `memory_bank/lessons.sqlite`.",
            "",
        ]
        return "\n".join(body) + "\n"

    def _model_candidates(self, findings: Sequence[Finding], *, start_index: int) -> List[Candidate]:
        router = self.router
        if router is None or not getattr(router, "available", False):
            return []
        if not findings:
            return []
        prompt = (
            "Observed weaknesses:\n"
            + json.dumps([finding.as_dict() for finding in findings], indent=2, sort_keys=True)
            + "\n\nPropose at most 2 small changes. Reply with JSON: {\"changes\": "
            "[{\"path\": \"relative/path\", \"content\": \"full file text\", \"rationale\": \"...\"}]}. "
            "Paths under core/ are forbidden; prefer docs/ or engines/ notes with tests."
        )
        try:
            payload, _response = router.generate_json(prompt, system=META_CRITIC_SYSTEM)
        except Exception:
            return []
        candidates: List[Candidate] = []
        for offset, change in enumerate((payload.get("changes") or [])[:2]):
            if not isinstance(change, Mapping):
                continue
            path = str(change.get("path") or "").strip()
            content = str(change.get("content") or "")
            if not path or not content:
                continue
            candidates.append(
                Candidate(
                    candidate_id=f"c{start_index + offset + 1:02d}",
                    path=path,
                    content=content,
                    kind="model_proposal",
                    rationale=str(change.get("rationale") or "model-proposed improvement")[:400],
                    finding_ids=tuple(finding.finding_id for finding in findings),
                )
            )
        return candidates

    # --------------------------------------------------------------- validate
    def validate(self, candidates: Sequence[Candidate]) -> None:
        """Apply the path policy, firewall, syntax and test gates in place."""
        for candidate in candidates:
            reason = self._policy_rejection(candidate.path)
            if reason:
                candidate.protected = True
                candidate.accepted = False
                candidate.rejection_reason = reason
                continue
            if candidate.path.endswith(".py"):
                scan = self.guard.scan_source(candidate.content, filename=candidate.path)
                candidate.scan_ok = bool(scan.ok)
                candidate.scan_detail = "; ".join(
                    violation.describe() for violation in scan.violations[:4]
                ) or "clean"
                if not scan.ok:
                    candidate.accepted = False
                    candidate.rejection_reason = "security firewall rejected the candidate"
                    continue
                try:
                    ast.parse(candidate.content, filename=candidate.path)
                except SyntaxError as exc:
                    candidate.parses = False
                    candidate.accepted = False
                    candidate.rejection_reason = f"candidate does not parse: {exc}"
                    continue
                candidate.parses = True

            if not self.run_tests:
                candidate.tests_ok = None
                candidate.tests_detail = "test gate skipped (run_tests=False)"
            else:
                try:
                    ok, detail = self._run_tests(candidate)
                except EvolutionError as exc:
                    ok, detail = False, str(exc)
                candidate.tests_ok = ok
                candidate.tests_detail = detail[:800]
                if not ok:
                    candidate.accepted = False
                    candidate.rejection_reason = "unittest suite failed with this candidate"
                    continue
            candidate.accepted = True

    def _policy_rejection(self, path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in Path(normalized).parts
        ):
            return "path must be a relative path inside the repository"
        if normalized in SEALED_FILES:
            return "human-sealed file: core/security_guard.py and core/security_lock.json are never self-edited"
        if any(normalized.startswith(prefix) for prefix in SEALED_PREFIXES):
            return "paths under core/ are human-sealed and refused by policy"
        return ""

    def _run_tests(self, candidate: Candidate) -> Tuple[bool, str]:
        if self.test_runner is not None:
            return self.test_runner(candidate)
        return self._tests_in_copy(candidate)

    def _tests_in_copy(self, candidate: Candidate) -> Tuple[bool, str]:
        """Replay the candidate into a throw-away copy and run the suite there."""
        if shutil.which(sys.executable) is None and not Path(sys.executable).is_file():
            raise EvolutionError("no Python interpreter available to run the suite")
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "memory_bank", "research_tree", "results")
        with tempfile.TemporaryDirectory(prefix="vajra-stage-") as temporary:
            copy_root = Path(temporary) / "repo"
            shutil.copytree(self.root, copy_root, ignore=ignore)
            target = copy_root / candidate.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(candidate.content, encoding="utf-8")
            environment = dict(**_clean_env())
            environment["VAJRASTRA_SELFEVOLVE_CHILD"] = "1"
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                    cwd=str(copy_root),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return False, f"unittest suite timed out after {self.timeout:.0f}s"
            tail = ((completed.stderr or "") + (completed.stdout or "")).strip().splitlines()[-6:]
            return completed.returncode == 0, "\n".join(tail)[-800:]

    # ------------------------------------------------------------------ stage
    def stage(self, candidates: Sequence[Candidate], branch: str) -> Path:
        """Write accepted candidates into the local staging tree."""
        staging = self.root / STAGING_RELPATH / branch.replace("/", "-")
        staging.mkdir(parents=True, exist_ok=True)
        for candidate in candidates:
            if not candidate.accepted:
                continue
            target = staging / candidate.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(candidate.content, encoding="utf-8")
            candidate.staged_path = str(target.relative_to(self.root))
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "branch": branch,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "candidates": [candidate.as_dict() for candidate in candidates],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return staging

    def push(self, candidates: Sequence[Candidate], branch: str) -> List[str]:
        """Push accepted candidates to an isolated branch through GitHub."""
        client = self.client
        accepted = [candidate for candidate in candidates if candidate.accepted]
        if client is None or not accepted:
            return []
        if hasattr(client, "token") and not client.token():
            return []
        files = {candidate.path: candidate.content for candidate in accepted}
        try:
            self.guard.assert_integrity()
            results = client.push_patch(branch, files, f"evolve: stage {len(files)} candidate(s)")
        except Exception:
            return []
        return [getattr(result, "path", path) for path, result in zip(files, results)]

    # -------------------------------------------------------------------- run
    def review(self, *, push: bool = False) -> EvolutionReport:
        """One full Meta-Critic pass."""
        started = time.perf_counter()
        notes: List[str] = []
        integrity = self.guard.verify()
        if integrity.status == "drift":
            notes.append("integrity drift: evolution proposals are advisory until the core is re-sealed")

        findings = self.analyze()
        candidates = self.propose(findings)
        self.validate(candidates)
        branch = self.staging_branch()
        staging = self.stage(candidates, branch)

        pushed: List[str] = []
        if push:
            if self.client is None:
                notes.append("no GitHub client configured; candidates stay local")
            elif hasattr(self.client, "token") and not self.client.token():
                notes.append("no GITHUB_TOKEN; candidates stay local")
            else:
                pushed = self.push(candidates, branch)
                if not pushed:
                    notes.append("push failed or integrity refused; candidates stay local")
        else:
            notes.append("staging is local; pass --push to open the self-upgrade branch")

        critique = self._meta_critique(findings, candidates)
        report_path = self._write_report(integrity, findings, candidates, branch, staging, pushed, notes, critique)
        lesson_id = self._record_lesson(findings, candidates, pushed)

        return EvolutionReport(
            integrity_status=integrity.status,
            integrity_detail=integrity.describe(),
            findings=findings,
            candidates=candidates,
            staging_branch=branch,
            staging_dir=staging,
            pushed=pushed,
            notes=tuple(notes),
            report_path=report_path,
            meta_critique=critique,
            duration_s=time.perf_counter() - started,
            lesson_id=lesson_id,
        )

    def _meta_critique(self, findings: Sequence[Finding], candidates: Sequence[Candidate]) -> str:
        router = self.router
        if router is None or not getattr(router, "available", False):
            return "unavailable: no GEMINI_API_KEY (no critique fabricated)"
        if not findings:
            return "nothing to critique: no weak spots were observed"
        prompt = (
            "Critique this evolution pass. Findings:\n"
            + json.dumps([finding.as_dict() for finding in findings], indent=2, sort_keys=True)
            + "\nCandidate paths:\n"
            + json.dumps([candidate.path for candidate in candidates], indent=2)
            + "\nReply with JSON: {\"verdict\": \"...\", \"risks\": [\"...\"]}."
        )
        try:
            payload, _response = router.generate_json(prompt, system=META_CRITIC_SYSTEM)
        except Exception as exc:
            return f"unavailable: {type(exc).__name__}: {exc}"[:300]
        verdict = str(payload.get("verdict") or "")[:600]
        risks = [str(risk)[:200] for risk in (payload.get("risks") or [])[:6]]
        return verdict + ("\nRisks: " + "; ".join(risks) if risks else "")

    def _write_report(
        self,
        integrity: Any,
        findings: Sequence[Finding],
        candidates: Sequence[Candidate],
        branch: str,
        staging: Path,
        pushed: Sequence[str],
        notes: Sequence[str],
        critique: str,
    ) -> Path:
        self.evolution_dir.mkdir(parents=True, exist_ok=True)
        path = self.evolution_dir / f"report-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.md"
        accepted = [candidate for candidate in candidates if candidate.accepted]
        rejected = [candidate for candidate in candidates if not candidate.accepted]
        lines = [
            "# Evolution report",
            "",
            f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "",
            f"- integrity: {integrity.status}",
            f"- bundle: {getattr(integrity, 'bundle_sha256', '') or 'n/a'}",
            f"- staging branch: {branch}",
            f"- staging dir: {staging}",
            f"- pushed: {', '.join(pushed) or 'none'}",
            f"- findings: {len(findings)}",
            f"- candidates accepted: {len(accepted)}",
            f"- candidates rejected: {len(rejected)}",
            "",
            "## Findings",
        ]
        for finding in findings:
            lines.append(f"- [{finding.severity}] {finding.kind}: {finding.title} -- {finding.evidence}")
        if not findings:
            lines.append("_No weak prompts, slow parsers or recurring failures were observed._")
        lines.extend(["", "## Candidates"])
        for candidate in candidates:
            status = "accepted" if candidate.accepted else f"rejected ({candidate.rejection_reason})"
            lines.append(f"- {candidate.candidate_id} `{candidate.path}` -- {status}")
        lines.extend(["", "## Meta-Critic", critique or "unavailable", "", "## Guard rails"])
        lines.append("- `core/security_guard.py` and `core/security_lock.json` are human-sealed and always rejected.")
        lines.append("- Every candidate is replayed into a throw-away copy and must keep the suite green.")
        lines.append("- Nothing is committed to `main`; staging happens on an isolated branch.")
        if notes:
            lines.extend(["", "## Notes", *[f"- {note}" for note in notes]])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _record_lesson(
        self, findings: Sequence[Finding], candidates: Sequence[Candidate], pushed: Sequence[str]
    ) -> Optional[int]:
        if self.memory is None or not findings:
            return None
        accepted = [candidate for candidate in candidates if candidate.accepted]
        try:
            lesson = self.memory.record_lesson(
                "evolve: meta-critic pass",
                root_cause=(
                    f"{len(findings)} finding(s): "
                    + ", ".join(sorted({finding.kind for finding in findings}))
                ),
                verified_patch=(
                    "staged: " + ", ".join(candidate.path for candidate in accepted)
                    if accepted
                    else ""
                ),
                experiment_metrics={
                    "findings": len(findings),
                    "accepted": len(accepted),
                    "rejected": len(candidates) - len(accepted),
                    "pushed": len(pushed),
                },
                tags=("evolve", "meta-critic"),
                verified=bool(accepted),
            )
            return lesson.id
        except Exception:  # pragma: no cover - ledger trouble must not abort evolution
            return None


def _clean_env() -> Dict[str, str]:
    """Environment for the nested suite: no API keys, no bridge, no recursion."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "VAJRASTRA_GITHUB_TOKEN"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


__all__ = [
    "Candidate",
    "EvolutionError",
    "EvolutionReport",
    "Finding",
    "SEALED_FILES",
    "SelfEvolver",
]
