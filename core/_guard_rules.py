"""Firewall tables and result models for :mod:`core.security_guard`.

Split out so each file of the trust root stays small enough to review line by
line.  This module is sealed and write-protected exactly like
``core/security_guard.py``: it is part of the immutable security layer.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

SECURITY_MODULE = "core/security_guard.py"
LOCK_RELPATH = "core/security_lock.json"
LOCK_VERSION = 1

CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
BLOCKING_SEVERITIES: FrozenSet[str] = frozenset({CRITICAL, HIGH})

PROCESS = "process_execution"
DYNAMIC = "dynamic_execution"
DESTRUCTIVE = "destructive_fs"
NETWORK = "network"
NATIVE = "native_bridge"
PRIVILEGE = "privilege_change"
SECRETS = "secret_literal"
EXFIL = "secret_exfiltration"
SHELL = "shell_injection"

CAPABILITY: Mapping[str, str] = {
    PROCESS: "process",
    DYNAMIC: "dynamic",
    DESTRUCTIVE: "destructive",
    NETWORK: "network",
    NATIVE: "native",
    PRIVILEGE: "privilege",
    SECRETS: "secrets",
    EXFIL: "exfiltration",
    SHELL: "shell",
}

#: (callee prefixes, category, severity, message)
CALL_RULES: Tuple[Tuple[Tuple[str, ...], str, str, str], ...] = (
    (
        (
            "os.system", "os.popen", "os.exec", "os.spawn", "os.posix_spawn",
            "os.startfile", "pty.spawn", "subprocess.",
        ),
        PROCESS, HIGH, "spawns a child process or shell",
    ),
    (
        (
            "eval", "exec", "compile", "__import__", "builtins.eval", "builtins.exec",
            "builtins.compile", "marshal.loads", "pickle.loads", "pickle.load", "dill.loads",
        ),
        DYNAMIC, HIGH, "executes or deserialises untrusted code",
    ),
    (
        (
            "shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
            "pathlib.Path.unlink", "pathlib.Path.rmdir",
        ),
        DESTRUCTIVE, HIGH, "deletes filesystem content",
    ),
    (
        (
            "socket.socket", "socket.create_connection", "socket.socketpair",
            "urllib.request.urlopen", "urllib.request.urlretrieve",
            "http.client.HTTPConnection", "http.client.HTTPSConnection", "ftplib.FTP",
            "smtplib.SMTP", "telnetlib.", "xmlrpc.client.", "requests.", "httpx.",
        ),
        NETWORK, HIGH, "opens an unauthorised network socket",
    ),
    (
        ("ctypes.CDLL", "ctypes.PyDLL", "ctypes.cdll.LoadLibrary", "ctypes.windll", "cffi.FFI"),
        NATIVE, HIGH, "bridges into native memory or libraries",
    ),
    (
        ("os.setuid", "os.setgid", "os.seteuid", "os.setegid", "os.chroot", "os.forkpty"),
        PRIVILEGE, HIGH, "changes privileges or escapes the sandbox",
    ),
)

#: (pattern, category, severity, message) applied to every string literal
STRING_RULES: Tuple[Tuple["re.Pattern[str]", str, str, str], ...] = (
    (re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/|~|\*)"), SHELL, CRITICAL, "destructive recursive delete command"),
    (re.compile(r":\s*\(\s*\)\s*\{"), SHELL, CRITICAL, "fork bomb payload"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), DESTRUCTIVE, CRITICAL, "filesystem format command"),
    (re.compile(r"dd\s+if=/dev/(?:zero|urandom)"), DESTRUCTIVE, CRITICAL, "raw device overwrite command"),
    (re.compile(r"/etc/shadow"), SECRETS, HIGH, "targets the system password database"),
    (re.compile(r"\bauthorized_keys\b"), SECRETS, CRITICAL, "tampers with SSH authorisation"),
    (re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b"), SHELL, CRITICAL, "pipes a remote download into a shell"),
    (re.compile(r"/dev/tcp/"), NETWORK, CRITICAL, "raw reverse shell channel"),
    (re.compile(r"\bnc\s+-[a-z]*e\b"), NETWORK, HIGH, "reverse shell via netcat"),
    (re.compile(r"\bshred\s+-"), DESTRUCTIVE, HIGH, "irreversible data destruction"),
    (re.compile(r"\bhistory\s+-c\b"), DESTRUCTIVE, MEDIUM, "erases audit history"),
    (re.compile(r"\bdrop\s+table\b", re.IGNORECASE), DESTRUCTIVE, MEDIUM, "destructive SQL statement"),
)

#: hard-coded credential shapes
SECRET_RULES: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
)

SECRET_ENV_RE = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|CREDENTIAL|ACCESS_KEY)", re.IGNORECASE
)
POLICY_RE = re.compile(r"#\s*vajra-allow\s*:\s*(?P<caps>[A-Za-z0-9_,\s]+)", re.IGNORECASE)
SOURCE_ENV_RE = re.compile(r"\b(?:os\.environ|os\.getenv|environ\b)")

IMMUTABLE: Tuple[str, ...] = (SECURITY_MODULE,)
PROTECTED_DIRS: Tuple[str, ...] = ("core",)

#: module imports that are suspicious on their own
GUARDED_IMPORTS: Mapping[str, Tuple[str, str, str]] = {
    "ctypes": (NATIVE, MEDIUM, "imports a native-code bridge"),
    "cffi": (NATIVE, MEDIUM, "imports a native-code bridge"),
    "telnetlib": (NETWORK, LOW, "imports a plaintext remote shell protocol"),
}


class SecurityViolation(Exception):
    """Raised when code fails the static firewall."""

    def __init__(self, message: str, violations: Sequence["Violation"] = ()) -> None:
        super().__init__(message)
        self.violations: Tuple["Violation", ...] = tuple(violations)


class IntegrityError(Exception):
    """Raised when the trusted core no longer matches its sealed digests."""


@dataclass(frozen=True)
class Violation:
    """One firewall finding."""

    rule: str
    category: str
    severity: str
    message: str
    filename: str = "<memory>"
    lineno: int = 0
    snippet: str = ""

    @property
    def capability(self) -> str:
        return CAPABILITY.get(self.category, self.category)

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "filename": self.filename,
            "lineno": self.lineno,
            "snippet": self.snippet,
        }

    def describe(self) -> str:
        where = f"{self.filename}:{self.lineno}" if self.lineno else self.filename
        return f"[{self.severity.upper()}] {self.category} ({where}) {self.message}"


@dataclass(frozen=True)
class ScanResult:
    """Outcome of scanning one source unit."""

    filename: str
    violations: Tuple[Violation, ...] = ()
    waived: Tuple[Violation, ...] = ()
    capabilities: FrozenSet[str] = frozenset()
    syntax_error: Optional[str] = None

    @property
    def blocking(self) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    @property
    def ok(self) -> bool:
        return not self.blocking and self.syntax_error is None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "ok": self.ok,
            "syntax_error": self.syntax_error,
            "blocking": [v.as_dict() for v in self.blocking],
            "violations": [v.as_dict() for v in self.violations],
            "waived": [v.as_dict() for v in self.waived],
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True)
class IntegrityReport:
    """Result of verifying the trusted core against its sealed manifest."""

    ok: bool
    status: str
    checked: Tuple[str, ...] = ()
    modified: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    unexpected: Tuple[str, ...] = ()
    bundle_sha256: str = ""
    expected_bundle: str = ""
    sealed_at: str = ""
    lock_path: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "checked": list(self.checked),
            "modified": list(self.modified),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "bundle_sha256": self.bundle_sha256,
            "expected_bundle": self.expected_bundle,
            "sealed_at": self.sealed_at,
            "lock_path": self.lock_path,
        }

    def describe(self) -> str:
        if self.status == "unsealed":
            return "integrity: UNSEALED (no security_lock.json; run SecurityGuard.seal())"
        if self.ok:
            return f"integrity: OK ({len(self.checked)} file(s), bundle {self.bundle_sha256[:12]})"
        parts: List[str] = []
        if self.modified:
            parts.append("modified=" + ", ".join(self.modified))
        if self.missing:
            parts.append("missing=" + ", ".join(self.missing))
        if self.unexpected:
            parts.append("unexpected=" + ", ".join(self.unexpected))
        return "integrity: DRIFT (" + "; ".join(parts) + ")"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_digest(files: Mapping[str, str]) -> str:
    payload = "\n".join(f"{name}:{files[name]}" for name in sorted(files))
    return sha256_text(payload)


def _dotted_name(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _matches_prefix(name: str, prefix: str) -> bool:
    if prefix.endswith("."):
        return name.startswith(prefix)
    return name == prefix or name.startswith(prefix + ".")
