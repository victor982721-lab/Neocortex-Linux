"""Synthetic image Semantic heads across a legacy checkpoint and Inventory COW."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.image.state import (
    file_key as image_file_key,
    image_database,
    initialize_image_state,
    stage_inventory_batch,
)
from neocortex.deduplication import (
    DedupIndex,
    FULL_ALGORITHM,
    FileSnapshot,
    full_fingerprint,
    snapshot_path,
)
from neocortex.documents.document_cache_sync import synchronize_moved_document
from neocortex.semantic.semantic_sources import semantic_source_heads


def _rebound_image_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, FileSnapshot, int, Path]:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "image.png"
    source.write_bytes(b"legacy image content")
    snapshot = snapshot_path(source)

    initialize_image_state(state / "image.sqlite3")
    stage_inventory_batch(state / "image.sqlite3", 1, (("image/png", snapshot),))
    with image_database(state / "image.sqlite3") as connection:
        connection.execute(
            "UPDATE images SET status='done',processing_signature=? WHERE file_key=?",
            ("legacy-image-fixture-v1", image_file_key(snapshot)),
        )

    index = DedupIndex(state / "dedup.sqlite3")
    scan = index.scan(corpus)
    digest = full_fingerprint(snapshot)
    with index._connection:
        index._connection.execute(
            """INSERT INTO fingerprints(
                volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest)
            VALUES(?,?,?,?,?,?,?)""",
            (
                snapshot.volume_id.to_bytes(16, "little"),
                snapshot.file_id.to_bytes(16, "little"),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                FULL_ALGORITHM,
                digest,
            ),
        )
        # This is the legacy published root: the COW successor edge does not
        # exist until organization rebinding, so the old adapter can see it.
        index._connection.execute(
            """INSERT INTO inventory_checkpoints(
                root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,?,?,?,?,?,?)""",
            (str(corpus), scan.scan_id, None, None, None, 1, 1),
        )
    index.close()

    before = semantic_source_heads(state, ("image",))[0]
    assert before.complete
    destination = corpus / "organized" / source.name
    destination.parent.mkdir()
    source.replace(destination)

    rebound = synchronize_moved_document(
        state,
        source_kind="image",
        file_key=image_file_key(snapshot),
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(snapshot.volume_id),
        file_id=str(snapshot.file_id),
    )
    assert rebound.complete
    return state, corpus, snapshot, scan.scan_id, destination


def test_image_head_follows_cow_successor_from_legacy_checkpoint(tmp_path: Path) -> None:
    state, _corpus, _snapshot, _scan_id, destination = _rebound_image_fixture(tmp_path)

    after = semantic_source_heads(state, ("image",))[0]
    assert after.complete
    assert after.reason is None
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        successor = connection.execute(
            "SELECT successor_scan_id FROM inventory_scan_successors WHERE predecessor_scan_id=?",
            (_scan_id,),
        ).fetchone()
        assert successor is not None
        assert connection.execute(
            "SELECT 1 FROM files WHERE scan_id=? AND path=?",
            (successor[0], str(destination)),
        ).fetchone() is not None


@pytest.mark.parametrize("fault", ("unpublished", "missing_head", "cycle"))
def test_image_head_abstains_from_unpublished_cow_successor(
    tmp_path: Path,
    fault: str,
) -> None:
    state, _corpus, _snapshot, scan_id, destination = _rebound_image_fixture(tmp_path)
    current_destination = destination
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        successor = connection.execute(
            "SELECT successor_scan_id FROM inventory_scan_successors WHERE predecessor_scan_id=?",
            (scan_id,),
        ).fetchone()
        assert successor is not None
        successor_id = int(successor[0])
    if fault == "unpublished":
        with sqlite3.connect(state / "dedup.sqlite3") as connection:
            connection.execute(
                "UPDATE scans SET status='building',completed_ns=NULL WHERE scan_id=?",
                (successor_id,),
            )
            connection.commit()
    elif fault == "missing_head":
        with sqlite3.connect(state / "dedup.sqlite3") as connection:
            connection.execute(
                "DELETE FROM inventory_generation_heads WHERE scan_id=?",
                (successor_id,),
            )
            connection.commit()
    else:
        second_destination = destination.parent / "cycle" / destination.name
        second_destination.parent.mkdir()
        destination.replace(second_destination)
        rebound = synchronize_moved_document(
            state,
            source_kind="image",
            file_key=image_file_key(_snapshot),
            old_path=str(destination),
            new_path=str(second_destination),
            volume_id=str(_snapshot.volume_id),
            file_id=str(_snapshot.file_id),
        )
        assert rebound.complete
        current_destination = second_destination
        with sqlite3.connect(state / "dedup.sqlite3") as connection:
            connection.execute(
                "UPDATE inventory_scan_successors SET successor_scan_id=? "
                "WHERE predecessor_scan_id=?",
                (scan_id, successor_id),
            )
            connection.commit()

    head = semantic_source_heads(state, ("image",))[0]
    assert head.complete is False
    assert head.reason == "dedup_full_fingerprint_missing"
    assert current_destination.is_file()
