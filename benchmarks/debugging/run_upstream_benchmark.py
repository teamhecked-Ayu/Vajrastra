"""Verify pinned BugsInPy bug/fix checkouts and focused regression tests."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("upstream_cases.json")


def git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True,
        text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"not a Git checkout: {path}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--checkout", action="append", default=[],
        help="case-id:buggy=/path or case-id:fixed=/path (repeat for every checkout)",
    )
    args = parser.parse_args()
    supplied = {}
    for item in args.checkout:
        try:
            key, raw_path = item.split("=", 1)
            case_id, revision = key.split(":", 1)
        except ValueError:
            parser.error(f"invalid --checkout value: {item}")
        if revision not in {"buggy", "fixed"} or (case_id, revision) in supplied:
            parser.error(f"invalid or duplicate checkout key: {key}")
        supplied[(case_id, revision)] = Path(raw_path).resolve()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    results = []
    started = time.perf_counter()
    failed = False
    for case in manifest["cases"]:
        case_id = case["id"]
        case_result = {"id": case_id, "python_expected": case["python_version"]}
        for revision in ("buggy", "fixed"):
            path = supplied.get((case_id, revision))
            if path is None:
                case_result[revision] = {"status": "missing-checkout"}
                failed = True
                continue
            expected_sha = case[f"{revision}_commit"]
            actual_sha = git_head(path)
            if actual_sha != expected_sha:
                case_result[revision] = {
                    "status": "wrong-revision", "expected": expected_sha, "actual": actual_sha,
                }
                failed = True
                continue
            if sys.version.split()[0] != case["python_version"]:
                case_result[revision] = {
                    "status": "wrong-python", "expected": case["python_version"],
                    "actual": sys.version.split()[0],
                }
                failed = True
                continue
            command = list(case["test_command"])
            if command[0] == "python":
                command[0] = sys.executable
            elif command[0] == "pytest":
                pytest = Path(sys.executable).with_name("pytest")
                command[0] = str(pytest) if pytest.exists() else "pytest"
            run = subprocess.run(
                command, cwd=str(path), capture_output=True, text=True,
                timeout=180, stdin=subprocess.DEVNULL, check=False,
            )
            passed = run.returncode == 0
            expected_pass = revision == "fixed"
            correct = passed == expected_pass
            failed = failed or not correct
            case_result[revision] = {
                "status": "expected-pass" if correct else "unexpected-result",
                "test_passed": passed,
                "returncode": run.returncode,
                "output": ((run.stdout or "") + (run.stderr or ""))[-3000:],
            }
        results.append(case_result)

    report = {
        "schema_version": 1,
        "benchmark": "bugsinpy-upstream-reproduction",
        "dataset": manifest["dataset"],
        "python_runtime": sys.version.split()[0],
        "results": results,
        "runtime_seconds": round(time.perf_counter() - started, 4),
    }
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
