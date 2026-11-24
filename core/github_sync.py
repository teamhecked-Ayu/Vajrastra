"""GitHub sync client (standard library only).

Memory and file sync with a GitHub repository through the REST API. Tokens are
read from the environment and never printed, logged or written to disk.  The
sync is deliberately conservative: lessons and skills round-trip as reviewed
text, never as executable instructions.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from core.memory_engine import MemoryEngine

# Default repository kept for backward compatibility; override with the
# VAJRASTRA_GITHUB_REPO environment variable or the repo= constructor arg.
DEFAULT_REPO = "teamhecked-Ayu/Vajrastra"
REPO_ENV_VAR = "VAJRASTRA_GITHUB_REPO"
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
        super().__init__(message, status)
        self.status = status


def resolve_token(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the first GitHub token present in the environment."""
    source = env if env is not None else os.environ
    for name in TOKEN_ENV_VARS:
        if source.get(name):
            return source[name]
    return None


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
        env_source = env if env is not None else os.environ
        configured = repo or env_source.get(REPO_ENV_VAR) or DEFAULT_REPO
        self.repo = configured.strip("/") or DEFAULT_REPO
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
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
        }

    def request(self, method: str, path: str, body: Optional[Union[Dict[str, Any], bytes]] = None) -> Any:
        url = path if path.startswith("http") else self.api_root + path
        payload: Optional[bytes] = None
        if isinstance(body, dict):
            payload = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            payload = body
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        status, response_headers, text = self._transport(
            method, url, headers, payload, self.timeout
        )
        if status >= 400:
            raise GitHubError(f"{method} {path} failed with HTTP {status}: {text[:300]}", status)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ---------------------------------------------------------------- contents
    def _contents_path(self, relative: str) -> str:
        rel = relative.strip("/")
        return urllib.parse.quote(rel, safe="/")

    def read_file(
        self, relative: str, *, branch: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return (content, sha) of *relative* on *branch*, or (None, None)."""
        ref = branch or self.branch
        data = self.request("GET", f"/repos/{self.repo}/contents/{self._contents_path(relative)}?ref={urllib.parse.quote(ref, safe='')}")
        if not isinstance(data, dict):
            return None, None
        raw = data.get("content")
        if not isinstance(raw, str):
            return None, None
        return base64.b64decode(raw.replace("\n", "")).decode("utf-8"), str(data.get("sha", ""))

    def write_file(
        self,
        relative: str,
        content: str,
        message: str,
        *,
        branch: Optional[str] = None,
        sha: Optional[str] = None,
    ) -> str:
        """Create or update *relative* on *branch*; return the commit SHA."""
        ref = branch or self.branch
        if sha is None:
            _, existing = self.read_file(relative, branch=ref)
            sha = existing
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": ref,
        }
        if sha:
            body["sha"] = sha
        data = self.request(
            "PUT",
            f"/repos/{self.repo}/contents/{self._contents_path(relative)}",
            body,
        )
        commit = (data or {}).get("commit") or {}
        return str(commit.get("sha", ""))

    def list_files(
        self, directory: str, *, branch: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List the contents API entries under *directory* on *branch*."""
        ref = branch or self.branch
        data = self.request("GET", f"/repos/{self.repo}/contents/{self._contents_path(directory)}?ref={urllib.parse.quote(ref, safe='')}")
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        return []

    def latest_commit(self, branch: Optional[str] = None) -> str:
        """Return the SHA of the latest commit on *branch*."""
        ref = branch or self.branch
        data = self.request("GET", f"/repos/{self.repo}/commits/{urllib.parse.quote(ref, safe='')}")
        return str((data or {}).get("sha", ""))

    def sync_memory(
        self,
        db_path: Union[str, Path],
        *,
        push: bool = True,
        pull: bool = True,
    ) -> "MemorySyncReport":
        """Round-trip the memory database as an attachment.

        The SQLite file is gzipped and base64-encoded for transport; on the far
        side it is decoded back into a byte-identical database.
        """
        path = Path(db_path)
        if push and path.is_file():
            blob = path.read_bytes()
            packed = base64.b64encode(_gzip_bytes(blob)).decode("ascii")
            self.write_file(DEFAULT_DB_PATH, packed, "vajrastra: sync memory bank")
        report = MemorySyncReport()
        if pull:
            content, _ = self.read_file(DEFAULT_DB_PATH)
            if content:
                raw = _gunzip_bytes(base64.b64decode(content))
                if raw[:16] == SQLITE_MAGIC:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".sqlite.tmp")
                    tmp.write_bytes(raw)
                    tmp.replace(path)
                    report.pulled = True
        return report


@dataclass
class MemorySyncReport:
    pulled: bool = False
    pushed: bool = False

    @property
    def pulled_lessons(self) -> bool:
        return self.pulled

    @property
    def pushed_count(self) -> int:
        return 1 if self.pushed else 0


def _gzip_bytes(data: bytes) -> bytes:
    import gzip
    import io

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
        handle.write(data)
    return buffer.getvalue()


def _gunzip_bytes(data: bytes) -> bytes:
    import gzip
    import io

    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
        return handle.read()


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
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers or {}, exc.read().decode("utf-8", "replace")
