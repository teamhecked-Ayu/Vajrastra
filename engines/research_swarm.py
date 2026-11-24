"""Source-linked research synthesis.

Articles and feeds are untrusted reference data. This engine only works
with sources explicitly provided by the caller (it does not pretend to
retrieve papers), links every claim to its sources, and marks anything
unsupported. Measured results stay separate from model-written
commentary: comparison tables carry raw metrics only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Source:
    url: str
    title: str = ""
    excerpt: str = ""
    retrieved_at: float = field(default_factory=time.time)


@dataclass
class Claim:
    text: str
    sources: list[Source] = field(default_factory=list)
    status: str = "unsupported"      # supported | partial | unsupported

    def refresh_status(self) -> None:
        valid = [s for s in self.sources if s.url]
        self.status = "supported" if valid else "unsupported"


@dataclass
class Synthesis:
    markdown: str
    supported: int
    partial: int
    unsupported: int


def add_claim(claims: list[Claim], text: str,
              sources: list[Source] | None = None) -> Claim:
    claim = Claim(text=text, sources=list(sources or []))
    claim.refresh_status()
    claims.append(claim)
    return claim


def synthesize(title: str, claims: list[Claim]) -> Synthesis:
    lines = [
        f"# {title}", "",
        "Every claim below is linked to its sources. "
        "Claims without sources are explicitly marked and must not be cited.", "",
    ]
    sup = part = unsup = 0
    for i, c in enumerate(claims, 1):
        c.refresh_status()
        if c.status == "supported":
            sup += 1
        elif c.status == "partial":
            part += 1
        else:
            unsup += 1
        lines.append(f"{i}. {c.text}")
        if c.sources:
            for s in c.sources:
                day = time.strftime("%Y-%m-%d", time.gmtime(s.retrieved_at))
                label = s.title or s.url
                lines.append(f"   - [{label}]({s.url}) (retrieved {day})")
        else:
            lines.append("   - **UNVERIFIED - no source linked; do not cite**")
        lines.append("")
    return Synthesis("\n".join(lines), sup, part, unsup)


def comparison_table(rows: list[dict], notes_key: str = "notes") -> str:
    """Render raw measured metrics; commentary columns are excluded."""
    if not rows:
        return "(no measured results)"
    keys = [k for k in rows[0] if k != notes_key]
    header = "| " + " | ".join(keys) + " |"
    sep = "|" + "---|" * len(keys)
    body = ["| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows]
    return "\n".join([header, sep, *body])
