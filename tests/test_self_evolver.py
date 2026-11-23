"""Self-evolver tests: Meta-Critic findings, policy, firewall, suite gate, staging.

The unittest gate is stubbed so the suite never recursively runs itself; the
policy, AST firewall and staging behaviour are exercised for real.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine  # noqa: E402
from core.model_router import ModelResponse  # noqa: E402
from core.security_guard import SecurityGuard  # noqa: E402
from engines.self_evolver import Candidate, SelfEvolver  # noqa: E402

SAFE_PLAYBOOK = "# playbook\n\n- tighten the prompt\n"


class StubClient:
    """Minimal GitHub client stand-in: records pushes, never dials out."""

    repo = "teamhecked-Ayu/Vajrastra"
    branch = "main"

    def __init__(self, token: str = "token"):
        self._token = token
        self.pushed = []

    def token(self):
        return self._token

    def push_patch(self, branch, files, message, *, from_branch=None):
        self.pushed.append((branch, dict(files), message))
        return [SimpleNamespace(path=path) for path in files]


class StubRouter:
    available = True

    def __init__(self, payload=None):
        self.payload = payload or {"verdict": "sound", "risks": ["docs drift"]}

    def generate_json(self, prompt, *, system=None, **kwargs):
        return self.payload, ModelResponse(text=json.dumps(self.payload), model="stub", tier="deep")


class SelfEvolverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.guard = SecurityGuard(self.root)
        self.memory = MemoryEngine(self.root)

    def tearDown(self) -> None:
        self.memory.close()
        self._tmp.cleanup()

    def evolver(self, **kwargs) -> SelfEvolver:
        kwargs.setdefault("test_runner", lambda candidate: (True, "stub suite: OK"))
        kwargs.setdefault("run_tests", True)
        return SelfEvolver(self.root, guard=self.guard, memory=self.memory, **kwargs)

    def candidate(self, path: str, content: str = SAFE_PLAYBOOK, kind: str = "doc") -> Candidate:
        return Candidate(
            candidate_id="c01", path=path, content=content, kind=kind, rationale="test"
        )

    def record_weak_lesson(self, hits: int = 3):
        lesson = self.memory.record_lesson(
            "KeyError: model reply did not contain a JSON object",
            root_cause="",
            tags=("parse", "llm"),
        )
        for _ in range(hits):
            self.memory.record_hit(lesson.id)
        return lesson


class FindingTests(SelfEvolverTestCase):
    def test_weak_prompt_and_recurrence_are_named(self) -> None:
        self.record_weak_lesson(hits=3)
        findings = self.evolver().analyze()
        kinds = {finding.kind for finding in findings}
        self.assertIn("weak_prompt", kinds)
        self.assertIn("recurring_failure", kinds)
        self.assertIn("slow_parser", kinds)  # the parser tag is open

    def test_verified_lessons_do_not_raise_findings(self) -> None:
        self.memory.record_lesson(
            "ValueError: model reply", root_cause="schema drift", verified_patch="fix", verified=True
        )
        findings = self.evolver().analyze()
        self.assertEqual([finding for finding in findings if finding.kind == "weak_prompt"], [])

    def test_slow_measured_run_is_reported(self) -> None:
        results = self.root / "results"
        results.mkdir(parents=True)
        (results / "metrics.json").write_text(
            json.dumps(
                {
                    "nodes": [
                        {"node_id": "n0001", "experiment": {"durations": [0.2, 9.5]}},
                        {"node_id": "n0002", "experiment": {"durations": [0.1]}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        findings = self.evolver().analyze()
        slow = [finding for finding in findings if finding.kind == "slow_parser"]
        self.assertTrue(slow)
        self.assertAlmostEqual(slow[0].metric, 9.5)

    def test_fast_measured_runs_are_quiet(self) -> None:
        results = self.root / "results"
        results.mkdir(parents=True)
        (results / "metrics.json").write_text(
            json.dumps({"nodes": [{"node_id": "n1", "experiment": {"durations": [0.01]}}]}),
            encoding="utf-8",
        )
        self.assertEqual(self.evolver().analyze(), [])

    def test_duplicate_findings_are_deduplicated(self) -> None:
        self.record_weak_lesson(hits=5)
        findings = self.evolver().analyze()
        keys = [(finding.kind, finding.subject) for finding in findings]
        self.assertEqual(len(keys), len(set(keys)))


class ProposalTests(SelfEvolverTestCase):
    def test_playbooks_are_generated_per_finding_kind(self) -> None:
        self.record_weak_lesson()
        evolver = self.evolver()
        candidates = evolver.propose(evolver.analyze())
        self.assertTrue(candidates)
        self.assertTrue(all(candidate.path.startswith("docs/evolution/") for candidate in candidates))
        self.assertTrue(all(candidate.path.endswith(".md") for candidate in candidates))
        self.assertIn("Evidence", candidates[0].content)

    def test_empty_findings_propose_nothing(self) -> None:
        self.assertEqual(self.evolver().propose([]), [])

    def test_model_proposals_are_added_when_a_router_exists(self) -> None:
        router = StubRouter(
            {
                "verdict": "ok",
                "risks": [],
                "changes": [
                    {"path": "docs/evolution/model-note.md", "content": "# note\n", "rationale": "r"}
                ],
            }
        )
        self.record_weak_lesson()
        evolver = self.evolver(router=router)
        candidates = evolver.propose(evolver.analyze())
        self.assertTrue(any(candidate.kind == "model_proposal" for candidate in candidates))
        self.assertTrue(any(candidate.path == "docs/evolution/model-note.md" for candidate in candidates))

    def test_model_failure_is_ignored(self) -> None:
        class Broken(StubRouter):
            def generate_json(self, *args, **kwargs):
                raise RuntimeError("no quota")

        self.record_weak_lesson()
        evolver = self.evolver(router=Broken())
        candidates = evolver.propose(evolver.analyze())
        self.assertTrue(all(candidate.kind != "model_proposal" for candidate in candidates))


class PolicyTests(SelfEvolverTestCase):
    def test_security_guard_is_never_self_edited(self) -> None:
        candidate = self.candidate("core/security_guard.py", "VALUE = 1\n")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertTrue(candidate.protected)
        self.assertIn("human-sealed", candidate.rejection_reason)

    def test_security_lock_is_never_self_edited(self) -> None:
        candidate = self.candidate("core/security_lock.json", "{}\n")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertIn("human-sealed", candidate.rejection_reason)

    def test_any_core_path_is_refused(self) -> None:
        candidate = self.candidate("core/model_router.py", "X = 1\n")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertIn("core/", candidate.rejection_reason)

    def test_path_traversal_is_refused(self) -> None:
        candidate = self.candidate("../escape.md")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertIn("relative path", candidate.rejection_reason)

    def test_absolute_path_is_refused(self) -> None:
        candidate = self.candidate("/etc/passwd")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)

    def test_unsafe_code_is_refused_by_the_firewall(self) -> None:
        candidate = self.candidate("engines/unsafe.py", 'import os\nos.system("id")\n', kind="code")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertFalse(candidate.scan_ok)
        self.assertIn("firewall", candidate.rejection_reason)

    def test_unparsable_python_is_refused(self) -> None:
        candidate = self.candidate("engines/broken.py", "def broken(:\n    pass\n", kind="code")
        self.evolver().validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertFalse(candidate.parses)

    def test_clean_python_is_accepted(self) -> None:
        candidate = self.candidate(
            "engines/helper.py", '"""Helper."""\n\nVALUE = 41 + 1\n', kind="code"
        )
        self.evolver().validate([candidate])
        self.assertTrue(candidate.accepted, candidate.rejection_reason)
        self.assertTrue(candidate.parses)

    def test_failing_suite_rejects_the_candidate(self) -> None:
        evolver = self.evolver(test_runner=lambda candidate: (False, "FAILED (failures=1)"))
        candidate = self.candidate("docs/evolution/note.md")
        evolver.validate([candidate])
        self.assertFalse(candidate.accepted)
        self.assertIn("unittest suite failed", candidate.rejection_reason)

    def test_test_gate_can_be_disabled(self) -> None:
        candidate = self.candidate("docs/evolution/note.md")
        self.evolver(run_tests=False).validate([candidate])
        self.assertTrue(candidate.accepted)
        self.assertIsNone(candidate.tests_ok)

    def test_nested_suite_disables_the_gate(self) -> None:
        with mock.patch.dict(os.environ, {"VAJRASTRA_SELFEVOLVE_CHILD": "1"}):
            evolver = SelfEvolver(self.root, guard=self.guard, memory=self.memory)
            self.assertFalse(evolver.run_tests)


class StagingTests(SelfEvolverTestCase):
    def test_stage_writes_accepted_candidates_and_manifest(self) -> None:
        evolver = self.evolver()
        candidate = self.candidate("docs/evolution/note.md")
        evolver.validate([candidate])
        staging = evolver.stage([candidate], "self-upgrade/test")
        self.assertTrue((staging / "docs" / "evolution" / "note.md").is_file())
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["branch"], "self-upgrade/test")
        self.assertEqual(manifest["candidates"][0]["path"], "docs/evolution/note.md")

    def test_stage_skips_rejected_candidates(self) -> None:
        evolver = self.evolver()
        rejected = self.candidate("core/security_guard.py", "X = 1\n")
        evolver.validate([rejected])
        staging = evolver.stage([rejected], "self-upgrade/reject")
        self.assertFalse((staging / "core" / "security_guard.py").exists())

    def test_push_requires_a_token(self) -> None:
        client = StubClient(token="")
        evolver = self.evolver(client=client)
        candidate = self.candidate("docs/evolution/note.md")
        evolver.validate([candidate])
        self.assertEqual(evolver.push([candidate], "self-upgrade/x"), [])
        self.assertEqual(client.pushed, [])

    def test_push_uses_an_isolated_branch(self) -> None:
        (self.root / "core").mkdir()
        (self.root / "core" / "security_guard.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "vajrastra.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.guard.seal(force=True)
        client = StubClient()
        evolver = self.evolver(client=client)
        candidate = self.candidate("docs/evolution/note.md")
        evolver.validate([candidate])
        pushed = evolver.push([candidate], "self-upgrade/20260101T000000Z")
        self.assertEqual(pushed, ["docs/evolution/note.md"])
        self.assertEqual(client.pushed[0][0], "self-upgrade/20260101T000000Z")
        self.assertIn("docs/evolution/note.md", client.pushed[0][1])

    def test_push_refuses_a_drifted_core(self) -> None:
        (self.root / "core").mkdir()
        (self.root / "core" / "security_guard.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.guard.seal(force=True)
        (self.root / "core" / "security_guard.py").write_text("VALUE = 2\n", encoding="utf-8")
        client = StubClient()
        evolver = self.evolver(client=client)
        candidate = self.candidate("docs/evolution/note.md")
        evolver.validate([candidate])
        self.assertEqual(evolver.push([candidate], "self-upgrade/drift"), [])
        self.assertEqual(client.pushed, [])


class ReviewTests(SelfEvolverTestCase):
    def test_review_writes_a_report_and_keeps_staging_local(self) -> None:
        self.record_weak_lesson()
        report = self.evolver().review()
        self.assertTrue(report.report_path.is_file())
        text = report.report_path.read_text(encoding="utf-8")
        self.assertIn("integrity", text)
        self.assertIn("staging branch", text)
        self.assertIn("self-upgrade/", text)
        self.assertEqual(report.pushed, [])
        self.assertTrue(any("--push" in note for note in report.notes))

    def test_review_accepts_playbooks_with_a_clean_suite(self) -> None:
        self.record_weak_lesson()
        report = self.evolver().review()
        self.assertTrue(report.findings)
        self.assertTrue(report.accepted)
        self.assertEqual(report.rejected, [])
        self.assertTrue(all(candidate.staged_path for candidate in report.accepted))

    def test_review_records_a_lesson(self) -> None:
        self.record_weak_lesson()
        report = self.evolver().review()
        lesson = self.memory.get(report.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertIn("meta-critic", lesson.tags)

    def test_meta_critique_is_not_fabricated(self) -> None:
        report = self.evolver().review()
        self.assertIn("no GEMINI_API_KEY", report.meta_critique)

    def test_meta_critique_uses_the_router(self) -> None:
        self.record_weak_lesson()
        report = self.evolver(router=StubRouter()).review()
        self.assertIn("sound", report.meta_critique)
        self.assertIn("docs drift", report.meta_critique)

    def test_unsealed_core_is_reported_not_hidden(self) -> None:
        report = self.evolver().review()
        self.assertEqual(report.integrity_status, "unsealed")
        self.assertEqual(report.as_dict()["integrity_status"], "unsealed")

    def test_push_without_a_client_is_noted(self) -> None:
        report = self.evolver().review(push=True)
        self.assertEqual(report.pushed, [])
        self.assertTrue(any("no GitHub client" in note for note in report.notes))


if __name__ == "__main__":
    unittest.main()
