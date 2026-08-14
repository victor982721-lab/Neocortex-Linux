"""Resource-safety contract for ``Neocortex code validate`` on Linux."""

from __future__ import annotations

import base64
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import code_validation_resources as resources


GIB = 1024**3


def _snapshot(*, available: int = 10 * GIB, some: float = 0.0, full: float = 0.0):
    return resources.LinuxResourceSnapshot(
        16 * GIB,
        available,
        8 * GIB,
        8 * GIB,
        some,
        full,
    )


def _admission() -> resources.CodeValidationResourceAdmission:
    snapshot = _snapshot()
    policy = resources.code_validation_resource_policy(snapshot)
    return resources.CodeValidationResourceAdmission(
        resources.CODE_VALIDATION_RESOURCE_SCHEMA,
        policy,
        snapshot,
        policy.desktop_reserve_bytes + policy.memory_max_bytes,
        "neocortex-code-validate-123-abcdef123456",
        "systemd-user-service-cgroup-v2",
        "private-network-namespace-no-external-egress",
    )


def test_linux_snapshot_parses_meminfo_and_pressure(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    pressure = tmp_path / "pressure"
    meminfo.write_text(
        "MemTotal:       16777216 kB\n"
        "MemAvailable:   10485760 kB\n"
        "SwapTotal:       8388608 kB\n"
        "SwapFree:        4194304 kB\n",
        encoding="ascii",
    )
    pressure.write_text(
        "some avg10=1.25 avg60=0.50 avg300=0.10 total=12\n"
        "full avg10=0.20 avg60=0.10 avg300=0.00 total=2\n",
        encoding="ascii",
    )

    observed = resources.read_linux_resource_snapshot(
        meminfo_path=meminfo,
        pressure_path=pressure,
    )

    assert observed.total_memory_bytes == 16 * GIB
    assert observed.available_memory_bytes == 10 * GIB
    assert observed.total_swap_bytes == 8 * GIB
    assert observed.free_swap_bytes == 4 * GIB
    assert observed.pressure_some_avg10 == 1.25
    assert observed.pressure_full_avg10 == 0.2


def test_resource_policy_preserves_desktop_and_caps_the_complete_tree() -> None:
    policy = resources.code_validation_resource_policy(_snapshot())

    assert policy.desktop_reserve_bytes >= 4 * GIB
    assert policy.memory_max_bytes == 4 * GIB
    assert policy.memory_high_bytes < policy.memory_max_bytes
    assert policy.memory_swap_max_bytes == 512 * 1024**2
    assert 100 <= policy.cpu_quota_percent <= 400
    assert policy.overall_runtime_seconds == 45 * 60


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    (
        (_snapshot(available=6 * GIB), "desktop_memory_reserve_unavailable"),
        (_snapshot(some=6.0), "memory_pressure_some_above_preflight"),
        (_snapshot(full=1.5), "memory_pressure_full_above_preflight"),
    ),
)
def test_preflight_fails_closed_before_launch(snapshot, reason: str) -> None:
    with pytest.raises(resources.CodeValidationResourceError, match=reason):
        resources._preflight(  # type: ignore[attr-defined]
            snapshot,
            resources.code_validation_resource_policy(snapshot),
        )


def test_admission_roundtrip_is_typed_and_tamper_evident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    encoded = base64.urlsafe_b64encode(
        json.dumps(admission.as_payload(), sort_keys=True).encode("ascii")
    ).decode("ascii")
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY",
        resources.CODE_VALIDATION_RESOURCE_POLICY,
    )
    monkeypatch.setenv("NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION", encoded)

    assert resources.current_code_validation_resource_admission() == admission

    payload = admission.as_payload()
    payload["required_available_memory_bytes"] = 1
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True).encode("ascii")
    ).decode("ascii")
    monkeypatch.setenv("NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION", forged)
    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_admission_receipt_invalid",
    ):
        resources.current_code_validation_resource_admission()


def test_systemd_command_contains_hard_tree_limits_without_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("NEOCORTEX_SECRET_FIXTURE", "must-not-cross")
    command = resources._systemd_command(  # type: ignore[attr-defined]
        ("/bin/true",),
        cwd=tmp_path,
        admission=_admission(),
        json_output=True,
    )
    joined = "\n".join(command)

    assert "--property=MemoryMax=4294967296" in command
    assert "--property=MemorySwapMax=536870912" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=OOMPolicy=stop" in command
    assert "--property=PrivateNetwork=yes" in command
    assert "--property=RuntimeMaxSec=2700s" in command
    assert "--quiet" in command
    assert "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY=" in joined
    assert (
        "NEOCORTEX_PIP_AUDIT_NETWORK_POLICY=disabled-by-code-validation" in joined
    )
    assert "must-not-cross" not in joined


def test_boundary_does_not_launch_when_live_headroom_is_insufficient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resources,
        "read_linux_resource_snapshot",
        lambda **_kwargs: _snapshot(available=6 * GIB),
    )
    launched = False

    def fail_if_launched(*_args, **_kwargs):
        nonlocal launched
        launched = True
        return subprocess.CompletedProcess((), 0)

    monkeypatch.setattr(resources.subprocess, "run", fail_if_launched)

    with pytest.raises(
        resources.CodeValidationResourceError,
        match="desktop_memory_reserve_unavailable",
    ):
        resources.run_code_validation_in_resource_boundary(
            ("/bin/true",),
            cwd=tmp_path,
            json_output=False,
        )

    assert launched is False


def test_boundary_runs_one_cgroup_worker_and_returns_its_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter((_snapshot(), _snapshot()))
    monkeypatch.setattr(
        resources,
        "read_linux_resource_snapshot",
        lambda **_kwargs: next(snapshots, _snapshot()),
    )

    @contextmanager
    def lock():
        yield None

    monkeypatch.setattr(resources, "_exclusive_validation_lock", lock)

    class AdmittedCoordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @contextmanager
        def admit(self, route_name: str, memory_bytes: int, cpu_slots: int):
            assert route_name == "code-validation"
            assert memory_bytes == 4 * GIB
            assert cpu_slots == 1
            yield None

    # This test exercises construction of the one-cgroup worker, not the live
    # host-admission algorithm.  Keeping host RAM/load out of the fixture makes
    # it deterministic when it is itself executed inside trusted-deep coverage.
    monkeypatch.setattr(resources, "GlobalResourceCoordinator", AdmittedCoordinator)
    monkeypatch.setattr(
        resources,
        "_watch_resources",
        lambda _admission, stopped, _state: stopped.wait(1),
    )
    observed: list[tuple[str, ...]] = []

    def completed(arguments, **_kwargs):
        observed.append(tuple(str(item) for item in arguments))
        return subprocess.CompletedProcess(arguments, 7)

    monkeypatch.setattr(resources.subprocess, "run", completed)

    status = resources.run_code_validation_in_resource_boundary(
        ("/bin/false",),
        cwd=tmp_path,
        json_output=True,
    )

    assert status == 7
    assert len(observed) == 1
    assert observed[0][0] == "/usr/bin/systemd-run"
    assert observed[0][-2:] == ("--", "/bin/false")
