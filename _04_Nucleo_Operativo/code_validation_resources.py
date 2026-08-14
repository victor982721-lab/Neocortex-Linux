"""Linux resource boundary for the canonical source-change validation.

``Neocortex code validate`` is intentionally re-executed inside one transient
user service.  The service cgroup owns the complete descendant tree, unlike a
per-process ``RLIMIT_AS``.  A live admission check preserves desktop headroom
before launch and a parent-side watchdog stops the whole unit if another
workload consumes that reserve while validation is running.

This module does not decide whether a source change is correct.  It only makes
the local experiment bounded, observable and fail-closed.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal, cast

from .global_resources import GlobalResourceCoordinator, GlobalResourceLimits
from .memory_runtime import MemoryBudgetExceeded, MemoryHeadroomTimeout


CODE_VALIDATION_RESOURCE_SCHEMA = "neocortex.code-validation-resources/v2"
CODE_VALIDATION_RESOURCE_POLICY = "linux-desktop-preserving-cgroup-v2"
_BOUNDARY_ENV = "NEOCORTEX_CODE_VALIDATION_RESOURCE_BOUNDARY"
_ADMISSION_ENV = "NEOCORTEX_CODE_VALIDATION_RESOURCE_ADMISSION"
_PIP_AUDIT_NETWORK_POLICY_ENV = "NEOCORTEX_PIP_AUDIT_NETWORK_POLICY"
_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")
_MEMINFO = Path("/proc/meminfo")
_MEMORY_PRESSURE = Path("/proc/pressure/memory")
_SELF_CGROUP = Path("/proc/self/cgroup")
_CGROUP_TEXT_MAX_BYTES = 16 * 1024
_SYSTEMCTL_PROPERTY_TIMEOUT_SECONDS = 5.0
_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_MONITOR_INTERVAL_SECONDS = 0.5
_STOP_TIMEOUT_SECONDS = 10.0
_OVERALL_RUNTIME_SECONDS = 45 * 60
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)


class CodeValidationResourceError(RuntimeError):
    """The canonical validation could not obtain its safe Linux boundary."""


@dataclass(frozen=True, slots=True)
class LinuxResourceSnapshot:
    total_memory_bytes: int
    available_memory_bytes: int
    total_swap_bytes: int
    free_swap_bytes: int
    pressure_some_avg10: float
    pressure_full_avg10: float

    def __post_init__(self) -> None:
        for value in (
            self.total_memory_bytes,
            self.available_memory_bytes,
            self.total_swap_bytes,
            self.free_swap_bytes,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Linux resource byte counters must be non-negative integers")
        if self.total_memory_bytes < 1 or self.available_memory_bytes > self.total_memory_bytes:
            raise ValueError("Linux physical-memory counters are inconsistent")
        if self.free_swap_bytes > self.total_swap_bytes:
            raise ValueError("Linux swap counters are inconsistent")
        for pressure_value in (self.pressure_some_avg10, self.pressure_full_avg10):
            if not 0.0 <= pressure_value <= 100.0:
                raise ValueError("Linux memory-pressure averages are invalid")

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CodeValidationResourcePolicy:
    policy_id: str
    memory_high_bytes: int
    memory_max_bytes: int
    memory_swap_max_bytes: int
    desktop_reserve_bytes: int
    cpu_quota_percent: int
    tasks_max: int
    overall_runtime_seconds: int
    preflight_some_avg10_max: float
    preflight_full_avg10_max: float
    abort_some_avg10_max: float
    abort_full_avg10_max: float

    def __post_init__(self) -> None:
        if self.policy_id != CODE_VALIDATION_RESOURCE_POLICY:
            raise ValueError("code-validation resource policy identity is invalid")
        if not 0 < self.memory_high_bytes <= self.memory_max_bytes:
            raise ValueError("code-validation cgroup memory bounds are invalid")
        if self.memory_swap_max_bytes < 0 or self.desktop_reserve_bytes < _GIB:
            raise ValueError("code-validation reserve or swap bound is invalid")
        if not 100 <= self.cpu_quota_percent <= 400 or self.tasks_max < 16:
            raise ValueError("code-validation CPU or task bound is invalid")
        if not 60 <= self.overall_runtime_seconds <= 90 * 60:
            raise ValueError("code-validation runtime bound is invalid")
        pressure_values = (
            self.preflight_some_avg10_max,
            self.preflight_full_avg10_max,
            self.abort_some_avg10_max,
            self.abort_full_avg10_max,
        )
        if any(not 0.0 <= value <= 100.0 for value in pressure_values):
            raise ValueError("code-validation pressure bounds are invalid")
        if (
            self.abort_some_avg10_max < self.preflight_some_avg10_max
            or self.abort_full_avg10_max < self.preflight_full_avg10_max
        ):
            raise ValueError("code-validation abort pressure must include preflight pressure")

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CodeValidationResourceAdmission:
    schema: str
    policy: CodeValidationResourcePolicy
    before: LinuxResourceSnapshot
    required_available_memory_bytes: int
    cgroup_unit: str
    containment: Literal["systemd-user-service-cgroup-v2"]
    network_policy: Literal["private-network-namespace-no-external-egress"]
    membership_contract: Literal["proc-self-cgroup-v2-exact-systemd-unit"]
    private_network_contract: Literal["systemd-unit-private-network-yes"]

    def __post_init__(self) -> None:
        if self.schema != CODE_VALIDATION_RESOURCE_SCHEMA:
            raise ValueError("code-validation resource schema is invalid")
        expected = self.policy.desktop_reserve_bytes + self.policy.memory_max_bytes
        if self.required_available_memory_bytes != expected:
            raise ValueError("code-validation admission requirement is inconsistent")
        if self.before.available_memory_bytes < expected:
            raise ValueError("code-validation admission lacks physical-memory headroom")
        if not re.fullmatch(r"neocortex-code-validate-[0-9]+-[0-9a-f]{12}", self.cgroup_unit):
            raise ValueError("code-validation cgroup unit is invalid")
        if self.containment != "systemd-user-service-cgroup-v2":
            raise ValueError("code-validation containment kind is invalid")
        if self.network_policy != "private-network-namespace-no-external-egress":
            raise ValueError("code-validation network policy is invalid")
        if self.membership_contract != "proc-self-cgroup-v2-exact-systemd-unit":
            raise ValueError("code-validation membership contract is invalid")
        if self.private_network_contract != "systemd-unit-private-network-yes":
            raise ValueError("code-validation private-network contract is invalid")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "policy": self.policy.as_payload(),
            "before": self.before.as_payload(),
            "required_available_memory_bytes": self.required_available_memory_bytes,
            "cgroup_unit": self.cgroup_unit,
            "containment": self.containment,
            "network_policy": self.network_policy,
            "membership_contract": self.membership_contract,
            "private_network_contract": self.private_network_contract,
        }


def _meminfo_bytes(text: str) -> tuple[int, int, int, int]:
    expected = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator or key not in expected:
            continue
        parts = raw.split()
        if len(parts) != 2 or parts[1] != "kB":
            raise CodeValidationResourceError("linux_meminfo_unit_invalid")
        try:
            value = int(parts[0]) * 1024
        except ValueError as exc:
            raise CodeValidationResourceError("linux_meminfo_value_invalid") from exc
        if value < 0:
            raise CodeValidationResourceError("linux_meminfo_value_invalid")
        values[key] = value
    if values.keys() != expected:
        missing = ",".join(sorted(expected - values.keys()))
        raise CodeValidationResourceError(f"linux_meminfo_incomplete:{missing}")
    return (
        values["MemTotal"],
        values["MemAvailable"],
        values["SwapTotal"],
        values["SwapFree"],
    )


def _pressure_avg10(text: str) -> tuple[float, float]:
    observed: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in {"some", "full"}:
            continue
        values = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
        try:
            observed[parts[0]] = float(values["avg10"])
        except (KeyError, ValueError) as exc:
            raise CodeValidationResourceError("linux_memory_pressure_invalid") from exc
    if observed.keys() != {"some", "full"}:
        raise CodeValidationResourceError("linux_memory_pressure_incomplete")
    return observed["some"], observed["full"]


def read_linux_resource_snapshot(
    *,
    meminfo_path: Path = _MEMINFO,
    pressure_path: Path = _MEMORY_PRESSURE,
) -> LinuxResourceSnapshot:
    """Read the physical/swap headroom and kernel memory PSI without mutation."""

    if sys.platform != "linux" or os.name != "posix":
        raise CodeValidationResourceError("code_validation_resources_are_linux_only")
    try:
        memory = _meminfo_bytes(meminfo_path.read_text(encoding="ascii"))
        pressure = _pressure_avg10(pressure_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError) as exc:
        raise CodeValidationResourceError(
            f"linux_resource_snapshot_unavailable:{type(exc).__name__}"
        ) from exc
    return LinuxResourceSnapshot(*memory, *pressure)


def code_validation_resource_policy(
    snapshot: LinuxResourceSnapshot,
) -> CodeValidationResourcePolicy:
    """Derive one bounded policy from the live host instead of fixed host assumptions."""

    total = snapshot.total_memory_bytes
    desktop_reserve = max(3 * _GIB, total * 30 // 100)
    memory_max = min(4 * _GIB, max(2 * _GIB, total * 30 // 100))
    memory_high = memory_max * 3 // 4
    swap_max = min(512 * _MIB, snapshot.total_swap_bytes // 8)
    cpu_count = os.cpu_count() or 2
    cpu_quota = max(100, min(400, max(1, cpu_count - 2) * 100))
    return CodeValidationResourcePolicy(
        CODE_VALIDATION_RESOURCE_POLICY,
        memory_high,
        memory_max,
        swap_max,
        desktop_reserve,
        cpu_quota,
        512,
        _OVERALL_RUNTIME_SECONDS,
        5.0,
        1.0,
        15.0,
        5.0,
    )


def _unit_name() -> str:
    identity = f"{os.getpid()}:{time.monotonic_ns()}".encode("ascii")
    import hashlib

    return f"neocortex-code-validate-{os.getpid()}-{hashlib.sha256(identity).hexdigest()[:12]}"


def _preflight(
    snapshot: LinuxResourceSnapshot,
    policy: CodeValidationResourcePolicy,
) -> None:
    required = policy.desktop_reserve_bytes + policy.memory_max_bytes
    if snapshot.available_memory_bytes < required:
        raise CodeValidationResourceError(
            "desktop_memory_reserve_unavailable:"
            f"available={snapshot.available_memory_bytes}:required={required}"
        )
    if snapshot.pressure_some_avg10 > policy.preflight_some_avg10_max:
        raise CodeValidationResourceError(
            "memory_pressure_some_above_preflight:"
            f"observed={snapshot.pressure_some_avg10}:"
            f"maximum={policy.preflight_some_avg10_max}"
        )
    if snapshot.pressure_full_avg10 > policy.preflight_full_avg10_max:
        raise CodeValidationResourceError(
            "memory_pressure_full_above_preflight:"
            f"observed={snapshot.pressure_full_avg10}:"
            f"maximum={policy.preflight_full_avg10_max}"
        )


def _runtime_directory() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    runtime = Path(configured) if configured else Path("/run/user") / str(os.getuid())
    if not runtime.is_dir() or runtime.is_symlink():
        raise CodeValidationResourceError("linux_user_runtime_directory_unavailable")
    return runtime


@contextmanager
def _exclusive_validation_lock() -> Iterator[IO[bytes]]:
    lock_path = _runtime_directory() / "neocortex-code-validate.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        raise CodeValidationResourceError(
            f"code_validation_lock_unavailable:{type(exc).__name__}"
        ) from exc
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodeValidationResourceError("code_validation_already_running") from exc
        yield stream
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _encode_admission(admission: CodeValidationResourceAdmission) -> str:
    raw = json.dumps(
        admission.as_payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _payload_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"resource payload {key} must be an integer")
    return value


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"resource payload {key} must be numeric")
    return float(value)


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"resource payload {key} must be text")
    return value


def _parse_snapshot(payload: object) -> LinuxResourceSnapshot:
    if not isinstance(payload, Mapping):
        raise ValueError("resource snapshot payload must be an object")
    return LinuxResourceSnapshot(
        _payload_integer(payload, "total_memory_bytes"),
        _payload_integer(payload, "available_memory_bytes"),
        _payload_integer(payload, "total_swap_bytes"),
        _payload_integer(payload, "free_swap_bytes"),
        _payload_float(payload, "pressure_some_avg10"),
        _payload_float(payload, "pressure_full_avg10"),
    )


def _parse_policy(payload: object) -> CodeValidationResourcePolicy:
    if not isinstance(payload, Mapping):
        raise ValueError("resource policy payload must be an object")
    return CodeValidationResourcePolicy(
        _payload_text(payload, "policy_id"),
        _payload_integer(payload, "memory_high_bytes"),
        _payload_integer(payload, "memory_max_bytes"),
        _payload_integer(payload, "memory_swap_max_bytes"),
        _payload_integer(payload, "desktop_reserve_bytes"),
        _payload_integer(payload, "cpu_quota_percent"),
        _payload_integer(payload, "tasks_max"),
        _payload_integer(payload, "overall_runtime_seconds"),
        _payload_float(payload, "preflight_some_avg10_max"),
        _payload_float(payload, "preflight_full_avg10_max"),
        _payload_float(payload, "abort_some_avg10_max"),
        _payload_float(payload, "abort_full_avg10_max"),
    )


def parse_code_validation_resource_admission(
    payload: Mapping[str, object],
) -> CodeValidationResourceAdmission:
    """Validate the bounded admission received by the cgroup worker."""

    return CodeValidationResourceAdmission(
        _payload_text(payload, "schema"),
        _parse_policy(payload.get("policy")),
        _parse_snapshot(payload.get("before")),
        _payload_integer(payload, "required_available_memory_bytes"),
        _payload_text(payload, "cgroup_unit"),
        cast(
            Literal["systemd-user-service-cgroup-v2"],
            _payload_text(payload, "containment"),
        ),
        cast(
            Literal["private-network-namespace-no-external-egress"],
            _payload_text(payload, "network_policy"),
        ),
        cast(
            Literal["proc-self-cgroup-v2-exact-systemd-unit"],
            _payload_text(payload, "membership_contract"),
        ),
        cast(
            Literal["systemd-unit-private-network-yes"],
            _payload_text(payload, "private_network_contract"),
        ),
    )


def _bounded_kernel_text(path: Path, *, label: str) -> str:
    try:
        with path.open("rb", buffering=0) as stream:
            raw = stream.read(_CGROUP_TEXT_MAX_BYTES + 1)
    except OSError as exc:
        raise CodeValidationResourceError(f"{label}_unavailable") from exc
    if not raw or len(raw) > _CGROUP_TEXT_MAX_BYTES or b"\x00" in raw:
        raise CodeValidationResourceError(f"{label}_invalid")
    try:
        return raw.decode("ascii")
    except UnicodeError as exc:
        raise CodeValidationResourceError(f"{label}_invalid") from exc


def _verified_cgroup_path(
    admission: CodeValidationResourceAdmission,
    *,
    cgroup_path: Path | None = None,
) -> str:
    """Resolve one exact cgroup-v2 membership for the transient service."""

    entries: list[str] = []
    selected = _SELF_CGROUP if cgroup_path is None else Path(cgroup_path)
    for line in _bounded_kernel_text(
        selected,
        label="code_validation_cgroup_membership",
    ).splitlines():
        hierarchy, separator, remainder = line.partition(":")
        controllers, separator2, raw_path = remainder.partition(":")
        if not separator or not separator2:
            raise CodeValidationResourceError("code_validation_cgroup_membership_invalid")
        if hierarchy == "0" and controllers == "":
            entries.append(raw_path)
    if len(entries) != 1:
        raise CodeValidationResourceError("code_validation_cgroup_membership_invalid")
    raw_path = entries[0]
    path = PurePosixPath(raw_path)
    if (
        not raw_path.startswith("/")
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or len(raw_path) > 4_096
    ):
        raise CodeValidationResourceError("code_validation_cgroup_membership_invalid")
    expected_component = f"{admission.cgroup_unit}.service"
    if path.name != expected_component:
        raise CodeValidationResourceError(
            "code_validation_admission_cgroup_mismatch:"
            f"expected={expected_component}:observed={path.name or '/'}"
        )
    return raw_path


def _verify_private_network_boundary(admission: CodeValidationResourceAdmission) -> None:
    """Resolve the live transient-unit property from systemd, not the environment."""

    try:
        completed = subprocess.run(
            (
                str(_SYSTEMCTL),
                "--user",
                "show",
                f"{admission.cgroup_unit}.service",
                "--property=PrivateNetwork",
                "--value",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_SYSTEMCTL_PROPERTY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeValidationResourceError(
            "code_validation_private_network_property_unavailable"
        ) from exc
    if completed.returncode != 0:
        raise CodeValidationResourceError("code_validation_private_network_property_unavailable")
    if completed.stdout.strip() != "yes":
        raise CodeValidationResourceError("code_validation_private_network_not_active")


def current_code_validation_resource_admission() -> CodeValidationResourceAdmission | None:
    """Return one environment-and-kernel verified worker boundary."""

    if os.environ.get(_BOUNDARY_ENV) != CODE_VALIDATION_RESOURCE_POLICY:
        return None
    encoded = os.environ.get(_ADMISSION_ENV)
    if not encoded:
        raise CodeValidationResourceError("code_validation_admission_receipt_missing")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodeValidationResourceError("code_validation_admission_receipt_invalid") from exc
    if not isinstance(payload, Mapping):
        raise CodeValidationResourceError("code_validation_admission_receipt_invalid")
    try:
        admission = parse_code_validation_resource_admission(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise CodeValidationResourceError("code_validation_admission_receipt_invalid") from exc
    _verified_cgroup_path(admission)
    _verify_private_network_boundary(admission)
    return admission


def inside_code_validation_resource_boundary() -> bool:
    return current_code_validation_resource_admission() is not None


def _environment_arguments(admission: CodeValidationResourceAdmission) -> tuple[str, ...]:
    values = {
        key: value for key, value in os.environ.items() if key in _SAFE_ENVIRONMENT_KEYS and value
    }
    values[_BOUNDARY_ENV] = CODE_VALIDATION_RESOURCE_POLICY
    values[_ADMISSION_ENV] = _encode_admission(admission)
    # Canonical validation is deliberately local-only.  A previously published
    # fresh audit may be resolved against the exact current inventory by the
    # validation verdict; the worker never initiates network egress itself.
    values[_PIP_AUDIT_NETWORK_POLICY_ENV] = "disabled-by-code-validation"
    return tuple(
        value for key, item in sorted(values.items()) for value in ("--setenv", f"{key}={item}")
    )


def _systemd_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    admission: CodeValidationResourceAdmission,
    json_output: bool,
) -> tuple[str, ...]:
    policy = admission.policy
    arguments = [
        str(_SYSTEMD_RUN),
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--service-type=exec",
        "--expand-environment=no",
        f"--unit={admission.cgroup_unit}",
        f"--working-directory={cwd}",
        "--property=MemoryAccounting=yes",
        f"--property=MemoryHigh={policy.memory_high_bytes}",
        f"--property=MemoryMax={policy.memory_max_bytes}",
        f"--property=MemorySwapMax={policy.memory_swap_max_bytes}",
        f"--property=CPUQuota={policy.cpu_quota_percent}%",
        f"--property=TasksMax={policy.tasks_max}",
        "--property=Nice=10",
        "--property=OOMPolicy=stop",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        # All descendants receive a private namespace without an external
        # route, so provider metadata cannot leave the machine.
        "--property=PrivateNetwork=yes",
        "--property=TimeoutStopSec=10s",
        f"--property=RuntimeMaxSec={policy.overall_runtime_seconds}s",
        *_environment_arguments(admission),
    ]
    if json_output:
        arguments.append("--quiet")
    arguments.extend(("--", *(os.fspath(item) for item in command)))
    return tuple(arguments)


@dataclass(slots=True)
class _WatchdogState:
    min_available_memory_bytes: int
    min_free_swap_bytes: int
    max_pressure_some_avg10: float
    max_pressure_full_avg10: float
    abort_reason: str | None = None


def _stop_unit(unit: str) -> None:
    try:
        subprocess.run(
            (str(_SYSTEMCTL), "--user", "stop", f"{unit}.service"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_STOP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _watch_resources(
    admission: CodeValidationResourceAdmission,
    stopped: threading.Event,
    state: _WatchdogState,
) -> None:
    policy = admission.policy
    while not stopped.wait(_MONITOR_INTERVAL_SECONDS):
        try:
            snapshot = read_linux_resource_snapshot()
        except CodeValidationResourceError:
            state.abort_reason = "resource_watchdog_snapshot_unavailable"
            _stop_unit(admission.cgroup_unit)
            return
        state.min_available_memory_bytes = min(
            state.min_available_memory_bytes,
            snapshot.available_memory_bytes,
        )
        state.min_free_swap_bytes = min(state.min_free_swap_bytes, snapshot.free_swap_bytes)
        state.max_pressure_some_avg10 = max(
            state.max_pressure_some_avg10,
            snapshot.pressure_some_avg10,
        )
        state.max_pressure_full_avg10 = max(
            state.max_pressure_full_avg10,
            snapshot.pressure_full_avg10,
        )
        reason = None
        if snapshot.available_memory_bytes < policy.desktop_reserve_bytes:
            reason = "desktop_memory_reserve_breached"
        elif snapshot.pressure_full_avg10 > policy.abort_full_avg10_max:
            reason = "memory_pressure_full_abort_threshold"
        elif snapshot.pressure_some_avg10 > policy.abort_some_avg10_max:
            reason = "memory_pressure_some_abort_threshold"
        if reason is not None:
            state.abort_reason = reason
            _stop_unit(admission.cgroup_unit)
            return


def _resource_summary(
    admission: CodeValidationResourceAdmission,
    state: _WatchdogState,
) -> str:
    return (
        "CODE_CHANGE_VALIDATION_RESOURCES "
        f"policy={admission.policy.policy_id} "
        f"memory_max={admission.policy.memory_max_bytes} "
        f"desktop_reserve={admission.policy.desktop_reserve_bytes} "
        f"min_available={state.min_available_memory_bytes} "
        f"max_psi_some_avg10={state.max_pressure_some_avg10:.2f} "
        f"max_psi_full_avg10={state.max_pressure_full_avg10:.2f}"
    )


def run_code_validation_in_resource_boundary(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    json_output: bool,
) -> int:
    """Run the complete validation tree in one adaptive cgroup and return its exit code."""

    if inside_code_validation_resource_boundary():
        raise CodeValidationResourceError("nested_code_validation_resource_boundary")
    if not _SYSTEMD_RUN.is_file() or not os.access(_SYSTEMD_RUN, os.X_OK):
        raise CodeValidationResourceError("systemd_run_unavailable")
    if not _SYSTEMCTL.is_file() or not os.access(_SYSTEMCTL, os.X_OK):
        raise CodeValidationResourceError("systemctl_unavailable")
    source = Path(cwd).resolve(strict=True)
    before = read_linux_resource_snapshot()
    policy = code_validation_resource_policy(before)
    _preflight(before, policy)
    unit = _unit_name()
    admission = CodeValidationResourceAdmission(
        CODE_VALIDATION_RESOURCE_SCHEMA,
        policy,
        before,
        policy.desktop_reserve_bytes + policy.memory_max_bytes,
        unit,
        "systemd-user-service-cgroup-v2",
        "private-network-namespace-no-external-egress",
        "proc-self-cgroup-v2-exact-systemd-unit",
        "systemd-unit-private-network-yes",
    )
    coordinator = GlobalResourceCoordinator(
        ("code-validation",),
        GlobalResourceLimits(
            memory_budget_bytes=policy.memory_max_bytes,
            min_free_memory_bytes=policy.desktop_reserve_bytes,
            min_free_commit_bytes=0,
            cpu_slots=1,
            max_cpu_load_percent=95.0,
            wait_timeout_seconds=0.0,
            poll_interval_seconds=0.1,
        ),
    )
    systemd_command = _systemd_command(
        command,
        cwd=source,
        admission=admission,
        json_output=json_output,
    )
    state = _WatchdogState(
        before.available_memory_bytes,
        before.free_swap_bytes,
        before.pressure_some_avg10,
        before.pressure_full_avg10,
    )
    stopped = threading.Event()
    watchdog = threading.Thread(
        target=_watch_resources,
        args=(admission, stopped, state),
        name="neocortex-code-validation-resource-watchdog",
        daemon=True,
    )
    try:
        with (
            _exclusive_validation_lock(),
            coordinator.admit("code-validation", policy.memory_max_bytes, 1),
        ):
            watchdog.start()
            try:
                completed = subprocess.run(
                    systemd_command,
                    cwd=source,
                    stdin=subprocess.DEVNULL,
                    timeout=policy.overall_runtime_seconds + 30,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                _stop_unit(unit)
                raise CodeValidationResourceError("code_validation_overall_timeout") from exc
            finally:
                stopped.set()
                watchdog.join(timeout=2.0)
    except (MemoryBudgetExceeded, MemoryHeadroomTimeout) as exc:
        raise CodeValidationResourceError(
            f"code_validation_resource_admission_failed:{type(exc).__name__}:{exc}"
        ) from exc
    if not json_output:
        print(_resource_summary(admission, state), file=sys.stderr, flush=True)
    if state.abort_reason is not None:
        raise CodeValidationResourceError(
            f"code_validation_resource_watchdog_aborted:{state.abort_reason}:"
            f"min_available={state.min_available_memory_bytes}"
        )
    return int(completed.returncode)


__all__ = [
    "CODE_VALIDATION_RESOURCE_POLICY",
    "CODE_VALIDATION_RESOURCE_SCHEMA",
    "CodeValidationResourceAdmission",
    "CodeValidationResourceError",
    "CodeValidationResourcePolicy",
    "LinuxResourceSnapshot",
    "code_validation_resource_policy",
    "current_code_validation_resource_admission",
    "inside_code_validation_resource_boundary",
    "parse_code_validation_resource_admission",
    "read_linux_resource_snapshot",
    "run_code_validation_in_resource_boundary",
]
