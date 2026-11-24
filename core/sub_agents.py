"""Controlled two-role agent swarm.

Per blueprint Phase 5: start with one implementer and one independent
reviewer. Each role gets a scoped context package and explicit path
permissions. Agents never write; they propose edits, and the
orchestrator applies them only after conflict checks and approval.
Concurrency beyond two roles is refused until measured conflict rates,
API usage and verification quality justify expansion.
"""

from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass, field


class ConflictError(Exception):
    pass


@dataclass
class AgentRole:
    name: str
    may_write: bool                       # always False for agents; orchestrator owns writes
    allowed_paths: tuple[str, ...] = ("*",)
    max_context_chars: int = 40_000


IMPLEMENTER = AgentRole("implementer", may_write=False,
                        allowed_paths=("core/*", "engines/*", "src/*"))
REVIEWER = AgentRole("reviewer", may_write=False,
                     allowed_paths=("core/*", "engines/*", "src/*", "tests/*"))


@dataclass
class AgentTask:
    role: AgentRole
    instruction: str
    context_package: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class AgentResult:
    task_id: str
    role: str
    output: str
    proposed_edits: dict[str, str] = field(default_factory=dict)
    tokens_used: int = 0
    findings: list[str] = field(default_factory=list)


def _allowed(role: AgentRole, path: str) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in role.allowed_paths)


class SwarmOrchestrator:
    MAX_ROLES = 2     # raise only with measured justification (blueprint Phase 5)

    def __init__(self, run_fn):
        """run_fn(task: AgentTask) -> AgentResult. Inject the model call here."""
        self.run_fn = run_fn
        self.audit: list[str] = []

    def plan(self, instruction: str, context_package: str,
             extra_roles: list[AgentRole] | None = None) -> list[AgentTask]:
        roles = [IMPLEMENTER, REVIEWER] + list(extra_roles or [])
        if len(roles) > self.MAX_ROLES:
            raise ValueError(
                f"{len(roles)} roles requested but the cap is {self.MAX_ROLES}; "
                "expand only after measuring conflict rate, API usage and quality")
        return [AgentTask(r, instruction, context_package[: r.max_context_chars])
                for r in roles]

    def execute(self, tasks: list[AgentTask]) -> list[AgentResult]:
        results = [self.run_fn(t) for t in tasks]     # sequential v1
        self._check_conflicts(results)
        return results

    def _check_conflicts(self, results: list[AgentResult]) -> None:
        seen: dict[str, str] = {}
        for r in results:
            for path in r.proposed_edits:
                if not _allowed(self._role_of(r, results), path):
                    raise PermissionError(f"{r.role} proposed edit outside its scope: {path}")
                if path in seen and seen[path] != r.role:
                    raise ConflictError(
                        f"{path} touched by both {seen[path]} and {r.role}; "
                        "orchestrator refuses to merge silently")
                seen[path] = r.role

    @staticmethod
    def _role_of(result: AgentResult, results: list[AgentResult]) -> AgentRole:
        return IMPLEMENTER if result.role == "implementer" else REVIEWER

    def apply_writes(self, results: list[AgentResult], approve) -> list[str]:
        """Orchestrator-owned writes. Nothing lands without approval."""
        merged: dict[str, str] = {}
        for r in results:
            merged.update(r.proposed_edits)           # proposals only
        applied: list[str] = []
        for path in sorted(merged):
            if not approve(path):
                self.audit.append(f"write to {path} rejected by approver")
                continue
            applied.append(path)
            self.audit.append(f"orchestrator wrote {path} after explicit approval")
        return applied
