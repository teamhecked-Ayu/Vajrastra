"""Vajrastra immutable security layer: AST firewall and SHA-256 integrity lock.

This module is the trust root of the project.  Two independent controls live
here:

1. **AST static firewall** -- every piece of code Vajrastra is about to execute,
   store as a procedural skill or write to disk is parsed with :mod:`ast` and
   checked for shell execution, dynamic evaluation, destructive filesystem
   calls, unauthorised sockets, native-library bridging, privilege changes,
   shell-injection payloads, hard-coded credentials and secret exfiltration.
   Import aliases are resolved, so renaming a callee does not hide it.
2. **Integrity locking** -- SHA-256 digests of the trusted core are sealed into
   ``core/security_lock.json``.  :meth:`SecurityGuard.verify` recomputes them,
   so a self-modified (or externally patched) core is caught before it can run.
   :meth:`SecurityGuard.write_patch` refuses the whole ``core/`` tree, so the
   firewall can never be rewritten by the code it judges.

The module depends on the Python standard library only and never executes,
imports or compiles the code it inspects.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core._guard_integrity import GuardIntegrityMixin
from core._guard_rules import (
    CALL_RULES,
    CRITICAL,
    EXFIL,
    GUARDED_IMPORTS,
    IMMUTABLE,
    IntegrityError,
    IntegrityReport,
    LOCK_RELPATH,
    NETWORK,
    POLICY_RE,
    PROTECTED_DIRS,
    SECRETS,
    SECRET_ENV_RE,
    SECRET_RULES,
    SOURCE_ENV_RE,
    STRING_RULES,
    ScanResult,
    SecurityViolation,
    Violation,
    _dotted_name,
    _matches_prefix,
    bundle_digest,
    sha256_file,
    sha256_text,
)

__all__ = [
    "IntegrityError",
    "IntegrityReport",
    "ScanResult",
    "SecurityGuard",
    "SecurityViolation",
    "Violation",
    "bundle_digest",
    "sha256_file",
    "sha256_text",
]

class SecurityGuard(GuardIntegrityMixin):
    """Static firewall + integrity lock for one Vajrastra workspace."""

    def __init__(
        self,
        root: Path | str,
        *,
        immutable: Sequence[str] = IMMUTABLE,
        protected_dirs: Sequence[str] = PROTECTED_DIRS,
    ) -> None:
        self.root = Path(root).resolve()
        self.immutable: Tuple[str, ...] = tuple(immutable)
        self.protected_dirs: Tuple[str, ...] = tuple(protected_dirs)

    # ------------------------------------------------------------------ paths
    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_RELPATH

    def relpath(self, path: Path | str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return candidate.resolve().as_posix()

    def is_protected(self, path: Path | str) -> bool:
        """True for the immutable security layer and the trusted core tree."""
        rel = self.relpath(path)
        if rel in self.immutable:
            return True
        return "/" in rel and rel.split("/", 1)[0] in self.protected_dirs

    def manifest_targets(self) -> List[str]:
        """Relative paths covered by the integrity lock (core + CLI entrypoint)."""
        targets: Set[str] = set(self.immutable)
        for directory in self.protected_dirs:
            base = self.root / directory
            if not base.is_dir():
                continue
            for candidate in sorted(base.glob("*.py")):
                targets.add(candidate.relative_to(self.root).as_posix())
        entrypoint = self.root / "vajrastra.py"
        if entrypoint.is_file():
            targets.add("vajrastra.py")
        return sorted(targets)

    # -------------------------------------------------------------- firewalling
    def scan_source(
        self,
        source: str,
        filename: str = "<memory>",
        *,
        authorized: Iterable[str] = (),
    ) -> ScanResult:
        """Parse *source* statically and return every firewall finding.

        ``authorized`` capabilities are granted by the caller; ``# vajra-allow:
        network`` grants them from inside the source.  Granted findings are still
        reported under ``waived``.
        """
        grants: Set[str] = {c.strip().lower() for c in authorized if c and c.strip()}
        inline = POLICY_RE.search(source)
        if inline:
            for token in re.split(r"[,\s]+", inline.group("caps")):
                if token:
                    grants.add(token.lower())

        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            return ScanResult(
                filename=filename, syntax_error=f"{exc.msg} (line {exc.lineno})"
            )

        aliases = self._import_aliases(tree)
        findings: List[Violation] = []
        findings.extend(self._scan_calls(tree, source, filename, aliases))
        findings.extend(self._scan_imports(tree, source, filename))
        findings.extend(self._scan_strings(tree, source, filename))
        findings.extend(self._scan_exfiltration(tree, source, filename))

        capabilities: Set[str] = set()
        blocking: List[Violation] = []
        waived: List[Violation] = []
        for finding in findings:
            capabilities.add(finding.capability)
            if finding.blocking and finding.capability in grants:
                waived.append(finding)
            else:
                blocking.append(finding)

        return ScanResult(
            filename=filename,
            violations=tuple(blocking),
            waived=tuple(waived),
            capabilities=frozenset(capabilities),
        )

    def scan_file(self, path: Path | str, *, authorized: Iterable[str] = ()) -> ScanResult:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ScanResult(filename=str(candidate), syntax_error=f"unreadable: {exc}")
        return self.scan_source(source, filename=self.relpath(candidate), authorized=authorized)

    def scan_paths(self, paths: Iterable[Path | str]) -> List[ScanResult]:
        return [self.scan_file(path) for path in paths]

    def require_safe_source(
        self, source: str, filename: str = "<memory>", *, authorized: Iterable[str] = ()
    ) -> ScanResult:
        result = self.scan_source(source, filename, authorized=authorized)
        if not result.ok:
            raise SecurityViolation(
                f"security firewall rejected {filename}", result.violations
            )
        return result

    def require_safe_file(self, path: Path | str, *, authorized: Iterable[str] = ()) -> ScanResult:
        result = self.scan_file(path, authorized=authorized)
        if not result.ok:
            raise SecurityViolation(
                f"security firewall rejected {self.relpath(path)}", result.violations
            )
        return result

    # ---------------------------------------------------------------- internals
    def _import_aliases(self, tree: ast.AST) -> Dict[str, str]:
        aliases: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    else:
                        # ``import urllib.request`` binds ``urllib``, not the leaf.
                        root = alias.name.split(".")[0]
                        aliases[root] = root
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    bound = alias.asname or alias.name
                    aliases[bound] = f"{module}.{alias.name}".strip(".")
        return aliases

    def _resolve_callee(self, func: ast.AST, aliases: Mapping[str, str]) -> Optional[str]:
        name = _dotted_name(func)
        if name is None:
            return None
        head, _, tail = name.partition(".")
        if head in aliases:
            base = aliases[head]
            return f"{base}.{tail}" if tail else base
        return name

    def _scan_calls(
        self, tree: ast.AST, source: str, filename: str, aliases: Mapping[str, str]
    ) -> List[Violation]:
        findings: List[Violation] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = self._resolve_callee(node.func, aliases)
            if not callee:
                continue
            for prefixes, category, severity, message in CALL_RULES:
                if any(_matches_prefix(callee, prefix) for prefix in prefixes):
                    findings.append(
                        Violation(
                            rule=f"call:{callee}",
                            category=category,
                            severity=severity,
                            message=f"{message} via {callee}()",
                            filename=filename,
                            lineno=getattr(node, "lineno", 0),
                            snippet=self._snippet(node, source),
                        )
                    )
                    break
        return findings

    def _scan_imports(self, tree: ast.AST, source: str, filename: str) -> List[Violation]:
        findings: List[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in GUARDED_IMPORTS:
                    category, severity, message = GUARDED_IMPORTS[root]
                    findings.append(
                        Violation(
                            rule=f"import:{root}",
                            category=category,
                            severity=severity,
                            message=message,
                            filename=filename,
                            lineno=getattr(node, "lineno", 0),
                            snippet=name,
                        )
                    )
        return findings

    def _scan_strings(self, tree: ast.AST, source: str, filename: str) -> List[Violation]:
        findings: List[Violation] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if not value:
                continue
            for pattern, category, severity, message in STRING_RULES:
                if pattern.search(value):
                    findings.append(
                        Violation(
                            rule=f"string:{pattern.pattern}",
                            category=category,
                            severity=severity,
                            message=message,
                            filename=filename,
                            lineno=getattr(node, "lineno", 0),
                            snippet=self._snippet(node, source),
                        )
                    )
            for pattern in SECRET_RULES:
                if pattern.search(value):
                    findings.append(
                        Violation(
                            rule="secret:hardcoded-credential",
                            category=SECRETS,
                            severity=CRITICAL,
                            message="hardcoded credential material in source",
                            filename=filename,
                            lineno=getattr(node, "lineno", 0),
                            snippet="<redacted literal>",
                        )
                    )
                    break
        return findings

    def _scan_exfiltration(self, tree: ast.AST, source: str, filename: str) -> List[Violation]:
        """Flag code that reads secret-bearing env vars *and* opens a socket."""
        if not SOURCE_ENV_RE.search(source):
            return []
        secret_reads: List[int] = []
        network_calls: List[int] = []
        aliases = self._import_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                base = _dotted_name(node.value)
                key = node.slice
                if base in {"os.environ", "environ"} and isinstance(key, ast.Constant):
                    if isinstance(key.value, str) and SECRET_ENV_RE.search(key.value):
                        secret_reads.append(getattr(node, "lineno", 0))
            elif isinstance(node, ast.Call):
                callee = self._resolve_callee(node.func, aliases) or ""
                if callee in {"os.getenv", "os.environ.get"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if SECRET_ENV_RE.search(first.value):
                            secret_reads.append(getattr(node, "lineno", 0))
                for prefixes, category, _severity, _message in CALL_RULES:
                    if category != NETWORK:
                        continue
                    if any(_matches_prefix(callee, prefix) for prefix in prefixes):
                        network_calls.append(getattr(node, "lineno", 0))
        if secret_reads and network_calls:
            return [
                Violation(
                    rule="exfil:env-secret-to-network",
                    category=EXFIL,
                    severity=CRITICAL,
                    message=(
                        "reads secret-bearing environment variables and opens a network "
                        f"channel (env line {min(secret_reads)}, network line {min(network_calls)})"
                    ),
                    filename=filename,
                    lineno=min(secret_reads),
                )
            ]
        return []

    @staticmethod
    def _snippet(node: ast.AST, source: str, limit: int = 160) -> str:
        try:
            segment = ast.get_source_segment(source, node) or ""
        except Exception:  # pragma: no cover - defensive; ast guarantees this works
            segment = ""
        return " ".join(segment.split())[:limit]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    guard = SecurityGuard(Path(__file__).resolve().parents[1])
    sealed = guard.seal(force=True)
    print(f"sealed {len(sealed['files'])} file(s): bundle {sealed['bundle_sha256']}")
