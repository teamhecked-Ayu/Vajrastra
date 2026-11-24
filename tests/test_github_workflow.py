from __future__ import annotations

import base64
import hashlib
import unittest
from types import SimpleNamespace

from engines.github_workflow import (
    ApprovalRequired,
    GitHubWorkflow,
    GitHubWorkflowError,
    StaleRepositorySnapshot,
)


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    ).hexdigest()


class FakeGitHubClient:
    repo = "owner/test-repo"
    branch = "main"

    def __init__(self, *, conclusion="success", truncated=False):
        self.branches = {"main": "base-sha"}
        self.files = {
            "src/service.py": b"def parse_timeout(value):\n    return int(value)\n",
            "tests/test_service.py": b"from src.service import parse_timeout\n\ndef test_timeout():\n    assert parse_timeout('5') == 5\n",
            "pyproject.toml": b"[project]\nname = 'sample'\n",
            "README.md": b"# Sample\n",
        }
        self.entries = [
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha(raw),
                "size": len(raw),
            }
            for path, raw in self.files.items()
        ]
        self.entries.append({
            "path": "private/link.py",
            "mode": "120000",
            "type": "blob",
            "sha": "symlink-sha",
            "size": 10,
        })
        self.truncated = truncated
        self.conclusion = conclusion
        self.calls = []
        self.ref_updates = []
        self.work_commit = None
        self.pull_payload = None
        self.dispatches = 0
        self._blob_count = 0
        self.artifacts = [{
            "name": "watchdog-verification-py3.12",
            "archive_download_url": "https://api.github.com/artifacts/77",
            "expired": False,
        }]

    def token(self):
        return "fake-token"

    def branch_sha(self, branch):
        return self.branches.get(branch)

    def ensure_branch(self, branch, *, from_branch=None):
        if branch in self.branches:
            return False
        base = from_branch or self.branch
        self.branches[branch] = self.branches[base]
        return True

    def get_file(self, path, ref=None):
        raw = self.files.get(path)
        if raw is None:
            return None
        return SimpleNamespace(path=path, sha=blob_sha(raw), data=raw, size=len(raw))

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.endswith("/git/commits/base-sha"):
            return {"tree": {"sha": "base-tree"}}
        if method == "GET" and "/git/trees/base-tree?recursive=1" in path:
            return {"truncated": self.truncated, "tree": self.entries}
        if method == "POST" and path.endswith("/git/blobs"):
            self._blob_count += 1
            decoded = base64.b64decode(payload["content"])
            return {"sha": f"blob-created-{self._blob_count}-{hashlib.sha1(decoded).hexdigest()[:8]}"}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "work-tree"}
        if method == "POST" and path.endswith("/git/commits"):
            self.work_commit = "work-commit"
            return {"sha": self.work_commit}
        if method == "PATCH" and "/git/refs/heads/" in path:
            branch = path.split("/git/refs/heads/", 1)[1]
            self.branches[branch] = payload["sha"]
            self.ref_updates.append(branch)
            return {"ref": f"refs/heads/{branch}"}
        if method == "POST" and "/actions/workflows/" in path:
            self.dispatches += 1
            return {}
        if method == "GET" and "/actions/runs?" in path:
            work_branch = next(name for name in self.branches if name.startswith("vajrastra/work/"))
            return {
                "workflow_runs": [{
                    "id": 77,
                    "path": ".github/workflows/watchdog.yml@refs/heads/" + work_branch,
                    "head_sha": self.work_commit,
                    "head_branch": work_branch,
                    "event": "push",
                    "status": "completed",
                    "conclusion": self.conclusion,
                    "html_url": "https://github.com/owner/test-repo/actions/runs/77",
                }]
            }
        if method == "GET" and path.endswith("/actions/runs/77/artifacts"):
            return {"artifacts": self.artifacts}
        if method == "POST" and path.endswith("/pulls"):
            self.pull_payload = payload
            return {
                "number": 9,
                "html_url": "https://github.com/owner/test-repo/pull/9",
                "state": "open",
                "draft": True,
            }
        raise AssertionError(f"unexpected fake GitHub request: {method} {path}")


class GitHubWorkflowTests(unittest.TestCase):
    def workflow(self, client=None, approvals=None):
        client = client or FakeGitHubClient()
        approvals = approvals if approvals is not None else []
        def approve(operation, fingerprint):
            approvals.append((operation, fingerprint))
            return True
        engine = GitHubWorkflow(
            client,
            approval_callback=approve,
            sleeper=lambda _: None,
            monotonic=iter([0.0, 0.1, 0.2, 0.3]).__next__,
        )
        return client, engine, approvals

    def plan(self, engine, client, branch="vajrastra/work/test-change"):
        snapshot = engine.inspect_branch("main", "fix service timeout parsing")
        plan = engine.create_plan(snapshot, "fix service timeout parsing", branch)
        return snapshot, plan

    def test_inspection_builds_safe_file_map_and_project_context_without_writes(self):
        client = FakeGitHubClient()
        engine = GitHubWorkflow(client)
        snapshot = engine.inspect_branch("main", "fix service timeout parsing")
        plan = engine.create_plan(snapshot, "fix service timeout parsing", "vajrastra/work/preview")

        paths = {item.path for item in snapshot.file_map}
        self.assertIn("src/service.py", paths)
        self.assertIn("tests/test_service.py", paths)
        self.assertNotIn("private/link.py", paths)
        self.assertIn("private/link.py", snapshot.unsafe_paths)
        self.assertIn("src/service.py", plan.selected_files)
        self.assertEqual(client.branches, {"main": "base-sha"})
        self.assertEqual(client.ref_updates, [])

    def test_prepare_changes_displays_diff_without_remote_mutation(self):
        client, engine, _ = self.workflow()
        _, plan = self.plan(engine, client)
        change = "def parse_timeout(value):\n    parsed = int(value)\n    if parsed < 0:\n        raise ValueError('timeout must be non-negative')\n    return parsed\n"
        changeset = engine.prepare_changes(plan, {"src/service.py": change})

        self.assertIn("--- a/src/service.py", changeset.diff)
        self.assertIn("+++ b/src/service.py", changeset.diff)
        self.assertIn("raise ValueError", changeset.diff)
        self.assertEqual(client.branches, {"main": "base-sha"})
        self.assertEqual(client.ref_updates, [])

    def test_full_task_requires_three_approvals_and_never_updates_main(self):
        client = FakeGitHubClient()
        approvals = []
        _, engine, _ = self.workflow(client, approvals)
        change = "def parse_timeout(value):\n    parsed = int(value)\n    if parsed < 0:\n        raise ValueError('timeout must be non-negative')\n    return parsed\n"

        result = engine.run_task(
            "main",
            "fix service timeout parsing",
            "vajrastra/work/test-change",
            {"src/service.py": change},
            commit_message="fix: validate timeout",
            pr_title="Fix timeout parsing",
            pr_body="Test-only workflow PR.",
            timeout_seconds=5,
            poll_interval=0.1,
        )

        self.assertEqual([item[0] for item in approvals], [
            "create_branch", "commit_push", "create_pull_request"
        ])
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.verification.head_sha, result.commit.commit_sha)
        self.assertEqual(result.verification.run_id, 77)
        self.assertEqual(result.verification.artifacts[0]["name"], "watchdog-verification-py3.12")
        self.assertEqual(result.pull_request.number, 9)
        self.assertEqual(result.pull_request.base_branch, "main")
        self.assertEqual(result.pull_request.head_branch, "vajrastra/work/test-change")
        self.assertEqual(client.branches["main"], "base-sha")
        self.assertEqual(client.branches["vajrastra/work/test-change"], "work-commit")
        self.assertEqual(client.ref_updates, ["vajrastra/work/test-change"])
        self.assertEqual(client.pull_payload["base"], "main")
        self.assertEqual(client.pull_payload["head"], "vajrastra/work/test-change")
        self.assertTrue(client.pull_payload["draft"])

    def test_no_approval_means_no_branch_or_commit_write(self):
        client = FakeGitHubClient()
        engine = GitHubWorkflow(client)
        snapshot = engine.inspect_branch("main", "fix service timeout parsing")
        plan = engine.create_plan(snapshot, "fix service timeout parsing", "vajrastra/work/denied")
        changeset = engine.prepare_changes(plan, {"src/service.py": "VALUE = 2\n"})

        with self.assertRaises(ApprovalRequired):
            engine.apply_changes(plan, changeset, message="should not commit")
        self.assertEqual(client.branches, {"main": "base-sha"})
        self.assertEqual(client.ref_updates, [])

    def test_commit_denial_leaves_only_the_approved_empty_work_branch(self):
        client = FakeGitHubClient()
        approvals = []
        def approve(operation, fingerprint):
            approvals.append(operation)
            return operation == "create_branch"
        engine = GitHubWorkflow(client, approval_callback=approve)
        snapshot = engine.inspect_branch("main", "fix service timeout parsing")
        plan = engine.create_plan(snapshot, "fix service timeout parsing", "vajrastra/work/commit-denied")
        changeset = engine.prepare_changes(plan, {"src/service.py": "VALUE = 3\n"})

        with self.assertRaises(ApprovalRequired):
            engine.apply_changes(plan, changeset, message="denied")
        self.assertEqual(approvals, ["create_branch", "commit_push"])
        self.assertEqual(client.branches["main"], "base-sha")
        self.assertEqual(client.branches["vajrastra/work/commit-denied"], "base-sha")
        self.assertEqual(client.ref_updates, [])

    def test_protected_paths_traversal_and_symlinks_are_rejected(self):
        client, engine, _ = self.workflow()
        _, plan = self.plan(engine, client)
        for path in ("../escape.py", "core/session_manager.py", "core/security_lock.json", "private/link.py", "node_modules/pkg/index.py"):
            with self.subTest(path=path):
                with self.assertRaises(GitHubWorkflowError):
                    engine.prepare_changes(plan, {path: "VALUE = 1\n"})

    def test_work_branch_name_is_restricted(self):
        for name in ("main", "feature/test", "vajrastra/work/../escape", "vajrastra/work/BadName"):
            with self.subTest(name=name):
                with self.assertRaises(GitHubWorkflowError):
                    GitHubWorkflow.validate_work_branch(name, "main")

    def test_stale_base_is_rejected_before_diff_or_write(self):
        client, engine, _ = self.workflow()
        _, plan = self.plan(engine, client)
        client.branches["main"] = "new-base-sha"
        with self.assertRaises(StaleRepositorySnapshot):
            engine.prepare_changes(plan, {"src/service.py": "VALUE = 4\n"})
        self.assertEqual(client.ref_updates, [])

    def test_failed_verification_blocks_pull_request(self):
        client = FakeGitHubClient(conclusion="failure")
        _, engine, _ = self.workflow(client)
        result = engine.run_task(
            "main",
            "fix service timeout parsing",
            "vajrastra/work/test-failure",
            {"src/service.py": "VALUE = 5\n"},
            commit_message="fix: failed test",
            pr_title="Must not open",
            pr_body="",
            timeout_seconds=5,
            poll_interval=0.1,
        )
        self.assertFalse(result.verification.passed)
        self.assertIsNone(result.pull_request)
        self.assertIsNone(client.pull_payload)
        self.assertEqual(client.branches["main"], "base-sha")

    def test_success_without_artifact_blocks_pull_request(self):
        client = FakeGitHubClient()
        client.artifacts = []
        _, engine, _ = self.workflow(client)
        result = engine.run_task(
            "main",
            "fix service timeout parsing",
            "vajrastra/work/no-artifact",
            {"src/service.py": "VALUE = 7\\n"},
            commit_message="fix: no artifact",
            pr_title="Must not open without logs",
            pr_body="",
            timeout_seconds=5,
            poll_interval=0.1,
        )
        self.assertEqual(result.verification.conclusion, "success")
        self.assertFalse(result.verification.passed)
        self.assertIsNone(result.pull_request)
        self.assertIsNone(client.pull_payload)

    def test_only_fixed_watchdog_verification_workflow_is_allowed(self):
        client, engine, _ = self.workflow()
        _, plan = self.plan(engine, client)
        changeset = engine.prepare_changes(plan, {"src/service.py": "VALUE = 6\n"})
        commit = engine.apply_changes(plan, changeset, message="approved")
        before = client.dispatches
        with self.assertRaises(GitHubWorkflowError):
            engine.verify_branch(plan, commit, workflow="untrusted.yml")
        self.assertEqual(client.dispatches, before)

    def test_truncated_tree_is_rejected(self):
        client = FakeGitHubClient(truncated=True)
        engine = GitHubWorkflow(client)
        with self.assertRaises(GitHubWorkflowError):
            engine.inspect_branch("main", "fix service timeout parsing")


if __name__ == "__main__":
    unittest.main()
