"""Verify pinned BugsInPy bug/fix checkouts and focused regression tests."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

MANIFEST = Path(__file__).with_name("upstream_cases.json")


def git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True,
        text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"not a Git checkout: {path}")
    return result.stdout.strip()


def select_cases(cases: list[dict], requested: list[str]) -> list[dict]:
    known = {case["id"] for case in cases}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise ValueError("unknown case id(s): " + ", ".join(unknown))
    selected = set(requested)
    return [case for case in cases if not selected or case["id"] in selected]


def _safe_relative_path(raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe overlay path: {raw_path}")
    if relative.parts[0] != "tests":
        raise ValueError(f"test overlay must stay under tests/: {raw_path}")
    return relative


def apply_test_overlay(case: dict, buggy: Path, fixed: Path) -> str:
    """Copy declared test-only files from fixed into buggy, without following symlinks."""
    raw_paths = case.get("test_overlays_from_fixed", case.get("test_overlay_from_fixed"))
    if not raw_paths:
        return "not-required"
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
        raise ValueError("test overlays must be a path or a list of paths")

    for raw_path in raw_paths:
        relative = _safe_relative_path(raw_path)
        source = fixed / relative
        target = buggy / relative

        current = fixed
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"symlink in fixed test overlay path: {current}")
        current = buggy
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"symlink in buggy test overlay path: {current}")

        if not source.is_file():
            raise RuntimeError(f"fixed revision is missing regression test: {source}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"unsafe existing regression test target: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.resolve() != (buggy / relative.parent).resolve():
            raise RuntimeError("regression test target escaped buggy checkout")
        shutil.copyfile(str(source), str(target))
    return "copied-from-fixed-revision"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--checkout", action="append", default=[],
        help="case-id:buggy=/path or case-id:fixed=/path (repeat for every checkout)",
    )
    parser.add_argument(
        "--python", action="append", default=[],
        help="case-id=/path/to/python interpreter (repeat for each case)",
    )
    parser.add_argument(
        "--case", action="append", default=[],
        help="run only this case ID; repeat to select more than one",
    )
    args = parser.parse_args()
    supplied = {}
    interpreters = {}
    for item in args.python:
        try:
            case_id, executable = item.split("=", 1)
        except ValueError:
            parser.error(f"invalid --python value: {item}")
        if case_id in interpreters:
            parser.error(f"duplicate interpreter for {case_id}")
        interpreters[case_id] = Path(executable).resolve()
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
    try:
        cases = select_cases(manifest["cases"], args.case)
    except ValueError as exc:
        parser.error(str(exc))

    results = []
    started = time.perf_counter()
    failed = False
    for case in cases:
        case_id = case["id"]
        case_result = {"id": case_id, "python_expected": case["python_version"]}
        interpreter = interpreters.get(case_id)
        if interpreter is None or not interpreter.is_file():
            case_result["interpreter"] = {"status": "missing-interpreter"}
            failed = True
            results.append(case_result)
            continue
        version_probe = subprocess.run(
            [str(interpreter), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        actual_python = version_probe.stdout.strip()
        case_result["python_actual"] = actual_python
        if version_probe.returncode or actual_python != case["python_version"]:
            case_result["interpreter"] = {
                "status": "wrong-python", "expected": case["python_version"],
                "actual": actual_python,
            }
            failed = True
            results.append(case_result)
            continue

        paths = {}
        revisions_valid = True
        for revision in ("buggy", "fixed"):
            path = supplied.get((case_id, revision))
            if path is None:
                case_result[revision] = {"status": "missing-checkout"}
                failed = True
                revisions_valid = False
                continue
            expected_sha = case[f"{revision}_commit"]
            actual_sha = git_head(path)
            if actual_sha != expected_sha:
                case_result[revision] = {
                    "status": "wrong-revision", "expected": expected_sha, "actual": actual_sha,
                }
                failed = True
                revisions_valid = False
            else:
                paths[revision] = path
        if not revisions_valid:
            results.append(case_result)
            continue

        try:
            case_result["test_overlay"] = apply_test_overlay(
                case, paths["buggy"], paths["fixed"]
            )
        except (OSError, RuntimeError, ValueError) as exc:
            case_result["test_overlay"] = {"status": "failed", "error": str(exc)}
            failed = True
            results.append(case_result)
            continue

        for revision in ("buggy", "fixed"):
            command = list(case["test_command"])
            if command[0] == "python":
                command[0] = str(interpreter)
            elif command[0] == "pytest":
                command = [str(interpreter), "-m", "pytest", *command[1:]]
            run = subprocess.run(
                command, cwd=str(paths[revision]), capture_output=True, text=True,
                timeout=180, stdin=subprocess.DEVNULL, check=False,
            )
            output = ((run.stdout or "") + (run.stderr or ""))[-5000:]
            passed = run.returncode == 0
            expected_pass = revision == "fixed"
            failure_pattern = case.get("buggy_failure_pattern")
            failure_is_expected = (
                revision != "buggy"
                or not failure_pattern
                or re.search(failure_pattern, output, re.IGNORECASE) is not None
            )
            correct = (passed == expected_pass) and failure_is_expected
            failed = failed or not correct
            case_result[revision] = {
                "status": (
                    "expected-pass" if correct and passed
                    else "expected-regression-failure" if correct
                    else "unexpected-failure-mode" if not passed and not failure_is_expected
                    else "unexpected-result"
                ),
                "test_passed": passed,
                "returncode": run.returncode,
                "expected_buggy_failure_pattern": failure_pattern if revision == "buggy" else None,
                "output": output,
            }
        results.append(case_result)

    report = {
        "schema_version": 1,
        "benchmark": "bugsinpy-upstream-reproduction",
        "dataset": manifest["dataset"],
        "selected_cases": [case["id"] for case in cases],
        "python_interpreters": {key: str(value) for key, value in interpreters.items()},
        "results": results,
        "runtime_seconds": round(time.perf_counter() - started, 4),
    }
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
