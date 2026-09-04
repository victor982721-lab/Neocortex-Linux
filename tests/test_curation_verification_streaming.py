"""Streaming and byte-budget regressions for exact curation verification."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

import neocortex.curation.verification as verification
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import (
    CurationVerificationSnapshotChanged,
    CurationWorkBudget,
    verify_curation_page,
)
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog


def _large_duplicate_page(tmp_path: Path):
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir(parents=True)
    corpus.mkdir(parents=True)
    size = 2 * verification._READ_CHUNK_SIZE + 17
    pattern = bytes(range(251))
    payload = (pattern * math.ceil(size / len(pattern)))[:size]
    (corpus / "keeper.bin").write_bytes(payload)
    (corpus / "member.bin").write_bytes(payload)

    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus, excluded_paths=())
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(
            summary.scan_id,
            exact_compare=True,
            preview_limit=None,
        )
    initialize_document_catalog(state / "document_catalog.sqlite3")
    return build_curation_plan_page(state, 100), size


def test_exact_verification_streams_reference_with_bounded_buffers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, size = _large_duplicate_page(tmp_path)
    reads: list[int] = []
    original_read = verification.os.read

    def bounded_read(descriptor: int, count: int) -> bytes:
        reads.append(count)
        return original_read(descriptor, count)

    monkeypatch.setattr(verification.os, "read", bounded_read)
    result = verify_curation_page(page)

    expected_chunks = 2 * math.ceil(size / verification._READ_CHUNK_SIZE)
    assert result.status == "complete"
    assert result.files_checked == 2
    assert result.bytes_checked == 2 * size
    assert reads == [
        verification._READ_CHUNK_SIZE,
        verification._READ_CHUNK_SIZE,
        17,
        verification._READ_CHUNK_SIZE,
        verification._READ_CHUNK_SIZE,
        17,
    ]
    assert result.metrics == {
        "files": 2,
        "bytes": 2 * size,
        "chunks": expected_chunks,
        "peak_buffer": 2 * verification._READ_CHUNK_SIZE,
        "keeper_replays": 1,
    }
    assert result.to_dict()["metrics"] == result.metrics


def test_streaming_verification_never_reads_past_budget_or_after_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, size = _large_duplicate_page(tmp_path / "budget")
    original_read = verification.os.read
    observed: list[int] = []

    def count_read(descriptor: int, count: int) -> bytes:
        data = original_read(descriptor, count)
        observed.append(len(data))
        return data

    monkeypatch.setattr(verification.os, "read", count_read)
    bounded = verify_curation_page(page, budget=CurationWorkBudget(max_bytes=size))

    assert bounded.status == "partial"
    assert bounded.items[0].reason == "budget_exhausted"
    assert bounded.bytes_checked == size
    assert sum(observed) == size
    assert bounded.metrics is not None
    assert bounded.metrics["bytes"] == size

    observed.clear()
    cancelled = False

    def cancellation_check() -> bool:
        return cancelled

    def cancel_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal cancelled
        data = original_read(descriptor, count)
        observed.append(len(data))
        cancelled = True
        return data

    monkeypatch.setattr(verification.os, "read", cancel_after_first_read)
    interrupted = verify_curation_page(
        page,
        budget=CurationWorkBudget(cancellation_check=cancellation_check),
    )

    assert interrupted.status == "partial"
    assert interrupted.items[0].reason == "cancelled"
    assert interrupted.bytes_checked == observed[0] <= verification._READ_CHUNK_SIZE
    assert len(observed) == 1


def test_final_component_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "candidate.bin"
    os.mkfifo(fifo)

    with pytest.raises(CurationVerificationSnapshotChanged):
        verification._open_regular_file_beneath(root, fifo)
