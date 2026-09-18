"""Format replay has bounded identity work and exact transactional row maps."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.audio.route import _audio_fts_matches
from neocortex.capabilities.formats.fts_lookup import (
    delete_format_fts_keys,
    format_fts_key_predicate,
    initialize_format_fts_lookup,
    insert_format_fts_row,
    refresh_format_fts_path,
)
from neocortex.capabilities.formats.office.models import ExtractedOfficeDocument
from neocortex.capabilities.formats.office.state import (
    _refresh_cached_path,
    _store_success,
    initialize_office_state,
    office_database,
)
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken


def _connection(table: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE VIRTUAL TABLE {table} USING fts5("
        "file_key UNINDEXED,path UNINDEXED,title,body)"
    )
    return conn


def _rows(conn: sqlite3.Connection, table: str, key: object) -> list[tuple]:
    predicate, parameters = format_fts_key_predicate(conn, table, (key,))
    return [tuple(row) for row in conn.execute(
        f"SELECT rowid,* FROM {table} WHERE {predicate} ORDER BY rowid", parameters
    )]


@pytest.mark.parametrize("table", ("document_fts", "transcript_fts", "frame_fts"))
def test_lookup_preserves_duplicates_and_rollback(table: str) -> None:
    with _connection(table) as conn:
        conn.executemany(
            f"INSERT INTO {table} VALUES(?,?,?,?)",
            ((key, "path", "title", "body") for key in ("same", "same", None, 17, b"raw")),
        )
        conn.commit()
        initialize_format_fts_lookup(conn, table)
        assert len(_rows(conn, table, "same")) == 2
        assert len(_rows(conn, table, 17)) == 1
        assert len(_rows(conn, table, b"raw")) == 1
        before = [tuple(row) for row in conn.execute(f"SELECT rowid,* FROM {table}")]
        conn.execute("BEGIN")
        delete_format_fts_keys(conn, table, ("same",))
        insert_format_fts_row(
            conn, table, ("file_key", "path", "title", "body"),
            ("new", "path", "title", "new text"),
        )
        assert not _rows(conn, table, "same")
        assert len(_rows(conn, table, "new")) == 1
        conn.rollback()
        assert len(_rows(conn, table, "same")) == 2
        assert not _rows(conn, table, "new")
        assert [tuple(row) for row in conn.execute(f"SELECT rowid,* FROM {table}")] == before


def test_audio_cache_lookup_work_does_not_scale_with_unrelated_rows() -> None:
    costs = []
    for count in (100, 5_000):
        with _connection("transcript_fts") as conn:
            conn.executemany(
                "INSERT INTO transcript_fts VALUES(?,?,?,?)",
                ((f"key-{index}", "path", "title", "text") for index in range(count)),
            )
            conn.commit()
            initialize_format_fts_lookup(conn, "transcript_fts")
            steps = 0

            def count_step() -> int:
                nonlocal steps
                steps += 1
                return 0

            conn.set_progress_handler(count_step, 1)
            assert _audio_fts_matches(conn, f"key-{count - 1}", "path", "title", "text")
            conn.set_progress_handler(None, 0)
            costs.append(steps)
    assert costs[1] <= costs[0] * 2 + 50
    assert costs[1] < 1_000


@pytest.mark.parametrize("table", ("document_fts", "transcript_fts", "frame_fts"))
def test_same_path_is_noop_but_rename_updates_every_duplicate(table: str) -> None:
    with _connection(table) as conn:
        conn.executemany(
            f"INSERT INTO {table} VALUES('key','old','title','body')", ((), ()),
        )
        conn.commit()
        initialize_format_fts_lookup(conn, table)
        changes = conn.total_changes
        refresh_format_fts_path(conn, table, "key", "old")
        assert conn.total_changes == changes
        refresh_format_fts_path(conn, table, "key", "new")
        assert [row[2] for row in _rows(conn, table, "key")] == ["new", "new"]


def test_lookup_initialization_cancellation_rolls_back_exactly() -> None:
    with _connection("document_fts") as conn:
        conn.execute("INSERT INTO document_fts VALUES('key','path','title','text')")
        conn.commit()

        def stop() -> None:
            raise CancellationRequested("fixture cancellation")

        with pytest.raises(CancellationRequested):
            initialize_format_fts_lookup(conn, "document_fts", checkpoint=stop)
        assert not conn.in_transaction
        assert len(_rows(conn, "document_fts", "key")) == 1
        initialize_format_fts_lookup(conn, "document_fts")
        assert len(_rows(conn, "document_fts", "key")) == 1


def test_lookup_index_build_checks_cancellation_inside_sql() -> None:
    with _connection("document_fts") as conn:
        conn.executemany(
            "INSERT INTO document_fts VALUES(?, 'path', 'title', 'text')",
            ((str(index),) for index in range(10_000)),
        )
        conn.commit()
        token = CancellationToken()
        index_started = []

        def trace(statement: str) -> None:
            if statement.startswith("CREATE INDEX temp."):
                index_started.append(True)
                token.cancel()

        conn.set_trace_callback(trace)
        with pytest.raises(CancellationRequested):
            initialize_format_fts_lookup(conn, "document_fts", checkpoint=token.checkpoint)
        conn.set_trace_callback(None)
        assert index_started == [True]
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 10_000
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_temp_master WHERE name LIKE '_format_%'"
        ).fetchone()[0] == 0


def test_office_intact_fts_is_not_repaired_but_corruption_is(tmp_path: Path) -> None:
    source = tmp_path / "fixture.odt"
    source.write_bytes(b"identity fixture")
    snapshot = snapshot_path(source)
    database = tmp_path / "office.sqlite3"
    initialize_office_state(database)
    with office_database(database) as conn:
        _store_success(
            conn, snapshot, ExtractedOfficeDocument("odt", "Title", "Author", "", "Body", 1),
            "fixture-signature", 1,
        )
        conn.commit()
        initialize_format_fts_lookup(conn, "document_fts")
        statements = []
        conn.set_trace_callback(statements.append)
        assert _refresh_cached_path(conn, snapshot, "odt", 2) is False
        assert not any(statement.startswith(("DELETE FROM document_fts", "INSERT INTO document_fts")) for statement in statements)
        conn.set_trace_callback(None)
        conn.execute("UPDATE document_fts SET body='damaged'")
        assert _refresh_cached_path(conn, snapshot, "odt", 3) is True
        assert conn.execute("SELECT body FROM document_fts").fetchone()[0] == "Body"
