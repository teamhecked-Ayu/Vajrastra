"""Run the pinned external-repository retrieval benchmark offline.

Clone/check out the refs in real_benchmark.json before running this script.
No network access is performed by the benchmark itself.

Example:
  python benchmarks/project_understanding/run_real_benchmark.py --repo flask=/work/flask --repo requests=/work/requests --repo httpx=/work/httpx
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engines.project_understanding import ProjectIndex


def git_revision(root: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=str(root), capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args()
    paths = {}
    for item in args.repo:
        if "=" not in item:
            parser.error("--repo must use NAME=PATH")
        name, path = item.split("=", 1)
        paths[name] = Path(path).expanduser().resolve()

    manifest_path = Path(__file__).with_name("real_benchmark.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    missing = []
    for case in manifest["repositories"]:
        name = case["name"]
        if name not in paths:
            missing.append(name)
            continue
        root = paths[name]
        if not root.is_dir():
            raise SystemExit(f"{name}: not a directory: {root}")
        expected_revision = git_revision(root, case["ref"])
        actual_revision = git_revision(root, "HEAD")
        if actual_revision != expected_revision:
            raise SystemExit(f"{name}: wrong checkout. expected {expected_revision}, got {actual_revision}")
        index = ProjectIndex.build(root, actual_revision)
        context = index.task_context(case["task"], current_commit_sha=actual_revision)
        selected = set(context["selected_files"])
        expected = set(case["expected"])
        hits = expected & selected
        recall = len(hits) / len(expected) if expected else 1.0
        precision = len(hits) / len(selected) if selected else 0.0
        rows.append({
            "repository": name, "revision": actual_revision,
            "recall": round(recall, 4), "precision": round(precision, 4),
            "expected": sorted(expected), "selected": sorted(selected),
            "missing": sorted(expected - selected),
        })

    for row in rows:
        print(json.dumps(row, sort_keys=True))
    if missing:
        print("not run (missing checkouts): " + ", ".join(missing))
        return 2
    mean_recall = sum(row["recall"] for row in rows) / len(rows) if rows else 0.0
    mean_precision = sum(row["precision"] for row in rows) / len(rows) if rows else 0.0
    print(json.dumps({
        "repositories": len(rows), "mean_recall": round(mean_recall, 4),
        "mean_precision": round(mean_precision, 4),
    }, sort_keys=True))
    return 0 if rows and mean_recall == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
