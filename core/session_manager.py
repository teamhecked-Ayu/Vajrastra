"""Crash-recoverable session snapshots backed by an append-only journal.

The JSON snapshot is a fast read model. The journal is the recovery source for
records written after the snapshot's last durable sequence number. A torn final
journal line is ignored; malformed complete records are reported as corruption.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SESSIONS_RELPATH = "memory_bank/sessions"
DEFAULT_COMPACTION_THRESHOLD = 200
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
    kind: str
    content: str
    step: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_utc_now)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "content": self.content, "step": self.step,
                "meta": dict(self.meta), "at": self.at}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Turn":
        if not isinstance(payload, Mapping):
            raise SessionError("journal turn must be an object")
        meta = payload.get("meta") or {}
        if not isinstance(meta, Mapping):
            raise SessionError("journal turn meta must be an object")
        try:
            step = int(payload.get("step") or 0)
        except (TypeError, ValueError) as exc:
            raise SessionError("journal turn step must be an integer") from exc
        return cls(kind=str(payload.get("kind") or "system"),
                   content=str(payload.get("content") or ""), step=step,
                   meta=dict(meta), at=str(payload.get("at") or _utc_now()))


@dataclass
class SessionState:
    session_id: str
    title: str
    goal: str = ""
    step: int = 0
    status: str = "active"
    summary: str = ""
    turns: List[Turn] = field(default_factory=list)
    checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "title": self.title, "goal": self.goal,
            "step": self.step, "status": self.status, "summary": self.summary,
            "turns": [turn.as_dict() for turn in self.turns],
            "checkpoints": [dict(cp) for cp in self.checkpoints],
            "created_at": self.created_at, "updated_at": self.updated_at,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionState":
        if not isinstance(payload, Mapping):
            raise SessionError("session snapshot must be a JSON object")
        try:
            turns = payload.get("turns") or []
            checkpoints = payload.get("checkpoints") or []
            meta = payload.get("meta") or {}
            if not isinstance(turns, list) or not isinstance(checkpoints, list):
                raise SessionError("session turns and checkpoints must be lists")
            if not isinstance(meta, Mapping):
                raise SessionError("session meta must be an object")
            return cls(
                session_id=str(payload.get("session_id") or ""),
                title=str(payload.get("title") or "session"),
                goal=str(payload.get("goal") or ""),
                step=int(payload.get("step") or 0),
                status=str(payload.get("status") or "active"),
                summary=str(payload.get("summary") or ""),
                turns=[Turn.from_dict(turn) for turn in turns],
                checkpoints=[dict(cp) for cp in checkpoints if isinstance(cp, Mapping)],
                created_at=str(payload.get("created_at") or _utc_now()),
                updated_at=str(payload.get("updated_at") or _utc_now()),
                meta=dict(meta),
            )
        except (TypeError, ValueError) as exc:
            raise SessionError(f"invalid session snapshot: {exc}") from exc


def extract_key_lines(content: str, *, limit: int = 8) -> List[str]:
    kept: List[str] = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped and IMPORTANT_RE.search(stripped):
            kept.append(stripped[:300])
        if len(kept) >= limit:
            break
    return kept


class SessionManager:
    """Owns session snapshots and journals under memory_bank/sessions."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / SESSIONS_RELPATH
        self.directory.mkdir(parents=True, exist_ok=True)

    def _state_path(self, session_id: str) -> Path:
        return self.directory / f"{_slug(session_id)}.json"

    def _journal_path(self, session_id: str) -> Path:
        return self.directory / f"{_slug(session_id)}.journal"

    def create(self, goal: str = "", *, title: Optional[str] = None) -> SessionState:
        clean_goal = (goal or "").strip()
        first_line = clean_goal.splitlines()[0] if clean_goal else "session"
        state = SessionState(session_id=uuid.uuid4().hex[:12],
                             title=_slug(title or first_line)[:48], goal=clean_goal,
                             meta={"_journal_entries": 0})
        self._persist(state)
        return state

    def load(self, session_id: str) -> SessionState:
        path = self._state_path(session_id)
        if not path.is_file():
            raise SessionError(f"no session named '{session_id}' under {self.directory}")
        try:
            state = SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except SessionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SessionError(f"session '{session_id}' is corrupted: {exc}") from exc
        if not state.session_id:
            raise SessionError("session snapshot has no session_id")
        self._replay_journal(state)
        return state

    def _replay_journal(self, state: SessionState) -> None:
        path = self._journal_path(state.session_id)
        if not path.exists():
            return
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SessionError(f"cannot read session journal: {exc}") from exc
        lines = raw.splitlines(keepends=True)
        complete = len(lines)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            complete -= 1  # a torn final append is not a committed record
            # Remove the torn bytes before the next append, or later records
            # would be concatenated onto an invalid JSON fragment.
            durable_bytes = sum(len(line) for line in lines[:complete])
            try:
                with path.open("r+b") as handle:
                    handle.truncate(durable_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise SessionError(f"cannot repair torn journal tail: {exc}") from exc
        if "_journal_entries" not in state.meta:
            # Legacy snapshots predate journal sequence tracking. Their
            # snapshots already contain the applied view, so mark the current
            # journal as consumed rather than duplicating historical turns.
            state.meta["_journal_entries"] = complete
            return
        try:
            already_applied = max(0, int(state.meta.get("_journal_entries", 0)))
        except (TypeError, ValueError):
            raise SessionError("snapshot journal sequence is invalid")
        if already_applied > complete:
            raise SessionError("journal is shorter than the snapshot's durable sequence")
        for index, line in enumerate(lines[:complete]):
            if index < already_applied:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
                turn = Turn.from_dict(record)
            except (UnicodeError, json.JSONDecodeError, SessionError) as exc:
                raise SessionError(f"malformed journal record at line {index + 1}: {exc}") from exc
            if turn.kind == "checkpoint":
                cp = dict(turn.meta)
                cp.setdefault("label", turn.content)
                cp.setdefault("step", turn.step)
                state.checkpoints.append(cp)
            else:
                state.turns.append(turn)
            state.step = max(state.step, turn.step)
        state.meta["_journal_entries"] = complete

    def list_sessions(self) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                state = self.load(path.stem)
            except SessionError:
                continue
            summaries.append({"session_id": state.session_id, "title": state.title,
                              "status": state.status, "step": state.step,
                              "turns": len(state.turns), "checkpoints": len(state.checkpoints),
                              "updated_at": state.updated_at})
        summaries.sort(key=lambda item: item["updated_at"], reverse=True)
        return summaries

    def _persist(self, state: SessionState) -> None:
        state.updated_at = _utc_now()
        target = self._state_path(state.session_id)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory,
                                             delete=False, suffix=".tmp") as handle:
                temp_name = handle.name
                json.dump(state.as_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = None
            self._fsync_directory()
        except OSError as exc:
            raise SessionError(f"cannot persist session snapshot: {exc}") from exc
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor = None
        try:
            descriptor = os.open(str(self.directory), os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            # Some filesystems do not support directory fsync.
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _append_journal(self, state: SessionState, turn: Turn) -> None:
        path = self._journal_path(state.session_id)
        data = (json.dumps(turn.as_dict(), sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("journal append made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise SessionError(f"cannot append session journal: {exc}") from exc
        state.meta["_journal_entries"] = int(state.meta.get("_journal_entries", 0)) + 1

    def append(self, state: SessionState, kind: str, content: str, *,
               step: Optional[int] = None, meta: Optional[Mapping[str, Any]] = None) -> Turn:
        turn = Turn(kind=kind, content=content,
                    step=state.step if step is None else int(step), meta=dict(meta or {}))
        self._append_journal(state, turn)
        state.turns.append(turn)
        self._persist(state)
        return turn

    def checkpoint(self, state: SessionState, label: str, **extra: Any) -> Dict[str, Any]:
        checkpoint = {"label": label, "step": state.step, "at": _utc_now(),
                      "turn_index": len(state.turns), **extra}
        record = Turn(kind="checkpoint", content=label, step=state.step, meta=checkpoint)
        self._append_journal(state, record)
        state.checkpoints.append(checkpoint)
        self._persist(state)
        return checkpoint

    def resume_from(self, state: SessionState, checkpoint: Mapping[str, Any]) -> SessionState:
        try:
            index = int(checkpoint.get("turn_index") or 0)
            step = int(checkpoint.get("step") or 0)
        except (TypeError, ValueError) as exc:
            raise SessionError("checkpoint index and step must be integers") from exc
        if index < 0 or index > len(state.turns):
            raise SessionError(f"checkpoint turn_index {index} is out of range")
        state.turns = state.turns[:index]
        state.step = step
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

    def compact(self, state: SessionState, *, summarize=None,
                threshold: int = DEFAULT_COMPACTION_THRESHOLD,
                max_turns: int = DEFAULT_MAX_TURNS) -> Dict[str, Any]:
        total = len(state.turns)
        fold = max(max(0, total - max_turns), max(0, total - threshold))
        if fold <= 0:
            return {"folded": 0, "remaining": total, "method": "none"}
        oldest = state.turns[:fold]
        block = "\n".join(f"[{turn.kind} step {turn.step}] {turn.content}" for turn in oldest)
        method, folded_text = "deterministic", ""
        if summarize is not None:
            try:
                folded_text = str(summarize(block)).strip()
                if folded_text:
                    method = "model"
            except Exception:
                pass
        if not folded_text:
            key_lines: List[str] = []
            for turn in oldest:
                key_lines.extend(f"- step {turn.step} ({turn.kind}): {line}"
                                 for line in extract_key_lines(turn.content))
            folded_text = "\n".join(key_lines[-40:]) or "(no key lines extracted)"
        stamp = _utc_now()
        state.summary = (f"{state.summary}\n\n--- folded {fold} turn(s) at {stamp} [{method}] ---\n"
                         f"{folded_text}").strip()
        state.turns = state.turns[fold:]
        self._persist(state)
        return {"folded": fold, "remaining": len(state.turns), "method": method}

    def context_payload(self, state: SessionState, *, budget_chars: int = 24000) -> str:
        parts: List[str] = []
        if state.goal:
            parts.append(f"GOAL: {state.goal}")
        if state.summary:
            parts.append(f"SUMMARY OF EARLIER WORK:\n{state.summary}")
        recent: List[str] = []
        used = sum(len(part) for part in parts)
        for turn in reversed(state.turns):
            line = f"[{turn.kind} step {turn.step}] {turn.content}"
            if used + len(line) > budget_chars:
                break
            recent.append(line)
            used += len(line)
        if recent:
            parts.append("RECENT TURNS (oldest first):\n" + "\n".join(reversed(recent)))
        return "\n\n".join(parts).strip()
