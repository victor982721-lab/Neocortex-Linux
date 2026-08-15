"""Resource-safety contract for ``Neocortex code validate`` on Linux."""

from __future__ import annotations

import base64
import errno
import json
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import replace
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
        "systemd-private-network-plus-kernel-denied-inet",
        "proc-self-cgroup-v2-exact-systemd-unit",
        "systemd-unit-private-network-yes",
        "kernel-denies-af-inet-and-af-inet6",
    )


def _bind_kernel_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admission: resources.CodeValidationResourceAdmission,
    *,
    member: bool = True,
    private_network: bool = True,
) -> None:
    cgroup = tmp_path / "self-cgroup"
    component = f"{admission.cgroup_unit}.service" if member else "unrelated.service"
    cgroup.write_text(f"0::/user.slice/app.slice/{component}\n", encoding="ascii")
    monkeypatch.setattr(resources, "_SELF_CGROUP", cgroup)

    def verify_network(_admission) -> None:
        if not private_network:
            raise resources.CodeValidationResourceError(
                "code_validation_private_network_not_active"
            )

    monkeypatch.setattr(resources, "_verify_private_network_boundary", verify_network)
    monkeypatch.setattr(resources, "_verify_inet_socket_boundary", lambda: None)


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
    assert policy.overall_runtime_seconds == 75 * 60


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    _bind_kernel_boundary(tmp_path, monkeypatch, admission)
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
    forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode("ascii")).decode(
        "ascii"
    )
    monkeypatch.setenv("NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION", forged)
    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_admission_receipt_invalid",
    ):
        resources.current_code_validation_resource_admission()


def test_installed_parent_runtime_tuning_remains_bootstrap_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    legacy_parent = replace(
        admission,
        policy=replace(admission.policy, overall_runtime_seconds=45 * 60),
    )
    _bind_kernel_boundary(tmp_path, monkeypatch, legacy_parent)
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY",
        "linux-desktop-preserving-cgroup-v2",
    )
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION",
        resources._encode_admission(legacy_parent),  # type: ignore[attr-defined]
    )

    observed = resources.current_code_validation_resource_admission()

    assert resources.CODE_VALIDATION_RESOURCE_POLICY == "linux-desktop-preserving-cgroup-v2"
    assert observed == legacy_parent
    assert observed.policy.overall_runtime_seconds == 45 * 60


def test_structurally_valid_admission_outside_claimed_cgroup_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    _bind_kernel_boundary(tmp_path, monkeypatch, admission, member=False)
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY",
        resources.CODE_VALIDATION_RESOURCE_POLICY,
    )
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION",
        resources._encode_admission(admission),  # type: ignore[attr-defined]
    )

    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_admission_cgroup_mismatch",
    ):
        resources.current_code_validation_resource_admission()


def test_admission_rejects_a_unit_without_private_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    _bind_kernel_boundary(tmp_path, monkeypatch, admission, private_network=False)
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY",
        resources.CODE_VALIDATION_RESOURCE_POLICY,
    )
    monkeypatch.setenv(
        "NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION",
        resources._encode_admission(admission),  # type: ignore[attr-defined]
    )

    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_private_network_not_active",
    ):
        resources.current_code_validation_resource_admission()


def test_private_network_verification_queries_the_exact_live_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    observed: list[tuple[str, ...]] = []

    def completed(arguments, **_kwargs):
        observed.append(tuple(str(item) for item in arguments))
        return subprocess.CompletedProcess(arguments, 0, "yes\n", "")

    monkeypatch.setattr(resources.subprocess, "run", completed)

    resources._verify_private_network_boundary(admission)  # type: ignore[attr-defined]

    assert observed == [
        (
            "/usr/bin/systemctl",
            "--user",
            "show",
            f"{admission.cgroup_unit}.service",
            "--property=PrivateNetwork",
            "--value",
        )
    ]

    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, "no\n", ""),
    )
    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_private_network_not_active",
    ):
        resources._verify_private_network_boundary(admission)  # type: ignore[attr-defined]


def test_inet_socket_boundary_requires_kernel_denial_for_both_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []

    def denied(family: int, _kind: int):
        observed.append(family)
        raise OSError(errno.EAFNOSUPPORT, "denied")

    monkeypatch.setattr(resources.socket, "socket", denied)

    resources._verify_inet_socket_boundary()  # type: ignore[attr-defined]

    assert observed == [socket.AF_INET, socket.AF_INET6]


def test_inet_socket_boundary_rejects_a_structurally_valid_but_open_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OpenSocket:
        def close(self) -> None:
            pass

    monkeypatch.setattr(resources.socket, "socket", lambda _family, _kind: OpenSocket())

    with pytest.raises(
        resources.CodeValidationResourceError,
        match="code_validation_inet_socket_boundary_not_active",
    ):
        resources._verify_inet_socket_boundary()  # type: ignore[attr-defined]


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
    assert "--property=KillSignal=SIGINT" in command
    assert "--property=FinalKillSignal=SIGKILL" in command
    assert "--property=SendSIGKILL=yes" in command
    assert "--property=OOMPolicy=stop" in command
    assert "--property=PrivateNetwork=yes" in command
    assert "--property=RestrictAddressFamilies=AF_UNIX" in command
    assert "--property=RuntimeMaxSec=4500s" in command
    assert "--quiet" in command
    assert "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY=" in joined
    assert "NEOCORTEX_PIP_AUDIT_NETWORK_POLICY=disabled-by-code-validation" in joined
    assert "must-not-cross" not in joined


def test_watchdog_does_not_mistake_contained_reclaim_for_desktop_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    snapshots = iter((_snapshot(available=10 * GIB, full=6.0),))
    monkeypatch.setattr(
        resources,
        "read_linux_resource_snapshot",
        lambda **_kwargs: next(snapshots),
    )
    stopped_units: list[str] = []
    monkeypatch.setattr(resources, "_stop_unit", stopped_units.append)

    class StopAfterOneSample:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    state = resources._WatchdogState(  # type: ignore[attr-defined]
        admission.before.available_memory_bytes,
        admission.before.free_swap_bytes,
        0.0,
        0.0,
    )

    resources._watch_resources(  # type: ignore[attr-defined]
        admission,
        StopAfterOneSample(),  # type: ignore[arg-type]
        state,
    )

    assert state.max_pressure_full_avg10 == 6.0
    assert state.abort_reason is None
    assert stopped_units == []


def test_watchdog_aborts_pressure_when_desktop_headroom_is_threatened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    pressure_headroom = admission.policy.desktop_reserve_bytes + admission.policy.memory_high_bytes
    snapshot = _snapshot(available=pressure_headroom - 1, full=6.0)
    monkeypatch.setattr(
        resources,
        "read_linux_resource_snapshot",
        lambda **_kwargs: snapshot,
    )
    stopped_units: list[str] = []
    monkeypatch.setattr(resources, "_stop_unit", stopped_units.append)

    class OneSample:
        def wait(self, _timeout: float) -> bool:
            return False

    state = resources._WatchdogState(  # type: ignore[attr-defined]
        admission.before.available_memory_bytes,
        admission.before.free_swap_bytes,
        0.0,
        0.0,
    )

    resources._watch_resources(  # type: ignore[attr-defined]
        admission,
        OneSample(),  # type: ignore[arg-type]
        state,
    )

    assert state.abort_reason == "memory_pressure_full_abort_threshold"
    assert stopped_units == [admission.cgroup_unit]


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
    capsys: pytest.CaptureFixture[str],
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
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("CODE_CHANGE_VALIDATION_RESOURCES ")
