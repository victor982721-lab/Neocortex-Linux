"""An empty but readable image owner is complete, not a partial projection."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.semantic import semantic_service as service
from neocortex.semantic import semantic_sources as sources
from neocortex.semantic.semantic_models import fingerprint_text
from neocortex.semantic.semantic_state import semantic_database
from neocortex.deduplication.fingerprinting import full_fingerprint, snapshot_path
from tests.test_semantic_service import _FixtureBackend
from tests.test_semantic_sources import (
    _create_dedup_state,
    _create_image_state,
    _create_image_state_v5,
)


TEST_CAPABILITIES = ("inference", "image")
pytestmark = pytest.mark.capability("inference", "image")


@pytest.mark.parametrize("embed_ocr_text", (False, True))
def test_empty_image_owner_publishes_complete_generations_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embed_ocr_text: bool,
) -> None:
    fixture = tmp_path / "synthetic.png"
    fixture.write_bytes(b"synthetic fixture, never decoded by a real model")
    _create_image_state(tmp_path, fixture)
    with sqlite3.connect(tmp_path / "image.sqlite3") as connection:
        connection.execute("DELETE FROM images")
    monkeypatch.setattr(service, "_backend", lambda model, **_kwargs: _FixtureBackend(model))

    result = service.index_image_embeddings(tmp_path, embed_ocr_text=embed_ocr_text)

    assert result.items_staged == result.chunks_staged == result.errors == 0
    assert result.complete
    assert len(result.generations) == (2 if embed_ocr_text else 1)
    assert {generation.summary.status for generation in result.generations} == {"ready"}
    head = sources.semantic_source_heads(tmp_path, ("image",))[0]
    assert head.complete and head.row_count == 0
    assert head.coverage == "complete" and head.source_status == "done"

    def no_backend(*_args, **_kwargs):
        raise AssertionError("an empty published owner must replay without an inference backend")

    monkeypatch.setattr(service, "_backend", no_backend)
    replay = service.index_image_embeddings(tmp_path, embed_ocr_text=embed_ocr_text)
    assert replay.execution_mode == "exact_replay" and replay.complete
    assert tuple(g.summary.generation_id for g in replay.generations) == tuple(
        g.summary.generation_id for g in result.generations
    )


def test_image_and_ocr_candidates_recover_together_from_real_owner_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "synthetic.png"
    fixture.write_bytes(b"synthetic image for the deterministic backend")
    _create_image_state_v5(tmp_path, fixture, ocr_text="Original transformer condition")
    _create_dedup_state(tmp_path, fixture, full_fingerprint(snapshot_path(fixture)))
    owner = tmp_path / "image.sqlite3"

    def replace_ocr(text: str) -> None:
        with sqlite3.connect(owner) as connection:
            connection.execute(
                """UPDATE images SET ocr_text_zlib=?,ocr_text_chars=?,
                    ocr_text_xxh3_128=?,ocr_text_truncated=0""",
                (zlib.compress(text.encode()), len(text), fingerprint_text(text).xxh3_128),
            )

    replace_ocr("Original transformer condition")
    monkeypatch.setattr(service, "_backend", lambda model, **_kwargs: _FixtureBackend(model))
    baseline = service.index_image_embeddings(tmp_path)
    assert baseline.complete
    baseline_ids = {g.summary.generation_id for g in baseline.generations}
    assert len(baseline_ids) == 2
    replace_ocr("Intermediate transformer inspection condition")
    original_records = service.iter_image_source_records
    changed = False

    def records_then_commit(state_directory: Path):
        nonlocal changed
        yield from original_records(state_directory)
        if not changed:
            changed = True
            replace_ocr("Latest transformer pressure inspection condition")

    monkeypatch.setattr(service, "iter_image_source_records", records_then_commit)
    with pytest.raises(RuntimeError, match="source heads changed during image enumeration"):
        service.index_image_embeddings(tmp_path)

    with semantic_database(baseline.semantic_database, readonly=True) as connection:
        assert {
            int(row[0])
            for row in connection.execute("SELECT generation_id FROM published_embedding_heads")
        } == baseline_ids
        failed = tuple(
            connection.execute(
                "SELECT generation_id FROM embedding_generations WHERE status='failed'"
            )
        )
        assert len(failed) == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generations WHERE status='building'"
            ).fetchone()[0]
            == 0
        )

    refreshed = service.index_image_embeddings(tmp_path)
    assert refreshed.complete
    assert not baseline_ids.intersection(g.summary.generation_id for g in refreshed.generations)
    replay = service.index_image_embeddings(tmp_path)
    assert replay.complete and replay.execution_mode == "exact_replay"
