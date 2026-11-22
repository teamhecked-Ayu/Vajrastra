"""Cross-OS adapter for Vajrastra.

Detects the host (``android``, ``parrot``, ``linux``, ``macos``, ``windows``),
profiles its compute (CPU, RAM, GPU) and decides where a task should run:

* **Local Native Mode** -- full toolchain on Linux, Parrot OS, macOS and
  Windows.  Nothing leaves the machine.
* **Cloud Bridge Mode** -- Android has no Termux in the supported matrix, so
  heavy GPU / fuzzing / training work is dispatched to Google Colab through the
  GitHub API by :mod:`vajrastra`.

Standard library only, and every probe degrades gracefully: an unknown value is
reported as unknown instead of breaking the run.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROBE_TIMEOUT = 4.0


class HostOS(str, Enum):
    ANDROID = "android"
    PARROT = "parrot"
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


class ExecutionMode(str, Enum):
    LOCAL_NATIVE = "local_native"
    CLOUD_BRIDGE = "cloud_bridge"


class TaskClass(str, Enum):
    DIAGNOSTIC = "diagnostic"
    PATCH = "patch"
    BUILD = "build"
    RESEARCH = "research"
    GPU = "gpu"
    TRAINING = "training"
    FUZZ = "fuzz"
    BENCHMARK = "benchmark"


HEAVY_TASKS: frozenset = frozenset(
    {TaskClass.GPU, TaskClass.TRAINING, TaskClass.FUZZ, TaskClass.BENCHMARK}
)

MODE_ENV_VAR = "VAJRASTRA_EXECUTION_MODE"
ROOT_ENV_VAR = "VAJRASTRA_ROOT"
BRIDGE_TOKEN_VARS: Tuple[str, ...] = ("VAJRASTRA_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


@dataclass(frozen=True)
class GPUInfo:
    vendor: str
    name: str
    memory_mb: int = 0
    backend: str = "unknown"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "name": self.name,
            "memory_mb": self.memory_mb,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class HardwareProfile:
    host_os: HostOS
    system: str
    release: str
    machine: str
    python_version: str
    cpu_count: int
    cpu_model: str
    total_memory_mb: int
    gpus: Tuple[GPUInfo, ...] = ()
    is_android: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def accelerators(self) -> Tuple[str, ...]:
        return tuple(sorted({gpu.backend for gpu in self.gpus if gpu.backend != "unknown"}))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "host_os": self.host_os.value,
            "system": self.system,
            "release": self.release,
            "machine": self.machine,
            "python_version": self.python_version,
            "cpu_count": self.cpu_count,
            "cpu_model": self.cpu_model,
            "total_memory_mb": self.total_memory_mb,
            "has_gpu": self.has_gpu,
            "accelerators": list(self.accelerators),
            "gpus": [gpu.as_dict() for gpu in self.gpus],
            "is_android": self.is_android,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class RoutingDecision:
    mode: ExecutionMode
    task_class: TaskClass
    reason: str
    cloud_ready: bool = False
    queue_required: bool = False
    notes: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "task_class": self.task_class.value,
            "reason": self.reason,
            "cloud_ready": self.cloud_ready,
            "queue_required": self.queue_required,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        label = (
            "Local Native Mode"
            if self.mode is ExecutionMode.LOCAL_NATIVE
            else "Colab Cloud Bridge Mode"
        )
        return f"{label} ({self.reason})"


# --------------------------------------------------------------------- probing
def _run(command: Sequence[str], timeout: float = PROBE_TIMEOUT) -> Optional[str]:
    """Run a short, read-only probe and return stdout, or ``None``."""
    executable = shutil.which(command[0])
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _read_os_release() -> Dict[str, str]:
    data: Dict[str, str] = {}
    for candidate in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
        break
    return data


def _is_android_environment() -> bool:
    if sys.platform == "android":
        return True
    android_markers = ("ANDROID_ROOT", "ANDROID_DATA", "ANDROID_STORAGE")
    if any(os.environ.get(marker) for marker in android_markers):
        return True
    return Path("/system/build.prop").is_file()


def detect_os() -> HostOS:
    """Classify the host into one of Vajrastra's supported targets."""
    if sys.platform.startswith(("win", "cygwin", "msys")):
        return HostOS.WINDOWS
    if sys.platform == "darwin":
        return HostOS.MACOS
    if sys.platform.startswith("linux") or sys.platform == "android":
        if _is_android_environment():
            return HostOS.ANDROID
        os_release = _read_os_release()
        identity = " ".join(
            [os_release.get("ID", ""), os_release.get("ID_LIKE", ""), os_release.get("NAME", "")]
        ).lower()
        if "parrot" in identity:
            return HostOS.PARROT
        return HostOS.LINUX
    return HostOS.UNKNOWN


def _cpu_model(host_os: HostOS) -> str:
    if host_os in {HostOS.LINUX, HostOS.PARROT}:
        try:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if line.lower().startswith(("model name", "hardware", "processor")):
                _, _, value = line.partition(":")
                if value.strip():
                    return value.strip()
    if host_os is HostOS.ANDROID:
        raw = _run(["getprop", "ro.product.model"]) or _run(["getprop", "ro.soc.model"])
        if raw:
            return raw
    if host_os is HostOS.MACOS:
        raw = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if raw:
            return raw
    if host_os is HostOS.WINDOWS:
        identifier = os.environ.get("PROCESSOR_IDENTIFIER", "")
        if identifier:
            return identifier.strip()
    return platform.processor() or platform.machine() or "unknown"


def _total_memory_mb(host_os: HostOS) -> int:
    if host_os in {HostOS.LINUX, HostOS.PARROT, HostOS.ANDROID}:
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kilobytes = int(line.split()[1])
                    return kilobytes // 1024
        except (OSError, ValueError, IndexError):
            pass
    if host_os is HostOS.MACOS:
        raw = _run(["sysctl", "-n", "hw.memsize"])
        if raw and raw.isdigit():
            return int(raw) // (1024 * 1024)
    if host_os is HostOS.WINDOWS:
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys) // (1024 * 1024)
        except Exception:
            return 0
    return 0


def _nvidia_gpus() -> List[GPUInfo]:
    output = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return []
    gpus: List[GPUInfo] = []
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if not cells or not cells[0]:
            continue
        memory = 0
        if len(cells) > 1:
            digits = "".join(ch for ch in cells[1] if ch.isdigit())
            memory = int(digits) if digits else 0
        gpus.append(
            GPUInfo(vendor="nvidia", name=cells[0], memory_mb=memory, backend="cuda")
        )
    return gpus


def _apple_gpus() -> List[GPUInfo]:
    output = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=8.0)
    if not output:
        return []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    gpus: List[GPUInfo] = []
    for entry in payload.get("SPDisplaysDataType", []) or []:
        name = str(entry.get("sppci_model") or entry.get("_name") or "Apple GPU")
        vendor = str(entry.get("spdisplays_vendor") or "apple").lower()
        gpus.append(GPUInfo(vendor=vendor, name=name, backend="metal"))
    return gpus


def _amd_gpus() -> List[GPUInfo]:
    if not (Path("/opt/rocm").exists() or Path("/dev/kfd").exists()):
        return []
    name = "AMD GPU (ROCm)"
    output = _run(["rocminfo"], timeout=6.0)
    if output:
        for line in output.splitlines():
            if line.strip().lower().startswith("name:") and "gfx" in line.lower():
                name = line.split(":", 1)[1].strip()
                break
    return [GPUInfo(vendor="amd", name=name, backend="rocm")]


def detect_gpus(host_os: Optional[HostOS] = None) -> Tuple[GPUInfo, ...]:
    """Best-effort GPU inventory without importing any third-party library."""
    host_os = host_os or detect_os()
    gpus: List[GPUInfo] = []
    if host_os is not HostOS.ANDROID:
        gpus.extend(_nvidia_gpus())
        if host_os is HostOS.MACOS:
            gpus.extend(_apple_gpus())
        if host_os in {HostOS.LINUX, HostOS.PARROT}:
            gpus.extend(_amd_gpus())
    return tuple(gpus)


def profile_host() -> HardwareProfile:
    host_os = detect_os()
    os_release = _read_os_release() if host_os in {HostOS.LINUX, HostOS.PARROT} else {}
    extra: Dict[str, Any] = {}
    if os_release:
        extra["distro_id"] = os_release.get("ID", "")
        extra["distro_name"] = os_release.get("NAME", "")
    if host_os is HostOS.ANDROID:
        extra["android_sdk"] = _run(["getprop", "ro.build.version.sdk"]) or ""
    return HardwareProfile(
        host_os=host_os,
        system=platform.system() or host_os.value,
        release=platform.release(),
        machine=platform.machine(),
        python_version=platform.python_version(),
        cpu_count=os.cpu_count() or 1,
        cpu_model=_cpu_model(host_os),
        total_memory_mb=_total_memory_mb(host_os),
        gpus=detect_gpus(host_os),
        is_android=host_os is HostOS.ANDROID,
        extra={key: value for key, value in extra.items() if value},
    )


def parse_task_class(value: str | TaskClass) -> TaskClass:
    if isinstance(value, TaskClass):
        return value
    try:
        return TaskClass(str(value).strip().lower())
    except ValueError:
        return TaskClass.BUILD


def bridge_token_present() -> bool:
    """True when a GitHub token is available for dispatching Colab jobs.

    The token itself is never read into memory here, only checked for presence.
    """
    return any(os.environ.get(name) for name in BRIDGE_TOKEN_VARS)


class OSAdapter:
    """Routes work between Local Native Mode and the Colab Cloud Bridge."""

    def __init__(self, profile: Optional[HardwareProfile] = None) -> None:
        self.profile = profile or profile_host()

    @property
    def host_os(self) -> HostOS:
        return self.profile.host_os

    @property
    def local_capable(self) -> bool:
        return self.host_os is not HostOS.ANDROID

    def cloud_ready(self) -> bool:
        return bridge_token_present()

    def available_modes(self) -> Tuple[ExecutionMode, ...]:
        modes: List[ExecutionMode] = []
        if self.local_capable:
            modes.append(ExecutionMode.LOCAL_NATIVE)
        modes.append(ExecutionMode.CLOUD_BRIDGE)
        return tuple(modes)

    def _override(self, mode: Optional[str | ExecutionMode]) -> Optional[ExecutionMode]:
        raw = mode or os.environ.get(MODE_ENV_VAR, "")
        if isinstance(raw, ExecutionMode):
            return raw
        token = str(raw).strip().lower()
        aliases = {
            "local": ExecutionMode.LOCAL_NATIVE,
            "native": ExecutionMode.LOCAL_NATIVE,
            "local_native": ExecutionMode.LOCAL_NATIVE,
            "cloud": ExecutionMode.CLOUD_BRIDGE,
            "bridge": ExecutionMode.CLOUD_BRIDGE,
            "cloud_bridge": ExecutionMode.CLOUD_BRIDGE,
            "colab": ExecutionMode.CLOUD_BRIDGE,
        }
        return aliases.get(token)

    def route(
        self,
        task_class: str | TaskClass = TaskClass.BUILD,
        *,
        mode: Optional[str | ExecutionMode] = None,
    ) -> RoutingDecision:
        """Decide where *task_class* executes on this host."""
        task = parse_task_class(task_class)
        override = self._override(mode)
        heavy = task in HEAVY_TASKS

        if override is ExecutionMode.CLOUD_BRIDGE:
            ready = self.cloud_ready()
            return RoutingDecision(
                mode=ExecutionMode.CLOUD_BRIDGE,
                task_class=task,
                reason="explicit override",
                cloud_ready=ready,
                queue_required=not ready,
                notes=() if ready else ("no GitHub token present; job will be queued",),
            )
        if override is ExecutionMode.LOCAL_NATIVE:
            return RoutingDecision(
                mode=ExecutionMode.LOCAL_NATIVE,
                task_class=task,
                reason="explicit override",
                notes=("forced local execution; heavy work may exceed local capacity",)
                if heavy and not self.profile.has_gpu
                else (),
            )

        if self.host_os is HostOS.ANDROID:
            if heavy:
                ready = self.cloud_ready()
                notes = ["Android has no native toolchain; heavy work is bridged to Colab"]
                if not ready:
                    notes.append("no GitHub token present; job queued for later dispatch")
                return RoutingDecision(
                    mode=ExecutionMode.CLOUD_BRIDGE,
                    task_class=task,
                    reason=f"{task.value} task is heavy and Android is bridge-only",
                    cloud_ready=ready,
                    queue_required=not ready,
                    notes=tuple(notes),
                )
            return RoutingDecision(
                mode=ExecutionMode.LOCAL_NATIVE,
                task_class=task,
                reason=f"{task.value} task runs on-device",
                notes=("on-device execution is CPU only",),
            )

        if heavy and not self.profile.has_gpu:
            return RoutingDecision(
                mode=ExecutionMode.LOCAL_NATIVE,
                task_class=task,
                reason=f"no local GPU detected for {task.value} task",
                cloud_ready=self.cloud_ready(),
                notes=(
                    "running on CPU; set "
                    f"{MODE_ENV_VAR}=cloud to bridge to Colab instead",
                ),
            )

        accelerator = ", ".join(self.profile.accelerators) or "cpu"
        return RoutingDecision(
            mode=ExecutionMode.LOCAL_NATIVE,
            task_class=task,
            reason=f"{self.host_os.value} host running {task.value} on {accelerator}",
        )

    def describe(self) -> Dict[str, Any]:
        profile = self.profile.as_dict()
        profile["mode"] = self.route().mode.value
        profile["available_modes"] = [mode.value for mode in self.available_modes()]
        profile["cloud_bridge_ready"] = self.cloud_ready()
        return profile

    def summary_lines(self) -> List[str]:
        profile = self.profile
        lines = [
            f"host        : {profile.host_os.value} ({profile.system} {profile.release}, {profile.machine})",
            f"cpu         : {profile.cpu_count} core(s) - {profile.cpu_model}",
            f"memory      : {profile.total_memory_mb} MiB" if profile.total_memory_mb else "memory      : unknown",
            "gpus        : "
            + (
                ", ".join(f"{gpu.name} [{gpu.backend}]" for gpu in profile.gpus)
                if profile.gpus
                else "none detected"
            ),
            f"python      : {profile.python_version}",
            f"cloud bridge: {'ready' if self.cloud_ready() else 'not configured (no GitHub token)'}",
        ]
        return lines


@lru_cache(maxsize=1)
def get_adapter() -> OSAdapter:
    """Cached adapter for the current process."""
    return OSAdapter()
