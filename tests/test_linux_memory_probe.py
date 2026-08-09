"""Linux physical-memory probing uses reclaimable headroom, not only free pages."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import memory_runtime, pdf_runtime


def test_linux_meminfo_prefers_memavailable_over_memfree(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       14144464 kB\nMemFree:         2127769 kB\nMemAvailable:   11060012 kB\n",
        encoding="ascii",
    )

    total, available = memory_runtime.posix_physical_memory_snapshot(meminfo)

    assert total == 14_144_464 * 1024
    assert available == 11_060_012 * 1024


def test_posix_memory_probe_falls_back_to_sysconf(tmp_path: Path) -> None:
    values = {
        "SC_PAGE_SIZE": 4096,
        "SC_PHYS_PAGES": 1024,
        "SC_AVPHYS_PAGES": 256,
    }

    total, available = memory_runtime.posix_physical_memory_snapshot(
        tmp_path / "missing-meminfo",
        sysconf=values.__getitem__,
    )

    assert total == 4_194_304
    assert available == 1_048_576


@pytest.mark.skipif(os.name == "nt", reason="POSIX memory probe is not used on Windows")
def test_runtime_memory_consumers_share_the_posix_probe(monkeypatch) -> None:
    physical = (16 * 1024**3, 12 * 1024**3)
    monkeypatch.setattr(
        memory_runtime,
        "posix_physical_memory_snapshot",
        lambda: physical,
    )
    monkeypatch.setattr(
        pdf_runtime,
        "posix_physical_memory_snapshot",
        lambda: physical,
    )

    shared = memory_runtime.memory_snapshot()
    pdf = pdf_runtime.memory_snapshot()

    assert (shared.total_physical, shared.available_physical) == physical
    assert (pdf.total_physical, pdf.available_physical) == physical
