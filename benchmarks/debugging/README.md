# Debugging and patch-verification benchmark

This benchmark is deliberately offline and deterministic. It runs the debugger
against a small set of checked-in regression fixtures, then runs each fixture's
failing baseline test and passing corrected test in a fresh subprocess.

It reports:
- defect-detection accuracy against the fixture's expected finding marker;
- baseline reproduction rate;
- fixed-test pass rate;
- wall-clock runtime.

These fixtures are an initial harness, **not yet the blueprint's required
external real-bug benchmark**. Do not claim Phase 3's benchmark exit gate passed
until cases are added from pinned real upstream bug reports/commits and the
results are published across supported Python versions.

Run from the repository root:

    python benchmarks/debugging/run_benchmark.py

No network calls or optional packages are required. The benchmark uses the
standard-library debugger path and disables model review.
