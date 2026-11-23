"""Pure-stdlib GitHub REST client and shared-memory synchroniser.

Vajrastra keeps one episodic ledger, ``memory_bank/lessons.sqlite``.  This
module makes that single file the shared brain of Android, Parrot OS, Linux
and Colab by moving it through the GitHub Contents API:

* read files, create or update them with base64 commits;
* push a set of patches to a branch (creating the branch when needed);
* pull the remote ledger, merge it into the local ledger, then push the merged
  database back as one consistent SQLite snapshot.

Only :mod:`urllib.request` and :mod:`json` touch the network, and the token is
read from the environment and never printed, logged or written to disk.  The
capability pragma is the narrowest grant the AST firewall accepts.
"""

from __future__ import annotations

# vajra-allow: network, exfiltration

import base64
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from core.memory_engine import MemoryEngine

DEFAULT_REPO = "teamhecked-Ayu/Vajrastra"
DEFAULT_BRANCH = "main"
DEFAULT_API_ROOT = "https://api.github.com"
DEFAULT_DB_PATH = "memory_bank/lessons.sqlite"
TOKEN_ENV_VARS: Tuple[str, ...] = ("GITHUB_TOKEN", "VAJRASTRA_GITHUB_TOKEN", "GH_TOKEN")
USER_AGENT = "vajrastra/0.2 (+stdlib; github-sync)"
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_FILE_BYTES = 40 * 1024 * 1024

# (status, lower-cased headers, body text)
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Tuple[int, Mapping[str, str], str]]


class GitHubError(RuntimeError):
    """Raised for a failed GitHub API call (with its HTTP status when known)."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_token(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the first GitHub token present in the environment."""
    source = env if env is not None else os.environ
    for name in TOKEN_ENV_VARS:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _lower_headers(headers: Any) -> Dict[str, str]:
    if not headers:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except (AttributeError, TypeError):
        return {}


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Tuple[int, Mapping[str, str], str]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "replace")
            return int(getattr(response, "status", 200)), _lower_headers(response.headers), payload
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return int(exc.code), _lower_headers(exc.headers), raw.decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise GitHubError(f"network error contacting GitHub: {exc.reason}") from exc


def encode_content(content: Union[str, bytes]) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return base64.b64encode(raw).decode("ascii")


def decode_content(encoded: str) -> bytes:
    return base64.b64decode("".join((encoded or "").split()))


@dataclass(frozen=True)
class RemoteFile:
    """One file fetched from the repository."""

    path: str
    sha: str = ""
    data: bytes = b""
    size: int = 0

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", "replace")

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha": self.sha, "size": self.size}


@dataclass(frozen=True)
class CommitResult:
    """Outcome of one Contents API write."""

    path: str
    commit_sha: str = ""
    content_sha: str = ""
    created: bool = False
    branch: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "commit_sha": self.commit_sha,
            "content_sha": self.content_sha,
            "created": self.created,
            "branch": self.branch,
        }


@dataclass(frozen=True)
class MemorySyncReport:
    """What a ledger sync actually moved."""

    pulled_lessons: int = 0
    pulled_skills: int = 0
    pushed: bool = False
    pushed_bytes: int = 0
    remote_sha: str = ""
    commit_sha: str = ""
    remote_path: str = DEFAULT_DB_PATH
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pulled_lessons": self.pulled_lessons,
            "pulled_skills": self.pulled_skills,
            "pushed": self.pushed,
            "pushed_bytes": self.pushed_bytes,
            "remote_sha": self.remote_sha,
            "commit_sha": self.commit_sha,
            "remote_path": self.remote_path,
            "notes": list(self.notes),
        }


class GitHubClient:
    """Minimal GitHub REST client: contents, commits, branches."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        repo: str = DEFAULT_REPO,
        branch: str = DEFAULT_BRANCH,
        api_root: str = DEFAULT_API_ROOT,
        transport: Optional[Transport] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        timeout: float = 45.0,
        retries: int = 3,
        base_delay: float = 1.0,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._explicit_token = token.strip() if token else None
        self.repo = repo.strip("/") or DEFAULT_REPO
        self.branch = branch or DEFAULT_BRANCH
        self.api_root = api_root.rstrip("/")
        self._transport = transport or _urllib_transport
        self._sleeper = sleeper or time.sleep
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.base_delay = max(0.0, float(base_delay))
        self._env = env

    # ------------------------------------------------------------------ config
    def token(self) -> Optional[str]:
        return self._explicit_token or resolve_token(self._env)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "token-set" if self.token() else "no-token"
        return f"GitHubClient(repo={self.repo!r}, branch={self.branch!r}, {state})"

    def _headers(self) -> Dict[str, str]:
        token = self.token()
        if not token:
            raise GitHubError(
                "no GitHub token found; set GITHUB_TOKEN to read or write " + self.repo
            )
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": "Bearer " + token,
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------- REST
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.api_root + path

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        raw_body: Optional[bytes] = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        """Perform one REST call; retries 429/5xx, raises on other failures."""
        headers = self._headers()
        headers["Accept"] = accept
        body: Optional[bytes] = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self._url(path)

        last_status = 0
        last_text = ""
        for attempt in range(self.retries + 1):
            status, response_headers, text = self._transport(
                method, url, headers, body, self.timeout
            )
            last_status, last_text = status, text
            if 200 <= status < 300:
                if not text.strip():
                    return {}
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
            if status == 429 or status >= 500:
                if attempt < self.retries:
                    hint = response_headers.get("retry-after")
                    delay = self.base_delay * (2 ** attempt)
                    if hint:
                        try:
                            delay = max(delay, float(hint))
                        except ValueError:
                            pass
                    self._sleeper(min(30.0, delay))
                    continue
            raise GitHubError(
                f"GitHub {method} {path} failed (HTTP {status}): {text[:300]}", status
            )
        raise GitHubError(
            f"GitHub {method} {path} failed after retries (HTTP {last_status}): {last_text[:300]}",
            last_status,
        )

    # ------------------------------------------------------------------ files
    @staticmethod
    def _contents_path(path: str) -> str:
        return urllib.parse.quote(path.lstrip("/"), safe="/")

    def get_file(self, path: str, ref: Optional[str] = None) -> Optional[RemoteFile]:
        """Fetch one file; ``None`` when it does not exist on *ref*."""
        relative = path.lstrip("/")
        url = f"/repos/{self.repo}/contents/{self._contents_path(relative)}"
        if ref or self.branch:
            url += "?ref=" + urllib.parse.quote(ref or self.branch, safe="")
        try:
            data = self.request("GET", url)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(data, list):
            raise GitHubError(f"{relative} is a directory, not a file")
        if not isinstance(data, Mapping):
            raise GitHubError(f"unexpected contents response for {relative}")

        sha = str(data.get("sha") or "")
        size = int(data.get("size") or 0)
        encoded = data.get("content")
        if isinstance(encoded, str) and encoded.strip():
            raw = decode_content(encoded)
        elif sha:
            raw = self._get_blob(sha)
        else:
            raw = b""
        return RemoteFile(path=relative, sha=sha, data=raw, size=size or len(raw))

    def _get_blob(self, sha: str) -> bytes:
        data = self.request("GET", f"/repos/{self.repo}/git/blobs/{sha}")
        if isinstance(data, Mapping) and isinstance(data.get("content"), str):
            return decode_content(str(data["content"]))
        raise GitHubError(f"blob {sha} had no content")

    def read_text(self, path: str, ref: Optional[str] = None) -> Optional[str]:
        remote = self.get_file(path, ref)
        return remote.text if remote else None

    def put_file(
        self,
        path: str,
        content: Union[str, bytes],
        message: str,
        *,
        branch: Optional[str] = None,
        sha: Optional[str] = None,
    ) -> CommitResult:
        """Create or update one file with a base64 commit."""
        target_branch = branch or self.branch
        relative = path.lstrip("/")
        existing_sha = sha
        if existing_sha is None:
            try:
                existing = self.get_file(relative, target_branch)
            except GitHubError as exc:
                if exc.status == 404:
                    existing = None
                else:
                    raise
            existing_sha = existing.sha if existing else None

        payload: Dict[str, Any] = {
            "message": message,
            "content": encode_content(content),
            "branch": target_branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha
        data = self.request(
            "PUT",
            f"/repos/{self.repo}/contents/{self._contents_path(relative)}",
            payload,
        )
        commit = (data or {}).get("commit") or {}
        entry = (data or {}).get("content") or {}
        return CommitResult(
            path=relative,
            commit_sha=str(commit.get("sha") or ""),
            content_sha=str(entry.get("sha") or ""),
            created=not existing_sha,
            branch=target_branch,
        )

    def write_file(
        self, path: str, content: Union[str, bytes], message: str, *, branch: Optional[str] = None
    ) -> CommitResult:
        return self.put_file(path, content, message, branch=branch)

    def commit_files(
        self,
        files: Mapping[str, Union[str, bytes]],
        message: str,
        *,
        branch: Optional[str] = None,
    ) -> List[CommitResult]:
        """Write several files; one Contents API commit per file."""
        results: List[CommitResult] = []
        for path, content in files.items():
            results.append(
                self.put_file(path, content, f"{message} ({path})", branch=branch)
            )
        return results

    # ---------------------------------------------------------------- branches
    def branch_sha(self, branch: str) -> Optional[str]:
        try:
            data = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(data, Mapping):
            obj = data.get("object") or {}
            if isinstance(obj, Mapping) and obj.get("sha"):
                return str(obj["sha"])
        return None

    def ensure_branch(self, branch: str, *, from_branch: Optional[str] = None) -> bool:
        """Create *branch* from *from_branch* when missing; True when created."""
        if self.branch_sha(branch):
            return False
        base = from_branch or self.branch
        base_sha = self.branch_sha(base)
        if not base_sha:
            raise GitHubError(f"cannot create branch {branch}: base {base} not found")
        self.request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        return True

    def push_patch(
        self,
        branch: str,
        patches: Mapping[str, Union[str, bytes]],
        message: str,
        *,
        from_branch: Optional[str] = None,
    ) -> List[CommitResult]:
        """Ensure *branch* exists, then commit every patch onto it."""
        self.ensure_branch(branch, from_branch=from_branch)
        return self.commit_files(patches, message, branch=branch)

    # ----------------------------------------------------------------- memory
    def pull_memory(
        self,
        engine: MemoryEngine,
        *,
        remote_path: str = DEFAULT_DB_PATH,
        ref: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Download the remote ledger and merge it into *engine*."""
        remote = self.get_file(remote_path, ref or self.branch)
        if remote is None:
            return (0, 0)
        counts = merge_database_bytes(engine, remote.data)
        return counts["lessons"], counts["skills"]

    def push_memory(
        self,
        engine: MemoryEngine,
        *,
        remote_path: str = DEFAULT_DB_PATH,
        branch: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Tuple[int, CommitResult]:
        """Upload a consistent SQLite snapshot of *engine*."""
        payload = export_database_bytes(engine.db_path)
        result = self.put_file(
            remote_path,
            payload,
            message or f"chore(memory): sync lessons.sqlite ({len(payload)} bytes)",
            branch=branch,
        )
        return len(payload), result

    def sync_memory(
        self,
        engine: MemoryEngine,
        *,
        push: bool = True,
        pull: bool = True,
        remote_path: str = DEFAULT_DB_PATH,
        branch: Optional[str] = None,
    ) -> MemorySyncReport:
        """Merge the remote ledger first, then publish the merged result."""
        lessons = skills = 0
        notes: List[str] = []
        remote_sha = ""
        target_branch = branch or self.branch
        if pull:
            remote = self.get_file(remote_path, target_branch)
            if remote is None:
                notes.append(f"{remote_path} does not exist on {target_branch} yet")
            else:
                remote_sha = remote.sha
                counts = merge_database_bytes(engine, remote.data)
                lessons, skills = counts["lessons"], counts["skills"]
                notes.append(f"merged {lessons} lesson(s) and {skills} skill(s) from GitHub")
        pushed_bytes = 0
        commit_sha = ""
        pushed = False
        if push:
            pushed_bytes, result = self.push_memory(
                engine, remote_path=remote_path, branch=target_branch
            )
            commit_sha = result.commit_sha
            remote_sha = result.content_sha or remote_sha
            pushed = True
            notes.append(f"published {pushed_bytes} byte(s) to {target_branch}")
        return MemorySyncReport(
            pulled_lessons=lessons,
            pulled_skills=skills,
            pushed=pushed,
            pushed_bytes=pushed_bytes,
            remote_sha=remote_sha,
            commit_sha=commit_sha,
            remote_path=remote_path,
            notes=tuple(notes),
        )


def export_database_bytes(db_path: Path | str) -> bytes:
    """Return a transaction-consistent SQLite snapshot as bytes."""
    source_path = Path(db_path)
    if not source_path.is_file():
        raise GitHubError(f"ledger not found: {source_path}")
    with tempfile.TemporaryDirectory(prefix="vajra-db-") as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        source = sqlite3.connect(str(source_path), timeout=30.0)
        target = sqlite3.connect(str(snapshot))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        raw = snapshot.read_bytes()
    if not raw.startswith(SQLITE_MAGIC):
        raise GitHubError("snapshot is not a SQLite database")
    if len(raw) > MAX_FILE_BYTES:
        raise GitHubError(f"ledger snapshot is too large for the Contents API ({len(raw)} bytes)")
    return raw


def merge_database_bytes(engine: MemoryEngine, raw: bytes) -> Dict[str, int]:
    """Validate a downloaded ledger and merge it into *engine*."""
    if not raw.startswith(SQLITE_MAGIC):
        raise GitHubError("remote lessons.sqlite is not a valid SQLite database")
    with tempfile.TemporaryDirectory(prefix="vajra-remote-") as tmp:
        remote_path = Path(tmp) / "lessons.sqlite"
        remote_path.write_bytes(raw)
        try:
            remote = MemoryEngine(tmp, db_path=remote_path)
        except sqlite3.DatabaseError as exc:
            raise GitHubError(f"remote ledger is unreadable: {exc}") from exc
        try:
            snapshot = remote.export_snapshot()
        finally:
            remote.close()
    return engine.import_snapshot(snapshot)
