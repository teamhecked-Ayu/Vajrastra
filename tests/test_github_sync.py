"""GitHub sync tests: contents API, base64 commits, branches, ledger merge.

Every HTTP call is served by a scripted transport; nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.github_sync import (  # noqa: E402
    SQLITE_MAGIC,
    GitHubClient,
    GitHubError,
    export_database_bytes,
    merge_database_bytes,
)
from core.memory_engine import MemoryEngine  # noqa: E402

TOKEN = "unit-test-token"


class ScriptedTransport:
    """Delegate each call to a handler and record the request."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        parsed = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": {str(key).lower(): value for key, value in headers.items()},
                "body": parsed,
            }
        )
        return self.handler(method, url, headers, parsed)


def make_client(handler, token: str = TOKEN, **kwargs):
    transport = ScriptedTransport(handler)
    client = GitHubClient(token, transport=transport, sleeper=lambda _s: None, **kwargs)
    return client, transport


def contents_response(text: str, sha: str = "abc123") -> tuple:
    return (
        200,
        {},
        json.dumps(
            {
                "sha": sha,
                "size": len(text.encode("utf-8")),
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            }
        ),
    )


class ContentsTests(unittest.TestCase):
    def test_get_file_decodes_base64(self) -> None:
        client, transport = make_client(lambda *_: contents_response("hello\n"))
        remote = client.get_file("core/example.py")
        self.assertIsNotNone(remote)
        self.assertEqual(remote.text, "hello\n")
        self.assertEqual(remote.sha, "abc123")
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/contents/core/example.py", call["url"])
        self.assertEqual(call["headers"]["authorization"], "Bearer " + TOKEN)

    def test_missing_file_returns_none(self) -> None:
        client, _ = make_client(lambda *_: (404, {}, "not found"))
        self.assertIsNone(client.get_file("nope.txt"))

    def test_put_new_file_sends_base64_without_sha(self) -> None:
        def handler(method, url, headers, body):
            if method == "GET":
                return (404, {}, "missing")
            return (201, {}, json.dumps({"commit": {"sha": "c1"}, "content": {"sha": "b1"}}))

        client, transport = make_client(handler)
        result = client.put_file("docs/new.md", "hello", "docs: add new")
        self.assertTrue(result.created)
        self.assertEqual(result.commit_sha, "c1")
        put = transport.calls[-1]
        self.assertEqual(put["method"], "PUT")
        self.assertNotIn("sha", put["body"])
        self.assertEqual(base64.b64decode(put["body"]["content"]).decode("utf-8"), "hello")
        self.assertEqual(put["body"]["branch"], "main")

    def test_put_existing_file_includes_sha(self) -> None:
        def handler(method, url, headers, body):
            if method == "GET":
                return contents_response("old", sha="deadbeef")
            return (200, {}, json.dumps({"commit": {"sha": "c2"}, "content": {"sha": "b2"}}))

        client, transport = make_client(handler)
        result = client.put_file("docs/new.md", "new", "docs: update")
        self.assertFalse(result.created)
        self.assertEqual(transport.calls[-1]["body"]["sha"], "deadbeef")

    def test_binary_payload_round_trips(self) -> None:
        payload = b"\x00\x01binary"

        def handler(method, url, headers, body):
            if method == "GET":
                return (404, {}, "missing")
            decoded = base64.b64decode(body["content"])
            self.assertEqual(decoded, payload)
            return (201, {}, json.dumps({"commit": {"sha": "c"}, "content": {"sha": "b"}}))

        client, _ = make_client(handler)
        client.put_file("memory_bank/lessons.sqlite", payload, "chore: binary")

    def test_rate_limit_is_retried(self) -> None:
        responses = iter([(429, {}, "slow down"), contents_response("ok")])
        delays = []
        transport = ScriptedTransport(lambda *_: next(responses))
        client = GitHubClient(TOKEN, transport=transport, sleeper=delays.append)
        self.assertEqual(client.read_text("file.txt"), "ok")
        self.assertEqual(len(delays), 1)

    def test_missing_token_is_explicit(self) -> None:
        client = GitHubClient(env={}, transport=ScriptedTransport(lambda *_: (200, {}, "{}")))
        with self.assertRaises(GitHubError) as caught:
            client.get_file("x")
        self.assertIn("token", str(caught.exception).lower())

    def test_repr_never_leaks_the_token(self) -> None:
        client = GitHubClient("very-secret-value")
        self.assertNotIn("very-secret-value", repr(client))
        self.assertIn("token-set", repr(client))


class BranchTests(unittest.TestCase):
    def test_push_patch_creates_branch_when_missing(self) -> None:
        def handler(method, url, headers, body):
            if method == "GET" and url.endswith("/git/ref/heads/feature"):
                return (404, {}, "missing")
            if method == "GET" and url.endswith("/git/ref/heads/main"):
                return (200, {}, json.dumps({"object": {"sha": "base-sha"}}))
            if method == "POST" and url.endswith("/git/refs"):
                return (201, {}, json.dumps({"ref": "refs/heads/feature"}))
            if method == "GET" and "/contents/" in url:
                return (404, {}, "missing")
            return (201, {}, json.dumps({"commit": {"sha": "c"}, "content": {"sha": "b"}}))

        client, transport = make_client(handler)
        results = client.push_patch(
            "feature", {"pkg/a.py": "A = 1\n"}, "feat: patch"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].branch, "feature")
        create = [call for call in transport.calls if call["method"] == "POST"][0]
        self.assertEqual(create["body"]["ref"], "refs/heads/feature")
        self.assertEqual(create["body"]["sha"], "base-sha")


class MemoryTests(unittest.TestCase):
    def _engine_with_lesson(self, root: Path) -> MemoryEngine:
        engine = MemoryEngine(root)
        engine.record_lesson(
            "KeyError: 'weights'",
            root_cause="the weights column was dropped before use",
            verified_patch="weights = frame['weights']",
            tags=("sqlite",),
            verified=True,
        )
        return engine

    def test_export_is_a_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_lesson(Path(tmp))
            try:
                raw = export_database_bytes(engine.db_path)
            finally:
                engine.close()
            self.assertTrue(raw.startswith(SQLITE_MAGIC))

    def test_pull_memory_merges_remote_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote_root = base / "remote"
            remote_root.mkdir()
            remote = self._engine_with_lesson(remote_root)
            raw = export_database_bytes(remote.db_path)
            remote.close()

            encoded = base64.b64encode(raw).decode("ascii")
            client, _ = make_client(
                lambda *_: (
                    200,
                    {},
                    json.dumps({"sha": "s1", "size": len(raw), "content": encoded}),
                )
            )
            local_root = base / "local"
            local_root.mkdir()
            engine = MemoryEngine(local_root)
            try:
                lessons, skills = client.pull_memory(engine)
                self.assertEqual(lessons, 1)
                self.assertEqual(skills, 0)
                self.assertTrue(engine.search("weights"))
            finally:
                engine.close()

    def test_push_memory_uploads_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_lesson(Path(tmp))

            def handler(method, url, headers, body):
                if method == "GET":
                    return (404, {}, "missing")
                self.assertTrue(base64.b64decode(body["content"]).startswith(SQLITE_MAGIC))
                return (201, {}, json.dumps({"commit": {"sha": "c"}, "content": {"sha": "b"}}))

            client, transport = make_client(handler)
            try:
                size, result = client.push_memory(engine)
                self.assertGreater(size, 0)
                self.assertEqual(result.path, "memory_bank/lessons.sqlite")
            finally:
                engine.close()
            self.assertEqual(transport.calls[-1]["method"], "PUT")

    def test_sync_memory_reports_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_lesson(Path(tmp))
            client, _ = make_client(
                lambda method, *_: (404, {}, "missing")
                if method == "GET"
                else (201, {}, json.dumps({"commit": {"sha": "c9"}, "content": {"sha": "b9"}}))
            )
            try:
                report = client.sync_memory(engine, push=True, pull=True)
            finally:
                engine.close()
            self.assertTrue(report.pushed)
            self.assertGreater(report.pushed_bytes, 0)
            self.assertEqual(report.commit_sha, "c9")

    def test_garbage_ledger_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = MemoryEngine(Path(tmp))
            try:
                with self.assertRaises(GitHubError):
                    merge_database_bytes(engine, b"not a database")
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
