"""Budget-aware context assembly.

Never passes the entire history by default. The active request is
assembled from ranked sections and trimmed to a declared character
budget; the package reports exactly which sections were dropped or
truncated, and flags any package that still does not fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextSection:
    name: str
    content: str
    priority: int          # lower = kept longer


@dataclass
class ContextPackage:
    sections: list[ContextSection]
    total_chars: int
    budget: int
    truncation_notes: list[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return self.total_chars <= self.budget

    def render(self) -> str:
        return "\n\n".join(f"[{s.name}]\n{s.content}" for s in self.sections)


class ContextEngine:
    def __init__(self, budget_chars: int = 60_000):
        if budget_chars < 1000:
            raise ValueError("budget too small to assemble a useful context")
        self.budget = budget_chars

    def build(self, system_prompt: str, user_request: str,
              selected_files: list[tuple[str, str]] | None = None,
              lessons: list[str] | None = None,
              task_summary: str = "") -> ContextPackage:
        sections = [
            ContextSection("system", system_prompt, 0),
            ContextSection("request", user_request, 1),
        ]
        for path, content in (selected_files or []):
            sections.append(ContextSection(f"file:{path}", content, 2))
        for i, lesson in enumerate(lessons or []):
            sections.append(ContextSection(f"lesson:{i + 1}", lesson, 3))
        if task_summary:
            sections.append(ContextSection("summary", task_summary, 4))

        overhead = lambda s: len(s.content) + len(s.name) + 12
        total = sum(overhead(s) for s in sections)
        notes: list[str] = []
        sections.sort(key=lambda s: s.priority)

        # 1. drop lowest-priority whole sections until we fit
        while total > self.budget and len(sections) > 2:
            dropped = sections.pop()
            total -= overhead(dropped)
            notes.append(f"dropped section '{dropped.name}' ({overhead(dropped)} chars)")

        # 2. trim the largest remaining non-system section
        if total > self.budget:
            for s in sorted(sections, key=lambda s: -len(s.content)):
                if s.name == "system":
                    continue
                overflow = total - self.budget
                keep = max(200, len(s.content) - overflow - 200)
                removed = s.content[keep:]
                s.content = s.content[:keep] + "\n...[truncated]"
                total -= len(removed)
                notes.append(f"truncated section '{s.name}' by {len(removed)} chars")
                if total <= self.budget:
                    break

        if total > self.budget:
            notes.append("WARNING: package exceeds budget even after trimming; raise budget or shrink input")
        return ContextPackage(sections, total, self.budget, notes)
