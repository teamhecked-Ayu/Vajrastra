# Vajrastra architecture (Phases 3 and 4)

`AGENTS.md` remains the phase-1 engineering contract and is maintained by hand;
this document records the phase-3/4 subsystems that build on it. If the two ever
disagree about the security model, `AGENTS.md` and `core/security_guard.py` win.

## 1. Constraints that shape everything

* `core/` and `vajrastra.py` are Python 3.10+ **standard library only**. No pip
  install is required to run the CLI on Android, Parrot OS, Linux, macOS or
  Windows.
* Heavy or optional packages (`numpy`, `scipy`, `z3`, `sympy`, `hypothesis`,
  `torch`, `gradio`) are imported lazily inside `engines/` and in the Colab
  notebook. Absence of any of them degrades a capability, never the CLI.
* Generated code is scanned by the AST firewall before it is written or run.
* `core/security_guard.py` and `core/security_lock.json` are **human-sealed**.
  No engine, worker or self-evolution path may modify them.

## 2. Repository map

```
vajrastra.py                  CLI + REPL (fix, build, research, evolve, worker, memory, sync)
core/
  security_guard.py           immutable AST firewall + SHA-256 integrity lock
  _guard_rules.py             firewall rule tables (calls, strings, imports, secrets)
  _guard_integrity.py         seal(), verify(), write_patch()
  os_adapter.py               host detection, hardware profile, execution routing
  memory_engine.py            SQLite episodic ledger, FTS index, procedural skills
  model_router.py             zero-cost Gemini cascade with 429 backoff
  github_sync.py              Contents API client, base64 commits, ledger snapshot merge
engines/
  hyper_debugger.py           four-stage debugger (recall, static, symbolic/fuzz, verified patch)
  code_architect.py           red/blue adversarial code builder
  ml_scientist.py             MCTS research loop + multi-seed gate + metrics.tex + peer review
  self_evolver.py             meta-critic loop, staged candidates, sealed-path policy
  job_worker.py               GitHub inbox worker (fix/build/research/memory/sync)
templates/
  tabular_or_micro_net.py     deterministic 3-seed benchmark (<1s/seed on CPU)
notebooks/
  Vajrastra_Colab_Worker.ipynb  one-tap Colab: setup, Gradio console, background worker
.github/workflows/watchdog.yml  compile + integrity + unittest on every push/PR
docs/ARCHITECTURE.md          this document
memory_bank/                  ledger, skills, plans, research, evolution, staging (runtime)
research_tree/                MCTS tree state and per-seed raw results (runtime)
results/                      metrics.json and generated metrics.tex (runtime)
workspace/                    inbox/ and outbox/ job files (remote, on the default branch)
```

## 3. ML scientist (`engines/ml_scientist.py`)

`vajrastra research "<topic>" --seeds 3 --budget 10` first writes the usual
memory brief, then runs:

1. **Baseline** -- the untouched template parameters evaluated once per paired
   seed.
2. **MCTS** -- UCB1 selection (`mean_value + c·sqrt(ln(parent.visits)/visits)`)
   over single-parameter mutations of `PARAM_GRID`; each expansion is evaluated
   on the same seed list and backpropagates a `tanh`-scaled improvement.
3. **Statistical gate** -- a candidate becomes `verified_success` only when it
   has at least three paired seeds, a positive mean improvement, and a two-sided
   paired t-test with `p < 0.05`. `scipy.stats.ttest_rel` is used when installed;
   otherwise the p-value comes from a stdlib regularized incomplete beta
   implementation (Lentz continued fraction).
4. **Numbers** -- every run writes `results/metrics.json` (raw per-seed results)
   and `results/metrics.tex`, where macros such as `\VajraBaselineMean`,
   `\VajraPValue` and `\VajraGateStatus` are rendered from that JSON. Papers
   quote the macros, so a model cannot invent a number.
5. **Peer review** -- three adversarial personas (Methodology Critic,
   Statistical Auditor, Novelty Reviewer) read the measured payload through
   `core.model_router.py`, and are told the macros are authoritative. Without a
   key the reviews are recorded as skipped, never fabricated.

The tree is checkpointed to `research_tree/tree_state.json` after every
experiment, so an interrupted run keeps its evidence. A verified run also
records a lesson (`tags=("research", "mcts", "ml", status)`); an inconclusive
run records the attempt with an empty patch.

Runtime budget: the template trains a synthetic 6-feature logistic model for at
most 60 epochs over 256 rows, so three seeded runs finish in about a second on
one CPU core (well under the 60 s CPU / 15 s T4 target).

## 4. Self-evolver (`engines/self_evolver.py`)

`vajrastra evolve` is a Meta-Critic pass:

1. **Observe** -- reads `memory_bank/lessons.sqlite` (hits, unresolved
   signatures, tags) and the latest `results/metrics.json` durations. It names
   three classes of weakness: weak prompts (failed JSON contracts), slow parsers
   (measured seeds above the threshold), and recurring failures (unsolved
   signatures with repeated hits).
2. **Propose** -- deterministic playbooks under `docs/evolution/` plus, when a
   model is configured, up to two model-drafted file candidates. Proposals are
   text, so they are reviewable before they are trusted.
3. **Verify** -- a candidate that touches `core/security_guard.py`,
   `core/security_lock.json` or any path under `core/` is rejected on policy.
   Python candidates must pass the AST firewall and `ast.parse`. Every candidate
   is replayed into a throw-away copy of the repository and the full `unittest`
   suite must stay green there. Nested runs are disabled with
   `VAJRASTRA_SELFEVOLVE_CHILD=1` so the suite cannot recurse.
4. **Stage** -- accepted candidates land in
   `memory_bank/evolution/staging/self-upgrade-<timestamp>/` with a manifest.
   `--push` (with `GITHUB_TOKEN`) also commits them to the isolated branch
   `self-upgrade/<timestamp>` through `core/github_sync.py`; nothing is committed
   to `main`, and a drifted core aborts the push.

Every pass writes `memory_bank/evolution/report-<timestamp>.md`.

## 5. Job worker (`engines/job_worker.py`)

Android devices dispatch work by writing a JSON file to `workspace/inbox/` on
the default branch:

```json
{"id": "20260101T000000Z-fix", "kind": "fix",
 "payload": {"target": "buggy.py", "repair": true}, "created_at": "2026-01-01T00:00:00Z"}
```

`vajrastra worker [--once] [--interval 30]` lists the inbox through the Contents
API, executes each pending job (fix, build, research, memory, sync), then commits
`workspace/outbox/<id>.result.json` and marks the inbox file `done` -- that
checkpoint commit is how the phone gets its answer. Paths are resolved inside the
workspace, `core/` is refused, and a `fix` job only writes its patch when the
worker was started with `--allow-apply`.

## 6. Colab notebook (`notebooks/Vajrastra_Colab_Worker.ipynb`)

* **Cell 1** reads `GITHUB_TOKEN`/`GEMINI_API_KEY` from `google.colab.userdata`,
  clones `teamhecked-Ayu/Vajrastra`, and installs
  `z3-solver sympy hypothesis gradio`.
* **Cell 2** launches a `share=True` Gradio console with four tabs: Upload &
  Hyper-Fix (any file or zip, downloadable patched file plus root cause), Code
  Architect, ML Scientist (MCTS with live metrics and `metrics.tex` download),
  and Memory & Self-Evolve.
* **Cell 3** runs the headless worker loop in the background; every task
  checkpoints itself through the GitHub API.

No secrets are stored in the notebook: only Colab Secrets are read.

## 7. CI watchdog (`.github/workflows/watchdog.yml`)

On every push and pull request, on Python 3.10 and 3.12:

1. `python -m compileall -q core engines templates vajrastra.py tests`
2. `SecurityGuard.verify()` must report `verified` (a drifted core fails CI, it
   is never silently re-sealed).
3. `python -m unittest discover -s tests -v`
4. CLI smoke: `--help`, `memory stats`, a one-experiment `research` run and
   `worker --once` (which exits 2 without a token, as designed).

## 8. Trust boundaries

| Boundary | Control |
| --- | --- |
| Model output | AST firewall, schema validation, no execution before scanning |
| Generated code | `SecurityGuard.write_patch()` refuses `core/` |
| Numbers in papers | rendered from raw JSON, never from model text |
| Self-evolution | sealed-path policy + firewall + full suite in a clean copy |
| Remote jobs | workspace-confined paths, explicit `--allow-apply` |
| Secrets | read from env/Colab Secrets, never committed, never in the ledger |
| Core drift | SHA-256 lock verified in the CLI and in CI |

## 9. Verification

```bash
python -m compileall -q core engines templates vajrastra.py tests
python -c "from pathlib import Path; from core.security_guard import SecurityGuard; print(SecurityGuard(Path('.')).verify().describe())"
python -m unittest discover -s tests
```

Optional engines can be exercised with `pip install z3-solver sympy hypothesis`
(or simply run the Colab notebook, which installs them).
