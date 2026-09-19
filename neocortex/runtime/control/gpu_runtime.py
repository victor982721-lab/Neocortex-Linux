"""Bounded, cached NVIDIA memory observations with conservative CUDA mapping."""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture


@dataclass(frozen=True, slots=True)
class GpuMemorySnapshot:
    device_id: str
    total_bytes: int
    available_bytes: int
    cuda_index: int
    process_memory_bytes: Mapping[tuple[int, int], int] = field(default_factory=dict)


_lock = threading.Lock()
_cached_at: float | None = None
_cached: tuple[GpuMemorySnapshot, ...] = ()
_cached_visibility: tuple[str | None, str | None] | None = None
_registered_processes: set[tuple[int, int]] = set()


def register_gpu_process(pid: int, start_time_ticks: int) -> None:
    """Observe memory only for a backend PID already verified by its owner."""
    with _lock:
        _registered_processes.add((pid, start_time_ticks))


def _process_start(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return int(stat[stat.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _process_allocations(executable: str) -> dict[str, dict[tuple[int, int], int]]:
    identities = {pid: start for pid, start in _registered_processes
                  if _process_start(pid) == start}
    _registered_processes.intersection_update(identities.items())
    if not identities:
        return {}
    try:
        result = run_bounded_capture(
            (executable, "--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits"),
            timeout_seconds=2.0, stdout_limit_bytes=65536, stderr_limit_bytes=8192,
        )
        if result.returncode:
            return {}
        values: dict[str, dict[tuple[int, int], int]] = {}
        for row in csv.reader(io.StringIO(result.stdout.decode("ascii"))):
            if len(row) != 3:
                return {}
            pid_raw, uuid, memory = (value.strip() for value in row)
            pid = int(pid_raw)
            if pid not in identities or _process_start(pid) != identities[pid]:
                continue
            amount = int(memory) * 1024**2
            if amount < 0 or not uuid.startswith("GPU-"):
                return {}
            identity = pid, identities[pid]
            device = values.setdefault(uuid, {})
            if identity in device:
                return {}  # Ambiguous rows must never create duplicate credit.
            device[identity] = amount
        return values
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, SubprocessOutputLimitError):
        return {}


def _read_devices() -> tuple[GpuMemorySnapshot, ...]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return ()
    # Observe allocation identities before device free memory: never combine
    # free VRAM from before our model loaded with credit sampled after loading.
    allocations = _process_allocations(executable)
    try:
        result = run_bounded_capture(
            (executable, "--query-gpu=uuid,pci.bus_id,memory.total,memory.free,mig.mode.current",
             "--format=csv,noheader,nounits"),
            timeout_seconds=2.0, stdout_limit_bytes=65536, stderr_limit_bytes=8192,
        )
        if result.returncode:
            return ()
        devices: list[tuple[str, str, int, int]] = []
        for row in csv.reader(io.StringIO(result.stdout.decode("ascii"))):
            if len(row) != 5:
                return ()
            uuid, bus, total, free, mig = (value.strip() for value in row)
            if mig.casefold() not in {"disabled", "n/a", "[n/a]"}:
                return ()
            total_bytes, available = int(total) * 1024**2, int(free) * 1024**2
            if not uuid.startswith("GPU-") or not 0 <= available <= total_bytes or not total_bytes:
                return ()
            devices.append((uuid, bus, total_bytes, available))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, SubprocessOutputLimitError):
        return ()
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    pci_order = os.environ.get("CUDA_DEVICE_ORDER") == "PCI_BUS_ID"
    selected = []
    if visible is not None:
        for value in visible.split(","):
            value = value.strip()
            if not value or value == "-1":
                return ()
            if value.startswith("GPU-"):
                matches = [item for item in devices if item[0].startswith(value)]
                if len(matches) != 1:
                    return ()
                selected.append(matches[0])
            elif value.isdecimal() and (pci_order or len(devices) == 1):
                ordered = sorted(devices, key=lambda item: item[1])
                index = int(value)
                if index >= len(ordered):
                    return ()
                selected.append(ordered[index])
            else:
                return ()  # MIG and ambiguous physical/CUDA ordinals stay unknown.
    elif len(devices) == 1 or pci_order:
        selected = sorted(devices, key=lambda item: item[1])
    else:
        return ()
    if len({item[0] for item in selected}) != len(selected):
        return ()
    return tuple(GpuMemorySnapshot(uuid, total, free, index, allocations.get(uuid, {}))
                 for index, (uuid, _bus, total, free) in enumerate(selected))


def cuda_memory_snapshot(device_index: int = 0) -> GpuMemorySnapshot | None:
    """Return known memory for a visible CUDA ordinal, never guessed free VRAM."""
    global _cached, _cached_at, _cached_visibility
    visibility = os.environ.get("CUDA_VISIBLE_DEVICES"), os.environ.get("CUDA_DEVICE_ORDER")
    with _lock:
        now = time.monotonic()
        if _cached_at is None or now - _cached_at >= 1.0 or visibility != _cached_visibility:
            _cached = _read_devices()
            _cached_at = time.monotonic()
            _cached_visibility = visibility
        return next((item for item in _cached if item.cuda_index == device_index), None)
