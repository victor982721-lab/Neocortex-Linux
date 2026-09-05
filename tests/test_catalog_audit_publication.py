"""Causal regressions for catalog invalidation and cache publication gates."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

import neocortex.documents.document_cache_sync as sync_module
import neocortex.persistence.state_publication as publication_module
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.deduplication import snapshot_path
from neocortex.documents.document_catalog import (
    document_catalog_database,
    list_catalog_documents,
    update_document_catalog_source,
)
from neocortex.persistence.state_publication import (
    STATE_EPOCH_FILENAME,
    StateOwnerHead,
    StatePublicationCommitError,
    read_state_epoch,
    read_state_publication_state,
    read_state_publications,
    record_state_publication,
)


def _seed_catalog_source(database: Path, source: Path) -> str:
    initialize_docx_state(database)
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,'fixture','complete','valid',?,4,'text',1,1,'IEEE','')""",
            (
                file_key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                zlib.compress(b"IEEE"),
            ),
        )
    return file_key


@pytest.mark.parametrize("staleness", ("removed", "changed"))
def test_catalog_invalidates_stale_only_and_replays_current_projection(
    tmp_path: Path, staleness: str
) -> None:
    catalog = tmp_path / "document_catalog.sqlite3"
    source_database = tmp_path / "docx.sqlite3"
    current = tmp_path / "current.docx"
    stale = tmp_path / "stale.docx"
    current.write_bytes(b"current fixture")
    stale.write_bytes(b"old fixture")
    _seed_catalog_source(source_database, current)
    stale_key = _seed_catalog_source(source_database, stale)
    initial = update_document_catalog_source(catalog, source_database, "docx")
    assert initial.classified == 2
    if staleness == "removed":
        stale.unlink()
    else:
        stale.write_bytes(b"changed fixture with different size")

    updated = update_document_catalog_source(catalog, source_database, "docx")
    replay = update_document_catalog_source(catalog, source_database, "docx")

    assert updated.source_stale == replay.source_stale == 1
    assert updated.cache_hits == replay.cache_hits == 1
    assert updated.classified == replay.classified == 0
    assert updated.stale_marked == 1
    assert replay.stale_marked == 0
    assert [item.path for item in list_catalog_documents(catalog, limit=10)] == [str(current)]
    with document_catalog_database(catalog, readonly=True) as connection:
        assert connection.execute(
            "SELECT active FROM documents WHERE file_key=?", (stale_key,)
        ).fetchone()[0] == 0
        summary = connection.execute(
            "SELECT status,summary_json FROM catalog_runs WHERE catalog_run_id=?",
            (replay.catalog_run_id,),
        ).fetchone()
        assert summary["status"] == "completed"
        assert json.loads(summary["summary_json"])["source_stale"] == 1


def _seed_sync_state(state: Path) -> dict[str, str]:
    state.mkdir()
    old_path = str(state.parent / "old.txt")
    new_path = str(state.parent / "new.txt")
    with sqlite3.connect(state / "text.sqlite3") as connection:
        connection.execute("CREATE TABLE documents(file_key TEXT PRIMARY KEY,path TEXT)")
        connection.execute("INSERT INTO documents VALUES('1:2',?)", (old_path,))
    return {
        "source_kind": "text",
        "file_key": "1:2",
        "old_path": old_path,
        "new_path": new_path,
        "volume_id": "1",
        "file_id": "2",
    }


def _cached_path(state: Path) -> str:
    with sqlite3.connect(state / "text.sqlite3") as connection:
        return str(connection.execute("SELECT path FROM documents").fetchone()[0])


def test_cache_sync_corrupt_epoch_rejects_before_any_owner_write(tmp_path: Path) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    (state / STATE_EPOCH_FILENAME).write_text("{invalid", encoding="utf-8")
    before = (state / "text.sqlite3").read_bytes()

    result = sync_module.synchronize_moved_document(state, **arguments)

    assert result.complete is False
    assert result.publication_status == "failed"
    assert result.updated_rows == 0
    assert _cached_path(state) == arguments["old_path"]
    assert (state / "text.sqlite3").read_bytes() == before


def test_cache_sync_refuses_another_pending_publication_before_writes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    record_state_publication(
        state,
        operation="restore",
        owners=("text",),
        status="partial",
        idempotency_key="other-operation",
    )
    before = (state / "text.sqlite3").read_bytes()

    result = sync_module.synchronize_moved_document(state, **arguments)

    assert result.complete is False
    assert result.publication_status == "failed"
    assert result.updated_rows == 0
    assert _cached_path(state) == arguments["old_path"]
    assert (state / "text.sqlite3").read_bytes() == before
    assert len(read_state_publications(state)) == 1


def test_cache_sync_prepares_before_first_owner_commit_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    original = sync_module._synchronize_database
    observed: list[tuple[str, str]] = []

    def observe(label: str, *args: object, **kwargs: object):
        result = original(label, *args, **kwargs)
        if label == "text":
            observed.append((read_state_publication_state(state).status, _cached_path(state)))
        return result

    monkeypatch.setattr(sync_module, "_synchronize_database", observe)
    first = sync_module.synchronize_moved_document(state, **arguments)
    second = sync_module.synchronize_moved_document(state, **arguments)

    assert first.complete is second.complete is True
    assert observed[0] == ("blocked", arguments["new_path"])
    assert first.publication_epoch == second.publication_epoch == 1
    assert first.publication_id == second.publication_id
    assert second.updated_rows == 0
    assert [item.status for item in read_state_publications(state)] == ["partial", "complete"]
    assert read_state_publication_state(state).status == "complete"


def test_cache_sync_partial_owner_failure_can_resume_same_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    original = sync_module._synchronize_database

    def fail_semantic(label: str, *args: object, **kwargs: object):
        if label == "semantic":
            return sync_module.CacheDatabaseSync(label, "error", detail="injected")
        return original(label, *args, **kwargs)

    monkeypatch.setattr(sync_module, "_synchronize_database", fail_semantic)
    first = sync_module.synchronize_moved_document(state, **arguments)
    assert first.complete is False
    assert first.publication_status == "partial"
    assert read_state_epoch(state).epoch == 0
    assert read_state_publication_state(state).status == "blocked"

    monkeypatch.setattr(sync_module, "_synchronize_database", original)
    resumed = sync_module.synchronize_moved_document(state, **arguments)

    assert resumed.complete is True
    assert resumed.updated_rows == 0
    assert resumed.publication_epoch == 1
    assert read_state_publication_state(state).status == "complete"


def test_completed_cache_sync_replay_refuses_to_apply_another_transition(tmp_path: Path) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    first = sync_module.synchronize_moved_document(state, **arguments)
    assert first.complete is True
    with sqlite3.connect(state / "text.sqlite3") as connection:
        connection.execute("UPDATE documents SET path=?", (arguments["old_path"],))
    before = (state / "text.sqlite3").read_bytes()

    replay = sync_module.synchronize_moved_document(state, **arguments)

    assert replay.complete is False
    assert replay.updated_rows == 0
    assert _cached_path(state) == arguments["old_path"]
    assert (state / "text.sqlite3").read_bytes() == before
    assert read_state_epoch(state).epoch == first.publication_epoch


def test_cache_sync_interruption_leaves_prepare_for_same_key_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    original = sync_module._synchronize_database

    def interrupt(label: str, *args: object, **kwargs: object):
        if label == "semantic":
            raise KeyboardInterrupt("injected between owner commits")
        return original(label, *args, **kwargs)

    monkeypatch.setattr(sync_module, "_synchronize_database", interrupt)
    with pytest.raises(KeyboardInterrupt, match="between owner commits"):
        sync_module.synchronize_moved_document(state, **arguments)
    assert _cached_path(state) == arguments["new_path"]
    assert read_state_publication_state(state).status == "blocked"

    monkeypatch.setattr(sync_module, "_synchronize_database", original)
    resumed = sync_module.synchronize_moved_document(state, **arguments)
    assert resumed.complete is True
    assert resumed.updated_rows == 0
    assert read_state_publication_state(state).status == "complete"


def test_cache_sync_preparation_io_error_preserves_owner_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    before = (state / "text.sqlite3").read_bytes()

    def fail_prepare(*args: object, **kwargs: object):
        raise OSError("injected prepare failure")

    monkeypatch.setattr(sync_module, "record_state_publication", fail_prepare)
    result = sync_module.synchronize_moved_document(state, **arguments)

    assert result.complete is False
    assert result.updated_rows == 0
    assert result.publication_status == "failed"
    assert (state / "text.sqlite3").read_bytes() == before


def test_cache_sync_durable_journal_reports_success_after_epoch_pointer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    original = publication_module._atomic_write_json

    def fail_pointer(path: Path, payload: dict[str, object]) -> None:
        if path.name == STATE_EPOCH_FILENAME:
            raise OSError("injected epoch pointer failure")
        original(path, payload)

    monkeypatch.setattr(publication_module, "_atomic_write_json", fail_pointer)
    first = sync_module.synchronize_moved_document(state, **arguments)

    assert first.complete is True
    assert first.publication_status == "complete"
    assert first.publication_epoch == 1
    assert any(item.database == "publication" and item.status == "warning" for item in first.databases)
    assert not (state / STATE_EPOCH_FILENAME).exists()
    assert read_state_epoch(state).source == "journal"
    assert _cached_path(state) == arguments["new_path"]

    monkeypatch.setattr(publication_module, "_atomic_write_json", original)
    replay = sync_module.synchronize_moved_document(state, **arguments)
    assert replay.complete is True
    assert replay.publication_id == first.publication_id
    assert replay.updated_rows == 0


def test_cache_sync_inconsistent_content_manifest_rejects_before_writes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    published = record_state_publication(
        state,
        operation="authenticated-fixture",
        owners=("text",),
        status="complete",
        idempotency_key="previous-operation",
        owner_heads=(StateOwnerHead("text", 1, "a" * 64),),
    )
    assert published.content_manifest_name is not None
    (state / published.content_manifest_name).unlink()
    before = (state / "text.sqlite3").read_bytes()

    result = sync_module.synchronize_moved_document(state, **arguments)

    assert result.complete is False
    assert result.publication_status == "failed"
    assert result.updated_rows == 0
    assert (state / "text.sqlite3").read_bytes() == before
    assert len(read_state_publications(state)) == 1


def test_cache_sync_uncertain_journal_does_not_claim_durable_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    arguments = _seed_sync_state(state)
    original = sync_module.record_state_publication

    def uncertain_append(*args: object, **kwargs: object):
        publication = original(*args, **kwargs)
        if kwargs.get("status") == "complete":
            raise StatePublicationCommitError("injected uncertain append", publication, durable=False)
        return publication

    monkeypatch.setattr(sync_module, "record_state_publication", uncertain_append)
    result = sync_module.synchronize_moved_document(state, **arguments)

    assert result.complete is False
    assert result.publication_status == "failed"
    assert result.updated_rows == 1
    assert _cached_path(state) == arguments["new_path"]
    assert "uncertain append" in str(result.error_message)

    monkeypatch.setattr(sync_module, "record_state_publication", original)
    replay = sync_module.synchronize_moved_document(state, **arguments)
    assert replay.complete is True
    assert replay.updated_rows == 0
