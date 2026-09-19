"""Isolated PDF pages retain headroom credit for their verified child process."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import fitz  # type: ignore[import-untyped]
import pytest

from neocortex.capabilities.formats.pdf import pdf_isolation
from neocortex.capabilities.formats.pdf.pdf_derived import search_pdf_state
from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute, PdfRouteConfig, PdfRouteState
from neocortex.deduplication import DedupIndex, snapshot_path
from neocortex.runtime.control import resource_sampler
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    GlobalResourceSummary,
    ResourceGrant,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from tests.test_pdf_route import _State


TEST_CAPABILITIES = ("documents",)
MIB = 1024**2
GIB = 1024**3


@pytest.mark.parametrize("private_readable", (True, False))
def test_isolated_pdf_page_uses_only_verified_private_memory_credit(
    tmp_path, monkeypatch, private_readable,
):
    """A missing children listing does not hide an explicitly registered worker.

    The synthetic host loses 64 MiB when the child starts. Its private pages
    account for that same amount, while the existing 512 MiB promise remains
    charged. Without readable private pages the checkpoint must still stop.
    """
    registered: list[tuple[int, int]] = []
    page_observations: list[GlobalResourceSummary] = []
    read_text = resource_sampler._read_text
    private_bytes = resource_sampler._private_resident_bytes
    sample = resource_sampler.OwnedResourceSampler._sample
    register = ResourceGrant.register_process
    handle_control = pdf_isolation._handle_extraction_control

    def missing_children(path):
        if path.name == "children":
            return None
        return read_text(path)

    def observed_private_bytes(path, state):
        if state == "Z":
            return 0
        if any(pid == int(path.name) for pid, _start in tuple(registered)):
            return 64 * MIB if private_readable else None
        return private_bytes(path, state)

    def controlled_sample(self, now):
        observed = sample(self, now)
        child_alive = any(
            counters is not None
            and counters.identity == identity
            and counters.state != "Z"
            for identity in tuple(registered)
            for counters in (
                resource_sampler._process_counters(Path("/proc") / str(identity[0]), identity[0]),
            )
        )
        return replace(
            observed,
            memory_snapshot=MemorySnapshot(
                available_physical=(608 - (64 if child_alive else 0)) * MIB,
                available_commit=None,
                total_physical=8 * GIB,
            ),
            effective_cpu_capacity=2,
            host_cpu_percent=0,
            own_cpu_cores=0,
            external_cpu_cores=0,
            memory_pressure_some_percent=None,
            memory_pressure_full_percent=None,
            memory_pressure_some_total_us=None,
            memory_pressure_full_total_us=None,
            io_pressure_some_percent=None,
            io_pressure_full_percent=None,
        )

    def observed_register(self, pid, start_time_ticks=None):
        identity = register(self, pid, start_time_ticks)
        registered.append(identity)
        return identity

    def observed_control(message, **kwargs):
        try:
            return handle_control(message, **kwargs)
        finally:
            if message[0] == "page_checkpoint":
                grant = kwargs["ocr_lease"].grant
                page_observations.append(grant.coordinator.summary())

    monkeypatch.setattr(resource_sampler, "_read_text", missing_children)
    monkeypatch.setattr(resource_sampler, "_private_resident_bytes", observed_private_bytes)
    monkeypatch.setattr(resource_sampler.OwnedResourceSampler, "_sample", controlled_sample)
    monkeypatch.setattr(ResourceGrant, "register_process", observed_register)
    monkeypatch.setattr(pdf_isolation, "_handle_extraction_control", observed_control)

    path = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Verified native transformer measurements.")
        document.save(path)
    original = path.read_bytes()
    coordinator = GlobalResourceCoordinator(
        ("pdf",),
        GlobalResourceLimits(
            memory_budget_bytes=2 * GIB,
            min_free_memory_bytes=64 * MIB,
            min_free_commit_bytes=0,
            memory_hysteresis_bytes=0,
            cpu_slots=2,
            native_thread_slots=2,
            sample_interval_seconds=0.001,
            poll_interval_seconds=0.005,
            wait_timeout_seconds=0.5,
        ),
        effective_cpu_probe=lambda: 2,
    )
    try:
        with DedupIndex(tmp_path / "dedup.sqlite3") as index:
            route = PdfRoute(
                PdfRouteConfig(
                    tmp_path / "pdf.sqlite3",
                    ocr_mode="never",
                    document_timeout_seconds=10,
                    min_free_bytes=0,
                ),
                index,
                cast(PdfRouteState, _State((snapshot_path(path),))),
                1,
                1,
                global_coordinator=coordinator,
            )
            summary = route.run()
    finally:
        coordinator.close()

    assert registered and page_observations
    assert all(item.transient_bytes == 512 * MIB for item in page_observations)
    results = search_pdf_state(tmp_path / "pdf.sqlite3", "transformer", 5)
    if private_readable:
        assert summary.extracted == 1 and summary.native_pages == 1
        assert summary.errors == 0 and summary.partial_documents == 0
        assert all(item.materialized_credit_bytes == 64 * MIB for item in page_observations)
        assert len(results) == 1
        assert results[0]["path"] == str(path) and results[0]["page_number"] == 0
        assert "[transformer]" in results[0]["snippet"]
    else:
        assert summary.extracted == 0 and summary.native_pages == 0
        assert summary.errors == 1 and summary.memory_waits >= 1
        assert all(item.materialized_credit_bytes == 0 for item in page_observations)
        assert results == []
    assert path.read_bytes() == original
    assert coordinator.summary().transient_bytes == 0
    assert coordinator.summary().cpu_slots_in_use == 0
