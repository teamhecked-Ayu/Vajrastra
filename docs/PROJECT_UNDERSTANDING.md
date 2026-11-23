# Project Understanding (Phase 2)

engines.project_understanding builds a bounded, read-only inventory and Python AST index. It does not import project modules, execute code, or require third-party packages.

## API

```python
from pathlib import Path
from engines.project_understanding import ProjectIndex

index = ProjectIndex.build(
    Path("/path/to/workspace"),
    commit_sha="the-selected-git-commit-sha",
)

context = index.task_context(
    "change the service retry logic and update its tests",
    current_commit_sha="the-same-selected-git-commit-sha",
)
print(context["selected_files"])
```

The caller must supply the selected Git commit SHA. The index does not guess the branch or run Git. A mismatch raises StaleProjectIndexError; do not use stale task context.

## Indexed data

- File inventory: relative path, extension, byte size and category.
- Project manifests and configuration files.
- Likely entry points and test locations.
- Python function, async-function and class definitions with line ranges.
- Python imports and resolvable local import edges.
- Per-source SHA-256 digest and parse errors.
- A stable index fingerprint for cache keys.

## Bounded behavior

Defaults cap the scan at 20,000 files, 2,000 Python files, 256 KB per Python file, 8 MB of Python source, 2,000 symbols per file, 500 imports per file, 24 context paths and 48,000 serialized context characters. Limits can be lowered or raised explicitly with IndexLimits. Oversized or excluded files are skipped and reported as warnings.

Generated directories such as .git, virtual environments, caches, node_modules, dist and build are excluded. Symlinks are not followed. The scanner reads text only.

## Task context behavior

task_context() ranks Python files using instruction terms, adds one-hop local imports, matches likely tests by module name, and adds manifests/configuration paths. It returns file paths and bounded symbol/import metadata, not source text. The caller must read selected files from the same workspace and commit before planning edits.

This is deterministic lexical relevance ranking, not semantic code search. It can miss dynamically loaded modules, plugin registries, generated code, non-Python dependency edges and tests with unrelated names. Review the selected file list before applying a patch.

## Verification

```bash
python -m unittest tests.test_project_understanding -v
python -m unittest discover -s tests -v
```

The tests use temporary repositories and do not execute indexed source files. Passing this test suite is not proof of correctness on every external repository; a broader benchmark set is still required before Phase 2 can pass its exit gate.

## Planner integration

The Python CodeArchitect build path now resolves the workspace Git HEAD, builds
a fresh ProjectIndex, and materializes selected source, related test, and
manifest/config excerpts into both the first Blue-Team prompt and later repair
prompts. The adapter lives in engines/project_planner.py, not core/.

The adapter re-checks Git HEAD after indexing, rejects stale commit context,
rejects symlink path swaps, checks that selected Python source still matches
its indexed SHA-256, and applies per-file and total prompt-context limits. If
Git is unavailable or the workspace is not a Git checkout, the build continues
without project context and records a note. This is a deliberate fallback, not
a claim that context was attached.

## External retrieval benchmark

Three pinned real repositories are defined in
benchmarks/project_understanding/real_benchmark.json: Flask 3.1.3, Requests
2.34.2, and HTTPX at commit 7cff5f7. The offline runner verifies the checked-out
revision and reports required-path recall and precision. See
benchmarks/project_understanding/README.md for setup.

The benchmark is reproducible, but it has not been executed in this CI run
because CI does not clone external repositories. Do not claim the external
benchmark passed until the runner has been executed against all three exact
checkouts.
