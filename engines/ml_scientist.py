"""Vajrastra ML scientist: MCTS over research hypotheses with a multi-seed gate.

The engine is intentionally small and stdlib-only:

* **Search** -- Monte Carlo Tree Search with UCB1 over hyper-parameter
  hypotheses.  Each tree node is one hypothesis; evaluating it runs the fast
  benchmark template once per seed and stores the raw JSON on disk.
* **Gate** -- a candidate is ``verified_success`` only when it beats the
  untouched baseline on at least three paired seeds *and* a two-sided paired
  t-test gives ``p < 0.05``.  ``scipy`` is used when installed; otherwise a
  pure-standard-library incomplete-beta implementation computes the same
  two-sided p-value.
* **Numbers** -- ``results/metrics.tex`` is rendered from the raw JSON results,
  never from model output, so a paper macro cannot disagree with the run.
* **Review** -- three adversarial personas (Methodology Critic, Statistical
  Auditor, Novelty Reviewer) read the measured run through
  :class:`core.model_router.ModelRouter`.  Without a model the reviews are
  explicitly marked unavailable instead of being invented.

Heavy libraries are never imported here: the template itself lazily uses
``numpy`` when present and falls back to pure Python otherwise.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_TEMPLATE = PROJECT_ROOT / "templates" / "tabular_or_micro_net.py"
TREE_RELPATH = Path("research_tree") / "tree_state.json"
RUNS_RELPATH = Path("research_tree") / "runs"
RESULTS_RELPATH = Path("results")
METRICS_JSON = RESULTS_RELPATH / "metrics.json"
METRICS_TEX = RESULTS_RELPATH / "metrics.tex"

VERIFIED_SUCCESS = "verified_success"
INCONCLUSIVE = "inconclusive"
REGRESSION = "regression"
INSUFFICIENT_SEEDS = "insufficient_seeds"
NOT_RUN = "not_run"

DEFAULT_PARAMS: Dict[str, Any] = {
    "epochs": 6,
    "lr": 0.05,
    "weight_decay": 0.0,
    "batch_size": 32,
    "optimizer": "sgd",
    "standardize": False,
    "hidden_units": 16,
}

#: parameter grids the search mutates; ordered so runs are reproducible.
PARAM_GRID: Dict[str, Tuple[Any, ...]] = {
    "epochs": (6, 15, 40),
    "lr": (0.02, 0.05, 0.1, 0.3),
    "weight_decay": (0.0, 0.001),
    "batch_size": (16, 32, 64),
    "optimizer": ("sgd", "adam"),
    "standardize": (False, True),
}

PERSONAS: Tuple[Tuple[str, str], ...] = (
    (
        "Methodology Critic",
        "Attack the experimental design: leakage, train/val contamination, "
        "confounds, and whether the hypothesis was really tested.",
    ),
    (
        "Statistical Auditor",
        "Attack the statistics: seed count, variance, multiple comparisons, "
        "effect size, and whether p < 0.05 is meaningful here.",
    ),
    (
        "Novelty Reviewer",
        "Attack the contribution: is this a known result, is the baseline "
        "strong enough, and what would a sceptical reviewer demand?",
    ),
)

REVIEW_SYSTEM = (
    "You are an adversarial peer reviewer. Use only the measured numbers in the "
    "prompt; every number comes from results/metrics.tex and is authoritative. "
    "Never invent, round or extrapolate a number. Reply with JSON only."
)


class ExperimentError(RuntimeError):
    """Raised when a benchmark run cannot be executed or parsed."""


# ------------------------------------------------------------------ statistics
@dataclass(frozen=True)
class TTestResult:
    """Two-sided paired t-test outcome."""

    t_statistic: float
    df: int
    p_value: float
    mean_difference: float
    sd_difference: float
    n: int
    method: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "t_statistic": self.t_statistic,
            "df": self.df,
            "p_value": self.p_value,
            "mean_difference": self.mean_difference,
            "sd_difference": self.sd_difference,
            "n": self.n,
            "method": self.method,
        }


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz)."""
    max_iterations, epsilon, tiny = 200, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    result = d
    for iteration in range(1, max_iterations + 1):
        doubled = 2 * iteration
        numerator = iteration * (b - iteration) * x / ((qam + doubled) * (a + doubled))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        result *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / ((a + doubled) * (qap + doubled))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return result


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _student_two_sided_p(t_statistic: float, df: int) -> float:
    """Two-sided p-value of Student's t with *df* degrees of freedom."""
    if df <= 0:
        return 1.0
    if math.isinf(t_statistic):
        return 0.0
    if math.isnan(t_statistic):
        return 1.0
    x = df / (df + t_statistic * t_statistic)
    return _betai(df / 2.0, 0.5, x)


def paired_t_test(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    use_scipy: Optional[bool] = None,
) -> TTestResult:
    """Two-sided paired t-test, scipy when available and stdlib otherwise."""
    if len(baseline) != len(candidate):
        raise ValueError("paired t-test needs equal-length samples")
    pairs = list(zip(baseline, candidate))
    n = len(pairs)
    if n < 2:
        raise ValueError("paired t-test needs at least two pairs")
    differences = [float(candidate_value) - float(baseline_value) for baseline_value, candidate_value in pairs]
    mean = sum(differences) / n
    variance = sum((value - mean) ** 2 for value in differences) / (n - 1)
    sd = math.sqrt(max(variance, 0.0))

    if sd == 0.0:
        if mean == 0.0:
            return TTestResult(0.0, n - 1, 1.0, 0.0, 0.0, n, "degenerate")
        sign = 1.0 if mean > 0 else -1.0
        return TTestResult(sign * math.inf, n - 1, 0.0, mean, 0.0, n, "degenerate")

    t_statistic = mean / (sd / math.sqrt(n))
    p_value = _student_two_sided_p(t_statistic, n - 1)
    method = "stdlib"

    want_scipy = use_scipy is None or use_scipy
    if want_scipy:
        try:  # lazy: scipy is optional everywhere, including Colab
            from scipy import stats as scipy_stats  # type: ignore

            result = scipy_stats.ttest_rel(candidate, baseline)
            candidate_p = float(getattr(result, "pvalue", float("nan")))
            candidate_t = float(getattr(result, "statistic", float("nan")))
            if not math.isnan(candidate_p) and not math.isnan(candidate_t):
                p_value = candidate_p
                t_statistic = candidate_t
                method = "scipy"
        except Exception:  # pragma: no cover - absent scipy or hostile build
            method = "stdlib"

    return TTestResult(t_statistic, n - 1, p_value, mean, sd, n, method)


@dataclass(frozen=True)
class GateReport:
    """Verdict of the multi-seed statistical gate for one candidate."""

    status: str
    n_seeds: int
    min_seeds: int
    alpha: float
    baseline_mean: Optional[float]
    candidate_mean: Optional[float]
    mean_improvement: Optional[float]
    wins: int
    losses: int
    p_value: Optional[float]
    t_statistic: Optional[float]
    df: int
    per_seed_deltas: Tuple[float, ...]
    method: str
    reason: str

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED_SUCCESS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "verified": self.verified,
            "n_seeds": self.n_seeds,
            "min_seeds": self.min_seeds,
            "alpha": self.alpha,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "mean_improvement": self.mean_improvement,
            "wins": self.wins,
            "losses": self.losses,
            "p_value": self.p_value,
            "t_statistic": self.t_statistic,
            "df": self.df,
            "per_seed_deltas": list(self.per_seed_deltas),
            "method": self.method,
            "reason": self.reason,
        }


def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def evaluate_gate(
    baseline: Sequence[Optional[float]],
    candidate: Sequence[Optional[float]],
    *,
    min_seeds: int = 3,
    alpha: float = 0.05,
    higher_is_better: bool = True,
) -> GateReport:
    """Decide whether *candidate* truly beats the untouched baseline.

    A candidate is verified only when it has at least ``min_seeds`` paired
    measurements, a positive mean improvement, and a two-sided paired t-test
    ``p < alpha``.
    """
    pairs = [
        (float(base), float(cand))
        for base, cand in zip(baseline, candidate)
        if base is not None and cand is not None
    ]
    if not pairs:
        return GateReport(
            NOT_RUN, 0, min_seeds, alpha, None, None, None, 0, 0, None, None, 0, (), "none",
            "no measured runs for this hypothesis",
        )

    bases = [pair[0] for pair in pairs]
    cands = [pair[1] for pair in pairs]
    deltas = [cand - base for base, cand in pairs]
    if not higher_is_better:
        deltas = [-delta for delta in deltas]
    wins = sum(1 for delta in deltas if delta > 0)
    losses = sum(1 for delta in deltas if delta < 0)
    baseline_mean, candidate_mean, improvement = _mean(bases), _mean(cands), _mean(deltas)

    if len(pairs) < min_seeds:
        return GateReport(
            INSUFFICIENT_SEEDS, len(pairs), min_seeds, alpha, baseline_mean, candidate_mean,
            improvement, wins, losses, None, None, 0, tuple(deltas), "none",
            f"{len(pairs)} seed(s) measured; the gate requires at least {min_seeds}",
        )

    test = paired_t_test(bases, cands)
    if not higher_is_better:
        test = TTestResult(
            -test.t_statistic, test.df, test.p_value, -test.mean_difference,
            test.sd_difference, test.n, test.method,
        )
    significant = test.p_value < alpha
    positive = improvement is not None and improvement > 0.0

    if significant and positive:
        status = VERIFIED_SUCCESS
        reason = (
            f"mean improvement {improvement:+.4f} over {len(pairs)} seeds, "
            f"paired t-test p={test.p_value:.4g} < {alpha} ({test.method})"
        )
    elif significant and not positive:
        status = REGRESSION
        reason = (
            f"mean change {improvement:+.4f} with p={test.p_value:.4g} < {alpha}: "
            "the candidate is reliably worse"
        )
    else:
        status = INCONCLUSIVE
        if improvement is not None and improvement > 0.0:
            reason = (
                f"p={test.p_value:.4g} does not clear alpha={alpha}, so the mean "
                f"improvement {improvement:+.4f} is not statistically reliable"
            )
        else:
            reason = (
                f"mean improvement {improvement:+.4f} is not positive "
                f"(p={test.p_value:.4g})"
            )

    return GateReport(
        status, len(pairs), min_seeds, alpha, baseline_mean, candidate_mean, improvement,
        wins, losses, test.p_value, test.t_statistic, test.df, tuple(deltas), test.method, reason,
    )


# ------------------------------------------------------------------- artifacts
def _latex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in str(text))


def _macro_name(text: str) -> str:
    cleaned = "".join(character if character.isalnum() else " " for character in str(text))
    parts = [part for part in cleaned.split() if part]
    return "Vajra" + "".join(part[:1].upper() + part[1:] for part in parts)


def _format_number(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and not math.isfinite(value):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _format_p_value(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value < 1e-4:
        return "<0.0001"
    return f"{value:.4f}"


def render_metrics_tex(payload: Mapping[str, Any]) -> str:
    """Render LaTeX macros straight from the raw results document."""
    gate = dict(payload.get("gate") or {})
    baseline = dict(payload.get("baseline") or {})
    best = dict(payload.get("best") or {})
    macros: List[Tuple[str, str]] = [
        ("VajraTopic", _latex_escape(str(payload.get("topic", "")))),
        ("VajraRunId", _latex_escape(str(payload.get("run_id", "")))),
        ("VajraGeneratedAt", _latex_escape(str(payload.get("generated_at", "")))),
        ("VajraSeedCount", str(int(payload.get("seeds") or 0))),
        ("VajraBudget", str(int(payload.get("budget") or 0))),
        ("VajraNodesEvaluated", str(int(payload.get("nodes_evaluated") or 0))),
        ("VajraMetricName", _latex_escape(str(payload.get("metric_name", "accuracy")))),
        ("VajraBaselineMean", _format_number(baseline.get("mean"))),
        ("VajraBaselineStdev", _format_number(baseline.get("stdev"))),
        ("VajraBestNode", _latex_escape(str(best.get("node_id", "n/a")))),
        ("VajraBestMean", _format_number(best.get("mean"))),
        ("VajraBestStdev", _format_number(best.get("stdev"))),
        ("VajraDeltaMean", _format_number(gate.get("mean_improvement"))),
        ("VajraPValue", _format_p_value(gate.get("p_value"))),
        ("VajraTStatistic", _format_number(gate.get("t_statistic"))),
        ("VajraDegreesOfFreedom", str(int(gate.get("df") or 0))),
        ("VajraSeedsWon", str(int(gate.get("wins") or 0))),
        ("VajraSeedsLost", str(int(gate.get("losses") or 0))),
        ("VajraGateStatus", _latex_escape(str(gate.get("status", NOT_RUN)))),
        ("VajraGateAlpha", _format_number(gate.get("alpha"))),
    ]
    lines = [
        "% Vajrastra metrics -- generated from results/metrics.json, never from model text.",
        f"% run: {payload.get('run_id', '')}",
        f"% gate: {gate.get('status', NOT_RUN)} ({gate.get('reason', '')})",
        "",
    ]
    for name, value in macros:
        lines.append(f"\\newcommand{{\\{name}}}{{{value}}}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- search
@dataclass
class Experiment:
    """One hypothesis evaluated across the paired seed list."""

    node_id: str
    params: Dict[str, Any]
    seeds: List[int] = field(default_factory=list)
    metrics: List[Optional[float]] = field(default_factory=list)
    durations: List[float] = field(default_factory=list)
    raw_paths: List[str] = field(default_factory=list)
    mean: Optional[float] = None
    stdev: Optional[float] = None
    engine: str = "unknown"
    ok: bool = True
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "params": dict(self.params),
            "seeds": list(self.seeds),
            "metrics": list(self.metrics),
            "durations": list(self.durations),
            "mean": self.mean,
            "stdev": self.stdev,
            "engine": self.engine,
            "ok": self.ok,
            "error": self.error,
            "raw_paths": list(self.raw_paths),
        }


@dataclass
class TreeNode:
    """One hypothesis in the research tree."""

    node_id: str
    params: Dict[str, Any]
    parent_id: Optional[str] = None
    depth: int = 0
    origin: str = "baseline"
    rationale: str = ""
    visits: int = 1
    total_value: float = 0.0
    experiment: Optional[Experiment] = None
    gate: Optional[GateReport] = None
    status: str = "open"

    @property
    def mean_value(self) -> float:
        return (self.total_value / self.visits) if self.visits else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "params": dict(self.params),
            "parent_id": self.parent_id,
            "depth": self.depth,
            "origin": self.origin,
            "rationale": self.rationale,
            "visits": self.visits,
            "total_value": round(self.total_value, 6),
            "mean_value": round(self.mean_value, 6),
            "status": self.status,
            "experiment": self.experiment.as_dict() if self.experiment else None,
            "gate": self.gate.as_dict() if self.gate else None,
        }


@dataclass
class PersonaReview:
    """One adversarial review of the measured run."""

    persona: str
    focus: str
    verdict: str = ""
    score: Optional[float] = None
    strengths: Tuple[str, ...] = ()
    concerns: Tuple[str, ...] = ()
    required_experiments: Tuple[str, ...] = ()
    model: str = ""
    skipped: bool = False
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "persona": self.persona,
            "focus": self.focus,
            "verdict": self.verdict,
            "score": self.score,
            "strengths": list(self.strengths),
            "concerns": list(self.concerns),
            "required_experiments": list(self.required_experiments),
            "model": self.model,
            "skipped": self.skipped,
            "error": self.error,
        }


@dataclass
class ResearchRun:
    """Everything one ``vajrastra research --seeds N --budget M`` produced."""

    run_id: str
    topic: str
    seeds: int
    budget: int
    baseline: Experiment
    nodes: List[TreeNode]
    best: Optional[TreeNode]
    gate: GateReport
    reviews: List[PersonaReview]
    metrics_json_path: Path
    metrics_tex_path: Path
    tree_path: Path
    duration_s: float
    notes: Tuple[str, ...] = ()
    lesson_id: Optional[int] = None

    @property
    def verified(self) -> bool:
        return self.gate.verified

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "topic": self.topic,
            "seeds": self.seeds,
            "budget": self.budget,
            "duration_s": round(self.duration_s, 3),
            "verified": self.verified,
            "baseline": self.baseline.as_dict(),
            "best": self.best.as_dict() if self.best else None,
            "gate": self.gate.as_dict(),
            "nodes_evaluated": len([node for node in self.nodes if node.experiment]),
            "nodes": [node.as_dict() for node in self.nodes],
            "reviews": [review.as_dict() for review in self.reviews],
            "notes": list(self.notes),
            "lesson_id": self.lesson_id,
        }


Runner = Callable[[Dict[str, Any], int], Mapping[str, Any]]


def _slug(text: str, limit: int = 32) -> str:
    cleaned = [character if character.isalnum() else "-" for character in (text or "topic").lower()]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:limit] or "topic"


def _canonical(params: Mapping[str, Any]) -> str:
    return json.dumps(dict(params), sort_keys=True)


class MLScientist:
    """MCTS research loop with a multi-seed statistical gate."""

    def __init__(
        self,
        root: Path | str,
        *,
        router: Any = None,
        guard: Any = None,
        memory: Any = None,
        template: Optional[Path | str] = None,
        seeds: int = 3,
        budget: int = 10,
        explorer: float = 1.414,
        max_children: int = 3,
        max_depth: int = 3,
        timeout: float = 180.0,
        runner: Optional[Runner] = None,
        base_seed: int = 2026,
        value_scale: float = 0.05,
        min_seeds: int = 3,
        alpha: float = 0.05,
        higher_is_better: bool = True,
        metric_name: str = "accuracy",
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.router = router
        self.guard = guard
        self.memory = memory
        self.template = Path(template) if template else DEFAULT_TEMPLATE
        self.seeds = max(1, int(seeds))
        self.budget = max(0, int(budget))
        self.explorer = float(explorer)
        self.max_children = max(1, int(max_children))
        self.max_depth = max(1, int(max_depth))
        self.timeout = float(timeout)
        self.runner = runner
        self.base_seed = int(base_seed)
        self.value_scale = float(value_scale)
        self.min_seeds = max(2, int(min_seeds))
        self.alpha = float(alpha)
        self.higher_is_better = bool(higher_is_better)
        self.metric_name = metric_name
        self.baseline_params: Dict[str, Any] = dict(DEFAULT_PARAMS)
        self._nodes: Dict[str, TreeNode] = {}
        self._run_id = ""
        self._tried: set = set()
        self._seed_list: List[int] = []

    # ------------------------------------------------------------------ paths
    @property
    def tree_path(self) -> Path:
        return self.root / TREE_RELPATH

    @property
    def runs_dir(self) -> Path:
        return self.root / RUNS_RELPATH

    @property
    def metrics_json_path(self) -> Path:
        return self.root / METRICS_JSON

    @property
    def metrics_tex_path(self) -> Path:
        return self.root / METRICS_TEX

    @property
    def seed_list(self) -> List[int]:
        """The paired seed list used by the most recent run."""
        return list(self._seed_list)

    # ----------------------------------------------------------------- running
    def run(self, topic: str, *, budget: Optional[int] = None, seeds: Optional[int] = None) -> ResearchRun:
        """Run the MCTS search and the statistical gate for *topic*."""
        started = time.perf_counter()
        topic = (topic or "").strip() or "untitled research topic"
        self.budget = max(0, int(self.budget if budget is None else budget))
        self.seeds = max(1, int(self.seeds if seeds is None else seeds))
        self._seed_list = [self.base_seed + index for index in range(self.seeds)]
        self._run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{_slug(topic)}"
        self._nodes = {}
        self._tried = set()

        notes: List[str] = []
        baseline_node = self._new_node("n0001", dict(self.baseline_params), None, 0, "baseline", "the untouched template")
        baseline_experiment = self._evaluate(baseline_node, "baseline")
        baseline_node.experiment = baseline_experiment
        baseline_node.status = "evaluated"
        baseline_node.total_value = 0.5
        self._tried.add(_canonical(baseline_node.params))
        if not baseline_experiment.ok:
            notes.append(f"baseline failed: {baseline_experiment.error}")
        self._write_tree(topic)

        evaluations = 0
        for _ in range(self.budget):
            parent = self._select(baseline_node)
            if parent is None:
                notes.append("tree depth reached; the search space is exhausted")
                break
            child = self._expand(parent)
            if child is None:
                notes.append("search space exhausted before the budget was spent")
                break
            experiment = self._evaluate(child, child.origin)
            child.experiment = experiment
            evaluations += 1
            if experiment.ok and experiment.metrics:
                child.gate = evaluate_gate(
                    baseline_experiment.metrics,
                    experiment.metrics,
                    min_seeds=self.min_seeds,
                    alpha=self.alpha,
                    higher_is_better=self.higher_is_better,
                )
                child.status = "evaluated"
                self._backpropagate(child, self._value(experiment.mean))
            else:
                child.status = "failed"
                notes.append(f"{child.node_id} failed: {experiment.error}")
            self._write_tree(topic)

        best = self._best_node(baseline_node)
        gate = best.gate if best is not None and best.gate is not None else evaluate_gate(
            baseline_experiment.metrics, [], min_seeds=self.min_seeds, alpha=self.alpha
        )
        if best is None:
            notes.append("no candidate beat the baseline; the run is reported as inconclusive")

        reviews = self.peer_review(topic, baseline_experiment, best, gate)
        payload = self._payload(topic, baseline_experiment, best, gate, reviews, evaluations)
        self._write_artifacts(payload)
        lesson_id = self._record_lesson(topic, payload, gate)

        return ResearchRun(
            run_id=self._run_id,
            topic=topic,
            seeds=self.seeds,
            budget=self.budget,
            baseline=baseline_experiment,
            nodes=list(self._nodes.values()),
            best=best,
            gate=gate,
            reviews=reviews,
            metrics_json_path=self.metrics_json_path,
            metrics_tex_path=self.metrics_tex_path,
            tree_path=self.tree_path,
            duration_s=time.perf_counter() - started,
            notes=tuple(notes),
            lesson_id=lesson_id,
        )

    # ------------------------------------------------------------------ search
    def _new_node(
        self,
        node_id: str,
        params: Mapping[str, Any],
        parent: Optional[TreeNode],
        depth: int,
        origin: str,
        rationale: str,
    ) -> TreeNode:
        node = TreeNode(
            node_id=node_id,
            params=dict(params),
            parent_id=parent.node_id if parent else None,
            depth=depth,
            origin=origin,
            rationale=rationale,
        )
        self._nodes[node_id] = node
        return node

    def _children(self, node: TreeNode) -> List[TreeNode]:
        return [child for child in self._nodes.values() if child.parent_id == node.node_id]

    def _ucb1(self, child: TreeNode, parent: TreeNode) -> float:
        if child.visits == 0:
            return float("inf")
        exploration = self.explorer * math.sqrt(math.log(max(parent.visits, 1)) / child.visits)
        return child.mean_value + exploration

    def _select(self, root: TreeNode) -> Optional[TreeNode]:
        """Descend by UCB1 to the next node that may still grow."""
        node = root
        if node.depth >= self.max_depth:
            return None
        while True:
            children = self._children(node)
            if len(children) < self.max_children:
                return node
            node = max(children, key=lambda child: self._ucb1(child, node))
            if node.depth >= self.max_depth:
                return None

    def _expand(self, parent: TreeNode) -> Optional[TreeNode]:
        if parent.depth >= self.max_depth:
            return None
        for params, rationale in self._candidates(parent.params):
            key = _canonical(params)
            if key in self._tried:
                continue
            self._tried.add(key)
            node_id = f"n{len(self._nodes) + 1:04d}"
            return self._new_node(node_id, params, parent, parent.depth + 1, "mutation", rationale)
        return None

    def _candidates(self, params: Mapping[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
        """Deterministic, ordered single-parameter mutations."""
        candidates: List[Tuple[Dict[str, Any], str]] = []
        for name in sorted(PARAM_GRID):
            for value in PARAM_GRID[name]:
                if params.get(name) == value:
                    continue
                proposal = dict(params)
                proposal[name] = value
                candidates.append((proposal, f"{name} {params.get(name)!r} -> {value!r}"))
        return candidates

    def _value(self, mean: Optional[float]) -> float:
        """Map a measured metric into [0, 1] for UCB1 backpropagation."""
        baseline = self._nodes.get("n0001")
        baseline_mean = None
        if baseline and baseline.experiment:
            baseline_mean = baseline.experiment.mean
        if mean is None or baseline_mean is None:
            return 0.0
        delta = (mean - baseline_mean) if self.higher_is_better else (baseline_mean - mean)
        scale = max(self.value_scale, 1e-9)
        return max(0.0, min(1.0, 0.5 + 0.5 * math.tanh(delta / scale)))

    def _backpropagate(self, node: TreeNode, value: float) -> None:
        cursor: Optional[TreeNode] = node
        while cursor is not None:
            cursor.visits += 1
            cursor.total_value += value
            cursor = self._nodes.get(cursor.parent_id) if cursor.parent_id else None

    def _best_node(self, baseline_node: TreeNode) -> Optional[TreeNode]:
        candidates = [
            node
            for node in self._nodes.values()
            if node is not baseline_node and node.experiment and node.experiment.ok and node.experiment.mean is not None
        ]
        if not candidates:
            return None
        verified = [node for node in candidates if node.gate is not None and node.gate.verified]
        pool = verified or candidates
        return max(
            pool,
            key=lambda node: (node.experiment.mean if self.higher_is_better else -node.experiment.mean, node.node_id),
        )

    # --------------------------------------------------------------- execution
    def _run_seed(self, params: Mapping[str, Any], seed: int) -> Mapping[str, Any]:
        if self.runner is not None:
            return self.runner(dict(params), seed)
        command = [
            sys.executable,
            str(self.template),
            "--params",
            json.dumps(dict(params), sort_keys=True),
            "--seed",
            str(seed),
        ]
        environment = dict(os.environ)
        environment.setdefault("PYTHONPATH", str(PROJECT_ROOT))
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(PROJECT_ROOT),
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExperimentError(f"benchmark did not run: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:] or ["no stderr"]
            raise ExperimentError(f"benchmark exited {completed.returncode}: {detail[0][:300]}")
        for line in reversed((completed.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ExperimentError(f"benchmark emitted invalid JSON: {exc}") from exc
        raise ExperimentError("benchmark produced no JSON on stdout")

    def _evaluate(self, node: TreeNode, slug: str) -> Experiment:
        experiment = Experiment(node_id=node.node_id, params=dict(node.params), seeds=list(self._seed_list))
        directory = self.runs_dir / self._run_id
        directory.mkdir(parents=True, exist_ok=True)
        errors: List[str] = []
        for seed in experiment.seeds:
            try:
                payload = dict(self._run_seed(node.params, seed))
                metric = payload.get("metric")
                if metric is None:
                    raise ExperimentError("benchmark result has no 'metric' field")
                metric_value = float(metric)
                experiment.metrics.append(metric_value)
                experiment.durations.append(float(payload.get("duration_s") or 0.0))
                experiment.engine = str(payload.get("engine") or experiment.engine)
                raw_path = directory / f"{node.node_id}-{seed}.json"
                raw_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                experiment.raw_paths.append(str(raw_path.relative_to(self.root)))
            except Exception as exc:  # any runner/template failure is a failed seed
                experiment.metrics.append(None)
                experiment.durations.append(0.0)
                errors.append(f"seed {seed}: {exc}")
        measured = [value for value in experiment.metrics if value is not None]
        if measured:
            experiment.mean = sum(measured) / len(measured)
            if len(measured) > 1:
                variance = sum((value - experiment.mean) ** 2 for value in measured) / (len(measured) - 1)
                experiment.stdev = math.sqrt(max(variance, 0.0))
            else:
                experiment.stdev = 0.0
        experiment.ok = bool(measured)
        if errors:
            experiment.error = "; ".join(errors)[:500]
        return experiment

    # ------------------------------------------------------------------ review
    def peer_review(
        self,
        topic: str,
        baseline: Experiment,
        best: Optional[TreeNode],
        gate: GateReport,
    ) -> List[PersonaReview]:
        measured = {
            "topic": topic,
            "metric": self.metric_name,
            "seeds": self.seeds,
            "baseline_mean": baseline.mean,
            "baseline_stdev": baseline.stdev,
            "candidate_mean": best.experiment.mean if best and best.experiment else None,
            "mean_improvement": gate.mean_improvement,
            "p_value": gate.p_value,
            "gate_status": gate.status,
            "seeds_won": gate.wins,
            "seeds_lost": gate.losses,
            "alpha": gate.alpha,
            "macros": "results/metrics.tex",
        }
        reviews: List[PersonaReview] = []
        for persona, focus in PERSONAS:
            review = PersonaReview(persona=persona, focus=focus)
            router = self.router
            if router is None or not getattr(router, "available", False):
                review.skipped = True
                review.error = "no GEMINI_API_KEY; review not fabricated"
                reviews.append(review)
                continue
            prompt = (
                f"Persona: {persona}.\nFocus: {focus}\n\n"
                "Measured results (authoritative, from results/metrics.tex):\n"
                f"{json.dumps(measured, indent=2, sort_keys=True)}\n\n"
                "Return JSON with keys: verdict (string), score (0-10 number), "
                "strengths (list of strings), concerns (list of strings), "
                "required_experiments (list of strings). Quote no numbers you were "
                "not given; refer to the macro names instead."
            )
            try:
                payload, response = router.generate_json(prompt, system=REVIEW_SYSTEM)
            except Exception as exc:  # model failures never corrupt the measurement
                review.skipped = True
                review.error = f"{type(exc).__name__}: {exc}"[:300]
                reviews.append(review)
                continue
            review.verdict = str(payload.get("verdict") or "")[:400]
            raw_score = payload.get("score")
            if isinstance(raw_score, (int, float)) and math.isfinite(float(raw_score)):
                review.score = max(0.0, min(10.0, float(raw_score)))
            review.strengths = tuple(str(item)[:300] for item in (payload.get("strengths") or [])[:6])
            review.concerns = tuple(str(item)[:300] for item in (payload.get("concerns") or [])[:6])
            review.required_experiments = tuple(
                str(item)[:300] for item in (payload.get("required_experiments") or [])[:6]
            )
            review.model = getattr(response, "model", "") or ""
            reviews.append(review)
        return reviews

    # --------------------------------------------------------------- artifacts
    def _payload(
        self,
        topic: str,
        baseline: Experiment,
        best: Optional[TreeNode],
        gate: GateReport,
        reviews: Sequence[PersonaReview],
        evaluations: int,
    ) -> Dict[str, Any]:
        return {
            "version": 1,
            "run_id": self._run_id,
            "topic": topic,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metric_name": self.metric_name,
            "seeds": self.seeds,
            "budget": self.budget,
            "alpha": self.alpha,
            "min_seeds": self.min_seeds,
            "nodes_evaluated": evaluations,
            "baseline": baseline.as_dict(),
            "best": (
                {
                    "node_id": best.node_id,
                    "params": dict(best.params),
                    "mean": best.experiment.mean if best.experiment else None,
                    "stdev": best.experiment.stdev if best.experiment else None,
                }
                if best is not None
                else {"node_id": None, "params": {}, "mean": None, "stdev": None}
            ),
            "gate": gate.as_dict(),
            "reviews": [review.as_dict() for review in reviews],
            "tree": str(self.tree_path.relative_to(self.root)),
        }

    def _write_tree(self, topic: str) -> None:
        payload = {
            "version": 1,
            "topic": topic,
            "run_id": self._run_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "algorithm": "mcts-ucb1",
            "exploration": self.explorer,
            "seeds": self.seeds,
            "budget": self.budget,
            "baseline_params": dict(self.baseline_params),
            "nodes": [node.as_dict() for node in self._nodes.values()],
        }
        self.tree_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.tree_path.with_name(self.tree_path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.tree_path)

    def _write_artifacts(self, payload: Mapping[str, Any]) -> None:
        self.metrics_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.metrics_json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.metrics_tex_path.write_text(render_metrics_tex(payload), encoding="utf-8")

    def _record_lesson(
        self, topic: str, payload: Mapping[str, Any], gate: GateReport
    ) -> Optional[int]:
        if self.memory is None:
            return None
        metrics = {
            "baseline_mean": payload.get("baseline", {}).get("mean"),
            "candidate_mean": payload.get("best", {}).get("mean"),
            "mean_improvement": gate.mean_improvement,
            "p_value": gate.p_value,
            "seeds": gate.n_seeds,
            "gate_status": gate.status,
        }
        lesson = self.memory.record_lesson(
            f"research: {topic}",
            root_cause=(
                f"MCTS searched {self.budget} hypotheses over {self.seeds} seeds; "
                f"gate {gate.status}: {gate.reason}"
            ),
            verified_patch=(
                json.dumps(payload.get("best", {}).get("params", {}), sort_keys=True)
                if gate.verified
                else ""
            ),
            experiment_metrics=metrics,
            tags=("research", "mcts", "ml", gate.status),
            verified=gate.verified,
        )
        return lesson.id


__all__ = [
    "DEFAULT_PARAMS",
    "DEFAULT_TEMPLATE",
    "Experiment",
    "ExperimentError",
    "GateReport",
    "MLScientist",
    "PERSONAS",
    "PersonaReview",
    "ResearchRun",
    "TreeNode",
    "TTestResult",
    "evaluate_gate",
    "paired_t_test",
    "render_metrics_tex",
]
