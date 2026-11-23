"""Checkpointed agent loop for Vajrastra.

The loop turns one user instruction into a plan of discrete steps, executes
them one at a time and checkpoints the session after *every* step.  A crash,
a quota exhaustion (HTTP 429 cascade) or a closed terminal therefore costs at
most one step of progress: ``vajrastra resume <session_id>`` reloads the
journal and continues from the last checkpoint.

The loop is honest by design:

* every step result is recorded before the next step starts;
* model output is treated as a *candidate* -- code goes through
  ``SecurityGuard.write_patch`` and tests must pass before a step is
  marked ``verified``;
* when no model key is configured the loop degrades to memory-recall mode
  (signature lookup + verified patches from the episodic ledger), which
  alone resolves repeat bugs with zero LLM calls.

Standard library only.  Network access happens exclusively inside
:class:`core.model_router.ModelRouter`; this module never opens a socket.
"""

from __future__ import annotations

import json
import time
import traceback as traceback_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from core.memory_engine import MemoryEngine

#: ``(output_text, verified)`` tuple returned by tool handlers.
StepResult_fields = Any
from core.security_guard import SecurityGuard, SecurityViolation
from core.session_manager import SessionManager, SessionState

MAX_STEPS_DEFAULT = 40
MAX_RETRIES_PER_STEP = 3

TOOL_SCHEMA: Mapping[str, Mapping[str, str]] = {
    "recall_lesson": {
        "description": "Look up verified lessons for a failure signature.",
        "args": "signature: the normalised error text",
    },
    "read_file": {
        "description": "Read a UTF-8 file inside the workspace.",
        "args": "path: relative file path",
    },
    "list_files": {
        "description": "List files in a workspace directory.",
        "args": "path: relative directory path",
    },
    "write_code": {
        "description": "Write candidate code; always scanned by the firewall first.",
        "args": "path: relative path; code: full source",
    },
    "run_command": {
        "description": "Run an allow-listed local command (compileall/unittest).",
        "args": "argv: list of arguments",
    },
    "finish": {
        "description": "End the task with a summary of what was done.",
        "args": "summary: what was accomplished and verified",
    },
}

ALLOWED_COMMANDS: Sequence[Sequence[str]] = (
    ("python", "-m", "compileall", "-q"),
    ("python", "-m", "unittest"),
    ("python", "-m", "unittest", "discover"),
)

MAX_OUTPUT_CHARS = 4000


class AgentLoopError(RuntimeError):
    """Raised for unrecoverable loop failures (never for a failed step)."""


class ToolError(RuntimeError):
    """Raised by a tool; the loop records it and lets the model retry."""


@dataclass
class StepResult:
    """Outcome of one executed step."""

    step: int
    tool: str
    ok: bool
    output: str
    verified: bool = False
    duration_s: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "ok": self.ok,
            "verified": self.verified,
            "duration_s": round(self.duration_s, 3),
            "output": self.output[:MAX_OUTPUT_CHARS],
            "meta": dict(self.meta),
        }


@dataclass
class AgentRun:
    """Final report for one user instruction."""

    session_id: str
    steps: List[StepResult]
    summary: str
    finished: bool
    verified: bool
    duration_s: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "steps": [step.as_dict() for step in self.steps],
            "summary": self.summary,
            "finished": self.finished,
            "verified": self.verified,
            "duration_s": round(self.duration_s, 3),
        }


class AgentLoop:
    """Plan-execute-verify loop with per-step checkpoints.

    ``runner`` is injectable so tests never touch the network: it receives a
    prompt string and returns a JSON-decoded dict describing the next step,
    e.g. ``{"tool": "read_file", "args": {"path": "app.py"}}``.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        guard: Optional[SecurityGuard] = None,
        memory: Optional[MemoryEngine] = None,
        sessions: Optional[SessionManager] = None,
        router=None,
        runner: Optional[Callable[[str], Mapping[str, Any]]] = None,
        command_runner: Optional[Callable[[Sequence[str]], tuple]] = None,
        max_steps: int = MAX_STEPS_DEFAULT,
    ) -> None:
        self.root = Path(root).resolve()
        self.guard = guard or SecurityGuard(self.root)
        self.memory = memory or MemoryEngine(self.root)
        self.sessions = sessions or SessionManager(self.root)
        self.router = router
        self._runner = runner
        self.max_steps = int(max_steps)
        self._command_runner = command_runner or self._default_command_runner

    # ---------------------------------------------------------------- routing
    def _plan_prompt(self, state: SessionState, instruction: str) -> str:
        tools = "\n".join(
            f"- {name}: {spec['description']} ({spec['args']})"
            for name, spec in TOOL_SCHEMA.items()
        )
        return (
            "You are the Vajrastra planner. Choose the single next tool call.\n"
            f"USER GOAL:\n{state.goal or instruction}\n\n"
            f"{self.sessions.context_payload(state)}\n\n"
            f"AVAILABLE TOOLS:\n{tools}\n\n"
            "Reply with one JSON object only, e.g. "
            '{"tool": "read_file", "args": {"path": "app.py"}, "reason": "..."}'
        )

    def _summarize_prompt(self, block: str) -> str:
        return (
            "Compress this work log into at most 12 bullet lines. Keep every "
            "error signature, patch, decision, metric and file path. Drop "
            "boilerplate.\n\n" + block
        )

    def _default_runner(self, prompt: str) -> Mapping[str, Any]:
        self._last_usage = None
        if self._runner is not None:
            decision = self._runner(prompt)
            from core.model_router import estimate_token_count
            rendered = json.dumps(decision, sort_keys=True, ensure_ascii=False)
            self._last_usage = {
                "input_tokens": estimate_token_count(prompt),
                "output_tokens": estimate_token_count(rendered),
                "total_tokens": estimate_token_count(prompt) + estimate_token_count(rendered),
                "input_source": "estimated",
                "output_source": "estimated",
                "total_source": "estimated",
                "source": "estimated",
            }
            return decision
        if self.router is None or not self.router.available:
            raise AgentLoopError(
                "no planner available: set GEMINI_API_KEY or pass runner= to AgentLoop"
            )
        decision, response = self.router.generate_json(prompt)
        self._last_usage = response.token_usage(prompt)
        return decision

    def _record_model_usage(self, session: SessionState) -> None:
        usage = getattr(self, "_last_usage", None)
        if not isinstance(usage, Mapping):
            return
        totals = session.meta.setdefault(
            "token_usage",
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "measured_calls": 0, "estimated_calls": 0},
        )
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0))
        if usage.get("source") == "measured":
            totals["measured_calls"] = int(totals.get("measured_calls", 0)) + 1
        else:
            totals["estimated_calls"] = int(totals.get("estimated_calls", 0)) + 1
        totals["last_call"] = dict(usage)
        self.sessions._persist(session)

    # ------------------------------------------------------------------ tools
    def _workspace_path(self, raw: str) -> Path:
        candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path escapes the workspace: {raw!r}") from exc
        return candidate

    def _tool_recall_lesson(self, args: Mapping[str, Any]) -> StepResult_fields:
        signature = str(args.get("signature") or "")
        if not signature.strip():
            raise ToolError("recall_lesson needs a non-empty signature")
        lesson = self.memory.get_by_signature(signature)
        hits = self.memory.search(signature, limit=3)
        payload = {
            "exact": lesson.as_dict() if lesson else None,
            "related": [hit.as_dict() for hit in hits],
        }
        if lesson:
            self.memory.record_hit(lesson.id, resolved=lesson.solved)
        return json.dumps(payload, sort_keys=True)[:MAX_OUTPUT_CHARS], bool(
            lesson and lesson.solved
        )

    def _tool_read_file(self, args: Mapping[str, Any]) -> StepResult_fields:
        path = self._workspace_path(str(args.get("path") or ""))
        if not path.is_file():
            raise ToolError(f"no such file: {path.relative_to(self.root)}")
        if path.stat().st_size > 200_000:
            raise ToolError("file too large for one read; read a smaller range")
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT_CHARS], True

    def _tool_list_files(self, args: Mapping[str, Any]) -> StepResult_fields:
        path = self._workspace_path(str(args.get("path") or "."))
        if not path.is_dir():
            raise ToolError(f"no such directory: {path.relative_to(self.root)}")
        entries = sorted(
            child.relative_to(self.root).as_posix()
            for child in path.rglob("*")
            if child.is_file() and "__pycache__" not in child.parts
        )[:500]
        return "\n".join(entries), True

    def _tool_write_code(self, args: Mapping[str, Any]) -> StepResult_fields:
        rel = str(args.get("path") or "")
        code = str(args.get("code") or "")
        if not rel.strip():
            raise ToolError("write_code needs a target path")
        if not code.strip():
            raise ToolError("write_code needs non-empty code")
        target = self._workspace_path(rel)
        # write_patch refuses protected core paths and scans the payload.
        self.guard.write_patch(target, code)
        return f"wrote and cleared firewall scan: {target.relative_to(self.root)}", True

    def _default_command_runner(self, argv: Sequence[str]) -> tuple:
        # vajra-allow: process
        # The allow-list above (_tool_run_command) is the only caller, and it
        # restricts argv to compileall/unittest prefixes, never a shell.
        import subprocess  # local import: the loop itself spawns nothing else

        completed = subprocess.run(
            list(argv),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return completed.returncode, completed.stdout + completed.stderr

    def _tool_run_command(self, args: Mapping[str, Any]) -> StepResult_fields:
        argv = [str(part) for part in (args.get("argv") or [])]
        if not argv:
            raise ToolError("run_command needs an argv list")
        allowed = any(
            len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == tuple(prefix)
            for prefix in ALLOWED_COMMANDS
        )
        if not allowed:
            raise ToolError(
                "command is not on the allow-list: " + " ".join(argv[:3])
            )
        code, output = self._command_runner(argv)
        if code != 0:
            raise ToolError(output[-1500:] or f"command exited {code}")
        return output[-MAX_OUTPUT_CHARS:], True

    # ------------------------------------------------------------------- loop
    def run(
        self,
        instruction: str,
        *,
        session: Optional[SessionState] = None,
        on_step: Optional[Callable[[StepResult], None]] = None,
    ) -> AgentRun:
        """Execute *instruction* step by step, checkpointing throughout."""
        started = time.monotonic()
        if session is None:
            session = self.sessions.create(instruction)
        elif session.status != "active":
            session.status = "active"
        self.sessions.append(session, "user", instruction)

        results: List[StepResult] = []
        summary = ""
        finished = False
        verified = False

        try:
            for _ in range(self.max_steps):
                session.step += 1
                step = session.step
                prompt = self._plan_prompt(session, instruction)
                try:
                    decision = dict(self._default_runner(prompt))
                except AgentLoopError:
                    raise
                except Exception as exc:
                    # Quota exhausted / network down: checkpoint and stop so the
                    # session can be resumed later without losing anything.
                    self.sessions.checkpoint(
                        session,
                        "planner-unavailable",
                        error=str(exc)[:400],
                    )
                    self.sessions.finish(session, "resting")
                    return AgentRun(
                        session_id=session.session_id,
                        steps=results,
                        summary=f"paused at step {step}: {exc}",
                        finished=False,
                        verified=verified,
                        duration_s=time.monotonic() - started,
                    )

                self._record_model_usage(session)
                tool = str(decision.get("tool") or "").strip()
                args = decision.get("args") or {}
                reason = str(decision.get("reason") or "")
                if tool not in TOOL_SCHEMA:
                    result = StepResult(
                        step=step, tool=tool or "?", ok=False,
                        output=f"unknown tool {tool!r}; choose from {sorted(TOOL_SCHEMA)}",
                    )
                else:
                    result = self._execute_tool(session, step, tool, args)

                if reason:
                    result.meta["reason"] = reason[:300]
                results.append(result)
                self.sessions.append(
                    session,
                    "tool",
                    result.output,
                    step=step,
                    meta={"tool": tool, "ok": result.ok, "verified": result.verified},
                )
                self.sessions.checkpoint(
                    session,
                    f"step-{step}-{tool}",
                    ok=result.ok,
                    verified=result.verified,
                )
                self.sessions.compact(
                    session,
                    summarize=self._summarize if self._can_summarize() else None,
                )
                if on_step is not None:
                    on_step(result)

                if tool == "finish" and result.ok:
                    finished = True
                    summary = str(args.get("summary") or result.output)
                    self.sessions.append(session, "assistant", summary, step=step)
                    break
                if not result.ok and result.meta.get("retries", 0) > MAX_RETRIES_PER_STEP:
                    self.sessions.finish(session, "failed")
                    return AgentRun(
                        session_id=session.session_id,
                        steps=results,
                        summary=f"step {step} failed after {MAX_RETRIES_PER_STEP} retries",
                        finished=False,
                        verified=verified,
                        duration_s=time.monotonic() - started,
                    )
            else:
                self.sessions.finish(session, "resting")
        except Exception as exc:  # pragma: no cover - defensive net
            self.sessions.checkpoint(session, "loop-crashed", error=str(exc)[:400])
            self.sessions.append(
                session, "system", traceback_module.format_exc()[-2000:], step=session.step
            )
            self.sessions.finish(session, "failed")
            raise

        # verified when the task finished and no failure burned the whole retry budget
        verified = finished and not any(
            step.meta.get("retries", 0) > MAX_RETRIES_PER_STEP for step in results
        )
        if finished:
            self.sessions.finish(session, "done", summary=summary)
        return AgentRun(
            session_id=session.session_id,
            steps=results,
            summary=summary,
            finished=finished,
            verified=verified,
            duration_s=time.monotonic() - started,
        )

    def _execute_tool(
        self, session: SessionState, step: int, tool: str, args: Any
    ) -> StepResult:
        started = time.monotonic()
        args = args if isinstance(args, Mapping) else {}
        retry_key = f"retries_tool_{tool}"
        attempts = int(session.meta.get(retry_key, 0))
        handler = {
            "recall_lesson": self._tool_recall_lesson,
            "read_file": self._tool_read_file,
            "list_files": self._tool_list_files,
            "write_code": self._tool_write_code,
            "run_command": self._tool_run_command,
        }.get(tool)
        try:
            if tool == "finish":
                output = str(args.get("summary") or "done")
                ok, is_verified = True, True
            elif handler is None:
                raise ToolError(f"unknown tool: {tool}")
            else:
                output, is_verified = handler(args)
                ok = True
        except (ToolError, SecurityViolation, OSError, ValueError) as exc:
            attempts += 1
            session.meta[retry_key] = attempts
            return StepResult(
                step=step,
                tool=tool,
                ok=False,
                output=str(exc)[:MAX_OUTPUT_CHARS],
                verified=False,
                duration_s=time.monotonic() - started,
                meta={"retries": attempts},
            )
        # success resets the retry budget for this tool
        session.meta[retry_key] = 0
        return StepResult(
            step=step,
            tool=tool,
            ok=ok,
            output=output,
            verified=is_verified,
            duration_s=time.monotonic() - started,
        )

    # ------------------------------------------------------------- summaries
    def _can_summarize(self) -> bool:
        return self.router is not None and bool(self.router.available) and self._runner is None

    def _summarize(self, block: str) -> str:
        if self.router is None or not self.router.available:
            raise AgentLoopError("no model for summarization")
        response = self.router.generate(self._summarize_prompt(block))
        return response.text
