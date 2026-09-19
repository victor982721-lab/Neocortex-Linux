"""Regression proposals for catalog lookup and publication observation changes."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

import pytest

import neocortex.documents.document_catalog as catalog
import neocortex.documents.document_catalog_schema as schema
import neocortex.persistence.state_publication as publication
from neocortex.curation.preview import _OrganizationPlanScope, _organization_page_rows
from neocortex.deduplication.persistence.ddl import build_current_schema
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    validate_sqlite_schema_contract,
)
from neocortex.workflow.review.value_review_contracts import ValueReviewQuery
from neocortex.workflow.review.value_review_repository import (
    _inventory_file_page,
    _inventory_files,
    _inventory_heads,
    _text_fingerprint_counts,
)


LOOKUP_INDEXES = {
    "classification_corrections_identity_idx": (
        "logical_identity", "root", "dimension", "correction_id",
    ),
    "catalog_generation_documents_fingerprint_idx": (
        "text_fingerprint", "generation_id", "source_kind",
    ),
    "organization_plans_run_root_idx": (
        "catalog_run_id", "organization_root", "plan_id",
    ),
}


def _legacy_catalog(connection: sqlite3.Connection, version: int) -> None:
    # Build the historical physical shape independently of the current builder.
    for statement in (
        *schema._V8_SCHEMA_DDL,
        *schema._V9_PUBLICATION_DDL,
        *schema._V10_CORRECTION_DDL,
    ):
        connection.execute(statement)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)", (str(version),)
    )
    connection.execute("INSERT INTO metadata VALUES('sentinel','preserved')")
    connection.execute(
        "INSERT INTO classification_corrections("
        "root,logical_identity,dimension,value_json,observed_fingerprint,created_ns) "
        "VALUES('/Source','text:1:2','primary_kind','\"otro\"',?,1)",
        ("a" * 64,),
    )
    connection.commit()


def _assert_lookup_contract(connection: sqlite3.Connection) -> None:
    for name, expected in LOOKUP_INDEXES.items():
        assert tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})")) == expected
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name=?",
        ("catalog_generation_documents_fingerprint_idx",),
    ).fetchone()[0]
    assert "WHERE active=1" in sql
    validate_sqlite_schema_contract(
        connection, schema.document_catalog_schema_contract(), label="catalog", exact=True,
    )


@pytest.mark.parametrize("version", [10, 11])
def test_catalog_lookup_migration_preserves_source_and_makes_backup(
    tmp_path: Path, version: int,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        _legacy_catalog(connection, version)
        correction = connection.execute("SELECT * FROM classification_corrections").fetchone()
        schema.validate_v11_document_catalog_schema(connection)

    assert catalog._read_catalog_version(database) == version
    catalog.initialize_document_catalog(database)

    with catalog.document_catalog_database(database, readonly=True) as connection:
        _assert_lookup_contract(connection)
        assert tuple(connection.execute("SELECT * FROM classification_corrections").fetchone()) == correction
        assert connection.execute("SELECT value FROM metadata WHERE key='sentinel'").fetchone()[0] == "preserved"
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "12"
    backups = tuple(tmp_path.glob(f"document_catalog.sqlite3.pre-v{version}-to-v{version + 1}-*.sqlite3"))
    assert len(backups) == 1
    assert catalog._read_catalog_version(backups[0]) == version
    assert backups[0].with_suffix(".sqlite3.json").is_file()
    before_reopen = database.read_bytes()
    catalog.initialize_document_catalog(database)
    assert database.read_bytes() == before_reopen


def test_fresh_catalog_uses_same_lookup_contract(tmp_path: Path) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    catalog.initialize_document_catalog(database)
    with catalog.document_catalog_database(database, readonly=True) as connection:
        _assert_lookup_contract(connection)


@pytest.mark.parametrize("version", [10, 11])
@pytest.mark.parametrize("damage", ["missing", "extra"])
def test_lookup_migration_rejects_unrecognized_legacy_schema_without_repair(
    tmp_path: Path, version: int, damage: str,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        _legacy_catalog(connection, version)
        if damage == "missing":
            connection.execute("DROP INDEX classification_corrections_lookup_idx")
        else:
            connection.execute("CREATE INDEX unrecognized_extra ON metadata(value)")
        connection.commit()
    original = database.read_bytes()
    with pytest.raises(SQLiteSchemaContractError):
        catalog.initialize_document_catalog(database)
    assert database.read_bytes() == original
    assert not tuple(tmp_path.glob("*.pre-v*"))


def test_lookup_migration_rolls_back_all_indexes_on_late_ddl_failure() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        _legacy_catalog(connection, 11)

        def deny_last_index(action, argument, _second, _database, _trigger):
            if action == sqlite3.SQLITE_CREATE_INDEX and argument == "organization_plans_run_root_idx":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_last_index)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.DatabaseError):
            schema.migrate_document_catalog_schema(
                connection, 11, identity_migrator=lambda _connection: None,
            )
        connection.rollback()
        connection.set_authorizer(None)
        schema.validate_v11_document_catalog_schema(connection)
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "11"


@pytest.mark.parametrize("scope", [None, "/selected", "/selected/sub", "/other", "/absent"])
@pytest.mark.parametrize("page_size", [1, 7, 100])
def test_inventory_cursor_preserves_blob_order_scope_and_overlapping_heads(
    scope: str | None, page_size: int,
) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        build_current_schema(connection)
        for scan_id, root in ((1, "/selected"), (2, "/selected/sub"), (3, "/other")):
            connection.execute(
                "INSERT INTO scans(scan_id,root,started_ns,completed_ns,status) VALUES(?,?,1,2,'complete')",
                (scan_id, root),
            )
            connection.execute(
                "INSERT INTO inventory_checkpoints(root,scan_id,valid,updated_ns) VALUES(?,?,1,?)",
                (root, scan_id, scan_id + 3),
            )
        keys = tuple(
            (volume.to_bytes(16, "little"), file_id.to_bytes(16, "little"), birth)
            for volume in (0, 1, 256, 65536)
            for file_id in (0, 1, 256)
            for birth in (-1, 0, 1)
        )
        for ordinal, (volume, file_id, birth) in enumerate(keys):
            for scan_id, root, present in (
                (1, "/selected", True),
                (2, "/selected/sub", ordinal % 3 == 0),
                (3, "/other", ordinal % 4 == 0),
            ):
                if present:
                    connection.execute(
                        "INSERT INTO files(scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
                        "VALUES(?,?,?,?,1,1,?)",
                        (scan_id, f"{root}/{ordinal:03}.txt", volume, file_id, birth),
                    )
        heads = _inventory_heads(connection)
        query = ValueReviewQuery(scope=scope)
        reference = _inventory_files(connection, query, heads)
        assert reference is not None
        expected = {(item.volume_blob, item.file_blob, item.birthtime_ns): item for item in reference}
        ordered_keys = sorted(expected)
        after = None
        seen = []
        for offset in range(0, max(1, len(ordered_keys)), page_size):
            page, after = _inventory_file_page(
                connection, query, heads, after=after, page_size=page_size,
            )
            page_keys = [(item.volume_blob, item.file_blob, item.birthtime_ns) for item in page]
            assert set(page_keys) == set(ordered_keys[offset:offset + page_size])
            assert all(item == expected[key] for item, key in zip(page, page_keys, strict=True))
            seen.extend(page_keys)
            if offset + page_size < len(ordered_keys):
                boundary = ordered_keys[offset + page_size - 1]
                assert after is not None
                assert (bytes.fromhex(after.volume_id_hex), bytes.fromhex(after.file_id_hex), after.birthtime_ns) == boundary
            else:
                assert after is None
        assert len(seen) == len(set(seen)) == len(expected)


def _generation_document(connection: sqlite3.Connection, generation: int, kind: str,
                         key: str, identity: str, fingerprint: str | None,
                         *, active: int = 1, status: str = "classified") -> None:
    connection.execute(
        """INSERT INTO catalog_generation_documents(
        generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
        source_status,processing_signature,text_fingerprint,classifier_signature,
        primary_kind,confidence,uncertainty,standard_references_json,organizations_json,
        topics_json,classification_json,catalog_status,active,last_seen_catalog_run_id,updated_ns)
        VALUES(?,?,?,?, '1',?,1,1,-1,'complete','v1',?,'classifier','otro',0.5,'alta',
        '[]','[]','[]','{}',?,?,1,1)""",
        (generation, kind, key, f"/selected/{generation}/{key}.txt", identity, fingerprint, status, active),
    )


def test_fingerprint_index_preserves_current_head_and_distinct_physical_identity() -> None:
    fingerprint = "a" * 32
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        schema.create_document_catalog_schema(connection)
        for generation, kind in ((1, "text"), (2, "text"), (3, "pdf")):
            connection.execute(
                "INSERT INTO catalog_generations(generation_id,source_kind,status,started_ns) VALUES(?,?,'building',1)",
                (generation, kind),
            )
        _generation_document(connection, 1, "text", "historical", "99", fingerprint)
        _generation_document(connection, 2, "text", "current", "7", fingerprint)
        _generation_document(connection, 2, "text", "inactive", "8", fingerprint, active=0)
        _generation_document(connection, 2, "text", "error", "9", fingerprint, status="error")
        _generation_document(connection, 2, "text", "missing", "10", None)
        _generation_document(connection, 3, "pdf", "same-identity", "7", fingerprint)
        _generation_document(connection, 3, "pdf", "different-identity", "11", fingerprint)
        connection.execute("UPDATE catalog_generations SET status='published'")
        connection.executemany(
            "INSERT INTO catalog_publications(source_kind,generation_id,published_ns) VALUES(?,?,2)",
            (("text", 2), ("pdf", 3)),
        )
        assert _text_fingerprint_counts(connection, (fingerprint, None, fingerprint, "invalid")) == {fingerprint: 2}


def test_organization_index_preserves_case_run_scope_status_and_cursor() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        schema.create_document_catalog_schema(connection)
        cases = (
            (1, 2, "/organized", "/selected/a", "review"),
            (2, 2, "/organized", "/selected/b", "review"),
            (3, 2, "/organized", "/selected/c", "review"),
            (4, 1, "/organized", "/selected/d", "review"),
            (5, 2, "/Organized", "/selected/e", "review"),
            (6, 2, "/organized", "/selected/f", "superseded"),
            (7, 2, "/organized", "/selected-other/g", "review"),
            (8, None, "/organized", "/selected/h", "review"),
        )
        for plan_id, run_id, root, path, status in cases:
            connection.execute(
                """INSERT INTO organization_plans(
                plan_id,catalog_run_id,source_kind,file_key,source_path,organization_root,
                volume_id,file_id,size,mtime_ns,birthtime_ns,classifier_signature,primary_kind,
                confidence,status,reason,evidence_json,planned_ns)
                VALUES(?,?,'text',?,?,?,'1','1',1,1,-1,'v1','otro',0.5,?,'fixture','{}',1)""",
                (plan_id, run_id, str(plan_id), path, root, status),
            )
        arguments = {
            "inventory_root": "/selected",
            "scope": _OrganizationPlanScope(2, "/organized"),
            "limit": 2,
        }
        first, more = _organization_page_rows(connection, after_plan_id=None, **arguments)
        assert [row["plan_id"] for row in first] == [3, 2]
        assert more
        second, more = _organization_page_rows(connection, after_plan_id=2, **arguments)
        assert [row["plan_id"] for row in second] == [1]
        assert not more


def _publish(state: Path, key: str = "first"):
    return publication.record_state_publication(
        state, operation="catalog-test", owners=("catalog",),
        status="complete", idempotency_key=key,
    )


def test_publication_view_and_locked_replay_parse_journal_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    first = _publish(state)
    journal_path = state / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    before = journal_path.read_bytes()
    original_read = publication._read_journal
    original_lock = publication._publication_lock
    reads = 0
    locked = False
    require_lock = False

    def counted_read(directory):
        nonlocal reads
        reads += 1
        assert not require_lock or locked
        return original_read(directory)

    @contextmanager
    def observed_lock(directory):
        nonlocal locked
        with original_lock(directory):
            locked = True
            try:
                yield
            finally:
                locked = False

    monkeypatch.setattr(publication, "_read_journal", counted_read)
    monkeypatch.setattr(publication, "_publication_lock", observed_lock)
    view = publication.read_state_publication_state(state)
    assert view.status == "complete" and view.publication == first
    assert reads == 1
    reads = 0
    require_lock = True
    assert _publish(state) == first
    assert reads == 1
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["append", "replace-pointer"])
def test_publication_observation_rejects_concurrent_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _publish(state)
    original = publication._read_journal

    def interleaved_read(directory):
        result = original(directory)
        if mutation == "append":
            with (state / publication.STATE_PUBLICATION_JOURNAL_FILENAME).open("ab") as handle:
                handle.write(b"\n")
        else:
            pointer = state / publication.STATE_EPOCH_FILENAME
            replacement = state / "replacement.json"
            replacement.write_bytes(pointer.read_bytes())
            os.replace(replacement, pointer)
        return result

    monkeypatch.setattr(publication, "_read_journal", interleaved_read)
    with pytest.raises(publication.StatePublicationConflictError, match="changed during observation"):
        publication.read_state_publication_state(state)


def test_publication_pointer_lag_recovers_without_repair_and_ahead_rejects(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    pointer = state / publication.STATE_EPOCH_FILENAME
    _publish(state)
    old_pointer = pointer.read_bytes()
    second = _publish(state, "second")
    new_pointer = pointer.read_bytes()
    pointer.write_bytes(old_pointer)
    view = publication.read_state_publication_state(state)
    assert view.epoch.epoch == 2 and view.epoch.source == "journal"
    assert view.publication == second
    assert pointer.read_bytes() == old_pointer
    payload = json.loads(new_pointer)
    payload["epoch"] = 3
    pointer.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(publication.StatePublicationError, match="ahead"):
        publication.read_state_epoch(state)


def test_absent_publication_reader_creates_no_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    assert publication.read_state_publication_state(state).status == "absent"
    assert publication.read_state_epoch(state).epoch == 0
    assert tuple(state.iterdir()) == ()


def test_publication_conflict_is_unavailable_evidence_in_read_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.api.read_api import ReadScope, ScopeBinding, _read_epoch_for_bindings

    def changed(_directory):
        raise publication.StatePublicationConflictError("publication metadata changed during observation")

    monkeypatch.setattr(publication, "read_state_epoch", changed)
    result = _read_epoch_for_bindings(
        (ScopeBinding(ReadScope.PERSONAL, tmp_path),), ReadScope.PERSONAL,
    )
    assert result["scopes"]["personal"]["status"] == "unavailable"
    assert "changed during observation" in result["scopes"]["personal"]["reason"]
