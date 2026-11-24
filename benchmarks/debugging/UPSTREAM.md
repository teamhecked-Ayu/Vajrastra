# Pinned upstream bug cases

This manifest records two real BugsInPy cases at an immutable dataset commit:

- PySnooper bug 1: buggy/fixed commits, Python 3.8.1, and the focused Chinese
  output regression test.
- Black bug 1: buggy/fixed commits, Python 3.8.3, and the focused
  mono-process-only regression test.

The PySnooper regression test was added in the fixed revision, so it is absent
from the buggy checkout. The manifest declares `test_overlay_from_fixed`.
Before running either revision, the runner copies that test file from the fixed
checkout into the buggy checkout. It rejects unsafe paths and symlinks and does
not modify tracked project source files.

## Automated GitHub Actions reproduction

The `upstream-debugging-reproduction` workflow checks out both exact commits
for each case, provisions the exact Python patch version, installs the test
dependencies, and invokes the runner for that case. It uploads a JSON report
for each case. A passing result requires the buggy test to fail and the fixed
test to pass.

The workflow does not claim that the Vajrastra model generated the upstream
patch. It only verifies the known upstream regression and fix.

## Run locally

Prepare the four exact Git checkouts and matching Python interpreters, then run:

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

The runner itself performs no network access and installs no packages.
It verifies exact Git SHAs and exact interpreter patch versions.

This is an upstream reproduction benchmark, not a model repair accuracy
benchmark. The checked-in deterministic fixtures separately measure static
detection. Do not combine their metrics or claim that this upstream runner
proves AI-generated patches.
