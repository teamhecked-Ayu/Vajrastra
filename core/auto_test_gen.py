"""Candidate test generation with honest execution reporting.

Generates candidate smoke/regression tests from Python function
signatures and observed bug reports, labels every candidate as
generated (never mixed with independently authored tests), executes
them in a throwaway workspace with a declared timeout, and runs each
candidate twice to flag flaky results. If nothing ran, the report says
so: a candidate is never counted as passed unless it actually ran.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestCandidate:
    name: str
    source: str            # full python source defining the check function
    entry: str             # check function name in source
    origin: str = "generated"   # always 'generated'; labeled separately


@dataclass
class CandidateReport:
    candidate: str
    ran: bool
    passed: bool
    flaky: bool = False
    detail: str = ""
    origin: str = "generated"


def _default_value(arg: str) -> str:
    a = arg.lower()
    if "count" in a or "num" in a or a in ("n", "x", "i"):
        return "1"
    if any(k in a for k in ("name", "text", "word", "path", "s")):
        return "'sample'"
    if "flag" in a or "enable" in a or a.startswith("is_"):
        return "True"
    return "1"


def generate_candidates(source_path: str | Path,
                        observed_bug: str | None = None) -> list[TestCandidate]:
    source_path = Path(source_path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    candidates: list[TestCandidate] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (node.args.vararg or node.args.kwarg or node.args.kwonlyargs
                or node.args.defaults or node.args.kw_defaults):
            continue   # keep v1 simple and honest
        args = [a.arg for a in node.args.args]
        vals = [_default_value(a) for a in args]
        entry = f"_check_{node.name}"
        check = (
            f"def {entry}():\n"
            f"    result = mod.{node.name}({', '.join(vals)})\n"
            f"    assert result is not None, {node.name!r} + ' returned None'\n"
        )
        if observed_bug:
            check += f"    # regression intent from observed bug: {observed_bug[:80]!r}\n"
        candidates.append(TestCandidate(
            name=f"test_{node.name}_smoke", source=check.rstrip(), entry=entry))
    return candidates


def _harness_script(candidates: list[TestCandidate]) -> str:
    checks = "\n\n".join(c.source for c in candidates)
    invocations = "\n".join(f"results[{c.name!r}] = _run({c.entry})" for c in candidates)
    return (
        "import importlib.util, json\n"
        "spec = importlib.util.spec_from_file_location('target', 'target.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        + checks + "\n\n"
        "def _run(fn):\n"
        "    try:\n"
        "        fn()\n"
        "        return 'pass'\n"
        "    except AssertionError as e:\n"
        "        return 'fail: ' + str(e)\n"
        "    except Exception as e:\n"
        "        return 'error: ' + type(e).__name__ + ': ' + str(e)\n"
        "results = {}\n"
        + invocations + "\n"
        "print('VAJASTRA_RESULT:' + json.dumps(results))\n"
    )


def _run_harness(script: Path, cwd: Path, timeout: float) -> dict:
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                              text=True, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {}
    marker = "VAJASTRA_RESULT:"
    line = next((l for l in proc.stdout.splitlines() if l.startswith(marker)), None)
    if line is None:
        return {}
    try:
        return json.loads(line[len(marker):])
    except json.JSONDecodeError:
        return {}


def run_candidates(candidates: list[TestCandidate], source_path: str | Path,
                   timeout: float = 15.0) -> list[CandidateReport]:
    reports: list[CandidateReport] = []
    if not candidates:
        return [CandidateReport(candidate="(none)", ran=False, passed=False,
                                detail="no candidates generated; nothing ran")]
    with tempfile.TemporaryDirectory(prefix="vajrastra_testgen_") as td:
        td = Path(td)
        (td / "target.py").write_text(Path(source_path).read_text(encoding="utf-8"),
                                      encoding="utf-8")
        script = td / "harness.py"
        script.write_text(_harness_script(candidates), encoding="utf-8")
        first = _run_harness(script, td, timeout)
        second = _run_harness(script, td, timeout)   # twice, to flag flakiness
    for c in candidates:
        r1 = first.get(c.name)
        r2 = second.get(c.name)
        ran = r1 is not None
        passed = ran and r1 == "pass"
        flaky = ran and r2 is not None and r1 != r2
        detail = r1 if ran else "harness produced no result (crash/timeout); NOT counted as passed"
        reports.append(CandidateReport(candidate=c.name, ran=ran, passed=passed,
                                       flaky=flaky, detail=detail))
    return reports
