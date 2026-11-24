# Pinned upstream bug cases

This manifest records two BugsInPy cases at an immutable dataset commit:

- **PySnooper bug 1:** the patch is a Python 2 compatibility fix. The dataset's
  recorded Python 3.8.1 environment does not reproduce it. The benchmark uses
  Python 2.7.18, the final Python 2 release.
- **Black bug 1:** Python 3.8.3, with a regression test added in the fixed
  revision.

The regression tests are test-only overlays copied from the fixed checkout
into the buggy checkout. For PySnooper, both `tests/test_chinese.py` and
`tests/utils.py` are overlaid. For Black, `tests/test_black.py` is overlaid.
The runner restricts overlays to regular files under `tests/`, rejects
symlinks, and verifies both Git revisions before running tests.

A non-zero exit code alone is not accepted as a reproduction. Each buggy case
must fail with its declared failure signature (`UnicodeDecodeError` for
PySnooper, `OSError` for Black), while the fixed revision must pass. This
prevents missing tests, import errors, or broken test environments from being
counted as successful bug reproductions.

## Automated GitHub Actions reproduction

The `upstream-debugging-reproduction` workflow uses pinned container images
for the exact Python patch versions, checks out both commits, installs the
test dependencies, overlays the fixed regression tests, and runs the focused
test against both revisions. A passing result requires the expected regression
failure and the fixed pass.

The workflow does not claim that the Vajrastra model generated the upstream
patch. It only verifies the known upstream regression and fix.

## Run locally

Prepare the exact Git checkouts and matching Python interpreters, then run:

    python benchmarks/debugging/run_upstream_benchmark.py \
      --case pysnooper-1 \
      --checkout pysnooper-1:buggy=/work/pysnooper-buggy \
      --checkout pysnooper-1:fixed=/work/pysnooper-fixed \
      --python pysnooper-1=/venvs/pysnooper/bin/python

    python benchmarks/debugging/run_upstream_benchmark.py \
      --case black-1 \
      --checkout black-1:buggy=/work/black-buggy \
      --checkout black-1:fixed=/work/black-fixed \
      --python black-1=/venvs/black/bin/python

The runner performs no network access and installs no packages. It verifies
exact Git SHAs and interpreter patch versions. It copies only declared test
files from the fixed checkout into the buggy checkout before execution.

This is an upstream reproduction benchmark, not a model repair accuracy
benchmark. The checked-in deterministic fixtures separately measure static
detection. Do not combine their metrics or claim that this upstream runner
proves AI-generated patches.
