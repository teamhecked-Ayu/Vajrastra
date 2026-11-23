"""Crash-safe session persistence and auto-compaction for Vajrastra.

A *session* is one user conversation with the agent loop.  Every message,
every tool call and every step result is appended to a session journal on
disk (``memory_bank/sessions/<session_id>.journal``) so a crash, a closed
terminal or a quota rest never loses work.  When the journal grows past the
compaction threshold the oldest turns are folded into a running summary --
either by the free-tier model (when a key is configured) or by a deterministic
extractor (always available) -- which is what gives Vajrastra its
"unlimited-feeling" context on a bounded model window.

Standard library only.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

SESSIONS_RELPATH = "memory_bank/sessions"

#: Default journal ceiling before the compactor folds old turns away.
DEFAULT_COMPACTION_THRESHOLD = 200
#: Hard cap on retained raw turns; older turns are always folded, never lost.
DEFAULT_MAX_TURNS = 1000

IMPORTANT_RE = re.compile(
    r"(error|exception|traceback|failed|root cause|verified|patch|fix|lesson|"
    r"commit|sha|signature|metric|loss|accuracy|todo|decision|conclusion)",
    re.IGNORECASE,
)


class SessionError(RuntimeError):
    """Raised for malformed journals or impossible resume requests."""


def _utc_now() -> str:
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{base}.{int(now % 1 * 1_000_000):06d}Z"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "session"


@dataclass
class Turn:
    """One journal entry: a message or a tool result."""

    kind: str                      # "user" | "assistant" | "tool" | "system" | "checkpoint"
    content: str
    step: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_utc_now)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "step": self.step,
            "meta": dict(self.meta),
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Turn":
        return cls(
            kind=str(payload.get("kind") or "system"),
            content=str(payload.get("content") or ""),
            step=int(payload.get("step") or 0),
            meta=dict(payload.get("meta") or {}),
            at=str(payload.get("at") or _utc_now()),
        )


@dataclass
class SessionState:
    """Everything needed to resume a session exactly where it stopped."""

    session_id: str
    title: str
    goal: str = ""
    step: int = 0
    status: str = "active"          # active | resting | done | failed
    summary: str = ""               # folded history of everything before `turns`
    turns: List[Turn] = field(default_factory=list)
    checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "goal": self.goal,
            "step": self.step,
            "status": self.status,
            "summary": self.summary,
            "turns": [turn.as_dict() for turn in self.turns],
            "checkpoints": [dict(cp) for cp in self.checkpoints],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionState":
        return cls(
            session_id=str(payload.get("session_id") or ""),
            title=str(payload.get("title") or "session"),
            goal=str(payload.get("goal") or ""),
            step=int(payload.get("step") or 0),
            status=str(payload.get("status") or "active"),
            summary=str(payload.get("summary") or ""),
            turns=[Turn.from_dict(t) for t in payload.get("turns") or ()],
            checkpoints=[dict(cp) for cp in payload.get("checkpoints") or ()],
            created_at=str(payload.get("created_at") or _utc_now()),
            updated_at=str(payload.get("updated_at") or _utc_now()),
            meta=dict(payload.get("meta") or {}),
        )


def extract_key_lines(content: str, *, limit: int = 8) -> List[str]:
    """Deterministic compaction: keep the lines that carry decisions/errors."""
    kept: List[str] = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if IMPORTANT_RE.search(stripped):
            kept.append(stripped[:300])
        if len(kept) >= limit:
            break
    return kept


class SessionManager:
    """Owns every session journal under ``memory_bank/sessions/``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / SESSIONS_RELPATH
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _state_path(self, session_id: str) -> Path:
        return self.directory / f"{_slug(session_id)}.json"

    def _journal_path(self, session_id: str) -> Path:
        return self.directory / f"{_slug(session_id)}.journal"

    # --------------------------------------------------------------- creation
    def create(self, goal: str = "", *, title: Optional[str] = None) -> SessionState:
        first_line = (goal or "").strip().splitlines()[0] if goal.strip() else "session"
        state = SessionState(
            session_id=uuid.uuid4().hex[:12],
            title=_slug(title or first_line)[:48],
            goal=(goal or "").strip(),
        )
        self._persist(state)
        return state

    # ---------------------------------------------------------------- loading
    def load(self, session_id: str) -> SessionState:
        path = self._state_path(session_id)
        if not path.is_file():
            raise SessionError(f"no session named '{session_id}' under {self.directory}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"session '{session_id}' is corrupted: {exc}") from exc
        return SessionState.from_dict(payload)

    def list_sessions(self) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                state = self.load(path.stem)
            except SessionError:
                continue
            summaries.append(
                {
                    "session_id": state.session_id,
                    "title": state.title,
                    "status": state.status,
                    "step": state.step,
                    "turns": len(state.turns),
                    "checkpoints": len(state.checkpoints),
                    "updated_at": state.updated_at,
                }
            )
        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries

    # ---------------------------------------------------------------- writing
    def _persist(self, state: SessionState) -> None:
        state.updated_at = _utc_now()
        target = self._state_path(state.session_id)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, delete=False, suffix=".tmp"
        )
        try:
            json.dump(state.as_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            handle.close()
            Path(handle.name).replace(target)
        finally:
            if not handle.closed:
                handle.close()
            leftover = Path(handle.name)
            if leftover.exists():
                leftover.unlink()

    def _append_journal(self, state: SessionState, turn: Turn) -> None:
        journal = self._journal_path(state.session_id)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(turn.as_dict(), sort_keys=True) + "\n")

    def append(
        self,
        state: SessionState,
        kind: str,
        content: str,
        *,
        step: Optional[int] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Turn:
        turn = Turn(
            kind=kind,
            content=content,
            step=state.step if step is None else int(step),
            meta=dict(meta or {}),
        )
        state.turns.append(turn)
        self._append_journal(state, turn)
        self._persist(state)
        return turn

    def checkpoint(self, state: SessionState, label: str, **extra: Any) -> Dict[str, Any]:
        """Record an atomic resume point; call after every completed step."""
        checkpoint = {
            "label": label,
            "step": state.step,
            "at": _utc_now(),
            "turn_index": len(state.turns),
            **extra,
        }
        state.checkpoints.append(checkpoint)
        self._append_journal(
            state, Turn(kind="checkpoint", content=label, step=state.step, meta=dict(extra))
        )
        self._persist(state)
        return checkpoint

    def resume_from(self, state: SessionState, checkpoint: Mapping[str, Any]) -> SessionState:
        """Rewind the session to a checkpoint; the journal keeps newer turns."""
        index = int(checkpoint.get("turn_index") or 0)
        if index < 0 or index > len(state.turns):
            raise SessionError(f"checkpoint turn_index {index} is out of range")
        state.turns = state.turns[:index]
        state.step = int(checkpoint.get("step") or 0)
        state.status = "active"
        self._persist(state)
        return state

    def finish(self, state: SessionState, status: str = "done", summary: str = "") -> None:
        if status not in {"active", "resting", "done", "failed"}:
            raise ValueError(f"unknown session status: {status!r}")
        state.status = status
        if summary:
            state.summary = summary
        self._persist(state)

    # -------------------------------------------------------------- compaction
    def compact(
        self,
        state: SessionState,
        *,
        summarize=None,
        threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> Dict[str, Any]:
        """Fold old turns into ``state.summary``.

        ``summarize(text)`` may call the model router; when it is absent (or
        fails) the deterministic extractor keeps every line that mentions an
        error, patch, decision or metric.  Nothing is ever deleted: the journal
        on disk still holds the full history.
        """
        total = len(state.turns)
        overflow = max(0, total - max_turns)
        soft = max(0, total - threshold)
        fold = max(overflow, soft)
        if fold <= 0:
            return {"folded": 0, "remaining": total, "method": "none"}

        oldest = state.turns[:fold]
        state.turns = state.turns[fold:]

        block = "\n".join(f"[{turn.kind} step {turn.step}] {turn.content}" for turn in oldest)
        method = "deterministic"
        folded_text = ""
        if summarize is not None:
            try:
                folded_text = str(summarize(block)).strip()
                method = "model"
            except Exception:
                folded_text = ""
                method = "deterministic"
        if not folded_text:
            key_lines: List[str] = []
            for turn in oldest:
                key_lines.extend(
                    f"- step {turn.step} ({turn.kind}): {line}"
                    for line in extract_key_lines(turn.content)
                )
            folded_text = "\n".join(key_lines[-40:]) or "(no key lines extracted)"

        stamp = _utc_now()
        state.summary = (
            f"{state.summary}\n\n--- folded {fold} turn(s) at {stamp} [{method}] ---\n"
            f"{folded_text}"
        ).strip()

        self._persist(state)
        return {"folded": fold, "remaining": len(state.turns), "method": method}

    # --------------------------------------------------------------- reminders
    def context_payload(self, state: SessionState, *, budget_chars: int = 24000) -> str:
        """Assemble what the model should see: summary + recent turns."""
        parts: List[str] = []
        if state.goal:
            parts.append(f"GOAL: {state.goal}")
        if state.summary:
            parts.append(f"SUMMARY OF EARLIER WORK:\n{state.summary}")
        recent: List[str] = []
        used = sum(len(p) for p in parts)
        for turn in reversed(state.turns):
            line = f"[{turn.kind} step {turn.step}] {turn.content}"
            if used + len(line) > budget_chars:
                break
            recent.append(line)
            used += len(line)
        if recent:
            parts.append("RECENT TURNS (oldest first):\n" + "\n".join(reversed(recent)))
        return "\n\n".join(parts).strip()
