"""Approval-gated GitHub repository workflows.

This module keeps repository reads separate from remote writes. It never merges
or deletes branches. Mutations are restricted to fresh vajrastra/work/* branches
and require a fresh approval callback for each operation.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

from engines.project_understanding import IndexLimits, ProjectIndex


MAX_TREE_FILES = 20000
MAX_CANDIDATE_FILES = 120
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PATCH_FILES = 40
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_DIFF_CHARS = 60000
DEFAULT_WORKFLOW = "github-task-verify.yml"
PROTECTED_PATHS = ("core/",)
PROTECTED_FILES = {"AGENTS.md", "core/security_lock.json"}
EXCLUDED_PARTS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "dist", "build", ".mypy_cache", ".pytest_cache",
}
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "tox.ini", "pytest.ini",
    "package.json", "Cargo.toml", "go.mod",
}


class GitHubWorkflowError(RuntimeError):
    """Raised when a GitHub workflow cannot be performed safely."""


class ApprovalRequired(GitHubWorkflowError):
    """Raised when a required human approval is missing or denied."""


class StaleRepositorySnapshot(GitHubWorkflowError):
    """Raised when the selected branch changes after inspection."""


@dataclass(frozen=True)
class WorkflowLimits:
    max_tree_files: int = MAX_TREE_FILES
    max_candidate_files: int = MAX_CANDIDATE_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_patch_files: int = MAX_PATCH_FILES
    max_patch_bytes: int = MAX_PATCH_BYTES
    max_diff_chars: int = MAX_DIFF_CHARS
    max_context_files: int = 24
    max_context_chars: int = 48000

    def validate(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or value <= 0:
                raise GitHubWorkflowError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    sha: str
    size: int
    mode: str
    category: str


@dataclass
class RepositorySnapshot:
    repo: str
    branch: str
    commit_sha: str
    tree_sha: str
    file_map: List[RepositoryFile]
    contents: Dict[str, bytes]
    index_context: Dict[str, Any]
    context_text: str
    warnings: List[str] = field(default_factory=list)
    unsafe_paths: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowPlan:
    snapshot: RepositorySnapshot
    instruction: str
    work_branch: str
    steps: Sequence[str]
    selected_files: Sequence[str]
    fingerprint: str


@dataclass
class ChangeSet:
    plan_fingerprint: str
    changes: Dict[str, bytes]
    diff: str
    fingerprint: str
    total_bytes: int


@dataclass(frozen=True)
class CommitPushResult:
    branch: str
    base_sha: str
    commit_sha: str
    changed_paths: Sequence[str]


@dataclass(frozen=True)
class VerificationResult:
    run_id: Optional[int]
    status: str
    conclusion: Optional[str]
    html_url: str
    artifacts: Sequence[Mapping[str, Any]] = ()

    @property
    def passed(self) -> bool:
        return self.status == "completed" and self.conclusion == "success"


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    html_url: str
    state: str
    draft: bool
    head_branch: str
    base_branch: str


@dataclass
class WorkflowTaskResult:
    snapshot: RepositorySnapshot
    plan: WorkflowPlan
    changeset: ChangeSet
    commit: CommitPushResult
    verification: VerificationResult
    pull_request: Optional[PullRequestResult]


def _safe_repo_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or raw.startswith("/"):
        raise GitHubWorkflowError(f"unsafe repository path: {raw!r}")
    if "\x00" in raw:
        raise GitHubWorkflowError("repository path contains a NUL byte")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GitHubWorkflowError(f"unsafe repository path: {raw!r}")
    if ".git" in parts:
        raise GitHubWorkflowError("Git metadata paths are never exposed")
    return "/".join(parts)


def _excluded_path(path: str) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.split("/"))


def _terms(value: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9]+", value.lower()) if len(item) > 1}


def _path_score(path: str, terms: set[str]) -> int:
    path_terms = _terms(path.replace("_", " ").replace("-", " ").replace("/", " "))
    return 3 * len(path_terms & terms)


def _category(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".py"):
        return "test" if "/test" in lower or lower.startswith("tests/") else "python"
    if PurePosixPath(path).name in CONFIG_NAMES or lower.startswith(".github/"):
        return "config"
    if lower.endswith((".md", ".rst", ".txt")):
        return "documentation"
    return "file"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GitHubWorkflow:
    """Inspect a remote branch, prepare a diff, and perform approved GitHub actions.

    approval_callback receives (operation, fingerprint) and must return True
    for that one operation. The default is deny. Library callers cannot
    accidentally write to GitHub by omitting approval.
    """

    def __init__(
        self,
        client: Any,
        *,
        approval_callback: Optional[Callable[[str, str], bool]] = None,
        approval_actor: str = "human-operator",
        limits: Optional[WorkflowLimits] = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.approval_callback = approval_callback
        self.approval_actor = approval_actor
        self.limits = limits or WorkflowLimits()
        self.limits.validate()
        self._sleep = sleeper
        self._monotonic = monotonic

    @property
    def repo(self) -> str:
        value = str(getattr(self.client, "repo", "")).strip()
        if not value or "/" not in value:
            raise GitHubWorkflowError("GitHub client must use owner/name repository format")
        return value

    def _approve(self, operation: str, fingerprint: str) -> None:
        callback = self.approval_callback
        if callback is None or callback(operation, fingerprint) is not True:
            raise ApprovalRequired(
                f"approval required for {operation}; no GitHub write was performed"
            )

    @staticmethod
    def validate_work_branch(branch: str, base_branch: str) -> str:
        if not isinstance(branch, str) or branch == base_branch:
            raise GitHubWorkflowError("work branch must differ from the selected base branch")
        if not re.fullmatch(r"vajrastra/work/[a-z0-9][a-z0-9._/-]{0,80}", branch):
            raise GitHubWorkflowError(
                "work branch must use the vajrastra/work/<lowercase-slug> prefix"
            )
        if "//" in branch or ".." in branch or branch.endswith("/"):
            raise GitHubWorkflowError("work branch contains an unsafe path segment")
        return branch

    def inspect_branch(
        self,
        branch: str,
        instruction: str,
    ) -> RepositorySnapshot:
        """Read a bounded branch inventory and build a read-only Python index."""
        if not instruction or not instruction.strip():
            raise GitHubWorkflowError("task instruction must be non-empty")
        commit_sha = self.client.branch_sha(branch)
        if not commit_sha:
            raise GitHubWorkflowError(f"branch not found: {branch}")

        commit_data = self.client.request(
            "GET", f"/repos/{self.repo}/git/commits/{quote(commit_sha, safe='')}"
        )
        tree_sha = str((commit_data.get("tree") or {}).get("sha") or "")
        if not tree_sha:
            raise GitHubWorkflowError("branch commit did not include a tree SHA")

        tree_data = self.client.request(
            "GET",
            f"/repos/{self.repo}/git/trees/{quote(tree_sha, safe='')}?recursive=1",
        )
        if tree_data.get("truncated"):
            raise GitHubWorkflowError(
                "GitHub returned a truncated tree; refusing to plan from an incomplete file map"
            )
        entries = tree_data.get("tree")
        if not isinstance(entries, list):
            raise GitHubWorkflowError("GitHub tree response has no file list")
        if len(entries) > self.limits.max_tree_files:
            raise GitHubWorkflowError(
                f"repository tree exceeds the {self.limits.max_tree_files} file safety limit"
            )

        file_map: List[RepositoryFile] = []
        unsafe_paths: List[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("type") != "blob":
                continue
            raw_path = str(entry.get("path") or "")
            try:
                path = _safe_repo_path(raw_path)
            except GitHubWorkflowError:
                unsafe_paths.append(raw_path)
                continue
            if _excluded_path(path):
                continue
            mode = str(entry.get("mode") or "")
            if mode == "120000":
                unsafe_paths.append(path)
                continue
            if mode not in {"100644", "100755"}:
                continue
            file_map.append(
                RepositoryFile(
                    path=path,
                    sha=str(entry.get("sha") or ""),
                    size=int(entry.get("size") or 0),
                    mode=mode,
                    category=_category(path),
                )
            )
        file_map.sort(key=lambda item: item.path)

        terms = _terms(instruction)
        python_files = [item for item in file_map if item.path.endswith(".py")]
        tests = [item for item in python_files if item.category == "test"]
        configs = [
            item for item in file_map
            if item.category == "config" or PurePosixPath(item.path).name in CONFIG_NAMES
        ]
        sources = [item for item in python_files if item.category != "test"]
        sources.sort(key=lambda item: (-_path_score(item.path, terms), item.path))
        tests.sort(key=lambda item: (-_path_score(item.path, terms), item.path))
        configs.sort(key=lambda item: (item.path.count("/"), item.path))

        candidates: List[RepositoryFile] = []
        seen = set()

        def add(items: Sequence[RepositoryFile], limit: int) -> None:
            for item in items[:limit]:
                if item.path not in seen:
                    seen.add(item.path)
                    candidates.append(item)

        add(sources, min(70, self.limits.max_candidate_files))
        add(tests, min(30, self.limits.max_candidate_files - len(candidates)))
        add(configs, min(20, self.limits.max_candidate_files - len(candidates)))
        if len(candidates) < min(10, self.limits.max_candidate_files):
            add(python_files, self.limits.max_candidate_files - len(candidates))
        candidates = candidates[: self.limits.max_candidate_files]

        contents: Dict[str, bytes] = {}
        warnings: List[str] = []
        total_bytes = 0
        for item in candidates:
            if item.size > self.limits.max_file_bytes:
                warnings.append(f"skipped oversized file: {item.path}")
                continue
            if total_bytes + item.size > self.limits.max_total_bytes:
                warnings.append(f"repository context byte budget reached before {item.path}")
                break
            remote = self.client.get_file(item.path, ref=branch)
            if remote is None:
                warnings.append(f"file disappeared during inspection: {item.path}")
                continue
            if item.sha and remote.sha != item.sha:
                raise StaleRepositorySnapshot(
                    f"{item.path} changed while the selected branch was being indexed"
                )
            if len(remote.data) > self.limits.max_file_bytes:
                warnings.append(f"skipped oversized content: {item.path}")
                continue
            contents[item.path] = remote.data
            total_bytes += len(remote.data)

        with tempfile.TemporaryDirectory(prefix="vajrastra-github-index-") as tmp:
            root = Path(tmp)
            for rel, raw in contents.items():
                target = root.joinpath(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            index = ProjectIndex.build(
                root,
                commit_sha,
                limits=IndexLimits(
                    max_files=max(1, len(contents) + 1),
                    max_file_bytes=self.limits.max_file_bytes,
                    max_total_bytes=self.limits.max_total_bytes,
                    max_python_files=max(1, sum(path.endswith(".py") for path in contents)),
                ),
            )
            context = index.task_context(
                instruction,
                current_commit_sha=commit_sha,
                max_files=self.limits.max_context_files,
                max_chars=self.limits.max_context_chars,
            )
            selected = [str(path) for path in context.get("selected_files", [])]

        chunks: List[str] = []
        used = 0
        for rel in selected:
            raw = contents.get(rel)
            if raw is None:
                continue
            source = raw.decode("utf-8", errors="replace")
            block = f"--- {rel} ---\n{source.rstrip()}\n"
            if used + len(block) > self.limits.max_context_chars:
                break
            chunks.append(block)
            used += len(block)

        if self.client.branch_sha(branch) != commit_sha:
            raise StaleRepositorySnapshot(
                "selected branch moved while the repository inventory was being built"
            )
        warnings.extend(index.warnings)
        return RepositorySnapshot(
            repo=self.repo,
            branch=branch,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            file_map=file_map,
            contents=contents,
            index_context=context,
            context_text="\n".join(chunks),
            warnings=warnings,
            unsafe_paths=unsafe_paths,
        )

    def create_plan(
        self,
        snapshot: RepositorySnapshot,
        instruction: str,
        work_branch: str,
    ) -> WorkflowPlan:
        if snapshot.repo != self.repo:
            raise GitHubWorkflowError("snapshot belongs to a different repository")
        if not instruction or not instruction.strip():
            raise GitHubWorkflowError("task instruction must be non-empty")
        work_branch = self.validate_work_branch(work_branch, snapshot.branch)
        selected = tuple(str(item) for item in snapshot.index_context.get("selected_files", []))
        steps = (
            f"Inspect the selected branch at commit {snapshot.commit_sha[:12]}",
            "Change only task-relevant files on the dedicated work branch",
            "Add or update focused regression tests",
            "Run the repository verification workflow and retain its test log artifact",
            "Review the final diff before opening a draft pull request",
        )
        payload = {
            "repo": snapshot.repo,
            "base_branch": snapshot.branch,
            "base_sha": snapshot.commit_sha,
            "instruction": instruction.strip(),
            "work_branch": work_branch,
            "selected_files": list(selected),
        }
        fingerprint = _sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return WorkflowPlan(
            snapshot=snapshot,
            instruction=instruction.strip(),
            work_branch=work_branch,
            steps=steps,
            selected_files=selected,
            fingerprint=fingerprint,
        )

    def prepare_changes(
        self,
        plan: WorkflowPlan,
        changes: Mapping[str, Union[str, bytes]],
    ) -> ChangeSet:
        """Validate text changes and render a diff without writing to GitHub."""
        if not isinstance(changes, Mapping) or not changes:
            raise GitHubWorkflowError("changes must be a non-empty path-to-content mapping")
        if len(changes) > self.limits.max_patch_files:
            raise GitHubWorkflowError("patch contains too many files")
        if self.client.branch_sha(plan.snapshot.branch) != plan.snapshot.commit_sha:
            raise StaleRepositorySnapshot("base branch changed after the plan was prepared")

        normalized: Dict[str, bytes] = {}
        diff_chunks: List[str] = []
        total = 0
        unsafe = set(plan.snapshot.unsafe_paths)
        file_index = {item.path: item for item in plan.snapshot.file_map}

        for raw_path, value in sorted(changes.items()):
            path = _safe_repo_path(raw_path)
            if path in unsafe:
                raise GitHubWorkflowError(f"refusing unsafe repository path: {path}")
            if path in PROTECTED_FILES or any(path.startswith(prefix) for prefix in PROTECTED_PATHS):
                raise GitHubWorkflowError(f"protected path cannot be changed by this workflow: {path}")
            if PurePosixPath(path).suffix.lower() not in {
                ".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini",
                ".yaml", ".yml", ".json", ".sh",
            }:
                raise GitHubWorkflowError(f"only text source/config/document files are accepted: {path}")
            if isinstance(value, str):
                raw = value.encode("utf-8")
            elif isinstance(value, bytes):
                raw = value
            else:
                raise GitHubWorkflowError(f"change for {path} must be text or bytes")
            try:
                new_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitHubWorkflowError(f"change for {path} is not UTF-8 text") from exc
            if len(raw) > self.limits.max_file_bytes:
                raise GitHubWorkflowError(f"change exceeds the per-file limit: {path}")
            total += len(raw)
            if total > self.limits.max_patch_bytes:
                raise GitHubWorkflowError("patch exceeds the total byte limit")

            original = b""
            entry = file_index.get(path)
            if entry is not None:
                if entry.mode != "100644":
                    raise GitHubWorkflowError(f"refusing to replace non-regular file: {path}")
                original = plan.snapshot.contents.get(path, b"")
                if path not in plan.snapshot.contents:
                    remote = self.client.get_file(path, ref=plan.snapshot.branch)
                    if remote is None or (entry.sha and remote.sha != entry.sha):
                        raise StaleRepositorySnapshot(f"{path} changed after inspection")
                    original = remote.data
            try:
                old_text = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitHubWorkflowError(f"refusing to patch non-UTF-8 file: {path}") from exc
            diff_chunks.extend(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{path}" if entry else "/dev/null",
                    tofile=f"b/{path}",
                )
            )
            normalized[path] = raw

        rendered = "".join(diff_chunks)
        if not rendered:
            raise GitHubWorkflowError("patch contains no content changes")
        if len(rendered) > self.limits.max_diff_chars:
            raise GitHubWorkflowError("rendered diff exceeds the display safety limit")
        fingerprint_data = {
            "plan": plan.fingerprint,
            "changes": {path: _sha256(raw) for path, raw in normalized.items()},
        }
        fingerprint = _sha256(
            json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return ChangeSet(
            plan_fingerprint=plan.fingerprint,
            changes=normalized,
            diff=rendered,
            fingerprint=fingerprint,
            total_bytes=total,
        )

    def apply_changes(
        self,
        plan: WorkflowPlan,
        changeset: ChangeSet,
        *,
        message: str,
    ) -> CommitPushResult:
        """Create a fresh work branch and push one atomic Git commit after approval."""
        if changeset.plan_fingerprint != plan.fingerprint:
            raise GitHubWorkflowError("change set does not belong to this plan")
        if self.client.branch_sha(plan.snapshot.branch) != plan.snapshot.commit_sha:
            raise StaleRepositorySnapshot("base branch changed before branch creation")
        if self.client.branch_sha(plan.work_branch):
            raise GitHubWorkflowError(
                f"work branch already exists; choose a fresh branch: {plan.work_branch}"
            )

        self._approve("create_branch", changeset.fingerprint)
        created = self.client.ensure_branch(plan.work_branch, from_branch=plan.snapshot.branch)
        if not created:
            raise GitHubWorkflowError("GitHub did not create the requested fresh work branch")
        parent_sha = self.client.branch_sha(plan.work_branch)
        if parent_sha != plan.snapshot.commit_sha:
            raise StaleRepositorySnapshot("work branch was not created from the inspected base SHA")

        self._approve("commit_push", changeset.fingerprint)
        commit_data = self.client.request(
            "GET", f"/repos/{self.repo}/git/commits/{quote(parent_sha, safe='')}"
        )
        base_tree = str((commit_data.get("tree") or {}).get("sha") or "")
        if not base_tree:
            raise GitHubWorkflowError("base commit has no tree")

        tree_entries = []
        for path, raw in sorted(changeset.changes.items()):
            blob = self.client.request(
                "POST",
                f"/repos/{self.repo}/git/blobs",
                {
                    "content": base64.b64encode(raw).decode("ascii"),
                    "encoding": "base64",
                },
            )
            blob_sha = str(blob.get("sha") or "")
            if not blob_sha:
                raise GitHubWorkflowError(f"GitHub did not return a blob SHA for {path}")
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
            )

        tree = self.client.request(
            "POST",
            f"/repos/{self.repo}/git/trees",
            {"base_tree": base_tree, "tree": tree_entries},
        )
        tree_sha = str(tree.get("sha") or "")
        if not tree_sha:
            raise GitHubWorkflowError("GitHub did not return a new tree SHA")
        commit = self.client.request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            {
                "message": message.strip() or "chore: apply approved repository task",
                "tree": tree_sha,
                "parents": [parent_sha],
            },
        )
        commit_sha = str(commit.get("sha") or "")
        if not commit_sha:
            raise GitHubWorkflowError("GitHub did not return a commit SHA")

        ref_path = f"/repos/{self.repo}/git/refs/heads/{quote(plan.work_branch, safe='/')}"
        self.client.request("PATCH", ref_path, {"sha": commit_sha, "force": False})
        if self.client.branch_sha(plan.work_branch) != commit_sha:
            raise GitHubWorkflowError("work branch ref did not advance to the approved commit")
        return CommitPushResult(
            branch=plan.work_branch,
            base_sha=parent_sha,
            commit_sha=commit_sha,
            changed_paths=tuple(sorted(changeset.changes)),
        )

    def verify_branch(
        self,
        plan: WorkflowPlan,
        commit: CommitPushResult,
        *,
        workflow: str = DEFAULT_WORKFLOW,
        timeout_seconds: float = 900.0,
        poll_interval: float = 15.0,
    ) -> VerificationResult:
        """Dispatch a fixed repository test workflow and collect its artifact links."""
        if commit.branch != plan.work_branch:
            raise GitHubWorkflowError("commit result belongs to a different work branch")
        if self.client.branch_sha(plan.work_branch) != commit.commit_sha:
            raise StaleRepositorySnapshot("work branch moved after the approved commit")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
            raise GitHubWorkflowError("workflow must be a workflow filename, not an arbitrary path")
        self.client.request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{quote(workflow, safe='')}/dispatches",
            {"ref": plan.work_branch, "inputs": {"target_ref": plan.work_branch}},
        )

        deadline = self._monotonic() + max(1.0, timeout_seconds)
        query = (
            f"/repos/{self.repo}/actions/runs?branch={quote(plan.work_branch, safe='')}"
            "&event=workflow_dispatch&per_page=50"
        )
        while self._monotonic() < deadline:
            response = self.client.request("GET", query)
            runs = response.get("workflow_runs") or []
            matching = [
                run for run in runs
                if isinstance(run, Mapping)
                and run.get("head_sha") == commit.commit_sha
                and run.get("event") == "workflow_dispatch"
                and str(run.get("path") or "").split("@", 1)[0].endswith(workflow)
            ]
            matching.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
            if matching:
                run = matching[0]
                if run.get("status") == "completed":
                    run_id = int(run.get("id") or 0)
                    artifact_response = self.client.request(
                        "GET", f"/repos/{self.repo}/actions/runs/{run_id}/artifacts"
                    )
                    artifacts = [
                        {
                            "name": str(item.get("name") or ""),
                            "url": str(item.get("archive_download_url") or ""),
                            "expired": bool(item.get("expired", False)),
                        }
                        for item in (artifact_response.get("artifacts") or [])
                        if isinstance(item, Mapping) and not item.get("expired", False)
                    ]
                    return VerificationResult(
                        run_id=run_id,
                        status="completed",
                        conclusion=str(run.get("conclusion") or "failure"),
                        html_url=str(run.get("html_url") or ""),
                        artifacts=tuple(artifacts),
                    )
            self._sleep(max(0.1, poll_interval))
        return VerificationResult(
            run_id=None,
            status="timeout",
            conclusion=None,
            html_url="",
            artifacts=(),
        )

    def create_pull_request(
        self,
        plan: WorkflowPlan,
        verification: VerificationResult,
        *,
        title: str,
        body: str,
        draft: bool = True,
    ) -> PullRequestResult:
        """Open a draft PR only after tests pass and the operator approves it."""
        if not verification.passed:
            raise GitHubWorkflowError("pull request blocked: verification has not passed")
        if plan.work_branch == plan.snapshot.branch:
            raise GitHubWorkflowError("pull request head and base must differ")
        if not title.strip():
            raise GitHubWorkflowError("pull request title must be non-empty")
        self._approve("create_pull_request", plan.fingerprint)
        response = self.client.request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {
                "title": title.strip(),
                "head": plan.work_branch,
                "base": plan.snapshot.branch,
                "body": body,
                "draft": bool(draft),
            },
        )
        return PullRequestResult(
            number=int(response.get("number") or 0),
            html_url=str(response.get("html_url") or ""),
            state=str(response.get("state") or "open"),
            draft=bool(response.get("draft", draft)),
            head_branch=plan.work_branch,
            base_branch=plan.snapshot.branch,
        )

    def run_task(
        self,
        base_branch: str,
        instruction: str,
        work_branch: str,
        changes: Mapping[str, Union[str, bytes]],
        *,
        commit_message: str,
        pr_title: str,
        pr_body: str,
        workflow: str = DEFAULT_WORKFLOW,
        timeout_seconds: float = 900.0,
        poll_interval: float = 15.0,
    ) -> WorkflowTaskResult:
        """Run the complete inspect -> diff -> approve -> push -> test -> PR flow."""
        snapshot = self.inspect_branch(base_branch, instruction)
        plan = self.create_plan(snapshot, instruction, work_branch)
        changeset = self.prepare_changes(plan, changes)
        commit = self.apply_changes(plan, changeset, message=commit_message)
        verification = self.verify_branch(
            plan,
            commit,
            workflow=workflow,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
        pull_request = None
        if verification.passed:
            pull_request = self.create_pull_request(
                plan, verification, title=pr_title, body=pr_body, draft=True
            )
        return WorkflowTaskResult(
            snapshot=snapshot,
            plan=plan,
            changeset=changeset,
            commit=commit,
            verification=verification,
            pull_request=pull_request,
        )
