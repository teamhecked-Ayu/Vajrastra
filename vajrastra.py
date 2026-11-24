#!/usr/bin/env python3
"""Vajrastra -- universal cross-OS autonomous AI engineer (Phase 3/4 CLI).

Runs identically as ``python vajrastra.py`` on Android, Parrot OS, Linux, macOS
and Windows using the Python standard library only.

Commands::

    vajrastra fix <target> [--repair] [--apply]
    vajrastra build "<prompt>" [--lang LANG]
    vajrastra research "<topic>" [--seeds N] [--budget M]
    vajrastra evolve [--push] [--no-tests]
    vajrastra worker [--once] [--interval SECONDS]
    vajrastra memory <search|stats> [query]
    vajrastra sync [--push|--pull] [--import PATH]
    vajrastra github inspect --base main --task "..."
    vajrastra github apply --base main --branch vajrastra/work/<slug> --task "..." --changes changes.json

GitHub writes require separate typed approval for branch creation, commit/push,
and pull-request creation. The workflow never merges or deletes branches.

With no arguments it opens the interactive REPL with the ``vajrastra>`` prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.memory_engine import (  # noqa: E402  (path setup must come first)
    LessonRecord,
    MemoryEngine,
    normalize_signature,
    signature_hash,
    slugify,
)
from core.os_adapter import (  # noqa: E402
    HEAVY_TASKS,
    MODE_ENV_VAR,
    ROOT_ENV_VAR,
    ExecutionMode,
    OSAdapter,
    TaskClass,
    parse_task_class,
)
from core.model_router import ModelRouter  # noqa: E402
from core.security_guard import (  # noqa: E402
    IntegrityError,
    SecurityGuard,
    SecurityViolation,
)

VERSION = "0.3.0"
REPL_PROMPT = "vajrastra> "
RULE_WIDTH = 72

_TASK_KEYWORDS: Dict[TaskClass, tuple] = {
    TaskClass.TRAINING: ("train", "training", "fine-tune", "finetune", "model", "dataset", "ml", "torch"),
    TaskClass.GPU: ("gpu", "cuda", "tensorrt", "kernel", "accelerator"),
    TaskClass.FUZZ: ("fuzz", "fuzzing", "afl", "libfuzzer", "crash", "exploit", "vulnerability"),
    TaskClass.BENCHMARK: ("benchmark", "perf", "latency", "throughput", "profil"),
    TaskClass.RESEARCH: ("research", "paper", "survey", "compare"),
    TaskClass.DIAGNOSTIC: ("fix", "bug", "error", "traceback", "debug", "exception"),
    TaskClass.PATCH: ("patch", "refactor", "repair"),
}


# --------------------------------------------------------------------- helpers
def _rule(title: str = "") -> str:
    if not title:
        return "-" * RULE_WIDTH
    padded = f" {title} "
    return padded + "-" * max(4, RULE_WIDTH - len(padded))


def _out(stream, text: str = "") -> None:
    print(text, file=stream if stream is not None else sys.stdout)


def _format_metrics(metrics: Dict[str, Any]) -> str:
    if not metrics:
        return "{}"
    return json.dumps(metrics, sort_keys=True)[:200]


def classify_prompt(prompt: str) -> TaskClass:
    lowered = (prompt or "").lower()
    for task in (
        TaskClass.FUZZ,
        TaskClass.TRAINING,
        TaskClass.GPU,
        TaskClass.BENCHMARK,
        TaskClass.RESEARCH,
        TaskClass.DIAGNOSTIC,
    ):
        if any(keyword in lowered for keyword in _TASK_KEYWORDS.get(task, ())):
            return task
    return TaskClass.BUILD


@dataclass
class AppContext:
    root: Path
    adapter: OSAdapter
    guard: SecurityGuard
    memory: MemoryEngine

    def close(self) -> None:
        self.memory.close()


def _resolve_root(override: Optional[str] = None) -> Path:
    candidate = override or os.environ.get(ROOT_ENV_VAR) or str(PROJECT_ROOT)
    return Path(candidate).expanduser().resolve()


def build_context(root_override: Optional[str] = None, *, quiet: bool = True) -> AppContext:
    root = _resolve_root(root_override)
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory_bank").mkdir(parents=True, exist_ok=True)
    adapter = OSAdapter()
    guard = SecurityGuard(root)

    if guard.lock_path.is_file() is False and guard.manifest_targets():
        try:
            guard.seal(force=False)
            if not quiet:
                _out(sys.stdout, "[security] sealed trusted core with SHA-256 digests")
        except IntegrityError:
            pass

    report = guard.verify()
    if report.status == "drift" and not quiet:
        _out(sys.stderr, "[security] WARNING: " + report.describe())

    return AppContext(root=root, adapter=adapter, guard=guard, memory=MemoryEngine(root))


def _print_routing(stream, decision) -> None:
    _out(stream, _rule("routing"))
    _out(stream, f"mode        : {decision.mode.value}")
    _out(stream, f"task class  : {decision.task_class.value}")
    _out(stream, f"reason      : {decision.reason}")
    if decision.mode is ExecutionMode.CLOUD_BRIDGE:
        _out(stream, f"bridge ready: {'yes' if decision.cloud_ready else 'no'}")
    for note in decision.notes:
        _out(stream, f"note        : {note}")


def _scan_query(ctx: AppContext, files: Sequence[Path]) -> str:
    categories = set()
    for path in files:
        for violation in ctx.guard.scan_file(path).violations:
            categories.add(violation.category)
            categories.add(violation.category.replace("_", " "))
    return " ".join(sorted(categories)) or "security firewall"


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _collect_python_files(root: Path, target: Path) -> List[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(
            path
            for path in target.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return []


# -------------------------------------------------------------------- commands
def cmd_fix(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    raw_target = " ".join(args.target).strip()
    _out(out, _rule("vajrastra fix"))
    _print_routing(out, ctx.adapter.route(TaskClass.PATCH))

    candidate = Path(raw_target).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()

    looks_like_traceback = any(
        marker in raw_target
        for marker in ("Traceback (most recent call last)", "Error:", "Exception")
    )

    if candidate.exists() and not looks_like_traceback:
        from engines.hyper_debugger import HyperDebugger

        debugger = HyperDebugger(
            ctx.root, guard=ctx.guard, memory=ctx.memory, router=ModelRouter()
        )
        analysis = debugger.analyze(
            candidate, use_llm=not bool(getattr(args, "no_llm", False))
        )
        _out(out, _rule("stages"))
        _out(out, f"files       : {len(analysis.files)}")
        _out(out, f"engines     : {analysis.engines.describe()}")
        if analysis.languages:
            _out(out, "languages   : " + ", ".join(
                f"{name}({count})" for name, count in sorted(analysis.languages.items())
            ))
        if analysis.lesson_ids:
            _out(out, "memories    : " + ", ".join(
                f"#{ident}" for ident in analysis.lesson_ids
            ))
        for note in analysis.notes:
            _out(out, f"note        : {note}")
        for finding in analysis.findings:
            _out(out, "  " + finding.render())
        _out(out, _rule("scan"))
        _out(out, f"findings    : {len(analysis.findings)} (blocking: {analysis.blocking_count})")
        _out(out, "signature   : " + analysis.signature.splitlines()[0][:200])

        patch = None
        if bool(getattr(args, "repair", False)):
            patch = debugger.repair(
                candidate,
                analysis,
                apply=bool(getattr(args, "apply", False)),
                test_command=getattr(args, "test", None),
            )
            _print_patch(out, patch)

        blocking = analysis.blocking_count
        if patch is not None and not patch.verified:
            blocking = max(blocking, 1)
        return 1 if blocking else 0

    signature_text = raw_target
    if not signature_text:
        _out(sys.stderr, "error: provide a file, directory or error text")
        return 2

    digest = signature_hash(signature_text)
    _out(out, _rule("signature"))
    _out(out, f"hash        : {digest[:16]}")
    _out(out, f"normalised  : {normalize_signature(signature_text)[:200]}")

    lesson = ctx.memory.get_by_hash(digest)
    if lesson:
        ctx.memory.record_hit(lesson.id, resolved=lesson.solved)
        _print_lessons(out, [lesson])
        return 0

    lessons = ctx.memory.search(signature_text, limit=3)
    if lessons:
        _out(out, _rule("related lessons"))
        _print_lessons(out, lessons)
        return 0

    _out(out, _rule("result"))
    _out(out, "no lesson stored for this signature yet.")
    _out(out, "after you verify a repair, record it with:")
    _out(out, '  python -c "from core.memory_engine import MemoryEngine; '
              "MemoryEngine('.').record_lesson('<signature>', root_cause='...', "
              "verified_patch='...', verified=True)\"")
    return 0


def _print_patch(out, patch) -> None:
    _out(out, _rule("verified patch"))
    _out(out, f"target      : {patch.target}")
    _out(out, f"verified    : {'yes' if patch.verified else 'no'}")
    if patch.model:
        _out(out, f"model       : {patch.model}")
    if patch.lesson_id:
        _out(out, f"lesson      : #{patch.lesson_id}")
    if patch.root_cause:
        _out(out, "root cause  : " + patch.root_cause[:600])
    if patch.message:
        _out(out, "status      : " + patch.message)
    if patch.test_output:
        _out(out, _rule("isolated test"))
        for line in patch.test_output.splitlines()[-20:]:
            _out(out, "  " + line)
    if patch.diff:
        _out(out, _rule("diff"))
        _out(out, patch.diff.rstrip())


def _print_lessons(out, lessons: Sequence[LessonRecord]) -> None:
    if not lessons:
        _out(out, "no matching lessons in memory_bank/lessons.sqlite")
        return
    for lesson in lessons:
        _out(out, f"- lesson #{lesson.id} (hits {lesson.hit_count}, wins {lesson.success_count})")
        _out(out, f"  signature : {lesson.error_signature.splitlines()[0][:160]}")
        if lesson.root_cause:
            _out(out, f"  root cause: {lesson.root_cause[:400]}")
        if lesson.verified_patch:
            _out(out, f"  patch     : {lesson.verified_patch[:600]}")
        else:
            _out(out, "  patch     : (not verified yet)")
        if lesson.experiment_metrics:
            _out(out, f"  metrics   : {_format_metrics(dict(lesson.experiment_metrics))}")
        if lesson.tags:
            _out(out, f"  tags      : {', '.join(lesson.tags)}")


_PLAN_TEMPLATES: Dict[TaskClass, List[str]] = {
    TaskClass.BUILD: [
        "resolve target package layout and entrypoints",
        "draft module skeleton and public interfaces",
        "clear every generated file through core.security_guard",
        "write stdlib unittest coverage",
        "record verified lessons in memory_bank/lessons.sqlite",
    ],
    TaskClass.TRAINING: [
        "freeze dataset + hyperparameter spec",
        "package the Colab notebook payload",
        "dispatch the job via the GitHub API bridge",
        "poll metrics and capture loss/accuracy curves",
        "store experiment_metrics against the lesson signature",
    ],
    TaskClass.GPU: [
        "profile kernel occupancy requirements",
        "select execution target from os_adapter routing",
        "run the kernel job and capture timing",
        "store the winning configuration as a skill",
    ],
    TaskClass.FUZZ: [
        "build corpus seed set",
        "compile harness with sanitizers on the target host",
        "run the fuzz campaign and minimise crashes",
        "trace each crash to a signature and record the patch",
    ],
    TaskClass.BENCHMARK: [
        "capture baseline timings",
        "apply the candidate change behind a flag",
        "compare distributions, guard against noise",
        "record measured deltas as experiment_metrics",
    ],
    TaskClass.RESEARCH: [
        "collect local lessons and skills for the topic",
        "outline the open questions",
        "write the brief in memory_bank/research/",
    ],
    TaskClass.DIAGNOSTIC: [
        "normalise the failure signature",
        "look the signature up in the episodic ledger",
        "apply the verified patch and re-run the failing check",
    ],
    TaskClass.PATCH: [
        "normalise the failure signature",
        "look up the verified patch",
        "validate the patch through the security firewall",
        "re-run the failing check and record the outcome",
    ],
}


def cmd_build(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        _out(sys.stderr, "error: provide a build prompt")
        return 2

    task = classify_prompt(prompt)
    decision = ctx.adapter.route(task)
    language = (getattr(args, "lang", None) or "python").strip().lower()
    _out(out, _rule("vajrastra build"))
    _out(out, f"prompt      : {prompt}")
    _out(out, f"language    : {language}")
    _print_routing(out, decision)

    steps = _PLAN_TEMPLATES.get(task, _PLAN_TEMPLATES[TaskClass.BUILD])
    _out(out, _rule("plan"))
    for index, step in enumerate(steps, start=1):
        _out(out, f"  {index}. {step}")

    slug = slugify(prompt)[:48]
    router = ModelRouter()
    plan_only = bool(getattr(args, "plan", False))
    can_generate = (
        not plan_only
        and task not in HEAVY_TASKS
        and decision.mode is ExecutionMode.LOCAL_NATIVE
        and router.available
    )

    if plan_only:
        _out(out, "engine      : plan only (--plan requested; no code generation)")

    if can_generate:
        from engines.code_architect import ArchitectError, CodeArchitect

        architect = CodeArchitect(
            ctx.root, router=router, guard=ctx.guard, memory=ctx.memory
        )
        try:
            result = architect.build(
                prompt,
                language=language,
                rounds=getattr(args, "rounds", None),
                out=getattr(args, "out", None),
            )
        except ArchitectError as exc:
            _out(sys.stderr, f"error: {exc}")
            return 2
        _out(out, _rule("adversarial build"))
        _out(out, f"rounds      : {result.rounds}")
        _out(out, f"verified    : {'yes' if result.verified else 'no'}")
        _out(out, f"lesson      : #{result.lesson_id}")
        if result.summary:
            _out(out, f"summary     : {result.summary[:400]}")
        for path in result.written:
            _out(out, f"  wrote     : {path}")
        for path in result.tests:
            _out(out, f"  tests     : {path}")
        if result.red_summary:
            _out(out, f"red team    : {result.red_summary[:400]}")
        for failure in result.failures:
            _out(out, f"  failure   : {failure[:300]}")
        for note in result.notes:
            _out(out, f"  note      : {note[:300]}")
        if result.workspace and result.verified:
            _out(out, f"workspace   : {result.workspace}")
        return 0 if result.verified else 1

    payload = {
        "prompt": prompt,
        "task_class": task.value,
        "language": language,
        "routing": decision.as_dict(),
        "steps": steps,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if not router.available and not plan_only and task not in HEAVY_TASKS:
        _out(out, "engine      : plan only (set GEMINI_API_KEY for adversarial code generation)")

    if decision.mode is ExecutionMode.CLOUD_BRIDGE:
        path = _write_json(
            ctx.root / "memory_bank" / "bridge" / "outbox" / f"{_stamp()}-{slug}.json",
            {**payload, "target": "google_colab", "dispatch": "github_api"},
        )
        _out(out, _rule("colab bridge"))
        _out(out, f"job spec    : {path.relative_to(ctx.root)}")
        if not decision.cloud_ready:
            _out(out, "status      : queued (no GitHub token; set VAJRASTRA_GITHUB_TOKEN)")
        else:
            _out(out, "status      : ready for dispatch via the GitHub API bridge")
    else:
        path = _write_json(
            ctx.root / "memory_bank" / "plans" / f"{_stamp()}-{slug}.json", payload
        )
        _out(out, _rule("local plan"))
        _out(out, f"plan        : {path.relative_to(ctx.root)}")
    return 0


def cmd_research(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    topic = " ".join(args.topic).strip()
    if not topic:
        _out(sys.stderr, "error: provide a research topic")
        return 2

    lessons = ctx.memory.search(topic, limit=10)
    open_lessons = ctx.memory.unresolved(limit=10)
    skills = ctx.memory.skills.search_skills(topic, limit=10)

    _out(out, _rule("vajrastra research"))
    _out(out, f"topic       : {topic}")
    _out(out, f"lessons     : {len(lessons)}")
    _out(out, f"skills      : {len(skills)}")

    lines = [f"# Research brief: {topic}", "", f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", ""]
    lines.append("## Known lessons")
    if lessons:
        for lesson in lessons:
            lines.append(f"### Lesson #{lesson.id}")
            lines.append(f"- signature: `{lesson.error_signature.splitlines()[0][:200]}`")
            lines.append(f"- root cause: {lesson.root_cause or 'unknown'}")
            lines.append(f"- patch: {lesson.verified_patch or '(unverified)'}")
            lines.append(f"- metrics: `{_format_metrics(dict(lesson.experiment_metrics))}`")
            lines.append("")
    else:
        lines.append("_No matching lessons in the episodic ledger._")
        lines.append("")

    lines.append("## Reusable skills")
    if skills:
        for skill in skills:
            lines.append(f"- `{skill.name}` ({skill.slug}): {skill.description or 'no description'}")
    else:
        lines.append("_No matching skills._")
    lines.append("")

    lines.append("## Open questions")
    if open_lessons:
        for lesson in open_lessons:
            lines.append(f"- unresolved signature: `{lesson.error_signature.splitlines()[0][:160]}`")
    else:
        lines.append("_Ledger has no unresolved signatures._")
    lines.append("")
    lines.append("## Method")
    lines.append("- Phase 1 research is offline: it mines local memory only.")
    lines.append("- Every new finding is recorded as a lesson with metrics before it is trusted.")

    path = ctx.root / "memory_bank" / "research" / f"{_stamp()}-{slugify(topic)[:48]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _out(out, _rule("brief"))
    _out(out, f"written     : {path.relative_to(ctx.root)}")

    seeds = getattr(args, "seeds", None)
    budget = getattr(args, "budget", None)
    if seeds is None and budget is None:
        _out(out, "next        : pass --seeds 3 --budget 10 to run the MCTS ML scientist")
        return 0

    from engines.ml_scientist import MLScientist

    scientist = MLScientist(
        ctx.root,
        router=ModelRouter(),
        guard=ctx.guard,
        memory=ctx.memory,
        seeds=3 if seeds is None else int(seeds),
        budget=10 if budget is None else int(budget),
    )
    run = scientist.run(topic)
    _out(out, _rule("mcts search"))
    _out(out, f"run         : {run.run_id}")
    _out(out, f"seeds       : {run.seeds} {scientist.seed_list}")
    _out(out, f"budget      : {run.budget} (evaluated {len([n for n in run.nodes if n.experiment])})")
    _out(out, f"baseline    : {run.baseline.mean if run.baseline.mean is not None else 'n/a'}")
    if run.best is not None:
        _out(out, f"best node   : {run.best.node_id} {json.dumps(run.best.params, sort_keys=True)}")
        _out(out, f"best mean   : {run.best.experiment.mean if run.best.experiment else 'n/a'}")
    _out(out, _rule("statistical gate"))
    _out(out, f"status      : {run.gate.status}")
    _out(out, f"reason      : {run.gate.reason}")
    if run.gate.p_value is not None:
        _out(out, f"p-value     : {run.gate.p_value:.6g} (alpha {run.gate.alpha}, {run.gate.method})")
    _out(out, f"wins/losses : {run.gate.wins}/{run.gate.losses} across {run.gate.n_seeds} seed(s)")
    _out(out, _rule("artifacts"))
    _out(out, f"tree        : {run.tree_path.relative_to(ctx.root)}")
    _out(out, f"results     : {run.metrics_json_path.relative_to(ctx.root)}")
    _out(out, f"macros      : {run.metrics_tex_path.relative_to(ctx.root)} (rendered from raw JSON)")
    _out(out, _rule("peer review"))
    for review in run.reviews:
        if review.skipped:
            _out(out, f"  {review.persona:<20} skipped ({review.error})")
            continue
        _out(out, f"  {review.persona:<20} score {review.score if review.score is not None else 'n/a'} -> {review.verdict[:160]}")
        for concern in review.concerns:
            _out(out, f"    concern   : {concern[:200]}")
        for experiment in review.required_experiments:
            _out(out, f"    required  : {experiment[:200]}")
    for note in run.notes:
        _out(out, f"note        : {note}")
    return 0 if run.verified else 1


def cmd_evolve(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    from engines.self_evolver import SelfEvolver

    _out(out, _rule("vajrastra evolve"))
    report = ctx.guard.verify()
    _out(out, f"integrity   : {report.status}")
    if report.status == "unsealed":
        _out(out, "action      : no core/security_lock.json; trusted core cannot be audited")
    elif report.modified or report.missing:
        _out(out, f"modified    : {', '.join(report.modified) or 'none'}")
        _out(out, f"missing     : {', '.join(report.missing) or 'none'}")

    _out(out, "immutable   : core/security_guard.py"
                f" -> {'protected' if ctx.guard.is_protected('core/security_guard.py') else 'UNPROTECTED'}")

    skills = ctx.memory.skills.list_skills()
    failed: List[str] = []
    if skills:
        _out(out, _rule("procedural skills"))
    for skill in skills:
        if not skill.path:
            continue
        result = ctx.guard.scan_source(skill.code, filename=skill.path)
        status = "safe" if result.ok else f"{len(result.blocking)} blocking finding(s)"
        if not result.ok:
            failed.append(skill.slug)
        _out(out, f"  {skill.slug:<32} {status}")

    push = bool(getattr(args, "push", False))
    client = None
    if push:
        from core.github_sync import GitHubClient

        client = GitHubClient(
            repo=getattr(args, "repo", None) or "teamhecked-Ayu/Vajrastra",
            branch=getattr(args, "branch", None) or "main",
        )
        if not client.token():
            _out(sys.stderr, "error: --push needs GITHUB_TOKEN set")
            return 2

    evolver = SelfEvolver(
        ctx.root,
        guard=ctx.guard,
        memory=ctx.memory,
        router=ModelRouter(),
        client=client,
        run_tests=not bool(getattr(args, "no_tests", False)),
    )
    result = evolver.review(push=push)

    _out(out, _rule("findings"))
    if result.findings:
        for finding in result.findings:
            _out(out, f"  [{finding.severity}] {finding.kind}: {finding.title} -- {finding.evidence[:160]}")
    else:
        _out(out, "  nothing observed; ledger is fully verified")

    _out(out, _rule("candidates"))
    if result.candidates:
        for candidate in result.candidates:
            if candidate.accepted:
                _out(out, f"  {candidate.candidate_id} {candidate.path} -> accepted")
            else:
                _out(out, f"  {candidate.candidate_id} {candidate.path} -> rejected ({candidate.rejection_reason})")
    else:
        _out(out, "  no candidate improvements were proposed")

    _out(out, _rule("staging"))
    _out(out, f"branch      : {result.staging_branch}")
    _out(out, f"directory   : {result.staging_dir.relative_to(ctx.root)}")
    _out(out, f"pushed      : {', '.join(result.pushed) or 'none (local staging only)'}")
    for note in result.notes:
        _out(out, f"note        : {note}")

    _out(out, _rule("report"))
    _out(out, f"written     : {result.report_path.relative_to(ctx.root)}")
    _out(out, f"duration    : {result.duration_s:.2f}s")
    if failed:
        _out(out, f"firewall    : {len(failed)} skill(s) failed: {', '.join(failed)}")
        return 1
    return 1 if report.status == "drift" else 0


def cmd_memory(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    action = args.memory_action
    if action == "stats":
        stats = ctx.memory.stats()
        _out(out, _rule("vajrastra memory stats"))
        _out(out, f"database    : {stats['db_path']}")
        _out(out, f"lessons     : {stats['lessons']} ({stats['verified_patches']} verified, {stats['unsolved']} open)")
        _out(out, f"hits        : {stats['total_hits']}")
        _out(out, f"skills      : {stats['skills']} in {stats['skill_dir']}")
        _out(out, f"fts5 index  : {'enabled' if stats['fts_enabled'] else 'unavailable (LIKE fallback)'}")
        _out(out, f"size        : {stats['db_bytes']} bytes")
        if stats["top_tags"]:
            _out(out, "top tags    : " + ", ".join(f"{tag}({count})" for tag, count in stats["top_tags"]))
        recent = ctx.memory.recent(limit=5)
        if recent:
            _out(out, _rule("recent"))
            _print_lessons(out, recent)
        return 0

    query = " ".join(args.query).strip()
    if not query:
        _out(sys.stderr, "error: memory search needs a query")
        return 2
    lessons = ctx.memory.search(query, limit=args.limit)
    exact = ctx.memory.get_by_signature(query)
    if exact and all(lesson.id != exact.id for lesson in lessons):
        lessons = [exact] + lessons
    _out(out, _rule("vajrastra memory search"))
    _out(out, f"query       : {query}")
    _out(out, f"results     : {len(lessons)}")
    _print_lessons(out, lessons)
    return 0


def cmd_sync(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    _out(out, _rule("vajrastra sync"))
    merged: Dict[str, int] = {"lessons": 0, "skills": 0}

    import_path = getattr(args, "import_path", None)
    if import_path:
        source = Path(import_path).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        try:
            snapshot = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _out(sys.stderr, f"error: cannot read snapshot: {exc}")
            return 2
        merged = ctx.memory.import_snapshot(snapshot)
        _out(out, f"imported    : {source}")
        _out(out, f"lessons     : +{merged['lessons']}")
        _out(out, f"skills      : +{merged['skills']}")

    snapshot = ctx.memory.export_snapshot()
    target = Path(getattr(args, "out", None) or ctx.root / "memory_bank" / "sync" / f"snapshot-{_stamp()}.json")
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    _write_json(target, snapshot)
    _out(out, _rule("snapshot"))
    _out(out, f"written     : {target}")
    _out(out, f"lessons     : {len(snapshot['lessons'])}")
    _out(out, f"skills      : {len(snapshot['skills'])}")

    push = bool(getattr(args, "push", False))
    pull = bool(getattr(args, "pull", False))
    if push or pull:
        from core.github_sync import GitHubClient, GitHubError

        client = GitHubClient(
            repo=getattr(args, "repo", None) or "teamhecked-Ayu/Vajrastra",
            branch=getattr(args, "branch", None) or "main",
        )
        if not client.token():
            _out(sys.stderr, "error: no GITHUB_TOKEN set; cannot reach GitHub")
            return 2
        _out(out, _rule("github"))
        _out(out, f"repository  : {client.repo}@{client.branch}")
        try:
            report = client.sync_memory(
                ctx.memory,
                push=push,
                pull=pull,
                remote_path=getattr(args, "remote_path", None)
                or "memory_bank/lessons.sqlite",
            )
        except GitHubError as exc:
            _out(sys.stderr, f"error: {exc}")
            return 2
        _out(out, f"pulled      : {report.pulled_lessons} lesson(s), {report.pulled_skills} skill(s)")
        _out(out, f"pushed      : {'yes' if report.pushed else 'no'} ({report.pushed_bytes} bytes)")
        if report.commit_sha:
            _out(out, f"commit      : {report.commit_sha[:12]}")
        for note in report.notes:
            _out(out, f"note        : {note}")

    device = ctx.adapter.profile.host_os.value
    _out(out, _rule("bridge"))
    if ctx.adapter.host_os.value == "android":
        _out(out, "device      : android (Cloud Bridge Mode)")
        _out(out, "next        : commit this snapshot and let a Colab worker import it")
    else:
        _out(out, f"device      : {device} (Local Native Mode)")
        _out(out, "next        : commit or copy the snapshot to sync another machine")
    return 0


def cmd_worker(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    from core.github_sync import GitHubClient
    from engines.job_worker import JobWorker, WorkerError

    _out(out, _rule("vajrastra worker"))
    client = GitHubClient(
        repo=getattr(args, "repo", None) or "teamhecked-Ayu/Vajrastra",
        branch=getattr(args, "branch", None) or "main",
    )
    if not client.token():
        _out(sys.stderr, "error: no GITHUB_TOKEN set; the inbox cannot be polled")
        return 2

    worker = JobWorker(
        ctx.root,
        client=client,
        router=ModelRouter(),
        guard=ctx.guard,
        memory=ctx.memory,
        adapter=ctx.adapter,
        branch=client.branch,
        inbox=getattr(args, "inbox", None) or "workspace/inbox",
        outbox=getattr(args, "outbox", None) or "workspace/outbox",
        max_jobs=getattr(args, "max_jobs", 5),
        allow_apply=bool(getattr(args, "allow_apply", False)),
    )
    _out(out, f"repository  : {client.repo}@{client.branch}")
    _out(out, f"inbox       : {worker.inbox}")
    iterations = 1 if bool(getattr(args, "once", False)) else getattr(args, "iterations", None)
    _out(out, f"polls       : {'1 (once)' if iterations == 1 else (iterations or 'forever')}")

    try:
        outcomes = worker.run(
            interval=float(getattr(args, "interval", 30.0) or 30.0), iterations=iterations
        )
    except WorkerError as exc:
        _out(sys.stderr, f"error: {exc}")
        return 2

    _out(out, _rule("jobs"))
    if not outcomes:
        _out(out, "no pending jobs in the inbox")
        return 0
    failures = 0
    for outcome in outcomes:
        _out(out, f"  {outcome.job_id:<32} {'ok' if outcome.ok else 'failed'} ({outcome.duration_s:.2f}s)")
        if outcome.summary:
            _out(out, f"    summary   : {outcome.summary[:200]}")
        if outcome.error:
            _out(out, f"    error     : {outcome.error[:200]}")
            failures += 1
        if outcome.checkpoint_sha:
            _out(out, f"    checkpoint: {outcome.checkpoint_sha[:12]}")
    return 1 if failures else 0



def cmd_github(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
    from core.github_sync import GitHubClient, GitHubError
    from engines.github_workflow import (
        ApprovalRequired,
        GitHubWorkflow,
        GitHubWorkflowError,
    )

    repo = getattr(args, "repo", None) or "teamhecked-Ayu/Vajrastra"
    base = getattr(args, "base", None) or "main"
    action = getattr(args, "github_action", "inspect")
    client = GitHubClient(repo=repo, branch=base)
    if not client.token():
        _out(sys.stderr, "error: no GITHUB_TOKEN set; GitHub inspection is unavailable")
        return 2

    def request_approval(operation: str, fingerprint: str) -> bool:
        phrase = f"APPROVE {operation} {fingerprint[:8]}"
        _out(out, _rule(f"approval required: {operation}"))
        _out(out, "This is a remote GitHub write. No action occurs unless you type the exact phrase.")
        try:
            answer = input(f"type {phrase}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == phrase

    workflow = GitHubWorkflow(
        client,
        approval_callback=request_approval if action == "apply" else None,
        approval_actor="interactive operator",
    )
    work_branch = getattr(args, "work_branch", None) or "vajrastra/work/preview"

    try:
        snapshot = workflow.inspect_branch(base, args.task)
        plan = workflow.create_plan(snapshot, args.task, work_branch)
        _out(out, _rule("github repository inspection"))
        _out(out, f"repository  : {repo}")
        _out(out, f"base branch : {base}")
        _out(out, f"commit      : {snapshot.commit_sha}")
        _out(out, f"safe files  : {len(snapshot.file_map)}")
        _out(out, f"indexed     : {len(snapshot.contents)}")
        _out(out, f"selected    : {len(plan.selected_files)}")
        for item in snapshot.file_map[:80]:
            _out(out, f"  {item.category:<14} {item.path} ({item.size} bytes)")
        if len(snapshot.file_map) > 80:
            _out(out, f"  ... {len(snapshot.file_map) - 80} more file(s)")
        _out(out, _rule("selected project context"))
        for selected in plan.selected_files:
            _out(out, f"  {selected}")
        _out(out, _rule("proposed plan"))
        for number, step in enumerate(plan.steps, 1):
            _out(out, f"  {number}. {step}")
        for warning in snapshot.warnings[:20]:
            _out(out, f"warning     : {warning}")
        if action == "inspect":
            _out(out, "read-only    : no branch, commit, push, or pull request was created")
            return 0

        changes_path = Path(args.changes).expanduser()
        if not changes_path.is_absolute():
            changes_path = (Path.cwd() / changes_path).resolve()
        payload = json.loads(changes_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise GitHubWorkflowError("changes JSON must be an object mapping file paths to new text")
        changeset = workflow.prepare_changes(plan, payload)
        _out(out, _rule("proposed diff (not yet committed)"))
        _out(out, changeset.diff)
        _out(out, f"patch bytes : {changeset.total_bytes}")
        commit = workflow.apply_changes(
            plan, changeset, message=getattr(args, "message", "feat: approved GitHub task")
        )
        _out(out, _rule("approved work branch commit"))
        _out(out, f"branch      : {commit.branch}")
        _out(out, f"commit      : {commit.commit_sha}")
        _out(out, f"changed     : {', '.join(commit.changed_paths)}")

        verification = workflow.verify_branch(
            plan,
            commit,
            workflow=getattr(args, "workflow", "watchdog.yml"),
            timeout_seconds=float(getattr(args, "timeout", 900.0)),
            poll_interval=float(getattr(args, "poll_interval", 15.0)),
        )
        _out(out, _rule("remote verification"))
        _out(out, f"status      : {verification.status}")
        _out(out, f"conclusion  : {verification.conclusion or 'pending'}")
        if verification.html_url:
            _out(out, f"run logs    : {verification.html_url}")
        for artifact in verification.artifacts:
            _out(out, f"artifact    : {artifact.get('name')} {artifact.get('url')}")
        if not verification.passed:
            _out(sys.stderr, "pull request blocked: remote verification did not pass")
            return 1

        pull = workflow.create_pull_request(
            plan,
            verification,
            title=getattr(args, "title", "Vajrastra: approved repository task"),
            body=getattr(args, "body", ""),
            draft=True,
        )
        _out(out, _rule("draft pull request"))
        _out(out, f"PR          : #{pull.number}")
        _out(out, f"url         : {pull.html_url}")
        _out(out, f"base        : {pull.base_branch}")
        _out(out, f"head        : {pull.head_branch}")
        _out(out, "merged      : no")
        return 0
    except ApprovalRequired as exc:
        _out(sys.stderr, f"approval denied: {exc}")
        return 3
    except (GitHubError, GitHubWorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        _out(sys.stderr, f"github workflow error: {exc}")
        return 2


# ------------------------------------------------------------------------ REPL
_HELP_TEXT = """\
commands:
  fix <target>            four-stage hyper-debugger: recall, static, symbolic, patch
  build "<prompt>"        adversarial red/blue build of verified code
  research "<topic>"      memory brief, then MCTS research with --seeds and --budget
  evolve                  meta-critic self-evolution into a staged branch
  worker                  poll workspace/inbox/ and execute dispatched jobs
  github inspect          read-only branch map and proposed plan
  github apply            approved work-branch, test, and draft-PR workflow
  memory stats            ledger and skill counters
  memory search <query>   keyword / signature lookup
  sync [--push|--pull]    share lessons.sqlite with the other hosts via GitHub
  help                    this text
  exit                    leave the REPL
"""


def dispatch(ctx: AppContext, parser: argparse.ArgumentParser, argv: Sequence[str]) -> int:
    handlers = {
        "fix": cmd_fix,
        "build": cmd_build,
        "research": cmd_research,
        "evolve": cmd_evolve,
        "worker": cmd_worker,
        "github": cmd_github,
        "memory": cmd_memory,
        "sync": cmd_sync,
    }
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:  # argparse already printed the message
        return int(exc.code or 0)
    if not args.command:
        _out(sys.stdout, _HELP_TEXT)
        return 0
    handler = handlers[args.command]
    return handler(ctx, args)


def repl(ctx: AppContext, parser: argparse.ArgumentParser) -> int:
    holder = ctx.adapter.route()
    _out(sys.stdout, f"Vajrastra {VERSION} - autonomous engineer console")
    _out(sys.stdout, f"host   : {ctx.adapter.profile.host_os.value} ({holder.describe()})")
    _out(sys.stdout, f"root   : {ctx.root}")
    _out(sys.stdout, f"memory : {ctx.memory.db_path.relative_to(ctx.root)}")
    _out(sys.stdout, "type 'help' for commands, 'exit' to leave")
    while True:
        try:
            line = input(REPL_PROMPT)
        except (EOFError, KeyboardInterrupt):
            _out(sys.stdout, "")
            return 0
        line = line.strip()
        if not line:
            continue
        if line in {"exit", "quit", ":q"}:
            return 0
        if line in {"help", "?"}:
            _out(sys.stdout, _HELP_TEXT)
            continue
        try:
            argv = shlex.split(line, posix=True)
        except ValueError as exc:
            _out(sys.stderr, f"parse error: {exc}")
            continue
        if not argv:
            continue
        try:
            dispatch(ctx, parser, argv)
        except SecurityViolation as exc:
            _out(sys.stderr, f"security firewall: {exc}")
            for violation in exc.violations:
                _out(sys.stderr, "  " + violation.describe())
        except IntegrityError as exc:
            _out(sys.stderr, f"integrity: {exc}")
        except (KeyError, OSError, ValueError) as exc:
            _out(sys.stderr, f"error: {exc}")


# ------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vajrastra",
        description="Universal cross-OS autonomous engineer, hyper-debugger and ML scientist.",
    )
    parser.add_argument("--root", default=None, help="workspace root (default: this checkout)")
    subparsers = parser.add_subparsers(dest="command")

    fix = subparsers.add_parser("fix", help="diagnose a file, tree or traceback")
    fix.add_argument("target", nargs="+", help="path, directory or error text")
    fix.add_argument("--repair", action="store_true", help="ask the model for a verified patch")
    fix.add_argument("--apply", action="store_true", help="write the patch once it verifies")
    fix.add_argument("--test", default=None, help="isolated verification command")
    fix.add_argument("--no-llm", action="store_true", help="skip the LLM adversarial review")

    build = subparsers.add_parser("build", help="build verified code with red/blue agents")
    build.add_argument("prompt", nargs="+", help="what to build")
    build.add_argument("--lang", default="python", help="target language (default: python)")
    build.add_argument("--rounds", type=int, default=None, help="red/blue rounds (default: 3)")
    build.add_argument("--out", default=None, help="output directory for verified files")
    build.add_argument("--plan", action="store_true", help="write a plan only, no generation")

    research = subparsers.add_parser("research", help="memory brief plus MCTS research")
    research.add_argument("topic", nargs="+", help="research topic")
    research.add_argument("--seeds", type=int, default=None, help="paired random seeds for the gate (default 3)")
    research.add_argument("--budget", type=int, default=None, help="MCTS experiments to run (default 10)")

    evolve = subparsers.add_parser("evolve", help="meta-critic self-evolution into a staged branch")
    evolve.add_argument("--push", action="store_true", help="push staged candidates to self-upgrade/<ts>")
    evolve.add_argument("--no-tests", action="store_true", help="skip the nested unittest gate")
    evolve.add_argument("--repo", default=None, help="owner/name for the staging branch")
    evolve.add_argument("--branch", default=None, help="base branch for staging (default: main)")

    worker = subparsers.add_parser("worker", help="poll workspace/inbox/ and execute jobs")
    worker.add_argument("--once", action="store_true", help="poll a single time and exit")
    worker.add_argument("--interval", type=float, default=30.0, help="seconds between polls (default 30)")
    worker.add_argument("--iterations", type=int, default=None, help="number of polls before exiting")
    worker.add_argument("--inbox", default=None, help="remote inbox directory (default: workspace/inbox)")
    worker.add_argument("--outbox", default=None, help="remote outbox directory (default: workspace/outbox)")
    worker.add_argument("--max-jobs", dest="max_jobs", type=int, default=5, help="jobs per poll (default 5)")
    worker.add_argument("--allow-apply", dest="allow_apply", action="store_true",
                        help="permit fix jobs to write their verified patch")
    worker.add_argument("--repo", default=None, help="owner/name (default: teamhecked-Ayu/Vajrastra)")
    worker.add_argument("--branch", default=None, help="branch to poll (default: main)")

    memory = subparsers.add_parser("memory", help="inspect episodic memory")
    memory.add_argument("memory_action", choices=["search", "stats"])
    memory.add_argument("query", nargs="*", default=[])
    memory.add_argument("--limit", type=int, default=10)

    sync = subparsers.add_parser("sync", help="export or merge a memory snapshot")
    sync.add_argument("--import", dest="import_path", default=None, help="snapshot to merge in")
    sync.add_argument("--out", default=None, help="snapshot destination path")
    sync.add_argument("--push", action="store_true", help="publish lessons.sqlite to GitHub")
    sync.add_argument("--pull", action="store_true", help="merge the GitHub lessons.sqlite")
    sync.add_argument("--repo", default=None, help="owner/name (default: teamhecked-Ayu/Vajrastra)")
    sync.add_argument("--branch", default=None, help="branch to sync with (default: main)")
    sync.add_argument("--remote-path", dest="remote_path", default=None,
                       help="remote ledger path (default: memory_bank/lessons.sqlite)")

    github = subparsers.add_parser("github", help="approval-gated GitHub repository workflow")
    github_sub = github.add_subparsers(dest="github_action", required=True)
    inspect = github_sub.add_parser("inspect", help="read-only branch inventory and task plan")
    inspect.add_argument("--repo", default="teamhecked-Ayu/Vajrastra")
    inspect.add_argument("--base", default="main")
    inspect.add_argument("--task", required=True)
    inspect.add_argument("--work-branch", default="vajrastra/work/preview")

    apply = github_sub.add_parser("apply", help="show diff, request approvals, verify, and open draft PR")
    apply.add_argument("--repo", default="teamhecked-Ayu/Vajrastra")
    apply.add_argument("--base", default="main")
    apply.add_argument("--branch", dest="work_branch", required=True)
    apply.add_argument("--task", required=True)
    apply.add_argument("--changes", required=True, help="JSON object mapping file paths to replacement text")
    apply.add_argument("--message", default="feat: approved GitHub repository task")
    apply.add_argument("--title", required=True)
    apply.add_argument("--body", default="")
    apply.add_argument("--workflow", default="watchdog.yml")
    apply.add_argument("--timeout", type=float, default=900.0)
    apply.add_argument("--poll-interval", type=float, default=15.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        ctx = build_context(quiet=False)
        try:
            return repl(ctx, parser)
        finally:
            ctx.close()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage advice
        return int(exc.code or 0)
    try:
        ctx = build_context(args.root, quiet=False)
    except IntegrityError as exc:
        _out(sys.stderr, f"integrity: {exc}")
        return 2
    try:
        if not args.command:
            return repl(ctx, parser)
        handlers = {
            "fix": cmd_fix,
            "build": cmd_build,
            "research": cmd_research,
            "evolve": cmd_evolve,
            "worker": cmd_worker,
            "github": cmd_github,
            "memory": cmd_memory,
            "sync": cmd_sync,
        }
        return handlers[args.command](ctx, args)
    except SecurityViolation as exc:
        _out(sys.stderr, f"security firewall: {exc}")
        for violation in exc.violations:
            _out(sys.stderr, "  " + violation.describe())
        return 2
    except IntegrityError as exc:
        _out(sys.stderr, f"integrity: {exc}")
        return 2
    except (KeyError, OSError, ValueError) as exc:
        _out(sys.stderr, f"error: {exc}")
        return 2
    finally:
        ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
