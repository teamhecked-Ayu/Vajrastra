"""Offline reproducible benchmark for HyperDebugger detection and regression tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory_engine import MemoryEngine
from core.security_guard import SecurityGuard
from engines.hyper_debugger import EngineStatus, HyperDebugger


def run_tests(directory: Path) -> tuple[bool, str]:
    command = [sys.executable, "-B", "-s", "-m", "unittest", "discover",
               "-s", str(directory), "-p", "test_*.py", "-q"]
    try:
        result = subprocess.run(command, cwd=str(directory), capture_output=True,
                                text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, output[-2000:]


def main() -> int:
    manifest = json.loads((Path(__file__).with_name("cases.json")).read_text(encoding="utf-8"))
    cases = manifest["cases"]
    started = time.perf_counter()
    rows = []
    detected = reproduced = fixed = 0

    for case in cases:
        buggy_source = ROOT / "benchmarks/debugging" / case["source"]
        buggy_test = ROOT / "benchmarks/debugging" / case["baseline_test"]
        fixed_source = ROOT / "benchmarks/debugging" / case["fixed_source"]
        fixed_test = ROOT / "benchmarks/debugging" / case["fixed_test"]

        with tempfile.TemporaryDirectory(prefix="vajra-bug-benchmark-") as tmp:
            work = Path(tmp)
            source_copy = work / buggy_source.name
            test_copy = work / buggy_test.name
            source_copy.write_bytes(buggy_source.read_bytes())
            test_copy.write_bytes(buggy_test.read_bytes())
            memory = MemoryEngine(work)
            try:
                debugger = HyperDebugger(work, guard=SecurityGuard(work), memory=memory)
                analysis = debugger.analyze(source_copy, use_llm=False, engines=EngineStatus())
                hit = any(case["expected_finding"] in finding.message
                          for finding in analysis.findings)
                reproduced_ok, reproduced_output = run_tests(work)
                debugger.close()
            finally:
                memory.close()

        with tempfile.TemporaryDirectory(prefix="vajra-fixed-benchmark-") as tmp:
            work = Path(tmp)
            (work / fixed_source.name).write_bytes(fixed_source.read_bytes())
            (work / fixed_test.name).write_bytes(fixed_test.read_bytes())
            fixed_ok, fixed_output = run_tests(work)

        detected += int(hit)
        reproduced += int(not reproduced_ok)
        fixed += int(fixed_ok)
        rows.append({
            "id": case["id"],
            "detected": hit,
            "baseline_reproduced": not reproduced_ok,
            "fixed_tests_passed": fixed_ok,
            "expected_finding": case["expected_finding"],
            "baseline_output": reproduced_output,
            "fixed_output": fixed_output,
        })

    elapsed = round(time.perf_counter() - started, 4)
    total = len(cases)
    report = {
        "schema_version": 1,
        "benchmark": "vajrastra-debugging-fixtures",
        "cases": rows,
        "metrics": {
            "detection_accuracy": round(detected / total, 4) if total else 0.0,
            "baseline_reproduction_rate": round(reproduced / total, 4) if total else 0.0,
            "fixed_regression_pass_rate": round(fixed / total, 4) if total else 0.0,
            "runtime_seconds": elapsed,
        },
    }
    print(json.dumps(report, indent=2))
    return 0 if total and detected == total and reproduced == total and fixed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
