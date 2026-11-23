"""ML scientist tests: statistics, the seed gate, MCTS and artifact honesty.

Model calls are stubbed; the template subprocess is exercised for real once so
the JSON contract between the search and the benchmark stays covered.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine  # noqa: E402
from core.model_router import ModelResponse  # noqa: E402
from engines.ml_scientist import (  # noqa: E402
    INCONCLUSIVE,
    INSUFFICIENT_SEEDS,
    NOT_RUN,
    REGRESSION,
    VERIFIED_SUCCESS,
    MLScientist,
    evaluate_gate,
    paired_t_test,
    render_metrics_tex,
)

BASELINE = [0.700, 0.720, 0.710]
BETTER = [0.800, 0.830, 0.810]


class StubRouter:
    """Router stand-in that answers every persona with the same review JSON."""

    available = True

    def __init__(self, payload=None):
        self.payload = payload or {
            "verdict": "needs a stronger baseline",
            "score": 6,
            "strengths": ["pre-registered gate"],
            "concerns": ["single dataset"],
            "required_experiments": ["replicate on a second split"],
        }
        self.prompts = []

    def generate_json(self, prompt, *, system=None, **kwargs):
        self.prompts.append(prompt)
        return self.payload, ModelResponse(
            text=json.dumps(self.payload), model="stub-model", tier="deep"
        )


class FailingRouter:
    available = True

    def generate_json(self, *args, **kwargs):
        raise RuntimeError("quota exhausted")


def linear_runner(gain_per_epoch: float = 0.01):
    """Deterministic runner: more epochs always win, with a seed-dependent jitter."""

    calls = []

    def runner(params, seed):
        calls.append((dict(params), seed))
        metric = 0.50 + gain_per_epoch * float(params["epochs"]) + ((seed * 37) % 7) / 10_000.0
        return {"metric": metric, "duration_s": 0.001, "engine": "stub"}

    runner.calls = calls
    return runner


def flat_runner(params, seed):
    return {"metric": 0.60, "duration_s": 0.001, "engine": "stub"}


class StatisticsTests(unittest.TestCase):
    def test_paired_t_test_stdlib_matches_expected_range(self) -> None:
        result = paired_t_test(BASELINE, BETTER, use_scipy=False)
        self.assertEqual(result.n, 3)
        self.assertEqual(result.df, 2)
        self.assertAlmostEqual(result.mean_difference, 0.103333, places=5)
        self.assertLess(result.p_value, 0.01)
        self.assertGreater(result.p_value, 0.0001)
        self.assertEqual(result.method, "stdlib")

    def test_paired_t_test_rejects_uneven_samples(self) -> None:
        with self.assertRaises(ValueError):
            paired_t_test([0.1, 0.2], [0.2])

    def test_zero_variance_improvement_is_significant(self) -> None:
        result = paired_t_test([0.5, 0.5, 0.5], [0.6, 0.6, 0.6], use_scipy=False)
        self.assertEqual(result.p_value, 0.0)
        self.assertEqual(result.method, "degenerate")

    def test_zero_variance_no_change_is_not_significant(self) -> None:
        result = paired_t_test([0.5, 0.5, 0.5], [0.5, 0.5, 0.5], use_scipy=False)
        self.assertEqual(result.p_value, 1.0)

    def test_two_sided_p_value_is_symmetric(self) -> None:
        worse = paired_t_test(BETTER, BASELINE, use_scipy=False)
        better = paired_t_test(BASELINE, BETTER, use_scipy=False)
        self.assertAlmostEqual(worse.p_value, better.p_value, places=9)
        self.assertLess(worse.mean_difference, 0.0)


class GateTests(unittest.TestCase):
    def test_three_winning_seeds_verify(self) -> None:
        gate = evaluate_gate(BASELINE, BETTER)
        self.assertEqual(gate.status, VERIFIED_SUCCESS)
        self.assertTrue(gate.verified)
        self.assertEqual(gate.wins, 3)
        self.assertEqual(gate.losses, 0)

    def test_two_seeds_are_never_enough(self) -> None:
        gate = evaluate_gate(BASELINE[:2], BETTER[:2])
        self.assertEqual(gate.status, INSUFFICIENT_SEEDS)
        self.assertFalse(gate.verified)
        self.assertIsNone(gate.p_value)

    def test_reliable_regression_is_flagged(self) -> None:
        gate = evaluate_gate(BETTER, BASELINE)
        self.assertEqual(gate.status, REGRESSION)
        self.assertFalse(gate.verified)

    def test_noise_is_inconclusive(self) -> None:
        gate = evaluate_gate(BASELINE, [0.701, 0.719, 0.712])
        self.assertEqual(gate.status, INCONCLUSIVE)
        self.assertFalse(gate.verified)

    def test_missing_measurements_are_not_run(self) -> None:
        gate = evaluate_gate([0.7, None, 0.71], [0.8, 0.81, None])
        self.assertEqual(gate.status, INSUFFICIENT_SEEDS)
        self.assertEqual(gate.n_seeds, 1)

    def test_empty_input_is_not_run(self) -> None:
        self.assertEqual(evaluate_gate([], []).status, NOT_RUN)

    def test_lower_is_better_flips_the_verdict(self) -> None:
        lower_is_better = evaluate_gate(
            [0.5, 0.52, 0.51], [0.4, 0.41, 0.42], higher_is_better=False
        )
        self.assertEqual(lower_is_better.status, VERIFIED_SUCCESS)


class MetricsTexTests(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "run_id": "20260101T000000Z-topic",
            "topic": "learning rate 50% & _warmup_",
            "generated_at": "2026-01-01T00:00:00Z",
            "metric_name": "accuracy",
            "seeds": 3,
            "budget": 4,
            "nodes_evaluated": 4,
            "baseline": {"mean": 0.7123456, "stdev": 0.0123},
            "best": {"node_id": "n0003", "mean": 0.8123456, "stdev": 0.0045},
            "gate": {
                "status": VERIFIED_SUCCESS,
                "mean_improvement": 0.1,
                "p_value": 0.00042,
                "t_statistic": 9.5,
                "df": 2,
                "wins": 3,
                "losses": 0,
                "alpha": 0.05,
                "reason": "mean improvement +0.1",
            },
        }

    def test_macros_carry_the_measured_numbers(self) -> None:
        text = render_metrics_tex(self.payload())
        self.assertIn(r"\newcommand{\VajraBaselineMean}{0.7123}", text)
        self.assertIn(r"\newcommand{\VajraBestMean}{0.8123}", text)
        self.assertIn(r"\newcommand{\VajraPValue}{0.0004}", text)
        self.assertIn(r"\newcommand{\VajraSeedsWon}{3}", text)
        self.assertIn(r"\newcommand{\VajraGateStatus}{verified\_success}", text)

    def test_topic_is_latex_escaped(self) -> None:
        text = render_metrics_tex(self.payload())
        self.assertIn("learning rate 50\\% \\& \\_warmup\\_", text)
        self.assertNotIn("50% & _warmup_", text)

    def test_tiny_p_value_uses_a_bound(self) -> None:
        payload = self.payload()
        payload["gate"]["p_value"] = 1e-9
        self.assertIn(r"\newcommand{\VajraPValue}{<0.0001}", render_metrics_tex(payload))

    def test_missing_numbers_render_as_not_available(self) -> None:
        payload = {"topic": "t", "gate": {"status": NOT_RUN}}
        text = render_metrics_tex(payload)
        self.assertIn(r"\newcommand{\VajraBaselineMean}{n/a}", text)
        self.assertIn(r"\newcommand{\VajraPValue}{n/a}", text)

    def test_no_python_repr_leaks_into_macros(self) -> None:
        text = render_metrics_tex(self.payload())
        self.assertNotIn("None", text)
        self.assertNotIn("{'", text)


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine = MemoryEngine(self.root)
        self._scientists = []

    def tearDown(self) -> None:
        for scientist in self._scientists:
            pass
        self.engine.close()
        self._tmp.cleanup()

    def scientist(self, **kwargs) -> MLScientist:
        kwargs.setdefault("runner", linear_runner())
        kwargs.setdefault("seeds", 3)
        kwargs.setdefault("budget", 3)
        scientist = MLScientist(self.root, memory=self.engine, **kwargs)
        self._scientists.append(scientist)
        return scientist

    def test_run_writes_tree_results_and_macros(self) -> None:
        run = self.scientist().run("epochs vs accuracy")
        self.assertTrue(run.tree_path.is_file())
        self.assertTrue(run.metrics_json_path.is_file())
        self.assertTrue(run.metrics_tex_path.is_file())
        tree = json.loads(run.tree_path.read_text(encoding="utf-8"))
        self.assertEqual(tree["algorithm"], "mcts-ucb1")
        self.assertEqual(tree["seeds"], 3)
        self.assertEqual(tree["topic"], "epochs vs accuracy")
        self.assertGreaterEqual(len(tree["nodes"]), 2)

    def test_budget_is_respected(self) -> None:
        scientist = self.scientist(budget=4)
        run = scientist.run("budget check")
        evaluated = [node for node in run.nodes if node.experiment is not None]
        self.assertEqual(len(evaluated), 5)  # baseline + 4 experiments

    def test_gate_verifies_a_real_improvement(self) -> None:
        run = self.scientist().run("more epochs help")
        self.assertEqual(run.gate.status, VERIFIED_SUCCESS)
        self.assertTrue(run.verified)
        self.assertIsNotNone(run.best)
        self.assertGreater(run.best.experiment.mean, run.baseline.mean)

    def test_flat_search_stays_inconclusive(self) -> None:
        run = self.scientist(runner=flat_runner, budget=2).run("nothing changes")
        self.assertFalse(run.verified)
        self.assertEqual(run.gate.status, INCONCLUSIVE)

    def test_seed_list_is_paired_across_baseline_and_candidates(self) -> None:
        runner = linear_runner()
        scientist = self.scientist(runner=runner, budget=1)
        scientist.run("paired seeds")
        seeds_per_call = [seed for _params, seed in runner.calls]
        self.assertEqual(seeds_per_call, [2026, 2027, 2028, 2026, 2027, 2028])
        self.assertEqual(scientist.seed_list, [2026, 2027, 2028])

    def test_zero_budget_reports_not_run(self) -> None:
        run = self.scientist(budget=0).run("no experiments")
        self.assertEqual(run.gate.status, NOT_RUN)
        self.assertIsNone(run.best)
        self.assertTrue(run.notes)

    def test_failing_runner_is_reported_not_hidden(self) -> None:
        def broken(params, seed):
            raise RuntimeError("benchmark exploded")

        run = self.scientist(runner=broken, budget=1).run("broken")
        self.assertFalse(run.baseline.ok)
        self.assertTrue(any("baseline failed" in note for note in run.notes))
        self.assertFalse((self.root / "results" / "metrics.tex").read_text().startswith("{"))
        payload = json.loads(run.metrics_json_path.read_text(encoding="utf-8"))
        self.assertIsNone(payload["baseline"]["mean"])

    def test_metrics_tex_matches_raw_json(self) -> None:
        run = self.scientist().run("macro honesty")
        payload = json.loads(run.metrics_json_path.read_text(encoding="utf-8"))
        tex = run.metrics_tex_path.read_text(encoding="utf-8")
        baseline_macro = re.search(r"\\VajraBaselineMean\}\{([^}]*)\}", tex).group(1)
        self.assertEqual(baseline_macro, f"{payload['baseline']['mean']:.4f}")
        gate_macro = re.search(r"\\VajraGateStatus\}\{([^}]*)\}", tex).group(1)
        self.assertEqual(gate_macro, payload["gate"]["status"].replace("_", r"\_"))

    def test_verified_run_records_a_lesson(self) -> None:
        run = self.scientist().run("record me")
        self.assertIsNotNone(run.lesson_id)
        lesson = self.engine.get(run.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertTrue(lesson.solved)
        self.assertIn("research", lesson.tags)

    def test_inconclusive_run_does_not_claim_a_patch(self) -> None:
        run = self.scientist(runner=flat_runner, budget=1).run("no lesson")
        lesson = self.engine.get(run.lesson_id)
        self.assertIsNotNone(lesson)
        self.assertFalse(lesson.solved)

    def test_peer_review_runs_three_personas(self) -> None:
        router = StubRouter()
        run = self.scientist(router=router).run("reviewed")
        self.assertEqual(len(run.reviews), 3)
        self.assertEqual(
            [review.persona for review in run.reviews],
            ["Methodology Critic", "Statistical Auditor", "Novelty Reviewer"],
        )
        self.assertTrue(all(review.score == 6.0 for review in run.reviews))
        self.assertEqual(len(router.prompts), 3)
        self.assertIn("results/metrics.tex", router.prompts[0])

    def test_review_is_skipped_without_a_model(self) -> None:
        run = self.scientist().run("unreviewed")
        self.assertTrue(all(review.skipped for review in run.reviews))
        self.assertTrue(all("not fabricated" in review.error for review in run.reviews))

    def test_review_failure_does_not_break_the_run(self) -> None:
        run = self.scientist(router=FailingRouter()).run("failing review")
        self.assertTrue(all(review.skipped for review in run.reviews))
        self.assertTrue(run.metrics_tex_path.is_file())

    def test_tree_nodes_have_unique_ids_and_depths(self) -> None:
        run = self.scientist(budget=3).run("structure")
        ids = [node.node_id for node in run.nodes]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(node.depth <= 3 for node in run.nodes))

    def test_real_template_runs_through_subprocess(self) -> None:
        scientist = self.scientist(runner=None, seeds=3, budget=1, max_children=1)
        run = scientist.run("real template smoke")
        self.assertIsNotNone(run.baseline.mean)
        self.assertGreaterEqual(run.baseline.mean, 0.0)
        self.assertLessEqual(run.baseline.mean, 1.0)
        self.assertTrue(run.metrics_tex_path.is_file())
        self.assertEqual(len(run.baseline.metrics), 3)


if __name__ == "__main__":
    unittest.main()
