"""Cached, read-only Linux observations of the host and our process tree.

CPU accounting distinguishes our work from competing work, including siblings
sharing a cgroup quota. Traversal follows each owned task's children; it never
scans the host process table. Unknown or raced observations grant no resource
credit. Private resident pages, rather than summed RSS, avoid counting shared
model or fork pages more than once.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .cgroup_runtime import (
    CgroupCpuSnapshot,
    cgroup_cpu_snapshot,
    cgroup_memory_snapshot,
    cgroup_v2_directories,
)
from .cpu_runtime import CpuTimes, cpu_capacity_snapshot
from .memory_runtime import MemorySnapshot, memory_snapshot


ProcessIdentity = tuple[int, int]


@dataclass(frozen=True, slots=True)
class OwnedResourceSnapshot:
    sampled_at: float
    effective_cpu_capacity: float
    memory_snapshot: MemorySnapshot
    host_cpu_percent: float | None = None
    own_cpu_cores: float | None = None
    external_cpu_cores: float | None = None
    owned_materialized_bytes: int | None = None
    owned_memory_complete: bool = False
    cpu_observation_complete: bool = False
    process_memory_bytes: Mapping[ProcessIdentity, int] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    process_identities: tuple[ProcessIdentity, ...] = ()
    owned_pids: tuple[int, ...] = ()
    observed_thread_count: int | None = None
    memory_pressure_some_percent: float | None = None
    memory_pressure_full_percent: float | None = None
    memory_pressure_some_total_us: int | None = None
    memory_pressure_full_total_us: int | None = None
    io_pressure_some_percent: float | None = None
    io_pressure_full_percent: float | None = None
    io_pressure_some_total_us: int | None = None
    io_pressure_full_total_us: int | None = None
    cpu_pressure_some_percent: float | None = None
    cpu_pressure_full_percent: float | None = None
    observation_duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _ProcessCounters:
    pid: int
    start_ticks: int
    parent_pid: int
    cpu_ticks: int
    reaped_ticks: int
    threads: int
    state: str

    @property
    def identity(self) -> ProcessIdentity:
        return self.pid, self.start_ticks


@dataclass(frozen=True, slots=True)
class _CpuObservation:
    at: float
    uptime_ticks: int | None
    host: Mapping[int, CpuTimes]
    allowed: frozenset[int]
    processes: Mapping[ProcessIdentity, _ProcessCounters]
    quotas: Mapping[Path, tuple[float, int | None]]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def _process_counters(path: Path, pid: int) -> _ProcessCounters | None:
    raw = _read_text(path / "stat")
    if raw is None:
        return None
    # comm may contain spaces and ')'; its last ')' terminates the field.
    before, separator, rest = raw.rpartition(")")
    fields = rest.split()
    try:
        if not separator or int(before.partition(" ")[0]) != pid:
            return None
        values = [int(fields[index]) for index in (1, 11, 12, 13, 14, 17, 19)]
        if min(values) < 0 or values[5] < 1:
            return None
        return _ProcessCounters(
            pid, values[6], values[0], values[1] + values[2],
            values[3] + values[4], values[5], fields[0],
        )
    except (IndexError, ValueError):
        return None


def _membership(raw: str | None) -> PurePosixPath | None:
    for line in () if raw is None else raw.splitlines():
        if line.startswith("0::"):
            value = PurePosixPath(line[3:])
            if value.is_absolute() and ".." not in value.parts:
                return value
    return None


def _private_resident_bytes(path: Path, state: str) -> int | None:
    if state == "Z":
        return 0
    raw = _read_text(path / "smaps_rollup")
    if raw is None:
        return None
    private: dict[str, int] = {}
    for line in raw.splitlines():
        name, separator, rest = line.partition(":")
        if not separator or name not in {"Private_Clean", "Private_Dirty", "Private_Hugetlb"}:
            continue
        parts = rest.split()
        try:
            if len(parts) != 2 or parts[1] != "kB" or name in private:
                return None
            value = int(parts[0])
        except ValueError:
            return None
        if value < 0:
            return None
        private[name] = value * 1024
    if not {"Private_Clean", "Private_Dirty"}.issubset(private):
        return None
    return sum(private.values())


def _host_cpu_counters(path: Path) -> dict[int, CpuTimes]:
    result: dict[int, CpuTimes] = {}
    raw = _read_text(path)
    for line in () if raw is None else raw.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        try:
            key = -1 if fields[0] == "cpu" else int(fields[0][3:])
            values = [int(value) for value in fields[1:9]]
            if len(values) < 4 or min(values) < 0:
                continue
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            result[key] = CpuTimes(idle, sum(values))
        except ValueError:
            continue
    return result


def _busy_fraction(current: CpuTimes | None, previous: CpuTimes | None) -> float | None:
    if current is None or previous is None:
        return None
    total = current.total - previous.total
    idle = current.idle - previous.idle
    if total <= 0 or idle < 0 or idle > total:
        return None
    return (total - idle) / total


def _pressure(paths: tuple[Path, ...]) -> dict[str, int | float]:
    """Keep the largest visible host/cgroup PSI observation for each field."""

    result: dict[str, int | float] = {}
    for path in paths:
        raw = _read_text(path)
        for line in () if raw is None else raw.splitlines():
            parts = line.split()
            if not parts or parts[0] not in {"some", "full"}:
                continue
            for item in parts[1:]:
                key, _, value = item.partition("=")
                try:
                    if key == "avg10":
                        parsed = float(value)
                        if not math.isfinite(parsed) or not 0 <= parsed <= 100:
                            continue
                        name = parts[0] + "_percent"
                    elif key == "total":
                        parsed = int(value)
                        if parsed < 0:
                            continue
                        name = parts[0] + "_total_us"
                    else:
                        continue
                except ValueError:
                    continue
                result[name] = max(result.get(name, 0), parsed)
    return result


class OwnedResourceSampler:
    """Share one coherent advisory snapshot per interval among all callers.

    ``external_cpu_cores`` is the part of effective capacity occupied by other
    work, not all externally busy host cores. For a two-CPU quota on a mostly
    idle large host, unrelated work on spare CPUs therefore costs no capacity.
    A finite shared ancestor quota additionally subtracts its sibling usage.

    No background thread or controller mutation is introduced. The coordinator
    calls sample at its periodic checkpoints; repeated calls reuse the same
    immutable object. Previously observed children remain tracked by PID/start
    time if reparented. Unobserved daemon escape requires a dedicated owned
    cgroup for complete lifecycle containment and is never claimed here.
    """

    def __init__(
        self,
        sample_interval_seconds: float = 0.25,
        *,
        proc_root: Path = Path("/proc"),
        pid: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_processes: int = 4096,
        max_threads: int = 16384,
    ) -> None:
        if not math.isfinite(sample_interval_seconds) or sample_interval_seconds < 0:
            raise ValueError("sample interval must be finite and nonnegative")
        if max_processes < 1 or max_threads < 1:
            raise ValueError("process and thread observation bounds must be positive")
        self._interval = sample_interval_seconds
        self._proc = proc_root
        self._pid = os.getpid() if pid is None else pid
        self._clock = clock
        self._max_processes = max_processes
        self._max_threads = max_threads
        self._lock = threading.Lock()
        self._cached: OwnedResourceSnapshot | None = None
        self._finished_at: float | None = None
        self._previous: _CpuObservation | None = None
        self._known: dict[int, int] = {}
        try:
            self._ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, TypeError, ValueError):
            self._ticks_per_second = 0

    def sample(self) -> OwnedResourceSnapshot:
        with self._lock:
            now = self._clock()
            if (
                self._cached is not None and self._finished_at is not None
                and 0 <= now - self._finished_at < self._interval
            ):
                return self._cached
            self._cached = self._sample(now)
            self._finished_at = self._clock()
            return self._cached

    def current_sample(self) -> OwnedResourceSnapshot | None:
        """Return the published immutable snapshot without waiting for a probe.

        The periodic owner replaces this reference only after a complete
        observation. Admission can keep its accounting lock short while the
        next snapshot is being assembled; it never sees mutable counters.
        """
        return self._cached

    def _track_verified_process(self, identity: ProcessIdentity) -> None:
        """Observe a child whose live lease has verified its ancestry.

        Some proc namespaces omit task child lists. An explicitly verified
        identity remains a valid observation seed there; each sample still
        checks its start time, cgroup membership and private resident pages.
        This never makes incomplete tree or CPU attribution complete.
        """
        pid, started = identity
        with self._lock:
            if self._known.get(pid) == started:
                return
            if pid not in self._known and len(self._known) >= self._max_processes:
                return
            self._known[pid] = started
            # Do not wait a full cache interval to observe a newly bound child.
            # Readers retain the last immutable sample until that observation.
            self._finished_at = None

    def _tree(
        self, allowed: frozenset[int], membership: PurePosixPath | None,
        root_membership_text: str | None,
    ) -> tuple[dict[ProcessIdentity, _ProcessCounters], dict[ProcessIdentity, int], bool, bool]:
        processes: dict[ProcessIdentity, _ProcessCounters] = {}
        private: dict[ProcessIdentity, int] = {}
        queue: list[tuple[int, int | None, int | None]] = [
            (self._pid, self._known.get(self._pid), None),
        ]
        queue.extend((pid, start, None) for pid, start in self._known.items() if pid != self._pid)
        seen: set[int] = set()
        complete = True
        affinity_complete = bool(allowed)
        thread_count = 0
        affinity_probe = getattr(os, "sched_getaffinity", None)
        while queue:
            pid, expected_start, expected_parent = queue.pop()
            if pid in seen:
                continue
            if len(seen) >= self._max_processes:
                complete = False
                break
            seen.add(pid)
            path = self._proc / str(pid)
            current = _process_counters(path, pid)
            if current is None:
                # A known process may have exited; an extant unreadable one
                # means that attribution is incomplete, never zero usage.
                if pid == self._pid or path.exists():
                    complete = False
                continue
            if expected_start is not None and current.start_ticks != expected_start:
                seen.remove(pid)
                complete = False
                continue  # PID reuse does not inherit ownership.
            if expected_parent is not None and current.parent_pid != expected_parent:
                complete = False
                continue
            memory_in_scope = True
            if membership is not None:
                raw = root_membership_text if pid == self._pid else _read_text(path / "cgroup")
                own_membership = _membership(raw)
                if own_membership is None or not own_membership.is_relative_to(membership):
                    affinity_complete = False
                    memory_in_scope = False
            processes[current.identity] = current
            amount = _private_resident_bytes(path, current.state) if memory_in_scope else None
            if amount is not None:
                private[current.identity] = amount
            try:
                tasks = tuple(islice((path / "task").iterdir(), self._max_threads + 1))
            except OSError:
                complete = False
                continue
            for task in tasks:
                if not task.name.isdecimal():
                    continue
                thread_count += 1
                if thread_count > self._max_threads:
                    complete = False
                    break
                if affinity_probe is None:
                    affinity_complete = False
                else:
                    try:
                        if not set(affinity_probe(int(task.name))).issubset(allowed):
                            affinity_complete = False
                    except (OSError, ValueError):
                        affinity_complete = False
                raw = _read_text(task / "children")
                if raw is None:
                    if task.exists():
                        complete = False
                    continue
                try:
                    children = [int(item) for item in raw.split()]
                except ValueError:
                    complete = False
                    continue
                queue.extend((child, None, pid) for child in children if child > 0)
            if thread_count > self._max_threads:
                break
        # A child reaped during the observation can move counters into its
        # parent. Do not grant duplicate CPU or memory credit across that race.
        for identity, before in tuple(processes.items()):
            after = _process_counters(self._proc / str(before.pid), before.pid)
            if after is None or after.identity != identity:
                complete = False
                private.pop(identity, None)
            elif after.reaped_ticks != before.reaped_ticks:
                complete = False
        self._known = dict(processes.keys())
        return processes, private, complete, affinity_complete

    def _cpu(
        self, current: _CpuObservation, *, complete: bool, capacity: float,
    ) -> tuple[float | None, float | None, float | None]:
        previous = self._previous
        self._previous = current if complete else None
        if previous is None or not complete or current.at <= previous.at:
            return None, None, None
        host = _busy_fraction(current.host.get(-1), previous.host.get(-1))
        host_percent = None if host is None else 100.0 * host
        if current.allowed != previous.allowed or self._ticks_per_second <= 0:
            return host_percent, None, None
        elapsed = current.at - previous.at
        own_delta = 0
        waited_delta = 0
        for identity, process in current.processes.items():
            old = previous.processes.get(identity)
            if old is not None:
                direct = process.cpu_ticks - old.cpu_ticks
                waited = process.reaped_ticks - old.reaped_ticks
                if direct < 0 or waited < 0:
                    return host_percent, None, None
                own_delta += direct
                waited_delta += waited
            elif previous.uptime_ticks is not None and process.start_ticks >= previous.uptime_ticks:
                own_delta += process.cpu_ticks
                waited_delta += process.reaped_ticks
        removed = sum(
            item.cpu_ticks + item.reaped_ticks
            for identity, item in previous.processes.items() if identity not in current.processes
        )
        # Parent c* counters contain reaped child lifetimes. Previously observed
        # child CPU was already charged; only its new tail belongs to this delta.
        own_delta += max(0, waited_delta - removed)
        own = own_delta / self._ticks_per_second / elapsed
        fractions = [
            _busy_fraction(current.host.get(cpu), previous.host.get(cpu))
            for cpu in current.allowed
        ]
        if not fractions or any(item is None for item in fractions):
            return host_percent, own, None
        busy = sum(item for item in fractions if item is not None)
        available = min(capacity, max(0.0, len(current.allowed) - max(0.0, busy - own)))
        for directory, (quota, usage) in current.quotas.items():
            old_quota = previous.quotas.get(directory)
            if usage is None or old_quota is None or old_quota[1] is None or usage < old_quota[1]:
                return host_percent, own, None
            group_cores = (usage - old_quota[1]) / 1_000_000 / elapsed
            available = min(available, max(0.0, quota - max(0.0, group_cores - own)))
        return host_percent, own, max(0.0, capacity - available)

    def _sample(self, now: float) -> OwnedResourceSnapshot:
        process_path = self._proc / str(self._pid)
        membership_text = _read_text(process_path / "cgroup")
        directories = cgroup_v2_directories(
            process_path / "cgroup", process_path / "mountinfo", membership_text=membership_text,
        ) if membership_text is not None else ()
        cgroup_cpu: CgroupCpuSnapshot = cgroup_cpu_snapshot(directories, include_usage=True)
        capacity = cpu_capacity_snapshot(cgroup_snapshot=cgroup_cpu)
        physical = memory_snapshot(
            cgroup_snapshot=cgroup_memory_snapshot(directories),
            meminfo_path=self._proc / "meminfo",
        )
        host = _host_cpu_counters(self._proc / "stat")
        allowed = frozenset(cpu for cpu in host if cpu >= 0)
        if capacity.affinity_cpus is not None:
            allowed &= capacity.affinity_cpus
        if cgroup_cpu.cpuset_ranges is not None:
            allowed = frozenset(
                cpu for cpu in allowed
                if any(first <= cpu <= last for first, last in cgroup_cpu.cpuset_ranges)
            )
        processes, private, complete, affinity_complete = self._tree(
            allowed, _membership(membership_text), membership_text,
        )
        uptime_ticks = None
        raw_uptime = _read_text(self._proc / "uptime")
        try:
            if raw_uptime is not None:
                uptime = float(raw_uptime.split()[0])
                if math.isfinite(uptime) and uptime >= 0:
                    uptime_ticks = math.floor(uptime * self._ticks_per_second)
        except (IndexError, ValueError):
            pass
        cpu_complete = complete and affinity_complete
        host_percent, own, external = self._cpu(
            _CpuObservation(now, uptime_ticks, host, allowed, processes, {
                item.directory: (float(item.quota_cpus), item.usage_usec)
                for item in cgroup_cpu.quota_observations
            }), complete=cpu_complete, capacity=capacity.effective_cpus,
        )
        pressure: dict[str, int | float] = {}
        for kind in ("memory", "io", "cpu"):
            values = _pressure((self._proc / "pressure" / kind, *(
                directory / f"{kind}.pressure" for directory in directories
            )))
            pressure.update({
                f"{kind}_pressure_{name}": value for name, value in values.items()
                if kind != "cpu" or name.endswith("percent")
            })
        memory_complete = complete and len(private) == len(processes) and bool(processes)

        def pressure_percent(kind: str, name: str) -> float | None:
            value = pressure.get(f"{kind}_pressure_{name}_percent")
            return None if value is None else float(value)

        def pressure_total(kind: str, name: str) -> int | None:
            value = pressure.get(f"{kind}_pressure_{name}_total_us")
            return None if value is None else int(value)

        return OwnedResourceSnapshot(
            sampled_at=now,
            effective_cpu_capacity=capacity.effective_cpus,
            memory_snapshot=physical,
            host_cpu_percent=host_percent,
            own_cpu_cores=own,
            external_cpu_cores=external,
            owned_materialized_bytes=sum(private.values()) if memory_complete else None,
            owned_memory_complete=memory_complete,
            cpu_observation_complete=cpu_complete and external is not None,
            process_memory_bytes=MappingProxyType(private),
            process_identities=tuple(sorted(processes)),
            owned_pids=tuple(sorted(pid for pid, _ in processes)),
            observed_thread_count=sum(item.threads for item in processes.values()) if complete else None,
            observation_duration_seconds=max(0.0, self._clock() - now),
            memory_pressure_some_percent=pressure_percent("memory", "some"),
            memory_pressure_full_percent=pressure_percent("memory", "full"),
            memory_pressure_some_total_us=pressure_total("memory", "some"),
            memory_pressure_full_total_us=pressure_total("memory", "full"),
            io_pressure_some_percent=pressure_percent("io", "some"),
            io_pressure_full_percent=pressure_percent("io", "full"),
            io_pressure_some_total_us=pressure_total("io", "some"),
            io_pressure_full_total_us=pressure_total("io", "full"),
            cpu_pressure_some_percent=pressure_percent("cpu", "some"),
            cpu_pressure_full_percent=pressure_percent("cpu", "full"),
        )
