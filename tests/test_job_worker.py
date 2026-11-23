"""Job-worker tests: inbox parsing, job execution, policy and checkpoints."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine  # noqa: E402
from core.security_guard import SecurityGuard  # noqa: E402
from engines.job_worker import Job, JobWorker, WorkerError  # noqa: E402


class StubRemote:
    def __init__(self, text: str, sha: str = "sha") -> None:
        self.text = text
        self.sha = sha


class StubClient:
    """In-memory stand-in for :class:`core.github_sync.GitHubClient`."""

    repo = "teamhecked-Ayu/Vajrastra"
    branch = "main"

    def __init__(self, jobs=None, token: str = "token") -> None:
        self.jobs = jobs or {}
        self._token = token
        self.puts = []
        self.paths = []
        self.existing_results = {}

    def token(self):
        return self._token

    def request(self, method, path, payload=None):
        self.paths.append((method, path))
        return [
            {
                "name": f"{key}.json",
                "path": f"workspace/inbox/{key}.json",
                "type": "file",
                "sha": "sha",
            }
            for key in self.jobs
        ]

    def get_file(self, path, ref=None):
        key = Path(path).stem
        if path.startswith("workspace/outbox/"):
            return self.existing_results.get(path)
        if key not in self.jobs:
            return None
        return StubRemote(json.dumps(self.jobs[key]))

    def put_file(self, path, content, message, *, branch=None):
        self.puts.append((path, content, message))
        return SimpleNamespace(commit_sha="checkpoint123", path=path)

    def sync_memory(self, *args, **kwargs):
        return SimpleNamespace(pulled_lessons=1, pushed=True, commit_sha="sync123")


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.guard = SecurityGuard(self.root)
        self.memory = MemoryEngine(self.root)

    def tearDown(self) -> None:
        self.memory.close()
        self._tmp.cleanup()

    def worker(self, client=None, **kwargs) -> JobWorker:
        return JobWorker(
            self.root,
            client=client,
            guard=self.guard,
            memory=self.memory,
            **kwargs,
        )


class InboxTests(WorkerTestCase):
    def test_pending_jobs_are_parsed(self) -> None:
        client = StubClient(
            {
                "job-1": {"id": "job-1", "kind": "memory", "payload": {"action": "stats"}},
                "job-2": {"id": "job-2", "kind": "research", "payload": {"topic": "x"}},
            }
        )
        jobs = self.worker(client).list_pending()
        self.assertEqual([job.job_id for job in jobs], ["job-1", "job-2"])
        self.assertEqual(jobs[0].kind, "memory")
        self.assertEqual(jobs[1].payload["topic"], "x")

    def test_completed_jobs_are_skipped(self) -> None:
        client = StubClient(
            {
                "done": {"id": "done", "kind": "memory", "status": "done"},
                "open": {"id": "open", "kind": "memory", "payload": {}},
            }
        )
        jobs = self.worker(client).list_pending()
        self.assertEqual([job.job_id for job in jobs], ["open"])

    def test_flat_jobs_are_accepted(self) -> None:
        client = StubClient({"legacy": {"id": "legacy", "kind": "memory", "action": "stats"}})
        jobs = self.worker(client).list_pending()
        self.assertEqual(jobs[0].payload["action"], "stats")

    def test_existing_outbox_result_prevents_duplicate_delivery(self) -> None:
        client = StubClient({
            "job-1": {"id": "job-1", "kind": "memory", "payload": {"action": "stats"}}
        })
        client.existing_results["workspace/outbox/job-1.result.json"] = StubRemote(
            json.dumps({"job_id": "job-1", "ok": True})
        )
        self.assertEqual(self.worker(client).list_pending(), [])

    def test_max_jobs_is_respected(self) -> None:
        client = StubClient(
            {f"job-{index}": {"id": f"job-{index}", "kind": "memory"} for index in range(6)}
        )
        self.assertEqual(len(self.worker(client, max_jobs=2).list_pending()), 2)

    def test_missing_token_is_an_error(self) -> None:
        with self.assertRaises(WorkerError):
            self.worker(StubClient(token="")).list_pending()

    def test_missing_client_is_an_error(self) -> None:
        with self.assertRaises(WorkerError):
            self.worker(None).list_pending()


class ExecutionTests(WorkerTestCase):
    def test_memory_stats_job(self) -> None:
        worker = self.worker()
        outcome = worker.execute(Job(job_id="j", kind="memory", payload={"action": "stats"}))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn("lessons", outcome.artifacts["stats"])

    def test_memory_search_job(self) -> None:
        self.memory.record_lesson("KeyError: weights", root_cause="dropped column")
        worker = self.worker()
        outcome = worker.execute(
            Job(job_id="j", kind="memory", payload={"action": "search", "query": "weights"})
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(outcome.artifacts["lessons"]), 1)

    def test_unsupported_kind_fails_cleanly(self) -> None:
        outcome = self.worker().execute(Job(job_id="j", kind="launch-missiles"))
        self.assertFalse(outcome.ok)
        self.assertIn("unsupported job kind", outcome.error)

    def test_fix_job_analyzes_a_file(self) -> None:
        target = self.root / "buggy.py"
        target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        outcome = self.worker().execute(
            Job(job_id="j", kind="fix", payload={"target": "buggy.py", "llm": False})
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn("finding(s)", outcome.summary)
        self.assertIn("findings", outcome.artifacts)

    def test_fix_job_refuses_core(self) -> None:
        (self.root / "core").mkdir()
        (self.root / "core" / "security_guard.py").write_text("X = 1\n", encoding="utf-8")
        outcome = self.worker().execute(
            Job(job_id="j", kind="fix", payload={"target": "core/security_guard.py"})
        )
        self.assertFalse(outcome.ok)
        self.assertIn("human-sealed", outcome.error)

    def test_fix_job_refuses_escaping_paths(self) -> None:
        outcome = self.worker().execute(
            Job(job_id="j", kind="fix", payload={"target": "../../etc/passwd"})
        )
        self.assertFalse(outcome.ok)
        self.assertIn("escapes the workspace", outcome.error)

    def test_missing_target_is_reported(self) -> None:
        outcome = self.worker().execute(
            Job(job_id="j", kind="fix", payload={"target": "nope.py"})
        )
        self.assertFalse(outcome.ok)
        self.assertIn("not found", outcome.error)

    def test_build_job_needs_a_model(self) -> None:
        outcome = self.worker().execute(
            Job(job_id="j", kind="build", payload={"prompt": "add two numbers"})
        )
        self.assertFalse(outcome.ok)
        self.assertIn("GEMINI_API_KEY", outcome.error)

    def test_sync_job_uses_the_client(self) -> None:
        outcome = self.worker(StubClient()).execute(Job(job_id="j", kind="sync", payload={}))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.artifacts["commit"], "sync123")


class CheckpointTests(WorkerTestCase):
    def test_checkpoint_writes_outbox_and_marks_inbox(self) -> None:
        client = StubClient()
        worker = self.worker(client)
        job = Job(job_id="job-9", kind="memory", path="workspace/inbox/job-9.json")
        outcome = worker.execute(job)
        sha = worker.checkpoint(job, outcome)
        self.assertEqual(sha, "checkpoint123")
        self.assertEqual(outcome.checkpoint_sha, "checkpoint123")
        paths = [entry[0] for entry in client.puts]
        self.assertIn("workspace/outbox/job-9.result.json", paths)
        self.assertIn("workspace/inbox/job-9.json", paths)
        outbox = json.loads(client.puts[0][1])
        self.assertEqual(outbox["job_id"], "job-9")
        self.assertTrue(outbox["ok"])
        self.assertIn("completed_at", outbox)

    def test_poll_once_runs_and_checkpoints_every_job(self) -> None:
        client = StubClient(
            {
                "a": {"id": "a", "kind": "memory", "payload": {"action": "stats"}},
                "b": {"id": "b", "kind": "nope"},
            }
        )
        outcomes = self.worker(client).poll_once()
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(outcomes[0].ok)
        self.assertFalse(outcomes[1].ok)
        self.assertEqual(len(client.puts), 4)  # result + inbox marker per job

    def test_repeated_delivery_is_claimed_only_once(self) -> None:
        client = StubClient({
            "repeat": {"id": "repeat", "kind": "memory", "payload": {"action": "stats"}}
        })
        worker = self.worker(client)
        first = worker.poll_once()
        second = worker.poll_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        self.assertEqual(len(client.puts), 2)

    def test_stale_claim_requires_explicit_operator_reset(self) -> None:
        worker = self.worker()
        job = Job(job_id="stale", kind="memory", payload={"action": "stats"})
        self.assertTrue(worker._claim_job(job))
        self.assertFalse(worker._claim_job(job))
        self.assertTrue(worker.reset_claim("stale"))
        self.assertTrue(worker._claim_job(job))

    def test_run_once_iterates_a_single_poll(self) -> None:
        client = StubClient({"a": {"id": "a", "kind": "memory", "payload": {}}})
        outcomes = self.worker(client).run(iterations=1, interval=1.0)
        self.assertEqual(len(outcomes), 1)


if __name__ == "__main__":
    unittest.main()
