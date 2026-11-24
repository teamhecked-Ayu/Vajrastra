"""Defensive security audit. No exploit execution, no third-party targets.

Static checks only: secret patterns, dangerous AST-level calls, path
traversal shapes, and dependency-manifest hygiene. Every finding's
severity is tied to concrete evidence (file, line, matched snippet).
The auditor never runs exploits and never touches systems it does not
own; findings feed reviewed rule proposals, not automatic changes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "generic_secret_assignment": re.compile(r"(?i)(api[_-]?key|secret|token)\s*=\s*[\"'][^\"']{12,}[\"']"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
}

TRAVERSAL = re.compile(r"(\.\./|\.\.\\|%2e%2e)")

SCAN_SUFFIXES = {".py", ".toml", ".txt", ".cfg", ".json", ".yml", ".yaml"}
SCAN_NAMES = {".env", "requirements.txt"}
MAX_FILE_BYTES = 512 * 1024


@dataclass
class AuditFinding:
    rule_id: str
    severity: str          # high | medium | low | info
    path: str
    line: int
    evidence: str
    recommendation: str


def _classify_call(node: ast.Call) -> tuple[str, str] | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        name = fn.id
    elif isinstance(fn, ast.Attribute):
        parts = [fn.attr]
        base = fn.value
        while isinstance(base, ast.Attribute):
            parts.append(base.attr)
            base = base.value
        if isinstance(base, ast.Name):
            parts.append(base.id)
        name = ".".join(reversed(parts))
    else:
        return None
    if name in ("eval", "exec", "os.system"):
        return name, "high"
    if name == "__import__":
        return "__import__", "medium"
    if name.startswith("subprocess."):
        shell_true = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant)
            and kw.value.value is True for kw in node.keywords)
        if shell_true:
            return "subprocess.call_shell_true", "medium"
    return None


def scan_path(root: str | Path) -> list[AuditFinding]:
    root = Path(root)
    findings: list[AuditFinding] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        if p.suffix not in SCAN_SUFFIXES and p.name not in SCAN_NAMES:
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            for rule_id, rx in SECRET_PATTERNS.items():
                if rx.search(line):
                    findings.append(AuditFinding(
                        rule_id, "high", rel, i, line.strip()[:120],
                        "rotate the secret now; move it to a secrets manager; "
                        "never commit secrets"))
            if TRAVERSAL.search(line):
                findings.append(AuditFinding(
                    "path_traversal_shape", "medium", rel, i, line.strip()[:120],
                    "validate and normalize user-controlled paths before use"))
        if p.suffix != ".py":
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            hit = _classify_call(node)
            if hit:
                rule, sev = hit
                evidence = (lines[node.lineno - 1].strip()[:120]
                            if node.lineno <= len(lines) else rule)
                findings.append(AuditFinding(
                    f"dangerous_call:{rule}", sev, rel, node.lineno, evidence,
                    "replace with a safe alternative; if unavoidable, document "
                    "and gate behind review"))
    return findings


def summarize(findings: list[AuditFinding]) -> dict:
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {"total": len(findings), **counts}
