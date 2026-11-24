# Phase 3 debugging benchmark results

**Measurement date:** 24 November 2026  
**Verified branch head before this documentation correction:** `6164fbecb6a5ebcfaa5cc3e2cd3b7d5546a081b2`

## Deterministic fixture benchmark

Workflow: [debugging-benchmark, run 45356055281](https://github.com/teamhecked-Ayu/Vajrastra/actions/runs/45356055281)

| Python | Detection | Baseline reproduction | Corrected test pass | Runtime |
|---|---:|---:|---:|---:|
| 3.10 | 3/3 (100%) | 3/3 (100%) | 3/3 (100%) | 0.2344 s |
| 3.12 | 3/3 (100%) | 3/3 (100%) | 3/3 (100%) | 0.3271 s |

The three fixtures cover division by literal zero, mutable default argument
state leakage, and swallowed exceptions. Each defect was detected, each
baseline test reproduced the defect, and each corrected test passed.

These figures describe only three controlled fixtures. They are not a
statistically meaningful estimate of general defect-detection or repair
accuracy. The benchmark disables model review and does not measure
model-generated patch accuracy.

The corrected-test pass rate is not a full upstream project-suite regression
rate. Full-suite safety still depends on the project's test suite and the
disposable verification process.

## Real upstream reproductions

Workflow: [upstream-debugging-reproduction, run 45356055224](https://github.com/teamhecked-Ayu/Vajrastra/actions/runs/45356055224)

| Case | Python | Buggy revision | Fixed revision | Result |
|---|---|---|---|---|
| PySnooper bug 1 | 2.7.18 | Focused test fails with `UnicodeEncodeError` | 1 focused test passes | **Passed** |
| Black bug 1 | 3.8.3 | Focused test fails with `AssertionError: 1 != 0` | Focused test passes | **Passed** |

Both upstream jobs completed successfully. Each exact buggy commit failed
with its declared regression signature, and each exact fixed commit passed.
The test-only overlays copy regression tests from the fixed revision into
the buggy checkout because those tests are not present in the buggy revision.

## Watchdog

Workflow: [watchdog, run 45356055251](https://github.com/teamhecked-Ayu/Vajrastra/actions/runs/45356055251)

Python 3.10 and 3.12 both passed the compile, sealed-core integrity, unit test,
and CLI smoke checks on the same verified branch head.

## Phase 3 exit gate

**Passed.**

The required benchmark accuracy, corrected-test pass rate, runtime, and
pinned real upstream bug/fix reproductions are published above. No universal
speed claim or general 100% accuracy claim is made.

## Claims deliberately not made

- No claim of 100% debugging accuracy on real-world repositories.
- No claim that Vajrastra generated the known upstream fixes.
- No universal speed or performance claim.
- No claim that focused regression tests replace full-suite testing.
