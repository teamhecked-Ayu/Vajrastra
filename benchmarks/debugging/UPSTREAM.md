# Pinned upstream bug cases

This manifest records two real BugsInPy cases at an immutable dataset commit:

- PySnooper bug 1: buggy/fixed commits, Python 3.8.1, and the focused Chinese
  output regression test.
- Black bug 1: buggy/fixed commits, Python 3.8.3, and the focused
  mono-process-only regression test.

The dataset metadata and upstream patch paths are recorded for audit. Vajrastra
does not download, install, or execute these projects automatically. Check out
the exact buggy and fixed revisions yourself, use the exact Python patch version,
install the project's pinned requirements, and run the focused test in each
checkout.

The runner verifies the Git revision and checks the expected result:
- buggy checkout must fail the focused test;
- fixed checkout must pass the focused test.

Run:

    python benchmarks/debugging/run_upstream_benchmark.py \
      --checkout pysnooper-1:buggy=/work/pysnooper-buggy \
      --checkout pysnooper-1:fixed=/work/pysnooper-fixed \
      --checkout black-1:buggy=/work/black-buggy \
      --checkout black-1:fixed=/work/black-fixed

Use an interpreter matching the exact Python version for the case. The runner
uses the current Python interpreter and does not create environments.

This is an upstream reproduction benchmark, not yet a model repair accuracy
benchmark. The checked-in deterministic fixtures separately measure static
detection accuracy. Do not combine their metrics or claim that this upstream
runner proves AI-generated patches.
