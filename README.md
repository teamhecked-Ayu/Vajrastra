# Vajrastra

Vajrastra is a Python-based AI engineering project. Its repository includes a command-line interface, core services, optional engines, tests, and a Google Colab worker notebook.

## Requirements

- Python 3.10 or newer.
- Git, to clone the repository.
- API credentials only for features that call an external model or GitHub.

The core code is designed to use the Python standard library. Optional engine features may need extra packages or external credentials. Android devices without a local Python runtime use the remote Colab/runner workflow rather than running the Python core directly.

## Setup

```bash
git clone https://github.com/teamhecked-Ayu/Vajrastra.git
cd Vajrastra
python -m venv .venv
```

Activate the virtual environment:

- Linux/macOS: `. .venv/bin/activate`
- Windows PowerShell: `.venv\\Scripts\\Activate.ps1`

Install the project in editable mode:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

## Run

Show the available commands:

```bash
python vajrastra.py --help
```

The installed command-line entry point is:

```bash
vajrastra --help
```

Some features require provider credentials. Do not commit API keys, access tokens, or other secrets to this repository.

## Tests

Run the repository test suite:

```bash
python -m compileall vajrastra.py core engines tests
python -m core._guard_integrity check --root .
python -m unittest discover -s tests -v
```

GitHub Actions runs the workflow in `.github/workflows/watchdog.yml` on Ubuntu, Windows, and macOS with Python 3.10 and 3.12.

## Repository guide

- `vajrastra.py`: command-line entry point.
- `core/`: shared runtime and security components.
- `engines/`: higher-level engineering and research features.
- `tests/`: automated tests.
- `templates/`: research templates.
- `notebooks/`: Colab workflow.
- `docs/`: project and architecture notes.

See [the architecture guide](docs/ARCHITECTURE.md) for the current structure and verification flow.
