"""Tests for host detection and execution routing."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.os_adapter import (  # noqa: E402
    GPUInfo,
    HostOS,
    HardwareProfile,
    OSAdapter,
    ExecutionMode,
    TaskClass,
    detect_os,
    parse_task_class,
    get_adapter,
)

TOKEN_VARS = ("VAJRASTRA_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def make_profile(host_os: HostOS, gpus=()) -> HardwareProfile:
    return HardwareProfile(
        host_os=host_os,
        system=host_os.value,
        release="test",
        machine="x86_64",
        python_version="3.12.0",
        cpu_count=4,
        cpu_model="Test CPU",
        total_memory_mb=4096,
        gpus=tuple(gpus),
        is_android=host_os is HostOS.ANDROID,
    )


def clean_env() -> dict:
    env = {key: value for key, value in os.environ.items() if key not in TOKEN_VARS + ("VAJRASTRA_EXECUTION_MODE",)}
    return env


class DetectionTests(unittest.TestCase):
    def test_detect_os_returns_supported_enum(self) -> None:
        self.assertIn(detect_os(), set(HostOS))

    def test_profile_serialises(self) -> None:
        adapter = OSAdapter(make_profile(HostOS.LINUX))
        payload = adapter.describe()
        self.assertIn("host_os", payload)
        self.assertIn("available_modes", payload)
        self.assertIsInstance(payload["gpus"], list)
        self.assertTrue(any("host" in line for line in adapter.summary_lines()))

    def test_task_class_parsing(self) -> None:
        self.assertIs(parse_task_class("FUZZ"), TaskClass.FUZZ)
        self.assertIs(parse_task_class("not-a-task"), TaskClass.BUILD)
        self.assertIs(parse_task_class(TaskClass.GPU), TaskClass.GPU)

    def test_get_adapter_is_cached(self) -> None:
        self.assertIs(get_adapter(), get_adapter())


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, clean_env(), clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_android_heavy_goes_to_cloud(self) -> None:
        decision = OSAdapter(make_profile(HostOS.ANDROID)).route(TaskClass.FUZZ)
        self.assertIs(decision.mode, ExecutionMode.CLOUD_BRIDGE)
        self.assertTrue(decision.queue_required)
        self.assertFalse(decision.cloud_ready)

    def test_android_light_stays_local(self) -> None:
        decision = OSAdapter(make_profile(HostOS.ANDROID)).route(TaskClass.BUILD)
        self.assertIs(decision.mode, ExecutionMode.LOCAL_NATIVE)
        self.assertIsNot(decision.mode, ExecutionMode.CLOUD_BRIDGE)

    def test_android_bridge_ready_with_token(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "present"}, clear=False):
            decision = OSAdapter(make_profile(HostOS.ANDROID)).route(TaskClass.TRAINING)
        self.assertTrue(decision.cloud_ready)
        self.assertFalse(decision.queue_required)

    def test_linux_with_gpu_runs_local(self) -> None:
        profile = make_profile(HostOS.LINUX, [GPUInfo("nvidia", "RTX 4090", 24576, "cuda")])
        decision = OSAdapter(profile).route(TaskClass.TRAINING)
        self.assertIs(decision.mode, ExecutionMode.LOCAL_NATIVE)
        self.assertIn("cuda", decision.reason)

    def test_linux_without_gpu_notes_cpu_fallback(self) -> None:
        decision = OSAdapter(make_profile(HostOS.LINUX)).route(TaskClass.TRAINING)
        self.assertIs(decision.mode, ExecutionMode.LOCAL_NATIVE)
        self.assertTrue(decision.notes)

    def test_parrot_and_windows_are_local(self) -> None:
        for host in (HostOS.PARROT, HostOS.MACOS, HostOS.WINDOWS):
            with self.subTest(host=host):
                decision = OSAdapter(make_profile(host)).route(TaskClass.BENCHMARK)
                self.assertIs(decision.mode, ExecutionMode.LOCAL_NATIVE)

    def test_explicit_override(self) -> None:
        adapter = OSAdapter(make_profile(HostOS.LINUX))
        self.assertIs(adapter.route("build", mode="cloud").mode, ExecutionMode.CLOUD_BRIDGE)
        self.assertIs(adapter.route("fuzz", mode="local").mode, ExecutionMode.LOCAL_NATIVE)

    def test_environment_override(self) -> None:
        with mock.patch.dict(os.environ, {"VAJRASTRA_EXECUTION_MODE": "cloud"}, clear=False):
            decision = OSAdapter(make_profile(HostOS.LINUX)).route(TaskClass.BUILD)
        self.assertIs(decision.mode, ExecutionMode.CLOUD_BRIDGE)
        self.assertTrue(decision.reason.startswith("explicit override"))

    def test_decision_serialises(self) -> None:
        payload = OSAdapter(make_profile(HostOS.LINUX)).route(TaskClass.BUILD).as_dict()
        self.assertEqual(payload["mode"], "local_native")
        self.assertIn("notes", payload)


if __name__ == "__main__":
    unittest.main()
