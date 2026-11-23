# Vajrastra

Vajrastra is an early-stage Python AI engineering framework. The repository currently contains a standard-library CLI, security checks, SQLite-backed memory, session persistence, a bounded agent loop, model routing, debugging and experimental engines.

> **Project status:** version 0.3.0. This is not yet a production-ready autonomous engineer. Features are at different maturity levels; see the limitations below.

## Requirements

- Python 3.10 or newer.
- No third-party runtime packages are required for the core CLI.
- Optional model calls require a Gemini API key. Provider quotas, model availability and free-tier limits are controlled by Google and can change.
- GitHub sync and the job worker require a GitHub token with the repository permissions needed for the selected operation.

## Run from a checkout

Run commands from the repository root:

```bash
python --version
python vajrastra.py --help
python vajrastra.py memory stats
python vajrastra.py research "example topic" --seeds 3 --budget 1
```

Running `python vajrastra.py` without arguments opens the interactive REPL.

The installed console entry point is `vajrastra`. The direct Python command above avoids requiring package installation. GitHub repository overrides are currently available through the relevant `--repo owner/name` CLI options; there is no repository-name environment variable yet.

## Configuration

Set only the credentials and command-line overrides needed for the operation. Never commit secrets.

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Enables Gemini model routing. Colab Secrets are also checked when running in Colab. |
| `GITHUB_TOKEN`, `VAJASTRA_GITHUB_TOKEN` or `GH_TOKEN` | Enables GitHub API operations. |
| `VAJASTRA_GITHUB_REPO` | Overrides the default GitHub repository in the sync client. Use `owner/repository`. |
| `VAJASTRA_EXECUTION_MODE` | Overrides execution routing with `local` or `cloud`. |
| `VAJASTRA_ROOT` | Sets the workspace root used by the CLI. |

Example (Linux/macOS shell):

```bash
export GEMINI_API_KEY="your-key"
export GITHUB_TOKEN="your-token"
python vajrastra.py --help
```

On Windows PowerShell, use `$env:NAME = "value"` instead of `export`.

## Current command surface

The CLI exposes these command families (run `python vajrastra.py --help` for exact arguments):

- `fix`: analyse a file, directory or error signature; optional repair flow depends on available model/configuration.
- `build`: classify and route a build request, producing a plan or job specification where supported.
- `research`: inspect local memory and run the available bounded experiment workflow. It is not a live literature search engine.
- `evolve`: audit the current integrity and stored skills; promotion and push operations are gated.
- `worker`: process supported jobs from the configured GitHub inbox.
- `memory`: inspect or search the local SQLite knowledge store.
- `sync`: import, export or synchronise memory through the GitHub API.

## Architecture at a glance

- `vajrastra.py`: CLI and REPL entry point.
- `core/security_guard.py`, `core/_guard_rules.py`, `core/_guard_integrity.py`: AST-based static checks and a SHA-256 integrity manifest.
- `core/memory_engine.py`: SQLite episodic memory, full-text search where available, and procedural skill storage.
- `core/session_manager.py` and `core/agent_loop.py`: persisted sessions, checkpoints, bounded tool execution and recovery.
- `core/model_router.py`: standard-library HTTP client for the configured Gemini model cascade.
- `core/os_adapter.py`: host detection and execution-mode selection.
- `core/github_sync.py`: GitHub Contents API operations for memory sync.
- `engines/`: debugging, code architecture, ML experiments, self-evolution and job processing.

The `core/` package and CLI are intended to remain Python-standard-library-only. Optional UI or ML dependencies must stay outside the core and be imported lazily.

## Security model and limits

- The AST scanner is a static policy check, not a complete sandbox. It cannot guarantee safe execution of arbitrary code.
- The SHA-256 manifest detects changes to the files it covers. It does not prove that the code is secure or trustworthy.
- Review changes to the security layer and integrity manifest carefully. Do not automatically re-seal after an unexplained integrity failure.
- Do not place API keys, access tokens or private data in source files, issues, logs or committed memory.
- Treat generated code, remote jobs and model output as untrusted until reviewed and tested.
- Do not use the Colab free tier as an always-on server. Runtime availability and limits are not guaranteed.

## Platform support

The project contains host detection for Android, Linux, Parrot OS, macOS and Windows. Detection is not the same as verified end-to-end support.

Android without Termux should be treated as a remote-control/client path that submits work to a separately started runner. It is not a native local Python runtime. Cross-platform support remains subject to actual platform testing.

## Tests and CI

Run the standard-library test suite:

```bash
python -m compileall -q core engines templates vajrastra.py tests
python -m unittest discover -s tests -v
```

GitHub Actions currently runs the watchdog workflow on Ubuntu with Python 3.10 and 3.12. That does not establish Windows, macOS, Android or Parrot runtime compatibility.

## Development status

The project is under active development. A module existing in the repository does not mean its feature is complete. Planned capabilities such as a vector store, knowledge graph, reasoning ensemble, multi-agent swarm, research swarm and dedicated automatic test generator must be treated as future work until implemented and tested.

Development follows the staged roadmap in the Vajrastra Final Project Blueprint. A phase is complete only when its documented exit criteria pass.
