# Phase 3: debugging and code generation

## Implemented in this branch

- A deterministic offline regression-fixture benchmark for three bug classes:
  literal division by zero, mutable-default state leakage, and silently swallowed
  exceptions.
- Each case checks that the buggy version fails its regression test, the corrected
  version passes, and HyperDebugger emits the expected static finding.
- The runner reports detection accuracy, baseline reproduction rate, fixed-test
  pass rate, and wall-clock runtime.
- A language capability probe reports only tools found on the current host.
  It does not install tools and does not infer compiler or test-runner support
  from a filename extension.
- CI runs the unit suite and deterministic benchmark on Python 3.10 and 3.12.
- A separate pinned BugsInPy workflow reproduces PySnooper bug 1 on Python 3.8.1
  and Black bug 1 on Python 3.8.3. It verifies buggy-fails/fixed-passes and
  uploads per-case JSON reports.

## Run locally

    python -m unittest discover -s tests -v
    python benchmarks/debugging/run_benchmark.py

See [UPSTREAM.md](../benchmarks/debugging/UPSTREAM.md) for the pinned upstream
reproduction workflow and exact local commands.

## Exit gate

Phase 3 is not complete until both pinned upstream cases pass:
- the exact buggy revision fails its focused regression test;
- the exact fixed revision passes the same test;
- the exact Python patch version and Git SHA are recorded;
- the uploaded JSON reports are inspected.

These tests validate known upstream bug reproduction and correction. They do
not measure whether Vajrastra's model can independently discover or generate
the correct patch.

The existing repair path still requires an explicit model response and a
verification harness. AST checks and subprocess execution are not an OS-level
sandbox. Never run untrusted code on a host with secrets or valuable files.
