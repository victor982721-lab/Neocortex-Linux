"""Scalable Text FTS identity lookups without weakening publication evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationIntegrityError,
    _validated_terminal_receipts,
    read_reusable_text_derivation_from_connection,
)
from neocortex.capabilities.formats.text.text_fts_lookup import (
    delete_text_fts_for_file_key,
    initialize_text_fts_lookup,
    prune_text_fts_for_run,
    record_text_fts_row,
    text_fts_file_key_predicate,
)
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken


def _fts_connection(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE document_fts USING fts5(file_key UNINDEXED,body)"
    )
    return connection


def _rows(connection: sqlite3.Connection, *file_keys: str) -> list[tuple]:
    predicate, parameters = text_fts_file_key_predicate(connection, file_keys)
    return connection.execute(
        f"SELECT rowid,file_key,body FROM document_fts WHERE {predicate} ORDER BY rowid",
        parameters,
    ).fetchall()


def _vm_steps(
    connection: sqlite3.Connection,
    operation: Callable[[], list[tuple]],
) -> tuple[int, list[tuple]]:
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(count, 1)
    try:
        result = operation()
    finally:
        connection.set_progress_handler(None, 0)
    return steps, result


def _assert_exact_lookup(connection: sqlite3.Connection) -> None:
    assert connection.execute(
        "SELECT fts_rowid,file_key FROM temp._text_fts_row_lookup ORDER BY fts_rowid"
    ).fetchall() == connection.execute(
        "SELECT rowid,file_key FROM document_fts ORDER BY rowid"
    ).fetchall()


def test_fts_key_lookup_work_stays_bounded_as_index_grows() -> None:
    """VM operations prove the access path; no machine-dependent timing gate."""

    indexed_costs = []
    legacy_costs = []
    missing_costs = []
    for count in (100, 5_000):
        connection = _fts_connection()
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO document_fts(file_key,body) VALUES(?,?)",
                    ((f"key-{index}", "stable searchable text") for index in range(count)),
                )
            initialize_text_fts_lookup(connection)
            key = f"key-{count - 1}"
            steps, rows = _vm_steps(
                connection, lambda connection=connection, key=key: _rows(connection, key)
            )
            assert len(rows) == 1
            indexed_costs.append(steps)
            steps, rows = _vm_steps(
                connection, lambda connection=connection: _rows(connection, "absent")
            )
            assert rows == []
            missing_costs.append(steps)
            steps, rows = _vm_steps(
                connection,
                lambda connection=connection, key=key: connection.execute(
                    "SELECT rowid,file_key,body FROM document_fts WHERE file_key=?",
                    (key,),
                ).fetchall(),
            )
            assert len(rows) == 1
            legacy_costs.append(steps)
        finally:
            connection.close()
    assert indexed_costs[1] <= indexed_costs[0] * 2 + 100
    assert missing_costs[1] <= missing_costs[0] * 2 + 100
    assert legacy_costs[1] > legacy_costs[0] * 20
    assert indexed_costs[1] * 20 < legacy_costs[1]


def test_lookup_preserves_duplicate_and_anomalous_keys() -> None:
    connection = _fts_connection()
    try:
        with connection:
            connection.executemany(
                "INSERT INTO document_fts(file_key,body) VALUES(?,?)",
                ((key, "body") for key in ("duplicate", "duplicate", None, 17, b"raw")),
            )
        initialize_text_fts_lookup(connection)
        _assert_exact_lookup(connection)
        assert len(_rows(connection, "duplicate")) == 2
        assert _rows(connection) == []
        with connection:
            delete_text_fts_for_file_key(connection, "duplicate")
        _assert_exact_lookup(connection)
        assert _rows(connection, "duplicate") == []
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 3
    finally:
        connection.close()


def test_lookup_insert_and_delete_share_fts_transaction_rollback() -> None:
    connection = _fts_connection()
    try:
        with connection:
            connection.execute("INSERT INTO document_fts VALUES('old','prior text')")
        initialize_text_fts_lookup(connection)
        connection.execute("BEGIN")
        cursor = connection.execute("INSERT INTO document_fts VALUES('new','next text')")
        assert cursor.lastrowid is not None
        record_text_fts_row(connection, cursor.lastrowid, "new")
        delete_text_fts_for_file_key(connection, "old")
        assert _rows(connection, "old") == []
        assert len(_rows(connection, "new")) == 1
        _assert_exact_lookup(connection)
        connection.rollback()
        assert len(_rows(connection, "old")) == 1
        assert _rows(connection, "new") == []
        _assert_exact_lookup(connection)
        with connection:
            cursor = connection.execute("INSERT INTO document_fts VALUES('new','next text')")
            assert cursor.lastrowid is not None
            record_text_fts_row(connection, cursor.lastrowid, "new")
        _assert_exact_lookup(connection)
        assert len(_rows(connection, "old", "new")) == 2
    finally:
        connection.close()


def test_lookup_pruning_preserves_current_and_orphan_rows_and_rolls_back() -> None:
    connection = _fts_connection()
    try:
        connection.execute("CREATE TABLE documents(file_key TEXT PRIMARY KEY,last_seen_run_id)")
        with connection:
            connection.executemany("INSERT INTO documents VALUES(?,?)", (("old", 1), ("live", 2)))
            connection.executemany(
                "INSERT INTO document_fts VALUES(?,?)",
                ((key, "body") for key in ("old", "old", "live", "orphan")),
            )
        initialize_text_fts_lookup(connection)
        connection.execute("BEGIN")
        prune_text_fts_for_run(connection, 2)
        assert _rows(connection, "old") == []
        assert len(_rows(connection, "live", "orphan")) == 2
        _assert_exact_lookup(connection)
        connection.rollback()
        assert len(_rows(connection, "old")) == 2
        _assert_exact_lookup(connection)
        with connection:
            prune_text_fts_for_run(connection, 2)
        _assert_exact_lookup(connection)
        assert len(_rows(connection, "live", "orphan")) == 2
    finally:
        connection.close()


def test_initialization_never_commits_a_callers_pending_transaction() -> None:
    connection = _fts_connection()
    try:
        connection.execute("INSERT INTO document_fts VALUES('pending','uncommitted')")
        with pytest.raises(ValueError, match="idle connection"):
            initialize_text_fts_lookup(connection)
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 0
    finally:
        connection.close()


def test_readonly_query_falls_back_without_creating_temp_state(tmp_path: Path) -> None:
    database = tmp_path / "fts.sqlite3"
    connection = _fts_connection(database)
    with connection:
        connection.execute("INSERT INTO document_fts VALUES('key','text')")
    connection.close()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as reader:
        reader.execute("PRAGMA query_only=ON")
        assert len(_rows(reader, "key")) == 1
        assert _rows(reader, "missing") == []
        assert reader.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0] == 0
        assert reader.total_changes == 0
        assert not reader.in_transaction


def test_lookup_rebuild_does_not_change_main_schema_or_persist(tmp_path: Path) -> None:
    database = tmp_path / "fts.sqlite3"
    connection = _fts_connection(database)
    try:
        with connection:
            connection.execute("INSERT INTO document_fts VALUES('old','text')")
        schema = connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        before = database.read_bytes()
        initialize_text_fts_lookup(connection)
        assert database.read_bytes() == before
        assert not connection.in_transaction
        assert connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == schema
        with connection:
            connection.execute("DELETE FROM document_fts")
            connection.execute("INSERT INTO document_fts VALUES('replacement','other')")
        initialize_text_fts_lookup(connection)
        _assert_exact_lookup(connection)
        assert _rows(connection, "old") == []
        assert len(_rows(connection, "replacement")) == 1
    finally:
        connection.close()
    with sqlite3.connect(database) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0] == 0
        assert len(_rows(reopened, "replacement")) == 1


def _add_lineage_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE text_materialization_heads("
        "resource_id TEXT,materialization_kind TEXT,materialization_owner TEXT,"
        "materialization_id TEXT,revision_id TEXT,producer_receipt_id TEXT,updated_ns INTEGER,"
        "PRIMARY KEY(resource_id,materialization_kind)) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TABLE text_materializations(owner TEXT,materialization_id TEXT,revision_id TEXT,"
        "PRIMARY KEY(owner,materialization_id)) WITHOUT ROWID"
    )


def _assert_exact_lineage_lookups(connection: sqlite3.Connection) -> None:
    for table, lookup in (
        ("text_materialization_heads", "_text_materialization_heads_lookup"),
        ("text_materializations", "_text_materializations_lookup"),
    ):
        assert connection.execute(f"SELECT * FROM temp.{lookup} ORDER BY 1,2").fetchall() == (
            connection.execute(f"SELECT * FROM main.{table} ORDER BY 1,2").fetchall()
        )


def test_lineage_mirrors_track_insert_update_delete_and_transaction_rollback() -> None:
    connection = _fts_connection()
    try:
        _add_lineage_tables(connection)
        with connection:
            connection.executemany(
                "INSERT INTO text_materializations VALUES('text',?,?)",
                (("existing", "rev-existing"), ("deleted", "rev-deleted")),
            )
            connection.executemany(
                "INSERT INTO text_materialization_heads VALUES(?,?,?,?,?,?,?)",
                (
                    ("resource-1", "body", "text", "existing", "rev-existing", "receipt-1", 1),
                    ("resource-2", "body", "text", "deleted", "rev-deleted", "receipt-2", 2),
                ),
            )
        schema = connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        initialize_text_fts_lookup(connection)
        assert connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == schema
        _assert_exact_lineage_lookups(connection)
        for commit in (False, True):
            connection.execute("BEGIN")
            connection.execute("INSERT INTO text_materializations VALUES('text','new','rev-new')")
            connection.execute(
                "INSERT INTO text_materialization_heads VALUES("
                "'resource-3','body','text','new','rev-new','receipt-3',3)"
            )
            connection.execute(
                "UPDATE text_materialization_heads SET resource_id='renamed',updated_ns=10 "
                "WHERE resource_id='resource-1'"
            )
            connection.execute(
                "UPDATE text_materializations SET revision_id='changed' "
                "WHERE materialization_id='existing'"
            )
            connection.execute("DELETE FROM text_materialization_heads WHERE resource_id='resource-2'")
            connection.execute("DELETE FROM text_materializations WHERE materialization_id='deleted'")
            _assert_exact_lineage_lookups(connection)
            if commit:
                connection.commit()
            else:
                connection.rollback()
            _assert_exact_lineage_lookups(connection)
        assert connection.execute(
            "SELECT resource_id FROM text_materialization_heads ORDER BY resource_id"
        ).fetchall() == [("renamed",), ("resource-3",)]
    finally:
        connection.close()


def test_lineage_alternate_key_lookups_stay_bounded_as_history_grows() -> None:
    costs: list[list[int]] = [[], []]
    for count in (100, 5_000):
        connection = _fts_connection()
        try:
            _add_lineage_tables(connection)
            with connection:
                connection.executemany(
                    "INSERT INTO text_materializations VALUES('text',?,?)",
                    ((f"mat-{index}", f"rev-{index}") for index in range(count)),
                )
                connection.executemany(
                    "INSERT INTO text_materialization_heads VALUES(?, 'body', 'text', ?, ?, ?, ?)",
                    (
                        (f"resource-{index}", f"mat-{index}", f"rev-{index}", f"receipt-{index}", index)
                        for index in range(count)
                    ),
                )
            initialize_text_fts_lookup(connection)
            for position, (sql, parameters) in enumerate((
                (
                    "SELECT resource_id FROM temp._text_materialization_heads_lookup "
                    "WHERE materialization_owner=? AND materialization_id=?",
                    ("text", f"mat-{count - 1}"),
                ),
                (
                    "SELECT owner,materialization_id FROM temp._text_materializations_lookup "
                    "WHERE revision_id=?",
                    (f"rev-{count - 1}",),
                ),
            )):
                steps, rows = _vm_steps(
                    connection,
                    lambda connection=connection, sql=sql, parameters=parameters: (
                        connection.execute(sql, parameters).fetchall()
                    ),
                )
                assert len(rows) == 1
                costs[position].append(steps)
        finally:
            connection.close()
    assert all(large <= small * 2 + 100 for small, large in costs)


class _OneTextCandidate:
    def __init__(self, path: Path) -> None:
        self.snapshot = snapshot_path(path)

    def selected_route_candidate_counts(self, _run, mime, _bound, _route, _selection):
        return (1, 1) if mime == "text/plain" else (0, 0)

    def iter_selected_route_candidates(self, _run, mime, _route, _selection):
        if mime == "text/plain":
            yield self.snapshot


def test_route_reextracts_duplicate_fts_corruption_instead_of_hiding_it(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("protection evidence", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    candidates = _OneTextCandidate(source)

    def run(run_id: int):
        return TextRoute(
            TextRouteConfig(state_path=state),
            candidates,
            run_id,
            cancellation=CancellationToken(),
        ).run()

    assert run(1).extracted == 1
    with sqlite3.connect(state) as connection:
        connection.execute(
            "INSERT INTO document_fts(file_key,path,content_kind,title,author,body) "
            "SELECT file_key,path,content_kind,title,author,body FROM document_fts"
        )
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 2
    repaired = run(2)
    assert repaired.cache_hits == 0
    assert repaired.extracted == 1
    assert repaired.errors == 0
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 1
    replay = run(3)
    assert replay.cache_hits == 1
    assert replay.extracted == 0


@pytest.mark.parametrize(
    ("use_lookup", "initialize_before_alias"),
    ((False, False), (True, False), (True, True)),
)
def test_wrong_resource_head_alias_remains_an_integrity_error(
    tmp_path: Path, use_lookup: bool, initialize_before_alias: bool,
) -> None:
    source = tmp_path / "document.txt"
    source.write_text("lineage integrity evidence", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    result = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(source),
        1,
        cancellation=CancellationToken(),
    ).run()
    assert result.extracted == 1
    connection = sqlite3.connect(state)
    connection.row_factory = sqlite3.Row
    try:
        receipt_id = str(connection.execute("SELECT receipt_id FROM text_work_receipts").fetchone()[0])
        if initialize_before_alias:
            initialize_text_fts_lookup(connection)
        with connection:
            connection.execute(
                "INSERT INTO text_materialization_heads "
                "SELECT 'wrong-resource',materialization_kind,materialization_owner,"
                "materialization_id,revision_id,producer_receipt_id,updated_ns "
                "FROM text_materialization_heads LIMIT 1"
            )
        if use_lookup and not initialize_before_alias:
            initialize_text_fts_lookup(connection)
        if use_lookup:
            assert connection.execute(
                "SELECT COUNT(*) FROM temp._text_materialization_heads_lookup"
            ).fetchone()[0] == 3
        with pytest.raises(TextDerivationIntegrityError):
            _validated_terminal_receipts(connection, (receipt_id,), lookup_available=use_lookup)
    finally:
        connection.close()


def test_reusable_derivation_matches_with_and_without_connection_lookups(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("reusable publication evidence", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    summary = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(source),
        1,
        cancellation=CancellationToken(),
    ).run()
    assert summary.extracted == 1
    connection = sqlite3.connect(state)
    connection.row_factory = sqlite3.Row
    try:
        file_key, signature = connection.execute(
            "SELECT file_key,processing_signature FROM documents"
        ).fetchone()
        fallback = read_reusable_text_derivation_from_connection(
            connection, file_key, stage_id="text.extract", processing_signature=signature,
        )
        assert fallback is not None
        initialize_text_fts_lookup(connection)
        indexed = read_reusable_text_derivation_from_connection(
            connection, file_key, stage_id="text.extract", processing_signature=signature,
        )
        assert indexed == fallback
        assert indexed is not None
        assert len(indexed.outputs) == 2
    finally:
        connection.close()
