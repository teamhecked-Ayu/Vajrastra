# Phase 3 debugging benchmark results

**Measurement date:** 24 November 2026  
**Scope:** deterministic fixture benchmark plus two pinned upstream bug/fix pairs.

## Deterministic fixture benchmark

Workflow: [debugging-benchmark, run 45355685924](https://github.com/teamhecked-Ayu/Vajrastra/actions/runs/45355685924)  
Commit: `5b3d639529dea363ddd24cf222fcd7f37492d6a8`

| Python | Detection | Baseline reproduction | Corrected test pass | Runtime |
|---|---:|---:|---:|---:|
| 3.10 | 3/3 (100%) | 3/3 (100%) | 3/3 (100%) | 0.387 s |
| 3.12 | 3/3 (100%) | 3/3 (100%) | 3/3 (100%) | 0.4195 s |

All three fixtures were detected, each broken baseline test reproduced its
failure, and each corrected test passed. These figures describe only the
three checked-in fixtures. They are not a statistically meaningful estimate
of general defect-detection or repair accuracy.

The corrected-test pass rate is not a full upstream project-suite regression
rate. Full-suite safety still depends on the project's test suite and the
disposable verification process.

## Real upstream reproductions

Workflow: [upstream-debugging-reproduction, run 45355685607](https://github.com/teamhecked-Ayu/Vajrastra/actions/runs/45355685607)  
Commit: `5b3d639529dea363ddd24cf222fcd7f37492d6a8`

| Case | Buggy revision | Fixed revision | Observed result |
|---|---|---|---|
| PySnooper bug 1, Python 2.7.18 | Regression test failed with `UnicodeEncodeError` | 1 focused test passed | Reproduced |
| Black bug 1, Python 3.8.3 | Focused test failed with `AssertionError: 1 != 0` | Focused test passed | Reproduced |

The PySnooper workflow step initially failed because its expected signature
was incorrectly set to `UnicodeDecodeError`. The raw test output showed
`UnicodeEncodeError` on the buggy revision and a passing test on the fixed
revision. The manifest and workflow have now been corrected to require the
observed `UnicodeEncodeError` signature.

**Final automated validation status:** pending. The corrected signature
check must complete successfully on the current branch head before the
upstream reproduction gate can be marked passed.

## Claims deliberately not made

- No claim of 100% debugging accuracy on real-world repositories.
- No claim that Vajrastra generated the known upstream fixes.
- No universal speed or performance claim.
- No claim that the focused tests replace full-suite regression testing.
