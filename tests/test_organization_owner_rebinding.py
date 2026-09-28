"""Synthetic owner-aware rebinding across modern Inventory and visual owners."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.capabilities.formats.image.state import (
    file_key as image_file_key,
    initialize_image_state,
    stage_inventory_batch,
)
from neocortex.capabilities.formats.video.state import (
    initialize_video_state,
    store_video_inventory,
    video_database,
)
from neocortex.deduplication import DedupIndex, snapshot_path
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.documents.document_cache_sync import (
    DocumentMoveTransition,
    synchronize_moved_document,
    synchronize_moved_documents,
)
from neocortex.persistence.framework_state_writer import FrameworkState


def _seed_docx(path: Path, source: Path, snapshot) -> str:
    initialize_docx_state(path)
    key = f"{snapshot.volume_id}:{snapshot.file_id}"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                str(source),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "c23-fixture-v1",
                "complete",
                "valid",
                b"fixture",
                7,
                "fixture",
                1,
                1,
                "fixture",
                "fixture",
            ),
        )
        connection.commit()
    return key


def _seed_modern_inventory(state: Path, source: Path):
    index = DedupIndex(state / "dedup.sqlite3")
    scan = index.scan(source.parent)
    snapshot = snapshot_path(source)
    return index, scan.scan_id, snapshot


def test_modern_inventory_rebinding_preserves_predecessor_and_replay(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "source.docx"
    source.write_bytes(b"c23 synthetic source")
    destination = corpus / "organized" / source.name
    destination.parent.mkdir()
    old_snapshot = snapshot_path(source)
    file_key = _seed_docx(state / "docx.sqlite3", source, old_snapshot)
    with FrameworkState(state / "framework.sqlite3"):
        pass
    index, scan_id, _ = _seed_modern_inventory(state, source)
    index.close()
    source.replace(destination)

    first = synchronize_moved_document(
        state,
        source_kind="docx",
        file_key=file_key,
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(old_snapshot.volume_id),
        file_id=str(old_snapshot.file_id),
    )
    assert first.complete

    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        rows = connection.execute(
            "SELECT scan_id,path FROM files ORDER BY scan_id,path"
        ).fetchall()
        successors = connection.execute(
            "SELECT predecessor_scan_id,successor_scan_id FROM inventory_scan_successors"
        ).fetchall()
        heads = connection.execute(
            "SELECT scan_id,content_digest FROM inventory_generation_heads ORDER BY scan_id"
        ).fetchall()
    assert (scan_id, str(source)) in rows
    assert any(path == str(destination) and scan != scan_id for scan, path in rows)
    assert successors == [(scan_id, max(scan for scan, _path in rows))]
    assert len(heads) == 2
    predecessor_digest = bytes(heads[0][1])

    replay = synchronize_moved_document(
        state,
        source_kind="docx",
        file_key=file_key,
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(old_snapshot.volume_id),
        file_id=str(old_snapshot.file_id),
    )
    assert replay.complete
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM scans"
        ).fetchone()[0] == 2
        assert bytes(
            connection.execute(
                "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
                (scan_id,),
            ).fetchone()[0]
        ) == predecessor_digest


def test_image_and_video_owner_paths_rebind_without_reprocessing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    image = corpus / "image.png"
    image.write_bytes(b"image fixture")
    video = corpus / "video.mp4"
    video.write_bytes(b"video fixture")
    image_snapshot = snapshot_path(image)
    video_snapshot = snapshot_path(video)
    image_key = image_file_key(image_snapshot)
    video_key = file_key_from_snapshot(video_snapshot)
    initialize_image_state(state / "image.sqlite3")
    stage_inventory_batch(state / "image.sqlite3", 1, (("image/png", image_snapshot),))
    initialize_video_state(state / "video.sqlite3")
    with video_database(state / "video.sqlite3") as connection:
        store_video_inventory(connection, video_snapshot, "video/mp4", 1)
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,
            status,title,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                video_key,
                str(video),
                "video/mp4",
                video_snapshot.size,
                video_snapshot.mtime_ns,
                video_snapshot.birthtime_ns,
                "c23-video-fixture-v1",
                "not_applicable",
                "video fixture",
                1,
                1,
            ),
        )
        connection.commit()
    image_destination = corpus / "organized" / image.name
    video_destination = corpus / "organized" / video.name
    image_destination.parent.mkdir()
    image.replace(image_destination)
    video.replace(video_destination)

    image_result = synchronize_moved_document(
        state,
        source_kind="image",
        file_key=image_key,
        old_path=str(image),
        new_path=str(image_destination),
        volume_id=str(image_snapshot.volume_id),
        file_id=str(image_snapshot.file_id),
    )
    video_result = synchronize_moved_document(
        state,
        source_kind="video",
        file_key=video_key,
        old_path=str(video),
        new_path=str(video_destination),
        volume_id=str(video_snapshot.volume_id),
        file_id=str(video_snapshot.file_id),
    )
    assert image_result.complete and video_result.complete
    with sqlite3.connect(state / "image.sqlite3") as connection:
        assert connection.execute("SELECT path FROM images WHERE file_key=?", (image_key,)).fetchone()[0] == str(image_destination)
    with sqlite3.connect(state / "video.sqlite3") as connection:
        assert connection.execute("SELECT path FROM video_inventory WHERE file_key=?", (video_key,)).fetchone()[0] == str(video_destination)


def test_bounded_batch_uses_one_inventory_successor_for_multiple_moves(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    sources = tuple(corpus / f"source-{index}.docx" for index in range(2))
    for source in sources:
        source.write_bytes(f"fixture-{source.name}".encode())
    snapshots = tuple(snapshot_path(source) for source in sources)
    keys = tuple(_seed_docx(state / "docx.sqlite3", source, snapshot) for source, snapshot in zip(sources, snapshots, strict=True))
    with FrameworkState(state / "framework.sqlite3"):
        pass
    index, _scan_id, _ = _seed_modern_inventory(state, sources[0])
    index.close()
    destinations = tuple(corpus / "organized" / source.name for source in sources)
    destinations[0].parent.mkdir()
    for source, destination in zip(sources, destinations, strict=True):
        source.replace(destination)
    transitions = tuple(
        DocumentMoveTransition("docx", key, str(source), str(destination), str(snapshot.volume_id), str(snapshot.file_id))
        for key, source, destination, snapshot in zip(keys, sources, destinations, snapshots, strict=True)
    )
    result = synchronize_moved_documents(state, transitions)
    assert result.complete
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 2
        rows = connection.execute("SELECT scan_id,path FROM files ORDER BY scan_id,path").fetchall()
    assert sum(path.startswith(str(corpus / "organized")) for _scan, path in rows) == 2
    replay = synchronize_moved_documents(state, transitions)
    assert replay.complete
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 2


def test_inventory_cow_cancellation_rolls_back_without_publishing_head(tmp_path: Path) -> None:
    class _Cancelled(RuntimeError):
        pass

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "source.bin"
    source.write_bytes(b"fixture")
    destination = corpus / "organized" / source.name
    destination.parent.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    index = DedupIndex(state / "dedup.sqlite3")
    scan = index.scan(corpus)
    # Synthetic metadata rows exercise the same INSERT SELECT COW boundary
    # without creating 100k filesystem objects.
    for number in range(100_000):
        index._connection.execute(
            "INSERT INTO files(scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns) VALUES(?,?,?,?,?,?,?)",
            (
                scan.scan_id,
                str(corpus / f"metadata-{number}.bin"),
                (number + 100).to_bytes(16, "little"),
                (number + 200).to_bytes(16, "little"),
                1,
                1,
                -1,
            ),
        )
    index._connection.execute(
        "UPDATE scans SET files_seen=100001,bytes_seen=100007 WHERE scan_id=?",
        (scan.scan_id,),
    )
    index._connection.commit()
    source_head = bytes(
        index._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone()[0]
    )
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise _Cancelled("synthetic COW cancellation")

    source.replace(destination)
    try:
        index.apply_reconciliation(
            scan.scan_id,
            upserts=(snapshot_path(destination),),
            remove_paths=(str(source),),
            work_check=cancel,
        )
    except _Cancelled:
        pass
    else:  # pragma: no cover - cancellation is the contract under test
        raise AssertionError("synthetic COW cancellation did not interrupt")
    finally:
        index.close()
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_scan_successors"
        ).fetchone()[0] == 0
        assert bytes(
            connection.execute(
                "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
                (scan.scan_id,),
            ).fetchone()[0]
        ) == source_head
        assert connection.execute(
            "SELECT path FROM files WHERE scan_id=? AND path=?",
            (scan.scan_id, str(source)),
        ).fetchone() is not None
