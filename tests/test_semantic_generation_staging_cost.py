"""Bounded VM-step regressions for Semantic v8 staging hot paths.

Setup is deliberately outside each measured window.  The measured operation
uses one SQLite owner connection and a progress callback every VM instruction;
no wall-clock threshold is used.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from neocortex.semantic.semantic_chunking import TextChunkingConfig, chunk_text_sections
from neocortex.semantic.semantic_item_repository import (
    _finalize_text_chunk_refresh,
    _upsert_item,
)
from neocortex.semantic.semantic_models import (
    SemanticItem,
    TextChunk,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic.semantic_state import (
    enqueue_text_chunk_jobs,
    finalize_text_chunk_refresh,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from tests.test_semantic_state import _initialize, _text_model


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_REFRESH_TOKEN = "staging-cost-shared-refresh-v1"


@dataclass(frozen=True, slots=True)
class _StagingFixture:
    database: Path
    item: SemanticItem
    chunks: tuple[TextChunk, ...]


@dataclass(frozen=True, slots=True)
class _Measured:
    vm_steps: int
    target_dirty: int
    unrelated_dirty: int
    target_ordinals: tuple[int, ...] = ()
    target_published_receipts: tuple[int, ...] = ()
    unrelated_published: int = 0


def _stage_item(
    database: Path,
    item_id: str,
    sections: tuple[TextSection, ...],
) -> tuple[SemanticItem, tuple[TextChunk, ...]]:
    source_text = "\n".join(section.text for section in sections)
    item = SemanticItem(
        item_id,
        "pdf",
        f"identity:{item_id}",
        "fixture-v1",
        fingerprint_text(source_text),
        path=f"/fixtures/{item_id}.pdf",
        provenance={"fixture": "staging-cost"},
        source_revision={"text_chars": len(source_text)},
    )
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    upsert_semantic_item(database, item, refresh_token=_REFRESH_TOKEN, updated_ns=10)
    chunks = chunk_text_sections(item.item_id, sections, config)
    assert len(chunks) == len(sections)
    assert stage_text_chunks(
        database, chunks, refresh_token=_REFRESH_TOKEN, updated_ns=11,
    ) == len(chunks)
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=config.signature,
        refresh_token=_REFRESH_TOKEN,
        updated_ns=12,
    )
    return item, chunks


def _build_fixture(root: Path, unrelated_items: int) -> _StagingFixture:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "semantic.sqlite3"
    model = _text_model("staging-cost-model", "staging-cost-space")
    _initialize(database, model)
    target_item, target_chunks = _stage_item(
        database,
        "staging-cost-target",
        (
            TextSection("pdf_page", "1", "target staging page one " * 8),
            TextSection("pdf_page", "2", "target staging page two " * 8),
        ),
    )
    all_chunks = list(target_chunks)
    for index in range(unrelated_items):
        _item, chunks = _stage_item(
            database,
            f"staging-cost-unrelated-{index}",
            (TextSection(
                "pdf_page", "1",
                f"unrelated staging source {index} with shared refresh token",
            ),),
        )
        all_chunks.extend(chunks)
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="staging-cost-generation-v1",
        provenance={"fixture": "staging-cost"},
        started_ns=20,
    )
    assert enqueue_text_chunk_jobs(
        database,
        generation_id,
        tuple(chunk.chunk_id for chunk in all_chunks),
        now_ns=21,
    ) == len(all_chunks)
    # The measured trigger is the transition 0 -> 1.  This is a derived v8
    # hint; resetting it is fixture setup, not part of the VM window.
    with semantic_database(database) as connection:
        connection.execute("UPDATE embedding_jobs SET source_dirty=0")
    return _StagingFixture(database, target_item, target_chunks)


def _measure_trigger(fixture: _StagingFixture) -> _Measured:
    target_ids = {chunk.chunk_id for chunk in fixture.chunks}
    changed = replace(
        fixture.item,
        # Non-NULL moves intentionally do not dirty text jobs.  A transition
        # to an unavailable path does trigger the source fanout contract.
        path=None,
        source_revision={**fixture.item.source_revision, "probe": "path-unavailable"},
    )
    vm_steps = 0

    def progress() -> int:
        nonlocal vm_steps
        vm_steps += 1
        return 0

    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.set_progress_handler(progress, 1)
        try:
            _upsert_item(
                connection,
                changed,
                refresh_token=_REFRESH_TOKEN,
                updated_ns=30,
                invalidate_text_on_fingerprint_change=True,
            )
            connection.commit()
        finally:
            connection.set_progress_handler(None, 0)
        rows = connection.execute(
            "SELECT entity_id,source_dirty FROM embedding_jobs ORDER BY job_id"
        ).fetchall()
    target_dirty = sum(int(row[1]) for row in rows if str(row[0]) in target_ids)
    unrelated_dirty = sum(int(row[1]) for row in rows if str(row[0]) not in target_ids)
    return _Measured(vm_steps, target_dirty, unrelated_dirty)


def _measure_publication(fixture: _StagingFixture) -> _Measured:
    vm_steps = 0

    def progress() -> int:
        nonlocal vm_steps
        vm_steps += 1
        return 0

    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        previous_receipts = {
            int(row[0])
            for row in connection.execute(
                """SELECT DISTINCT derivation.publication_receipt_id
                FROM semantic_chunk_derivations derivation
                JOIN semantic_chunk_revisions revision
                  ON revision.chunk_revision_id=derivation.chunk_revision_id
                WHERE revision.item_id=? AND derivation.refresh_token=?""",
                (fixture.item.item_id, _REFRESH_TOKEN),
            )
            if row[0] is not None
        }
        assert len(previous_receipts) == 1
        connection.execute("BEGIN IMMEDIATE")
        connection.set_progress_handler(progress, 1)
        try:
            deactivated = _finalize_text_chunk_refresh(
                connection,
                item_id=fixture.item.item_id,
                chunking_signature=fixture.chunks[0].chunking_signature,
                refresh_token=_REFRESH_TOKEN,
                updated_ns=31,
            )
            connection.commit()
        finally:
            connection.set_progress_handler(None, 0)
        assert deactivated == 0
        rows = connection.execute(
            """SELECT revision.ordinal,derivation.publication_receipt_id
            FROM semantic_chunk_revisions revision
            JOIN semantic_chunk_derivations derivation
              ON derivation.chunk_revision_id=revision.chunk_revision_id
            WHERE revision.item_id=? AND revision.chunking_signature=?
              AND derivation.refresh_token=?
            ORDER BY revision.ordinal""",
            (fixture.item.item_id, fixture.chunks[0].chunking_signature, _REFRESH_TOKEN),
        ).fetchall()
        unrelated_published = int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_chunk_derivations derivation
                JOIN semantic_chunk_revisions revision
                  ON revision.chunk_revision_id=derivation.chunk_revision_id
                WHERE derivation.refresh_token=? AND revision.item_id<>?
                  AND derivation.publication_receipt_id IS NOT NULL""",
                (_REFRESH_TOKEN, fixture.item.item_id),
            ).fetchone()[0]
        )
    ordinals = tuple(int(row[0]) for row in rows)
    published = tuple(int(row[1]) for row in rows if row[1] is not None)
    assert published and set(published) == previous_receipts
    return _Measured(
        vm_steps,
        target_dirty=0,
        unrelated_dirty=0,
        target_ordinals=ordinals,
        target_published_receipts=published,
        unrelated_published=unrelated_published,
    )


def _assert_constant_delta(small: _Measured, large: _Measured) -> None:
    assert min(small.vm_steps, large.vm_steps) > 0
    # 224 unrelated items must not add their per-row scans to this delta.
    # Leave bounded slack for index/tree bookkeeping, not a factor-of-four
    # allowance that can accidentally admit the original linear rescan.
    assert large.vm_steps <= small.vm_steps + 750


def test_staging_trigger_update_scales_with_target_chunks_not_unrelated_jobs(
    tmp_path: Path,
) -> None:
    small_fixture = _build_fixture(tmp_path / "small", 32)
    large_fixture = _build_fixture(tmp_path / "large", 256)
    small = _measure_trigger(small_fixture)
    large = _measure_trigger(large_fixture)
    for result in (small, large):
        assert result.target_dirty == 2
        assert result.unrelated_dirty == 0
    _assert_constant_delta(small, large)


def test_chunk_publication_queries_scale_with_target_refresh_membership(
    tmp_path: Path,
) -> None:
    small_fixture = _build_fixture(tmp_path / "small", 32)
    large_fixture = _build_fixture(tmp_path / "large", 256)
    small = _measure_publication(small_fixture)
    large = _measure_publication(large_fixture)
    for result in (small, large):
        assert result.target_ordinals == (0, 1)
        assert len(result.target_published_receipts) == 2
    assert small.unrelated_published == 32
    assert large.unrelated_published == 256
    _assert_constant_delta(small, large)
