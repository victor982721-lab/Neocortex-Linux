from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from neocortex.deduplication import FULL_ALGORITHM, DedupIndex, full_fingerprint, snapshot_path
from neocortex.documents.document_retirement import run_document_retirement
from neocortex.semantic.semantic_item_repository import stage_text_chunks, upsert_semantic_item
from neocortex.semantic.semantic_models import SemanticItem, TextChunk, fingerprint_text
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.safety.kio_trash import metadata_binding


def _framework(
    source: Path,
    *,
    action_id: int = 1,
    action_type: str = "trash_artifact",
    source_digest: str | None = None,
) -> SimpleNamespace:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE file_actions(
            action_id INTEGER PRIMARY KEY, run_id INTEGER, action_type TEXT,
            status TEXT, source_path TEXT, expected_identity_json TEXT,
            effect_receipt_json TEXT
        );
        CREATE TABLE route_candidates(volume_id INTEGER, file_id INTEGER, path TEXT);
        """
    )
    snapshot = snapshot_path(source)
    expected = {
        "schema_version": 1,
        "source": {
            "path": str(source),
            "volume_id": f"{snapshot.volume_id:x}",
            "file_id": f"{snapshot.file_id:x}",
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
            "birthtime_ns": snapshot.birthtime_ns,
        },
        "target_path": None,
    }
    trash_root = source.parent / ".trash"
    digest = source_digest or metadata_binding(snapshot)
    evidence = {
        "trash_root": str(trash_root),
        "trash_path": str(trash_root / "files" / source.name),
        "info_path": str(trash_root / "info" / (source.name + ".trashinfo")),
        "volume_id": f"{snapshot.volume_id:x}",
        "file_id": f"{snapshot.file_id:x}",
        "size": snapshot.size,
        "digest": digest,
    }
    receipt = {
        "schema_version": 1,
        "receipt_type": "successful_return_and_observation",
        "operation": "trash",
        "source_absent": True,
        "source_path": str(source),
        "source_digest": digest,
        "target_path": None,
        "trash": evidence,
    }
    connection.execute(
        "INSERT INTO file_actions VALUES(?,?,?,?,?,?,?)",
        (action_id, 9, action_type, "applied", str(source), json.dumps(expected), json.dumps(receipt)),
    )
    connection.execute(
        "INSERT INTO route_candidates VALUES(?,?,?)",
        (snapshot.volume_id, snapshot.file_id, str(source)),
    )
    connection.commit()
    return SimpleNamespace(_connection=connection)


def _text_owner(path: Path, source: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE documents(file_key TEXT PRIMARY KEY,path TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER)"
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
    connection.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?)",
        (file_key, str(source), snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns),
    )
    connection.commit()
    connection.close()


def _catalog_owner(path: Path, source: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE documents(
            source_kind TEXT,file_key TEXT,path TEXT,size INTEGER,mtime_ns INTEGER,
            birthtime_ns INTEGER,active INTEGER,updated_ns INTEGER,
            PRIMARY KEY(source_kind,file_key)
        )"""
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
    connection.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
        ("text", file_key, str(source), snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns, 1, 1),
    )
    connection.commit()
    connection.close()


def test_retirement_invalidates_route_catalog_semantic_framework_and_replays(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "discarded.txt"
    source.write_text("discarded", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    _text_owner(state / "text.sqlite3", source)
    _catalog_owner(state / "document_catalog.sqlite3", source)
    with DedupIndex(state / "dedup.sqlite3") as index:
        index.scan(root)
    semantic_path = state / "semantic.sqlite3"
    initialize_semantic_state(semantic_path)
    snapshot = snapshot_path(source)
    item = SemanticItem(
        item_id="item:text:" + f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
        source_kind="text",
        source_identity=f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
        identity_version="file-key-v1",
        fingerprint=fingerprint_text("discarded"),
        path=str(source),
        source_revision={"volume_id": snapshot.volume_id, "file_id": snapshot.file_id},
    )
    upsert_semantic_item(semantic_path, item)
    chunk = TextChunk(
        chunk_id="chunk-1", item_id=item.item_id, ordinal=0,
        section_kind="text", section_id="body", start_char=0, end_char=9,
        text="discarded", fingerprint=fingerprint_text("discarded"),
        chunking_signature="chunk-v1",
    )
    stage_text_chunks(semantic_path, (chunk,), refresh_token="r1")

    framework = _framework(source)
    source.unlink()
    result = run_document_retirement(
        state,
        root=root,
        framework_state=framework,
        run_id=9,
        framework_lock_held=True,
    )
    assert result["status"] == "complete", result
    assert result["retired_sources"] == 1
    assert result["semantic_items_deactivated"] == 1
    assert result["dedup_rows_removed"] == 1
    assert framework._connection.execute("SELECT COUNT(*) FROM route_candidates").fetchone()[0] == 0
    with sqlite3.connect(state / "text.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        assert connection.execute("SELECT active FROM documents").fetchone()[0] == 0
    with sqlite3.connect(semantic_path) as connection:
        assert connection.execute("SELECT active FROM semantic_items").fetchone()[0] == 0
        assert connection.execute("SELECT active FROM text_chunks").fetchone()[0] == 0

    replay = run_document_retirement(
        state,
        root=root,
        framework_state=framework,
        run_id=9,
        framework_lock_held=True,
    )
    assert replay["status"] == "complete"
    assert replay["replay"] is True


def test_replacement_identity_is_not_withdrawn(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "same-path.txt"
    source.write_text("old", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    old = snapshot_path(source)
    replacement = root / "replacement.txt"
    replacement.write_text("new", encoding="utf-8")
    replacement.replace(source)
    _text_owner(state / "text.sqlite3", source)
    placeholder = root / "retired-placeholder"
    placeholder.write_text("placeholder", encoding="utf-8")
    framework = _framework(placeholder)
    placeholder.unlink()
    # Replace the fixture action with the old identity while the current owner
    # row deliberately points at the new file key.
    connection = framework._connection
    connection.execute("DELETE FROM file_actions")
    digest = metadata_binding(old)
    expected = {"schema_version": 1, "source": {"path": str(source), "volume_id": f"{old.volume_id:x}", "file_id": f"{old.file_id:x}", "size": old.size, "mtime_ns": old.mtime_ns, "birthtime_ns": old.birthtime_ns}, "target_path": None}
    evidence = {"trash_root": str(root / ".trash"), "trash_path": str(root / ".trash/files/x"), "info_path": str(root / ".trash/info/x.trashinfo"), "volume_id": f"{old.volume_id:x}", "file_id": f"{old.file_id:x}", "size": old.size, "digest": digest}
    receipt = {"schema_version": 1, "receipt_type": "successful_return_and_observation", "operation": "trash", "source_absent": True, "source_path": str(source), "source_digest": digest, "target_path": None, "trash": evidence}
    connection.execute("INSERT INTO file_actions VALUES(?,?,?,?,?,?,?)", (1, 9, "trash_artifact", "applied", str(source), json.dumps(expected), json.dumps(receipt)))
    connection.commit()
    result = run_document_retirement(state, root=root, framework_state=framework, run_id=9, framework_lock_held=True)
    assert result["status"] == "complete", result
    with sqlite3.connect(state / "text.sqlite3") as owner:
        assert owner.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_empty_file_full_sha_receipt_is_retired_by_identity(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "empty.bin"
    source.write_bytes(b"")
    state = tmp_path / "state"
    state.mkdir()
    _text_owner(state / "text.sqlite3", source)
    _catalog_owner(state / "document_catalog.sqlite3", source)
    with DedupIndex(state / "dedup.sqlite3") as index:
        index.scan(root)
    semantic_path = state / "semantic.sqlite3"
    initialize_semantic_state(semantic_path)
    snapshot = snapshot_path(source)
    item = SemanticItem(
        item_id="item:text:" + f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
        source_kind="text",
        source_identity=f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
        identity_version="file-key-v1",
        fingerprint=fingerprint_text(""),
        path=str(source),
        source_revision={"volume_id": snapshot.volume_id, "file_id": snapshot.file_id},
    )
    upsert_semantic_item(semantic_path, item)
    framework = _framework(
        source,
        action_type="trash_empty_file",
        source_digest=FULL_ALGORITHM + ":" + full_fingerprint(snapshot).hex(),
    )
    source.unlink()

    result = run_document_retirement(
        state,
        root=root,
        framework_state=framework,
        run_id=9,
        framework_lock_held=True,
    )
    assert result["status"] == "complete", result
    assert result["observed_actions"] == 1
    assert result["retired_sources"] == 1
    assert result["dedup_rows_removed"] == 1
    assert result["semantic_items_deactivated"] == 1
    assert framework._connection.execute("SELECT COUNT(*) FROM route_candidates").fetchone()[0] == 0
    with sqlite3.connect(state / "text.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        assert connection.execute("SELECT active FROM documents").fetchone()[0] == 0
    with sqlite3.connect(semantic_path) as connection:
        assert connection.execute("SELECT active FROM semantic_items").fetchone()[0] == 0


def test_malformed_applied_receipt_requires_recovery(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "broken.txt"
    source.write_text("broken", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    framework = _framework(source)
    framework._connection.execute(
        "UPDATE file_actions SET effect_receipt_json=?",
        (json.dumps({"schema_version": 1, "operation": "trash"}),),
    )
    framework._connection.commit()
    result = run_document_retirement(
        state,
        root=root,
        framework_state=framework,
        run_id=9,
        framework_lock_held=True,
    )
    assert result["status"] == "recovery_required"
    assert result["recovery_required"] >= 1


def test_existing_owner_with_incompatible_schema_blocks_retirement(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "schema.txt"
    source.write_text("schema", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    connection = sqlite3.connect(state / "text.sqlite3")
    connection.execute("CREATE TABLE documents(path TEXT)")
    connection.commit()
    connection.close()
    framework = _framework(source)
    source.unlink()

    result = run_document_retirement(
        state,
        root=root,
        framework_state=framework,
        run_id=9,
        framework_lock_held=True,
    )
    assert result["status"] == "recovery_required"
    assert result["recovery_required"] >= 1
