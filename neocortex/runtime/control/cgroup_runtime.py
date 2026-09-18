"""Read the current process's visible cgroup-v2 resource constraints.

All reads are advisory snapshots; this module never changes a controller.  The
mount root is a visibility boundary, not necessarily the root of the host's
cgroup hierarchy.  Ancestors hidden by a cgroup/mount namespace cannot be
inspected here and are not reported as unlimited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath


_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_PROC_SELF_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _read_ascii(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _read_proc_paths(path: Path) -> str | None:
    # Unlike the numerical control files, membership and mount names can be
    # non-ASCII and must retain undecodable filesystem bytes losslessly.
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def _unsigned(raw: str | None) -> int | None:
    if raw is None or not raw.isascii() or not raw.isdecimal():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cgroup_v2_directories(
    cgroup_path: Path = _PROC_SELF_CGROUP,
    mountinfo_path: Path = _PROC_SELF_MOUNTINFO,
) -> tuple[Path, ...]:
    """Locate membership through mountinfo and include every visible ancestor.

    Both paths in proc are relative to the caller's namespaces.  Subtree bind
    mounts must subtract their mount root; simply appending the membership to
    ``/sys/fs/cgroup`` can miss the actual limits.  Never walk above a mount or
    guess a mapping when membership is outside that mount's exposed subtree.
    """

    membership_text = _read_proc_paths(cgroup_path)
    mountinfo = _read_proc_paths(mountinfo_path)
    if membership_text is None or mountinfo is None:
        return ()
    membership = None
    for line in membership_text.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[:2] == ["0", ""]:
            membership = PurePosixPath(fields[2])
            break
    if membership is None or not membership.is_absolute():
        return ()

    directories: list[Path] = []
    seen: set[Path] = set()
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if not separator or not after.startswith("cgroup2 ") or len(fields) < 6:
            continue
        mount_root, mount_name = (
            _MOUNT_ESCAPE.sub(lambda match: chr(int(match[1], 8)), value) for value in fields[3:5]
        )
        root = PurePosixPath(mount_root)
        mount = Path(mount_name)
        if not root.is_absolute() or not mount.is_absolute() or ".." in mount.parts:
            continue
        try:
            relative = membership.relative_to(root)
        except ValueError:
            continue
        if ".." in relative.parts:
            continue
        current = mount.joinpath(*relative.parts)
        if not current.is_dir():
            continue
        while True:
            if current not in seen:
                directories.append(current)
                seen.add(current)
            if current == mount:
                break
            current = current.parent
    return tuple(directories)


@dataclass(frozen=True, slots=True)
class CgroupMemorySnapshot:
    limit_bytes: int | None
    available_bytes: int | None
    visible_cgroups: int
    unreadable_current: tuple[Path, ...] = ()
    high_bytes: int | None = None
    raw_available_bytes: int | None = None
    estimated_reclaimable_file_bytes: int = 0


def _inactive_file_reclaim_estimate(directory: Path, current: int) -> int:
    """Estimate clean unmapped inactive file cache, never promise its reclaim.

    memory.current includes page cache, so max-current alone can report no
    useful capacity after a file-heavy run. Linux memory.stat separates file
    types from reclaim lists: cap inactive_file by file-shmem, then subtract
    all observed dirty, writeback, mapped and unevictable bytes. Those sets
    can overlap, making this deliberately conservative. Active file cache,
    tmpfs/shmem, slab and anonymous memory never add estimated headroom.
    Missing, malformed or contradictory accounting retains the raw margin.
    See docs.kernel.org/admin-guide/cgroup-v2.html#memory.
    """

    required = {
        "file", "shmem", "inactive_file", "file_dirty", "file_writeback",
        "file_mapped", "unevictable",
    }
    raw = _read_ascii(directory / "memory.stat")
    if raw is None:
        return 0
    values: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if not parts or parts[0] not in required:
            continue
        if len(parts) != 2 or parts[0] in values:
            return 0
        value = _unsigned(parts[1])
        if value is None:
            return 0
        values[parts[0]] = value
    if set(values) != required:
        return 0
    file_bytes = values["file"]
    if (
        file_bytes > current
        or values["shmem"] > file_bytes
        or values["inactive_file"] > file_bytes - values["shmem"]
        or any(values[name] > file_bytes for name in ("file_dirty", "file_writeback", "file_mapped"))
        or values["unevictable"] > current
    ):
        return 0
    unavailable = sum(values[name] for name in (
        "file_dirty", "file_writeback", "file_mapped", "unevictable",
    ))
    return max(0, values["inactive_file"] - unavailable)


def cgroup_memory_snapshot(
    directories: tuple[Path, ...] | None = None,
) -> CgroupMemorySnapshot:
    """Combine limits and current use at each level, including shared parents.

    Subtract each ancestor's *own* current consumption, not the leaf's use:
    siblings may have consumed most of a parent's remaining allowance.  Once a
    finite limit is known, an unreadable current value cannot mean free memory;
    reserve no new headroom until a later sample can measure it. A complete
    memory.stat may add a conservative estimate of clean inactive file cache
    to the hard-limit margin. memory.high still constrains raw pressure-free
    headroom, and is kept separate from the hard max:
    reaching its throttle/reclaim threshold does not reduce physical capacity.
    """

    visible = cgroup_v2_directories() if directories is None else directories
    limits: list[int] = []
    highs: list[int] = []
    headrooms: list[int] = []
    raw_headrooms: list[int] = []
    unreadable: list[Path] = []
    for directory in visible:
        maximum = _unsigned(_read_ascii(directory / "memory.max"))
        high = _unsigned(_read_ascii(directory / "memory.high"))
        if maximum is not None:
            limits.append(maximum)
        if high is not None:
            highs.append(high)
        bounds = [bound for bound in (maximum, high) if bound is not None]
        if not bounds:
            continue
        current = _unsigned(_read_ascii(directory / "memory.current"))
        if current is None:
            unreadable.append(directory / "memory.current")
            headrooms.append(0)
            raw_headrooms.append(0)
        else:
            reclaimable = (
                0 if maximum is None else _inactive_file_reclaim_estimate(directory, current)
            )
            if reclaimable:
                # Account for growth while the additional counters were read.
                # An unreadable recapture cannot authorize the estimate.
                after = _unsigned(_read_ascii(directory / "memory.current"))
                if after is None:
                    unreadable.append(directory / "memory.current")
                    headrooms.append(0)
                    raw_headrooms.append(0)
                    continue
                current = max(current, after)
            raw_margin = max(0, min(bounds) - current)
            raw_headrooms.append(raw_margin)
            margins = []
            if maximum is not None:
                margins.append(max(0, maximum - current + reclaimable))
            if high is not None:
                margins.append(max(0, high - current))
            headrooms.append(min(margins))
    available = min(headrooms, default=None)
    raw_available = min(raw_headrooms, default=None)
    return CgroupMemorySnapshot(
        min(limits, default=None),
        available,
        len(visible),
        tuple(unreadable),
        min(highs, default=None),
        raw_available,
        0 if available is None or raw_available is None else max(0, available - raw_available),
    )


CpuRanges = tuple[tuple[int, int], ...]


def _cpu_ranges(raw: str | None) -> CpuRanges | None:
    """Parse interval lists without materializing one item per logical CPU."""

    if not raw:
        return None
    ranges: list[tuple[int, int]] = []
    for component in raw.split(","):
        ends = component.split("-")
        if len(ends) not in (1, 2):
            return None
        first, last = _unsigned(ends[0]), _unsigned(ends[-1])
        if first is None or last is None or first > last:
            return None
        ranges.append((first, last))
    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return tuple(merged)


def _intersect_cpu_ranges(left: CpuRanges, right: CpuRanges) -> CpuRanges:
    intersection: list[tuple[int, int]] = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        low = max(left[left_index][0], right[right_index][0])
        high = min(left[left_index][1], right[right_index][1])
        if low <= high:
            intersection.append((low, high))
        if left[left_index][1] < right[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return tuple(intersection)


@dataclass(frozen=True, slots=True)
class CgroupCpuSnapshot:
    quota_cpus: Fraction | None
    cpuset_ranges: CpuRanges | None
    visible_cgroups: int


def cgroup_cpu_snapshot(
    directories: tuple[Path, ...] | None = None,
) -> CgroupCpuSnapshot:
    """Return the strictest visible bandwidth quota and effective cpuset."""

    visible = cgroup_v2_directories() if directories is None else directories
    quotas: list[Fraction] = []
    cpus = None
    for directory in visible:
        quota = _read_ascii(directory / "cpu.max")
        if quota is not None:
            values = quota.split()
            if len(values) == 2:
                maximum, period = (_unsigned(value) for value in values)
                if maximum is not None and maximum > 0 and period is not None and period > 0:
                    quotas.append(Fraction(maximum, period))
        effective = _cpu_ranges(_read_ascii(directory / "cpuset.cpus.effective"))
        if effective is not None:
            cpus = effective if cpus is None else _intersect_cpu_ranges(cpus, effective)
    return CgroupCpuSnapshot(min(quotas, default=None), cpus, len(visible))
