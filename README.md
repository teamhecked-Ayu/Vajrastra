# Vajrastra

Vajrastra is a Python 3.10+ AI engineering assistant. It reads project files,
plans bounded tasks, asks a model for candidate changes, applies policy checks,
runs tests where a supported runtime is available, records verified outcomes and
resumes interrupted work from checkpoints.

Current version: **0.3.0** (see `pyproject.toml`, the single source of truth for
the package version). This is an engineering foundation, not a finished
autonomous engineer; see [Limitations](#limitations).

## Install

```bash
git clone https://github.com/teamhecked-Ayu/Vajrastra.git
cd Vajrastra
python vajrastra.py --help          # run from a checkout, no build step needed
```

The core (`core/`) is **standard-library only**. Optional ML, scientific and UI
packages are loaded lazily inside `engines/` and are never imported at core
import time. To install as a package:

```bash
pip install .            # exposes the `vajrastra` console script
pip install -e .[dev]    # editable install for development
```

## Supported operating modes

| Mode | Where Python runs | Notes |
| --- | --- | --- |
| Desktop (Linux / macOS / Windows) | local | full CLI + REPL |
| Android with Termux | local | same as desktop, inside Termux |
| Android **without** Termux | remote | the phone is a **control device** only; execution happens on GitHub Actions or a user-started Colab runtime |
| Colab (free tier) | user-started notebook | not an always-on worker; runtime limits apply |
| GitHub Actions | short-lived runner | bounded CPU-only jobs, narrow permissions, manual approval for writes |

## Commands

```bash
python vajrastra.py                          # interactive REPL
python vajrastra.py --root /path/to/project  # operate on another project root
python vajrastra.py memory stats             # memory ledger statistics
python vajrastra.py research "<hypothesis>" --seeds 3 --budget 1
python vajrastra.py worker --once            # process one GitHub inbox job
python -m core.security_guard                # seal / verify the trusted core
```

Run `python vajrastra.py --help` for the full command list.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | model provider key (never printed or stored) |
| `GITHUB_TOKEN` / `VAJRASTRA_GITHUB_TOKEN` / `GH_TOKEN` | GitHub REST access (first match wins) |
| `VAJRASTRA_GITHUB_REPO` | override the sync repository (default `teamhecked-Ayu/Vajrastra`) |
| `VAJRASTRA_EXECUTION_MODE` | force an execution mode for `core.os_adapter` |
| `VAJRASTRA_ROOT` | override the project root |

Secrets are read from the environment only. They never belong in the
repository, notebook output, job payloads, logs or the memory database.

## Security model

- `core/` is a sealed trust root: `core/security_lock.json` records SHA-256
digests of every protected core file. `verify()` fails on modified, missing
**or unexpected** protected files; CI enforces this on every push and PR.
- Every model response is untrusted input — a proposal, not authority. Source
passes an AST firewall (risky calls, imports, secrets, suspicious data flow)
before it can be written, and `write_patch` refuses protected paths.
- A subprocess with a temporary directory is a useful control, **not** a full
OS sandbox. Do not run untrusted code automatically where network or
filesystem isolation cannot be enforced.
- Resealing is an explicit maintainer action (`python -m core.security_guard`)
performed after code review. Self-evolution candidates can stage branches but
can never reseal the manifest, edit the trust root or merge to `main`.

## Android without Termux: the control path

The phone submits a bounded task through an approved control surface and a
short-lived runner executes it:

1. Open the repository **Actions** page on the phone and run the
   **Android Task Runner** workflow with a task description. The repository
   stores only the task specification.
2. A short-lived GitHub Actions runner executes the bounded, CPU-only task and
   uploads the result as an artifact.
3. Check the result from the phone. If the runtime ends, the next run resumes
   from the last valid checkpoint (session journals + memory ledger).

For GPU work, start the Colab notebook yourself and run the job through the
notebook UI; free Colab is not guaranteed or persistent.

## Tests

```bash
python -m unittest discover -s tests -v
```

The whole suite (including `tests/test_agent_loop.py`) runs without network
access, models or third-party packages. Baseline recorded on 2026-11-24:
**247 tests, OK (1 skipped)** on Python 3.12. CI runs the same suite on
Python 3.10 and 3.12 plus byte-compilation, integrity and CLI smoke checks.

## Limitations (honest list)

- Model routing is a Gemini-API model cascade with retry/fallback — not a
multi-provider router.
- The debugger implements four stages (recall, static analysis, adversarial
verification, repair-and-verify); optional tools such as Z3, SymPy and
Hypothesis are capability-dependent.
- Memory is an episodic SQLite ledger + FTS5/LIKE keyword index + skill files;
there is no embedding-based semantic memory or knowledge graph yet.
- "Verified" is claimed only when the required tests actually ran and passed
in the declared environment; otherwise results are labelled static review /
not executed.
- No fixed "10B context" claim: context is managed per request and provider
usage is recorded in a token ledger when the provider reports it.

## License

Proprietary — see `pyproject.toml`.
