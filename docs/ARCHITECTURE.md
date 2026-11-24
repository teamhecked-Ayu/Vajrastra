# Vajrastra architecture

This document describes the repository as it exists today. It does not claim that every feature or operating system has completed end-to-end acceptance testing.

## 1. Runtime boundaries

- Python 3.10 or newer is the declared minimum.
- The `core/` package is intended to remain Python standard-library-only.
- Optional engines may use additional packages or external services. Those dependencies must stay outside core imports and should be loaded only by the feature that needs them.
- The CLI runs where a supported Python runtime is installed.
- Android without Termux or another local Python runtime is a remote-client path to a separately running Colab/worker environment. It is not a native local Python runtime.
- CI currently checks Ubuntu, Windows, and macOS on Python 3.10 and 3.12. This is platform coverage in CI, not proof that every feature is fully supported on every platform.

## 2. Repository map

```text
vajrastra.py                    CLI entry point
core/                           shared runtime, security, state, and platform services
engines/                        higher-level debugging, generation, research, and workflow engines
tests/                          automated test suite
templates/                      research and model templates
notebooks/                      Google Colab worker workflow
docs/                           architecture and project documentation
.github/workflows/watchdog.yml  CI workflow
pyproject.toml                  package metadata and CLI entry point
README.md                       setup and usage guide
```

Runtime data directories such as memory and research state may be created or updated when features run. They are not substitutes for source code or test fixtures.

## 3. Security and integrity boundary

The security implementation and its lock manifest are protected project assets. Changes to protected files require a deliberate security review and a valid reseal process. Routine documentation, packaging, or test maintenance must not edit the protected security files or silently regenerate their lock.

The CI integrity gate runs:

```bash
python -m core._guard_integrity check --root .
```

A failed integrity check must be investigated. Do not bypass it by weakening the gate or changing the sealed manifest without authorization.

## 4. Packaging

`pyproject.toml` declares:

- package name and version;
- Python minimum version;
- the `vajrastra` console command;
- package discovery for `core` and `engines`.

The project README is the package long description. Development and optional feature dependencies must be documented separately from the standard-library core.

## 5. Continuous integration

The workflow at `.github/workflows/watchdog.yml` runs on pushes, pull requests, and manual dispatch. Its matrix is:

- Operating systems: Ubuntu, Windows, macOS.
- Python versions: 3.10 and 3.12.

Each job performs these steps in order:

1. Check out the repository and install the selected Python version.
2. Byte-compile `vajrastra.py`, `core/`, `engines/`, and `tests/`.
3. Run the protected-file integrity gate.
4. Run `python -m unittest discover -s tests -v`.
5. Run the core smoke import, only if earlier steps pass.

The current workflow does not run the CLI smoke commands or the research benchmark described in older documentation. Do not report those checks as CI coverage unless the workflow is changed and the new run passes.

## 6. Local verification

From the repository root:

```bash
python -m compileall vajrastra.py core engines tests
python -m core._guard_integrity check --root .
python -m unittest discover -s tests -v
python -c "import core.security_guard, core.agent_loop, core.memory_engine; print('core imports OK')"
```

A command is verified only when it has actually completed successfully in the target environment. Passing on one operating system does not prove it passes on the others.

## 7. Change discipline

- Keep core free of third-party runtime dependencies.
- Do not weaken tests to make CI green.
- Do not bypass or automatically reseal the integrity lock.
- Keep optional dependencies lazy and isolated from core.
- Treat platform detection separately from tested platform support.
- Record real test results and unresolved failures in the pull request.
