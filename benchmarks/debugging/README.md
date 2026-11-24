# Debugging and patch-verification benchmark

This benchmark has two separate layers.

## 1. Deterministic offline fixtures

The checked-in fixtures exercise three known defect classes:

- division by a literal zero;
- mutable default argument state leakage;
- swallowed exceptions.

The runner executes the debugger against each fixture, reproduces the broken
baseline test, then verifies the corrected test in a fresh subprocess.

Run from the repository root:

    python benchmarks/debugging/run_benchmark.py

No network calls or optional packages are required.

## 2. Pinned real upstream bugs

The BugsInPy benchmark records exact buggy/fixed commits for PySnooper and
Black. It checks out both revisions, overlays regression tests that were added
in the fixed commits, and requires the buggy revision to fail with the declared
regression signature while the fixed revision passes.

See [UPSTREAM.md](UPSTREAM.md), the machine-readable
[upstream_cases.json](upstream_cases.json), and the
[latest measured results](RESULTS.md).

## Measurement limits

The three deterministic fixtures are a small smoke benchmark, not a
representative sample of arbitrary software defects. Their 100% results do not
mean Vajrastra has 100% general debugging accuracy. The benchmark disables
model review and does not measure model-generated patch accuracy.

The upstream tests verify known historical bug/fix pairs. They do not prove
that Vajrastra independently discovered or generated those upstream patches.
