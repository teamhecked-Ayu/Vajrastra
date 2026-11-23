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
- CI runs the unit suite and benchmark on Python 3.10 and 3.12 and uploads the
  JSON report.

## Run locally

    python -m unittest discover -s tests -v
    python benchmarks/debugging/run_benchmark.py

## Limits and exit gate

The checked-in cases are deterministic engineering fixtures. They are not a
substitute for a benchmark of pinned upstream bugs. The blueprint requires real,
reproducible bugs and published benchmark accuracy, regression rate, and runtime.
Add upstream cases with issue/commit references, exact reproduction commands,
expected fixes, and license-compatible minimal fixtures before calling Phase 3
complete.

The existing repair path still requires an explicit model response and a
verification harness. AST checks and subprocess execution are not an OS-level
sandbox. Never run untrusted code on a host with secrets or valuable files.
