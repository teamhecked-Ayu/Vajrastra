"""Evidence-based parser, compiler, and test-runner capability reporting.

Detection never installs packages and never treats a file extension as proof that
a language can be compiled or tested on this host.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, Optional, Tuple


@dataclass(frozen=True)
class ToolCapability:
    language: str
    parser: Optional[str] = None
    compiler: Optional[str] = None
    runtime: Optional[str] = None
    test_runner: Optional[str] = None
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def _version(executable: str, args: Iterable[str] = ("--version",)) -> Optional[str]:
    path = shutil.which(executable)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, *args], capture_output=True, text=True, timeout=3,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return path
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return output[0][:160] if output else path


def detect_language_capabilities() -> Dict[str, ToolCapability]:
    """Return capabilities backed by installed executables, not assumptions."""
    python_version = sys.version.split()[0]
    python = ToolCapability(
        language="python",
        parser="stdlib ast",
        compiler=f"python {python_version} (py_compile)",
        runtime=f"python {python_version}",
        test_runner="unittest (stdlib)",
        notes=("pytest: " + (_version("pytest") or "not installed"),),
    )

    specs = {
        "javascript": ("node", None, "npm", "node --test"),
        "typescript": ("node", "tsc", "npm", "requires TypeScript compiler for type-checking"),
        "java": (None, "javac", "java", "JUnit availability is project-dependent"),
        "go": (None, "go", "go", "go test"),
        "rust": (None, "rustc", "cargo", "cargo test"),
        "c": (None, "gcc", None, "test harness is project-defined"),
        "cpp": (None, "g++", None, "test harness is project-defined"),
        "ruby": (None, None, "ruby", "Minitest is part of standard Ruby"),
        "php": (None, None, "php", "PHPUnit availability is project-dependent"),
    }
    result: Dict[str, ToolCapability] = {"python": python}
    for language, (runtime, compiler, runner, note) in specs.items():
        runtime_version = _version(runtime) if runtime else None
        compiler_version = _version(compiler) if compiler else None
        runner_version = _version(runner) if runner and runner != runtime else None
        notes = []
        if runtime and not runtime_version:
            notes.append(f"{runtime} runtime not installed")
        if compiler and not compiler_version:
            notes.append(f"{compiler} compiler not installed")
        if runner and not runner_version and runner != runtime:
            notes.append(f"{runner} test runner not installed")
        notes.append(note)
        result[language] = ToolCapability(
            language=language,
            parser=None,
            compiler=compiler_version,
            runtime=runtime_version,
            test_runner=runner_version,
            notes=tuple(notes),
        )
    return result


def capability_report() -> Dict[str, object]:
    """Serializable report suitable for CLI output and benchmark artifacts."""
    capabilities = detect_language_capabilities()
    return {
        "python_version": sys.version.split()[0],
        "languages": {name: item.as_dict() for name, item in capabilities.items()},
    }
