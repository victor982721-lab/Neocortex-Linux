"""Focused keyset/published-plan pagination contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import neocortex.curation.preview as preview_module
from neocortex.curation.preview import CurationStateError, build_curation_plan_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog


def _state_with_organization_plans(tmp_path: Path, count: int) -> Path:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    source = corpus / "source.bin"
    source.write_bytes(b"source")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id, exact_compare=False)

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (1,'all','plan','completed',1,2,'{}')"""
        )
        connection.executemany(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,evidence_json,
            planned_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    1,
                    "text",
                    f"text:{number}",
                    str(source),
                    str(state / "organized" / f"item-{number:04d}.bin"),
                    str(state / "organized"),
                    "1",
                    str(number + 1),
                    6,
                    number + 1,
                    -1,
                    "pagination-test-classifier",
                    "text",
                    0.9,
                    "planned",
                    f"reason-{number:04d}",
                    "{}",
                    number + 1,
                )
                for number in range(count)
            ),
        )
    return state


def _state_with_duplicate_groups(tmp_path: Path, count: int) -> Path:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    for number in range(count):
        payload = f"duplicate-{number}".encode("ascii")
        (corpus / f"keep-{number:04d}.bin").write_bytes(payload)
        (corpus / f"copy-{number:04d}.bin").write_bytes(payload)
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id, exact_compare=False)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    return state


def test_page_reads_reuse_complete_publication_and_bound_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_organization_plans(tmp_path, 1_024)
    streamed_rows = 0
    materialized_items = 0
    publications = 0
    page_batches: list[tuple[int, int, bool]] = []

    original_iter = preview_module._iter_organization_rows
    original_item = preview_module._organization_item_from_row
    original_publish = preview_module._digest_plan_publication
    original_page_rows = preview_module._organization_page_rows

    def observe_iter(*args: object, **kwargs: object):
        nonlocal streamed_rows
        for row in original_iter(*args, **kwargs):
            streamed_rows += 1
            yield row

    def observe_item(row: object):
        nonlocal materialized_items
        materialized_items += 1
        return original_item(row)

    def observe_publish(*args: object, **kwargs: object):
        nonlocal publications
        publications += 1
        return original_publish(*args, **kwargs)

    def observe_page_rows(*args: object, **kwargs: object):
        result = original_page_rows(*args, **kwargs)
        page_batches.append((int(kwargs["limit"]), len(result[0]), result[1]))
        return result

    monkeypatch.setattr(preview_module, "_iter_organization_rows", observe_iter)
    monkeypatch.setattr(preview_module, "_organization_item_from_row", observe_item)
    monkeypatch.setattr(preview_module, "_digest_plan_publication", observe_publish)
    monkeypatch.setattr(preview_module, "_organization_page_rows", observe_page_rows)

    first = build_curation_plan_page(state, 1)
    after_first_items = materialized_items
    assert first.items_total == 1_024
    assert first.next_cursor is not None
    assert publications == 1
    assert streamed_rows == 1_024

    second = build_curation_plan_page(state, 1, first.next_cursor)
    assert second.items_total == first.items_total
    assert second.items[0].item_id != first.items[0].item_id
    assert publications == 1
    assert streamed_rows == 1_024
    assert materialized_items - after_first_items == 1
    assert all(batch[1] <= 1 for batch in page_batches)
    assert any(batch[2] for batch in page_batches)


def test_keyset_pages_are_deterministic_without_gaps_or_duplicates(tmp_path: Path) -> None:
    state = _state_with_organization_plans(tmp_path, 1_024)
    expected = build_curation_plan_page(state, 2_000)
    observed: list[str] = []
    cursor: str | None = None
    while True:
        page = build_curation_plan_page(state, 17, cursor)
        observed.extend(item.item_id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert observed == [item.item_id for item in expected.items]
    assert len(observed) == len(set(observed)) == 1_024
    assert page.plan_digest == expected.plan_digest
    assert page.snapshot_id == expected.snapshot_id


def test_cursor_is_invalidated_by_a_concurrent_catalog_head_change(tmp_path: Path) -> None:
    state = _state_with_organization_plans(tmp_path, 8)
    first = build_curation_plan_page(state, 1)
    assert first.next_cursor is not None
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE organization_plans SET reason='changed-after-publication' WHERE plan_id=1"
        )

    with pytest.raises(CurationStateError, match="snapshot changed"):
        build_curation_plan_page(state, 1, first.next_cursor)


def test_unselected_duplicate_groups_do_not_materialize_page_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_duplicate_groups(tmp_path, 32)
    materialized_groups = 0
    publications = 0
    original_item = preview_module._duplicate_item
    original_publish = preview_module._digest_plan_publication

    def observe_item(*args: object, **kwargs: object):
        nonlocal materialized_groups
        materialized_groups += 1
        return original_item(*args, **kwargs)

    def observe_publish(*args: object, **kwargs: object):
        nonlocal publications
        publications += 1
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(preview_module, "_duplicate_item", observe_item)
    monkeypatch.setattr(preview_module, "_digest_plan_publication", observe_publish)

    first = build_curation_plan_page(state, 1)
    assert first.duplicate_groups == 32
    assert materialized_groups == 1
    assert publications == 1
    assert first.next_cursor is not None

    build_curation_plan_page(state, 1, first.next_cursor)
    assert materialized_groups == 2
    assert publications == 1
