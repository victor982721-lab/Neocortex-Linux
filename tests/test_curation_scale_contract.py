"""Bounded curation scale contracts for the first 0.12 slice.

The fixtures are temporary and deliberately stay below the real user corpus.
These tests exercise only public or already-existing production boundaries:
inventory batching, published-plan pagination, exact verification budgets and
the durable partial scan left by cancellation.  A true resume-from-checkpoint
API is not invented here; the explicit skipped expectation records that gap.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from neocortex.api import curation_api, curation_verification_api
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import (
    CurationVerificationUnavailable,
    verify_curation_page,
)
from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    ScanSummary,
)
from neocortex.deduplication.inventory.scanner import InventoryBatch
from neocortex.documents.document_catalog import initialize_document_catalog


@dataclass(frozen=True, slots=True)
class _ScaleFixture:
    state: Path
    corpus: Path
    source_manifest: dict[str, tuple[object, ...]]


def _tree_manifest(root: Path) -> dict[str, tuple[object, ...]]:
    """Capture bytes and lstat facts without following fixture links."""

    result: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            result[relative] = ("symlink", os.readlink(path))
        elif stat.S_ISREG(mode):
            result[relative] = (
                "file",
                path.read_bytes(),
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_nlink),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
        elif stat.S_ISDIR(mode):
            result[relative] = (
                "directory",
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_nlink),
            )
    return result


def _state_manifest(state: Path) -> dict[str, tuple[object, ...]]:
    """Capture owner bytes and metadata after all writers have closed."""

    return _tree_manifest(state)


def _build_scale_fixture(tmp_path: Path) -> _ScaleFixture:
    """Build 48 entries: 46 inventory files and two skipped symlinks."""

    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    external = tmp_path / "external"
    state.mkdir(parents=True)
    corpus.mkdir()
    external.mkdir()

    for index in range(16):
        payload = f"scale-pair-{index:02d}-payload".encode("utf-8")
        (corpus / f"pair-{index:02d}-keep.bin").write_bytes(payload)
        (corpus / f"pair-{index:02d}-duplicate.bin").write_bytes(payload)
    for index in range(4):
        (corpus / f"same-size-{index}.bin").write_bytes(bytes([65 + index]) * 16)
    for index in range(4):
        (corpus / f"unique-{index}.bin").write_bytes(
            f"unique-{index:02d}-payload".encode("utf-8")
        )
    for index in range(4):
        (corpus / f"empty-{index}.bin").write_bytes(b"")
    for index in range(2):
        (corpus / f"hardlink-{index}.bin").hardlink_to(corpus / f"unique-{index}.bin")

    (external / "outside.bin").write_bytes(b"outside-payload")
    (external / "outside-dir").mkdir()
    (corpus / "external-link.bin").symlink_to(external / "outside.bin")
    (corpus / "external-dir").symlink_to(external / "outside-dir", target_is_directory=True)

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
        assert summary.files_seen == 46
        assert summary.skipped_links == 2

    initialize_document_catalog(state / "document_catalog.sqlite3")
    page = build_curation_plan_page(state, 100)
    assert page.coverage == "complete"
    assert page.inventory_files == 46
    assert page.duplicate_groups == 16
    assert page.empty_files == 4
    assert page.items_total == 20
    return _ScaleFixture(state, corpus, _tree_manifest(corpus))


def _build_linear_corpus(root: Path, count: int = 520) -> None:
    root.mkdir(parents=True)
    for index in range(count):
        (root / f"item-{index:04d}.bin").write_bytes(f"payload-{index:04d}".encode())


def _summary_without_identifier(summary: ScanSummary) -> tuple[object, ...]:
    return astuple(summary)[1:]


def _patch_default_state(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    monkeypatch.setattr(curation_api, "default_state_directory", lambda: state)
    monkeypatch.setattr(curation_verification_api, "default_state_directory", lambda: state)


def test_scale_plan_pagination_is_stable_and_read_only(tmp_path: Path) -> None:
    fixture = _build_scale_fixture(tmp_path)
    before_corpus = _tree_manifest(fixture.corpus)
    before_state = _state_manifest(fixture.state)
    full = build_curation_plan_page(fixture.state, 100)

    observed: list[str] = []
    cursor: str | None = None
    while True:
        page = build_curation_plan_page(fixture.state, 7, cursor)
        replay = build_curation_plan_page(fixture.state, 7, cursor)
        assert replay == page
        assert page.cursor == cursor
        observed.extend(item.item_id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert observed == [item.item_id for item in full.items]
    assert len(observed) == len(set(observed)) == 20
    assert page.plan_digest == full.plan_digest
    assert page.snapshot_id == full.snapshot_id
    assert _tree_manifest(fixture.corpus) == before_corpus == fixture.source_manifest
    assert _state_manifest(fixture.state) == before_state


def test_scale_inventory_batches_preserve_projection_and_bound_pending_rows(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    external = tmp_path / "external"
    corpus.mkdir()
    external.mkdir()
    for index in range(16):
        (corpus / f"item-{index:02d}.bin").write_bytes(f"payload-{index}".encode())
    (corpus / "alias.bin").hardlink_to(corpus / "item-00.bin")
    (external / "outside.bin").write_bytes(b"outside")
    (corpus / "link.bin").symlink_to(external / "outside.bin")

    results: list[tuple[ScanSummary, tuple[object, ...], tuple[int, ...]]] = []
    for ordinal, batch_size in enumerate((1, 7, 64), start=1):
        pending_sizes: list[int] = []
        original_flush = InventoryBatch.flush

        def observed_flush(
            batch: InventoryBatch,
            *,
            sink: list[int] = pending_sizes,
            delegate: Any = original_flush,
        ) -> None:
            sink.append(len(batch._rows))  # type: ignore[attr-defined]
            delegate(batch)

        with (
            patch.object(InventoryBatch, "flush", observed_flush),
            DedupIndex(tmp_path / f"state-{ordinal}.sqlite3") as index,
        ):
            summary = index.scan(corpus, batch_size=batch_size, excluded_paths=())
            snapshots = tuple(index.snapshots(summary.scan_id))
        results.append((summary, snapshots, tuple(pending_sizes)))
        assert max(pending_sizes) <= batch_size

    first_summary, first_snapshots, _ = results[0]
    for summary, snapshots, _ in results[1:]:
        assert _summary_without_identifier(summary) == _summary_without_identifier(first_summary)
        assert snapshots == first_snapshots
    assert first_summary.files_seen == 17
    assert first_summary.skipped_links == 1


def test_scale_verify_replay_and_exact_item_byte_limits(tmp_path: Path) -> None:
    fixture = _build_scale_fixture(tmp_path)
    page = build_curation_plan_page(fixture.state, 100)
    duplicate_items = [item for item in page.items if item.kind == "duplicate_group"]
    expected_bytes = sum(
        int(member["size"])
        for item in duplicate_items
        for member in item.evidence["members"]
    )

    result = verify_curation_page(
        page,
        max_items=20,
        max_files=32,
        max_bytes=expected_bytes,
    )
    replay = verify_curation_page(
        page,
        max_items=20,
        max_files=32,
        max_bytes=expected_bytes,
    )

    assert result == replay
    assert result.status == "complete"
    assert result.items_total == 20
    assert result.items_verified == 16
    assert result.items_skipped == 0
    assert result.files_checked == 32
    assert result.bytes_checked == expected_bytes

    bounded = verify_curation_page(
        page,
        max_items=20,
        max_files=32,
        max_bytes=expected_bytes - 1,
    )
    assert bounded.status == "partial"
    assert bounded.bytes_checked <= expected_bytes - 1
    assert any(item.reason == "budget_exhausted" for item in bounded.items)

    with pytest.raises(CurationVerificationUnavailable, match="item bound"):
        verify_curation_page(
            page,
            max_items=19,
            max_files=32,
            max_bytes=expected_bytes,
        )


def test_scale_public_scan_verify_pages_replay_with_stable_source_heads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_scale_fixture(tmp_path)
    _patch_default_state(monkeypatch, fixture.state)
    before_corpus = _tree_manifest(fixture.corpus)
    before_state = _state_manifest(fixture.state)

    first = curation_verification_api.curation_scan_payload(
        limit=7,
        request_id="scale-scan-first",
    )
    assert first["status"] == "complete"
    plan_id = first["plan_id"]
    assert isinstance(plan_id, str)
    first_snapshot = first["snapshot"]
    assert isinstance(first_snapshot, dict)
    expected_heads = first_snapshot["source_heads"]

    observed: list[str] = []
    cursor: str | None = None
    statuses: list[str] = []
    while True:
        scan = curation_verification_api.curation_scan_payload(
            limit=7,
            cursor=cursor,
            request_id=f"scale-scan-{len(statuses)}",
        )
        assert scan["status"] == "complete"
        snapshot = scan["snapshot"]
        assert isinstance(snapshot, dict)
        assert snapshot["source_heads"] == expected_heads
        result = scan["result"]
        assert isinstance(result, dict)
        page = result["page"]
        assert isinstance(page, dict)
        items = page["items"]
        assert isinstance(items, list)
        observed.extend(str(item["item_id"]) for item in items)

        verification = curation_verification_api.curation_verify_payload(
            plan_id,
            limit=7,
            cursor=cursor,
            request_id=f"scale-verify-{len(statuses)}",
        )
        replay = curation_verification_api.curation_verify_payload(
            plan_id,
            limit=7,
            cursor=cursor,
            request_id=f"scale-replay-{len(statuses)}",
        )
        assert verification["result"] == replay["result"]
        assert verification["snapshot"] == replay["snapshot"]
        statuses.append(str(verification["status"]))

        next_cursor = page["next_cursor"]
        if next_cursor is None:
            break
        assert isinstance(next_cursor, str)
        cursor = next_cursor

    expected = build_curation_plan_page(fixture.state, 100)
    assert observed == [item.item_id for item in expected.items]
    assert statuses[:-1] == ["partial", "partial"]
    assert statuses[-1] == "complete"
    assert _tree_manifest(fixture.corpus) == before_corpus
    assert _state_manifest(fixture.state) == before_state


def test_scale_inventory_cancellation_leaves_a_durable_partial_prefix(
    tmp_path: Path,
) -> None:
    class Cancelled(BaseException):
        pass

    corpus = tmp_path / "cancel-corpus"
    database = tmp_path / "cancel-state.sqlite3"
    _build_linear_corpus(corpus)

    def cancel_after_batch(event: object) -> None:
        if getattr(event, "completed", None) == 512:
            raise Cancelled

    with pytest.raises(Cancelled):
        with DedupIndex(database) as index:
            index.scan(
                corpus,
                batch_size=64,
                excluded_paths=(),
                progress=cancel_after_batch,
            )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """SELECT status,completed_ns,files_seen,
            (SELECT COUNT(*) FROM files f WHERE f.scan_id=scans.scan_id)
            FROM scans ORDER BY scan_id DESC LIMIT 1"""
        ).fetchone()
    assert row is not None
    assert row[0] == "partial"
    assert row[1] is not None
    assert row[2:] == (512, 512)


def test_scale_resume_checkpoint_contract_is_not_public_yet() -> None:
    pytest.skip(
        "0.12 pendiente: no existe API pública para reanudar un scan parcial "
        "desde su cursor/batch sin reconstruirlo"
    )


def test_scale_public_page_limits_reject_out_of_range_without_state_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_scale_fixture(tmp_path)
    _patch_default_state(monkeypatch, fixture.state)
    before_corpus = _tree_manifest(fixture.corpus)
    before_state = _state_manifest(fixture.state)
    plan = build_curation_plan_page(fixture.state, 100)

    scan_error = curation_verification_api.curation_scan_payload(limit=101)
    verify_error = curation_verification_api.curation_verify_payload(plan.plan_digest, limit=101)

    assert scan_error["status"] == "unavailable"
    assert scan_error["error"]["code"] == "invalid_request"  # type: ignore[index]
    assert verify_error["status"] == "unavailable"
    assert verify_error["error"]["code"] == "invalid_request"  # type: ignore[index]
    assert _tree_manifest(fixture.corpus) == before_corpus
    assert _state_manifest(fixture.state) == before_state


@pytest.mark.skip(
    reason=(
        "0.12 pendiente: scan/verify/apply no exponen todavía un contrato uniforme "
        "de límites de tiempo, RAM y disco"
    )
)
def test_scale_uniform_time_memory_disk_limits_are_public() -> None:
    """Expectation-only marker for the next bounded-runtime contract."""
