"""Vajrastra background worker: execute jobs dispatched from Android.

An Android device cannot run heavy code, so it only *writes a job file* into the
repository.  This worker polls ``workspace/inbox/`` through the GitHub Contents
API, runs the job locally (or in Colab), and commits a result checkpoint to
``workspace/outbox/`` after every task so the phone can pick the answer up.

Job file::

    {"id": "20260101T000000Z-fix",
     "kind": "fix",
     "payload": {"target": "buggy.py", "repair": true},
     "created_at": "2026-01-01T00:00:00Z"}

Supported kinds: ``fix``, ``build``, ``research``, ``memory``, ``sync``.
Jobs can never touch ``core/`` -- the same policy the rest of Vajrastra uses.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_INBOX = "workspace/inbox"
DEFAULT_OUTBOX = "workspace/outbox"
SUPPORTED_KINDS = ("fix", "build", "research", "memory", "sync")


class WorkerError(RuntimeError):
    """Raised when the inbox cannot be read or a job cannot be executed."""


@dataclass
class Job:
    """One queued job file from the repository inbox."""

    job_id: str
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    path: str = ""
    sha: str = ""
    created_at: str = ""
    status: str = "pending"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.job_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "path": self.path,
            "sha": self.sha,
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass
class JobOutcome:
    """Result of executing one job."""

    job_id: str
    kind: str
    ok: bool
    summary: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_s: float = 0.0
    checkpoint_sha: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "ok": self.ok,
            "summary": self.summary,
            "artifacts": dict(self.artifacts),
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
            "checkpoint_sha": self.checkpoint_sha,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


class JobWorker:
    """Poll the repository inbox, execute jobs, checkpoint the results."""

    def __init__(
        self,
        root: Path | str,
        *,
        client: Any = None,
        router: Any = None,
        guard: Any = None,
        memory: Any = None,
        adapter: Any = None,
        branch: str = "main",
        inbox: str = DEFAULT_INBOX,
        outbox: str = DEFAULT_OUTBOX,
        max_jobs: int = 5,
        allow_apply: bool = False,
        sleeper: Any = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.client = client
        self.router = router
        self.guard = guard
        self.memory = memory
        self.adapter = adapter
        self.branch = branch
        self.inbox = inbox.strip("/")
        self.outbox = outbox.strip("/")
        self.max_jobs = max(1, int(max_jobs))
        self.allow_apply = bool(allow_apply)
        self.sleeper = sleeper or time.sleep
        self.claim_db = self.root / "memory_bank" / "worker_claims.sqlite3"
        self.claim_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.claim_db), timeout=30.0) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_claims ("
                "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
                "completed_at TEXT, result_path TEXT)"
            )

    # ------------------------------------------------------------ idempotency
    def _claim_job(self, job: Job) -> bool:
        """Atomically claim a job once on this worker's shared filesystem.

        A claim is intentionally not expired automatically: retrying a job
        after an unknown crash point could repeat a destructive side effect.
        An operator must inspect the outbox and explicitly reset a stale claim.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with sqlite3.connect(str(self.claim_db), timeout=30.0) as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO job_claims(job_id, status, claimed_at) "
                    "VALUES (?, 'running', ?)",
                    (job.job_id, now),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise WorkerError(f"cannot claim job {job.job_id}: {exc}") from exc

    def _finish_claim(self, job: Job, outcome: JobOutcome) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result_path = f"{self.outbox}/{job.job_id}.result.json"
        try:
            with sqlite3.connect(str(self.claim_db), timeout=30.0) as connection:
                connection.execute(
                    "UPDATE job_claims SET status=?, completed_at=?, result_path=? WHERE job_id=?",
                    ("done" if outcome.ok else "failed", now, result_path, job.job_id),
                )
        except sqlite3.Error as exc:
            raise WorkerError(f"cannot finalize job claim {job.job_id}: {exc}") from exc

    def reset_claim(self, job_id: str) -> bool:
        """Explicitly release a stale claim after a human has checked its effects."""
        try:
            with sqlite3.connect(str(self.claim_db), timeout=30.0) as connection:
                cursor = connection.execute("DELETE FROM job_claims WHERE job_id=?", (job_id,))
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise WorkerError(f"cannot reset job claim {job_id}: {exc}") from exc

    # ------------------------------------------------------------------- inbox
    def _require_client(self) -> Any:
        if self.client is None:
            raise WorkerError("no GitHub client configured; the inbox lives in the repository")
        if hasattr(self.client, "token") and not self.client.token():
            raise WorkerError("no GITHUB_TOKEN set; cannot poll the inbox")
        return self.client

    def list_pending(self) -> List[Job]:
        """List ``*.json`` job files in the inbox and parse the pending ones."""
        client = self._require_client()
        try:
            listing = client.request(
                "GET", f"/repos/{client.repo}/contents/{self.inbox}?ref={self.branch}"
            )
        except Exception as exc:
            raise WorkerError(f"cannot list {self.inbox}: {exc}") from exc
        if not isinstance(listing, list):
            return []
        jobs: List[Job] = []
        for entry in listing:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or "")
            if not name.endswith(".json") or entry.get("type") == "dir":
                continue
            path = str(entry.get("path") or f"{self.inbox}/{name}")
            try:
                remote = client.get_file(path, self.branch)
            except Exception as exc:
                raise WorkerError(f"cannot read {path}: {exc}") from exc
            if remote is None:
                continue
            try:
                payload = json.loads(remote.text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if str(payload.get("status") or "pending").lower() in {"done", "completed", "failed"}:
                continue
            # The outbox result is the durable idempotency marker. If a worker
            # crashed after writing the result but before marking the inbox,
            # never execute that job a second time.
            job_id = str(payload.get("id") or Path(path).stem)
            result_path = f"{self.outbox}/{job_id}.result.json"
            try:
                existing_result = client.get_file(result_path, self.branch)
            except Exception as exc:
                raise WorkerError(f"cannot check prior result {result_path}: {exc}") from exc
            if existing_result is not None:
                continue
            jobs.append(self._to_job(payload, path, getattr(remote, "sha", "")))
            if len(jobs) >= self.max_jobs:
                break
        return jobs

    def _to_job(self, payload: Mapping[str, Any], path: str, sha: str) -> Job:
        job_id = str(payload.get("id") or Path(path).stem)
        kind = str(payload.get("kind") or payload.get("command") or "").lower()
        body = payload.get("payload")
        if not isinstance(body, Mapping):
            body = {key: value for key, value in payload.items() if key not in {"id", "kind", "command", "created_at", "status"}}
        return Job(
            job_id=job_id,
            kind=kind,
            payload=dict(body),
            path=path,
            sha=sha,
            created_at=str(payload.get("created_at") or ""),
            status=str(payload.get("status") or "pending"),
        )

    # ----------------------------------------------------------------- execute
    def _safe_target(self, raw: str) -> Path:
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = (self.root / candidate).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkerError(f"job target escapes the workspace: {raw}") from exc
        if self.guard is not None and self.guard.is_protected(candidate):
            raise WorkerError("jobs may not touch the human-sealed core/")
        return candidate

    def execute(self, job: Job) -> JobOutcome:
        """Run one job; failures are captured, never raised at the caller."""
        started = time.perf_counter()
        outcome = JobOutcome(job_id=job.job_id, kind=job.kind, ok=False)
        try:
            handler = {
                "fix": self._job_fix,
                "build": self._job_build,
                "research": self._job_research,
                "memory": self._job_memory,
                "sync": self._job_sync,
            }.get(job.kind)
            if handler is None:
                raise WorkerError(f"unsupported job kind {job.kind!r}; expected {', '.join(SUPPORTED_KINDS)}")
            handler(job, outcome)
            outcome.ok = True
        except Exception as exc:
            outcome.ok = False
            outcome.error = f"{type(exc).__name__}: {exc}"[:600]
            outcome.summary = outcome.summary or "job failed"
        outcome.duration_s = time.perf_counter() - started
        return outcome

    def _job_fix(self, job: Job, outcome: JobOutcome) -> None:
        from engines.hyper_debugger import HyperDebugger

        target = self._safe_target(str(job.payload.get("target") or ""))
        if not target.exists():
            raise WorkerError(f"target not found: {target}")
        debugger = HyperDebugger(self.root, guard=self.guard, memory=self.memory, router=self.router)
        try:
            analysis = debugger.analyze(target, use_llm=bool(job.payload.get("llm", True)))
            outcome.summary = (
                f"{len(analysis.findings)} finding(s), {analysis.blocking_count} blocking, "
                f"signature {analysis.signature.splitlines()[0][:120]}"
            )
            outcome.artifacts["findings"] = [finding.as_dict() for finding in analysis.findings[:20]]
            if job.payload.get("repair"):
                patch = debugger.repair(
                    target,
                    analysis,
                    apply=bool(job.payload.get("apply") and self.allow_apply),
                    test_command=job.payload.get("test"),
                )
                outcome.artifacts["patch"] = {
                    "verified": patch.verified,
                    "applied": patch.applied,
                    "root_cause": patch.root_cause,
                    "diff": patch.diff[:8000],
                    "lesson_id": patch.lesson_id,
                    "message": patch.message,
                }
                outcome.summary += f"; repair verified={patch.verified} applied={patch.applied}"
        finally:
            close = getattr(debugger, "close", None)
            if callable(close):
                close()

    def _job_build(self, job: Job, outcome: JobOutcome) -> None:
        from engines.code_architect import CodeArchitect

        if self.router is None or not getattr(self.router, "available", False):
            raise WorkerError("build jobs need GEMINI_API_KEY on the worker")
        prompt = str(job.payload.get("prompt") or "").strip()
        if not prompt:
            raise WorkerError("build job needs a prompt")
        out_dir = self.root / "memory_bank" / "worker" / job.job_id
        architect = CodeArchitect(self.root, router=self.router, guard=self.guard, memory=self.memory)
        try:
            result = architect.build(
                prompt,
                language=str(job.payload.get("language") or "python"),
                rounds=job.payload.get("rounds"),
                out=str(out_dir),
            )
            outcome.summary = f"verified={result.verified} rounds={result.rounds}"
            outcome.artifacts["written"] = list(result.written)
            outcome.artifacts["failures"] = list(result.failures)
            outcome.artifacts["lesson_id"] = result.lesson_id
            if not result.verified:
                raise WorkerError("architect could not verify the build: " + "; ".join(result.failures)[:400])
        finally:
            close = getattr(architect, "close", None)
            if callable(close):
                close()

    def _job_research(self, job: Job, outcome: JobOutcome) -> None:
        from engines.ml_scientist import MLScientist

        topic = str(job.payload.get("topic") or "").strip()
        if not topic:
            raise WorkerError("research job needs a topic")
        scientist = MLScientist(
            self.root,
            router=self.router,
            guard=self.guard,
            memory=self.memory,
            seeds=int(job.payload.get("seeds") or 3),
            budget=int(job.payload.get("budget") or 10),
        )
        run = scientist.run(topic)
        outcome.summary = f"gate={run.gate.status} p={run.gate.p_value} best={run.best.node_id if run.best else None}"
        outcome.artifacts["metrics_json"] = str(run.metrics_json_path.relative_to(self.root))
        outcome.artifacts["metrics_tex"] = str(run.metrics_tex_path.relative_to(self.root))
        outcome.artifacts["verified"] = run.verified
        outcome.artifacts["lesson_id"] = run.lesson_id

    def _job_memory(self, job: Job, outcome: JobOutcome) -> None:
        if self.memory is None:
            raise WorkerError("no memory engine attached")
        action = str(job.payload.get("action") or "stats")
        if action == "stats":
            outcome.artifacts["stats"] = self.memory.stats()
            outcome.summary = f"{outcome.artifacts['stats']['lessons']} lesson(s)"
            return
        if action == "search":
            query = str(job.payload.get("query") or "").strip()
            if not query:
                raise WorkerError("memory search needs a query")
            lessons = self.memory.search(query, limit=int(job.payload.get("limit") or 10))
            outcome.artifacts["lessons"] = [lesson.as_dict() for lesson in lessons]
            outcome.summary = f"{len(lessons)} result(s) for {query!r}"
            return
        raise WorkerError(f"unsupported memory action {action!r}")

    def _job_sync(self, job: Job, outcome: JobOutcome) -> None:
        client = self._require_client()
        if self.memory is None:
            raise WorkerError("no memory engine attached")
        report = client.sync_memory(
            self.memory,
            push=bool(job.payload.get("push", True)),
            pull=bool(job.payload.get("pull", False)),
            remote_path=str(job.payload.get("remote_path") or "memory_bank/lessons.sqlite"),
        )
        outcome.summary = f"pulled={report.pulled_lessons} pushed={report.pushed}"
        outcome.artifacts["commit"] = report.commit_sha

    # -------------------------------------------------------------- checkpoints
    def checkpoint(self, job: Job, outcome: JobOutcome) -> str:
        """Commit the result to the outbox and mark the inbox job done."""
        client = self._require_client()
        payload = outcome.as_dict()
        payload["inbox_path"] = job.path
        path = f"{self.outbox}/{job.job_id}.result.json"
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        try:
            result = client.put_file(path, text, f"worker: checkpoint {job.job_id}", branch=self.branch)
            outcome.checkpoint_sha = str(getattr(result, "commit_sha", "") or "")
        except Exception as exc:
            raise WorkerError(f"cannot checkpoint {job.job_id}: {exc}") from exc
        if job.path:
            try:
                marker = {
                    "id": job.job_id,
                    "kind": job.kind,
                    "status": "done" if outcome.ok else "failed",
                    "result": path,
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                client.put_file(
                    job.path,
                    json.dumps(marker, indent=2, sort_keys=True) + "\n",
                    f"worker: mark {job.job_id} done",
                    branch=self.branch,
                )
            except Exception:
                pass
        return outcome.checkpoint_sha

    # -------------------------------------------------------------------- loop
    def poll_once(self) -> List[JobOutcome]:
        """Run every pending job once and checkpoint each result."""
        jobs = self.list_pending()
        outcomes: List[JobOutcome] = []
        for job in jobs:
            if not self._claim_job(job):
                continue
            outcome = self.execute(job)
            try:
                self.checkpoint(job, outcome)
                self._finish_claim(job, outcome)
            except WorkerError as exc:
                # Keep the durable claim in "running" state. Re-executing after
                # an uncertain checkpoint could repeat a destructive action.
                outcome.error = (outcome.error + f"; checkpoint: {exc}").strip("; ")[:800]
            outcomes.append(outcome)
        return outcomes

    def run(self, *, interval: float = 30.0, iterations: Optional[int] = None) -> List[JobOutcome]:
        """Poll until *iterations* is reached (or forever when it is ``None``)."""
        collected: List[JobOutcome] = []
        completed = 0
        while iterations is None or completed < iterations:
            collected.extend(self.poll_once())
            completed += 1
            if iterations is not None and completed >= iterations:
                break
            self.sleeper(max(1.0, float(interval)))
        return collected


__all__ = ["DEFAULT_INBOX", "DEFAULT_OUTBOX", "Job", "JobOutcome", "JobWorker", "SUPPORTED_KINDS", "WorkerError"]
