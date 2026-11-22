#!/usr/bin/env python3
"""Vajrastra -- universal cross-OS autonomous AI engineer (Phase 1 CLI).

Runs identically as ``python vajrastra.py`` on Android, Parrot OS, Linux, macOS
and Windows using the Python standard library only.

Commands::

    vajrastra fix <target>
    vajrastra build "<prompt>"
    vajrastra research "<topic>"
    vajrastra evolve
    vajrastra memory <search|stats> [query]
    vajrastra sync [--import PATH]

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
from core.security_guard import (  # noqa: E402
    IntegrityError,
    SecurityGuard,
    SecurityViolation,
)

VERSION = "0.1.0"
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
        files = _collect_python_files(ctx.root, candidate)
        if not files:
            _out(out, f"nothing to analyse: {candidate} contains no Python files")
            return 0
        findings = 0
        blocking = 0
        for path in files:
            result = ctx.guard.scan_file(path)
            findings += len(result.violations)
            blocking += len(result.blocking)
            rel = ctx.guard.relpath(path)
            if result.syntax_error:
                _out(out, f"  [syntax] {rel}: {result.syntax_error}")
                blocking += 1
                continue
            for violation in result.violations:
                _out(out, f"  {violation.describe()}  <- {rel}")
        _out(out, _rule("scan"))
        _out(out, f"files       : {len(files)}")
        _out(out, f"findings    : {findings} (blocking: {blocking})")

        if findings:
            lessons = ctx.memory.search(_scan_query(ctx, files[:20]), limit=3)
            _print_lessons(out, lessons)
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
    _out(out, _rule("vajrastra build"))
    _out(out, f"prompt      : {prompt}")
    _print_routing(out, decision)

    steps = _PLAN_TEMPLATES.get(task, _PLAN_TEMPLATES[TaskClass.BUILD])
    _out(out, _rule("plan"))
    for index, step in enumerate(steps, start=1):
        _out(out, f"  {index}. {step}")

    slug = slugify(prompt)[:48]
    payload = {
        "prompt": prompt,
        "task_class": task.value,
        "routing": decision.as_dict(),
        "steps": steps,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

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
    return 0


def cmd_evolve(ctx: AppContext, args: argparse.Namespace, out=None) -> int:
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
    _out(out, _rule("procedural skills"))
    for skill in skills:
        if not skill.path:
            continue
        result = ctx.guard.scan_source(skill.code, filename=skill.path)
        status = "safe" if result.ok else f"{len(result.blocking)} blocking finding(s)"
        if not result.ok:
            failed.append(skill.slug)
        _out(out, f"  {skill.slug:<32} {status}")

    unresolved = ctx.memory.unresolved(limit=5)
    _out(out, _rule("evolution backlog"))
    if unresolved:
        for lesson in unresolved:
            _out(out, f"  - #{lesson.id}: {lesson.error_signature.splitlines()[0][:120]}")
    else:
        _out(out, "  nothing pending; ledger is fully verified")

    manifest = ctx.guard.read_manifest() or {}
    lines = [
        "# Evolution report",
        "",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        f"- integrity: {report.status}",
        f"- bundle: {report.bundle_sha256 or 'n/a'}",
        f"- expected: {report.expected_bundle or 'n/a'}",
        f"- sealed at: {report.sealed_at or 'n/a'}",
        f"- files in lock: {len(manifest.get('files') or {})}",
        f"- skills checked: {len(skills)}",
        f"- skills failing firewall: {', '.join(failed) or 'none'}",
        f"- unresolved lessons: {len(unresolved)}",
        "",
        "## Locked files",
    ]
    for rel in sorted((manifest.get("files") or {}).keys()):
        lines.append(f"- {rel}")
    lines.append("")
    lines.append("## Guard rails")
    lines.append("- The security layer is read-only: write_patch() refuses core/ paths.")
    lines.append("- Every generated file is parsed by the AST firewall before it is written.")
    lines.append("- Verified patches only: a lesson without a patch stays on the backlog.")

    path = ctx.root / "memory_bank" / "evolution" / f"report-{_stamp()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _out(out, _rule("report"))
    _out(out, f"written     : {path.relative_to(ctx.root)}")
    return 1 if failed else 0


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

    device = ctx.adapter.profile.host_os.value
    _out(out, _rule("bridge"))
    if ctx.adapter.host_os.value == "android":
        _out(out, "device      : android (Cloud Bridge Mode)")
        _out(out, "next        : commit this snapshot and let a Colab worker import it")
    else:
        _out(out, f"device      : {device} (Local Native Mode)")
        _out(out, "next        : commit or copy the snapshot to sync another machine")
    return 0


# ------------------------------------------------------------------------ REPL
_HELP_TEXT = """\
commands:
  fix <target>            analyse a file, tree or traceback and recall verified patches
  build "<prompt>"        plan a task and route it local or to the Colab bridge
  research "<topic>"      mine local memory and write a research brief
  evolve                  integrity + firewall audit of the self-evolved surface
  memory stats            ledger and skill counters
  memory search <query>   keyword / signature lookup
  sync [--import PATH]    export (or merge) a portable memory snapshot
  help                    this text
  exit                    leave the REPL
"""


def dispatch(ctx: AppContext, parser: argparse.ArgumentParser, argv: Sequence[str]) -> int:
    handlers = {
        "fix": cmd_fix,
        "build": cmd_build,
        "research": cmd_research,
        "evolve": cmd_evolve,
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

    build = subparsers.add_parser("build", help="plan and route an engineering task")
    build.add_argument("prompt", nargs="+", help="what to build")

    research = subparsers.add_parser("research", help="mine local memory for a topic")
    research.add_argument("topic", nargs="+", help="research topic")

    subparsers.add_parser("evolve", help="audit integrity and the self-evolved surface")

    memory = subparsers.add_parser("memory", help="inspect episodic memory")
    memory.add_argument("memory_action", choices=["search", "stats"])
    memory.add_argument("query", nargs="*", default=[])
    memory.add_argument("--limit", type=int, default=10)

    sync = subparsers.add_parser("sync", help="export or merge a memory snapshot")
    sync.add_argument("--import", dest="import_path", default=None, help="snapshot to merge in")
    sync.add_argument("--out", default=None, help="snapshot destination path")
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
