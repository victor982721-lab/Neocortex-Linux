"""Adversarial lifecycle coverage for enumeration and dedup SQLite state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import _02_Deduplicacion.inventory_schema as inventory_schema_module
from _01_Enumeracion.path_index import SqlitePathIndex
from _01_Enumeracion.path_index_schema import (
    initialize_path_index_schema,
    validate_path_index_schema,
)
from _02_Deduplicacion import DedupIndex, InventoryError
from _02_Deduplicacion.inventory_schema import (
    initialize_inventory_schema,
    validate_inventory_schema,
)
from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


def _execute(database: Path, sql: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(sql)


def _metadata(database: Path) -> dict[str, str]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT key,value FROM metadata"))


def _columns(database: Path, table: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


# region [01] Path-index v1 lifecycle


def test_path_index_fresh_schema_is_exact_and_readonly_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paths.sqlite3"
    initialize_path_index_schema(database)
    with sqlite3.connect(database) as connection:
        validate_path_index_schema(connection)
    before = database.read_bytes()

    initialize_path_index_schema(database)

    assert database.read_bytes() == before
    with SqlitePathIndex(database) as index:
        assert index.journal_cursor is None


@pytest.mark.parametrize("raw_version", ("2", "01", "future"))
def test_path_index_rejects_unsupported_or_noncanonical_version_without_mutation(
    tmp_path: Path,
    raw_version: str,
) -> None:
    database = tmp_path / f"paths-{raw_version}.sqlite3"
    initialize_path_index_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (raw_version,),
        )
        connection.commit()
    before = database.read_bytes()

    with pytest.raises(RuntimeError):
        SqlitePathIndex(database)

    assert database.read_bytes() == before


def test_path_index_rejects_false_current_index_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paths-malformed.sqlite3"
    initialize_path_index_schema(database)
    _execute(
        database,
        """
        DROP INDEX nodes_parent_idx;
        CREATE INDEX nodes_parent_idx ON nodes(parent_frn, name);
        """,
    )
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="nodes_parent_idx"):
        SqlitePathIndex(database)

    assert database.read_bytes() == before


# endregion [01]


# region [02] Historical dedup fixtures


_METADATA_FIXTURE = """
CREATE TABLE metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
INSERT INTO metadata VALUES('schema_version', '{version}');
INSERT INTO metadata VALUES('preserved', 'yes');
"""
_SCANS_V2 = """
CREATE TABLE scans(
    scan_id INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    started_ns INTEGER NOT NULL,
    completed_ns INTEGER
);
INSERT INTO scans(scan_id,root,started_ns,completed_ns)
VALUES(7, 'C:\\corpus', 111, 222);
"""
_SCANS_V3 = """
CREATE TABLE scans(
    scan_id INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    started_ns INTEGER NOT NULL,
    completed_ns INTEGER,
    files_seen INTEGER,
    directories_seen INTEGER,
    bytes_seen INTEGER,
    skipped_links INTEGER,
    excluded_directories INTEGER,
    errors INTEGER
);
INSERT INTO scans VALUES(7, 'C:\\corpus', 111, 222, 3, 4, 5, 6, 7, 8);
"""
_CHECKPOINT = """
CREATE TABLE inventory_checkpoints(
    root TEXT PRIMARY KEY COLLATE NOCASE,
    scan_id INTEGER NOT NULL,
    volume TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    next_usn INTEGER NOT NULL,
    valid INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
) WITHOUT ROWID;
INSERT INTO inventory_checkpoints
VALUES('C:\\corpus', 7, 'C:', '9', 10, 1, 11);
"""
_FINGERPRINTS_V4 = """
CREATE TABLE fingerprints(
    volume_id BLOB NOT NULL,
    file_id BLOB NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    algorithm TEXT NOT NULL,
    digest BLOB NOT NULL,
    PRIMARY KEY(volume_id,file_id,size,mtime_ns,algorithm)
) WITHOUT ROWID;
INSERT INTO fingerprints VALUES(X'01', X'02', 3, 4, 'fixture-v1', X'05');
"""
_FINGERPRINTS_V5 = """
CREATE TABLE fingerprints(
    volume_id BLOB NOT NULL,
    file_id BLOB NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    algorithm TEXT NOT NULL,
    digest BLOB NOT NULL,
    birthtime_ns INTEGER NOT NULL DEFAULT -1,
    PRIMARY KEY(volume_id,file_id,size,mtime_ns,algorithm)
) WITHOUT ROWID;
INSERT INTO fingerprints VALUES(X'01', X'02', 3, 4, 'fixture-v1', X'05', 12);
"""


def _historical_fixture(database: Path, version: int) -> None:
    fragments = [_METADATA_FIXTURE.format(version=version)]
    if version == 2:
        fragments.append(_SCANS_V2)
    elif version == 3:
        fragments.extend((_SCANS_V3, _CHECKPOINT))
    elif version == 4:
        fragments.append(_FINGERPRINTS_V4)
    elif version == 5:
        fragments.extend((_SCANS_V3, _CHECKPOINT, _FINGERPRINTS_V5))
    _execute(database, "".join(fragments))


@pytest.mark.parametrize("version", range(1, 6))
def test_dedup_migrates_each_historical_version_and_preserves_rows(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / f"dedup-v{version}.sqlite3"
    _historical_fixture(database, version)

    initialize_inventory_schema(database)

    assert _metadata(database) == {"preserved": "yes", "schema_version": "10"}
    with sqlite3.connect(database) as connection:
        validate_inventory_schema(connection)
        if version in {2, 3, 5}:
            scan = connection.execute(
                "SELECT root,started_ns,completed_ns FROM scans WHERE scan_id=7"
            ).fetchone()
            assert scan == ("C:\\corpus", 111, 222)
        if version in {3, 5}:
            valid = connection.execute(
                "SELECT valid FROM inventory_checkpoints WHERE root='C:\\corpus'"
            ).fetchone()[0]
            assert valid == 0
        if version == 4:
            fingerprint = connection.execute(
                "SELECT algorithm,digest,birthtime_ns FROM fingerprints"
            ).fetchone()
            assert fingerprint == ("fixture-v1", b"\x05", -1)
        if version == 5:
            fingerprint = connection.execute(
                "SELECT algorithm,digest,birthtime_ns FROM fingerprints"
            ).fetchone()
            assert fingerprint == ("fixture-v1", b"\x05", 12)


def test_dedup_records_every_sequential_version_inside_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "dedup-sequential.sqlite3"
    _historical_fixture(database, 1)
    traces: list[str] = []
    original_connect = inventory_schema_module._connect

    def traced_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        connection = original_connect(path, readonly=readonly)
        if not readonly:
            connection.set_trace_callback(traces.append)
        return connection

    monkeypatch.setattr(inventory_schema_module, "_connect", traced_connect)

    initialize_inventory_schema(database)

    updates = [
        statement for statement in traces if statement.startswith("UPDATE metadata SET value=")
    ]
    assert [
        f"'{version}'" in statement
        for version, statement in zip(range(2, 11), updates, strict=True)
    ] == [
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    ]


def test_dedup_rolls_back_all_steps_when_final_contract_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dedup-rollback.sqlite3"
    _execute(
        database,
        _METADATA_FIXTURE.format(version=4)
        + """
        CREATE TABLE scans(scan_id TEXT PRIMARY KEY, root TEXT NOT NULL);
        INSERT INTO scans VALUES('sentinel', 'C:\\corpus');
        """
        + _FINGERPRINTS_V4,
    )

    with pytest.raises(InventoryError, match="schema contract"):
        initialize_inventory_schema(database)

    assert _metadata(database)["schema_version"] == "4"
    assert "birthtime_ns" not in _columns(database, "fingerprints")
    assert "root_volume_id" not in _columns(database, "scans")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM scans").fetchone() == (
            "sentinel",
            "C:\\corpus",
        )


# endregion [02]


# region [03] Dedup read-only rejection and exact index semantics


@pytest.mark.parametrize("raw_version", ("11", "09", "future"))
def test_dedup_rejects_unsupported_or_noncanonical_version_without_mutation(
    tmp_path: Path,
    raw_version: str,
) -> None:
    database = tmp_path / f"dedup-{raw_version}.sqlite3"
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (raw_version,),
        )
        connection.commit()
    before = database.read_bytes()

    with pytest.raises(InventoryError):
        DedupIndex(database)

    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "index_name,replacement",
    (
        (
            "planned_groups_scan_order_idx",
            """CREATE INDEX planned_groups_scan_order_idx
            ON planned_duplicate_groups(scan_id,reclaimable_bytes,keep_path)""",
        ),
        (
            "planned_members_path_idx",
            """CREATE INDEX planned_members_path_idx
            ON planned_duplicate_members(path,role)""",
        ),
        (
            "files_identity_idx",
            """CREATE INDEX files_identity_idx ON files(volume_id,file_id)
            WHERE file_id IS NOT NULL""",
        ),
        (
            "files_identity_birth_scan_idx",
            """CREATE INDEX files_identity_birth_scan_idx
            ON files(volume_id,file_id,scan_id,birthtime_ns)""",
        ),
        (
            "planned_members_identity_idx",
            """CREATE INDEX planned_members_identity_idx
            ON planned_duplicate_members(volume_id,file_id)""",
        ),
    ),
)
def test_dedup_rejects_false_current_index_semantics_without_mutation(
    tmp_path: Path,
    index_name: str,
    replacement: str,
) -> None:
    database = tmp_path / f"dedup-{index_name}.sqlite3"
    initialize_inventory_schema(database)
    _execute(database, f"DROP INDEX {index_name}; {replacement};")
    before = database.read_bytes()

    with pytest.raises(InventoryError, match=index_name):
        DedupIndex(database)

    assert database.read_bytes() == before


def test_dedup_current_schema_is_readonly_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "dedup-current.sqlite3"
    initialize_inventory_schema(database)
    before = database.read_bytes()

    initialize_inventory_schema(database)

    assert database.read_bytes() == before


# endregion [03]


# region [04] Neutral contract fidelity


def _relational_schema(
    index_sql: str,
    *,
    foreign_key: bool = True,
) -> Callable[[sqlite3.Connection], None]:
    def build(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE parent(parent_id INTEGER PRIMARY KEY);
            CREATE TABLE child(
                child_id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL,
                priority INTEGER NOT NULL,
                active INTEGER NOT NULL
            """
            + (
                ",FOREIGN KEY(parent_id) REFERENCES parent(parent_id) ON DELETE CASCADE"
                if foreign_key
                else ""
            )
            + ");"
            + index_sql
        )

    return build


@pytest.mark.parametrize(
    "actual_index,foreign_key",
    (
        (
            """CREATE INDEX child_rank_idx
            ON child(parent_id COLLATE BINARY,priority) WHERE active=1;""",
            True,
        ),
        (
            """CREATE INDEX child_rank_idx
            ON child(parent_id COLLATE NOCASE,priority DESC) WHERE active=1;""",
            False,
        ),
        (
            """CREATE INDEX child_rank_idx
            ON child(parent_id COLLATE NOCASE,priority DESC) WHERE active=0;""",
            True,
        ),
    ),
)
def test_neutral_contract_captures_desc_collation_where_and_foreign_keys(
    actual_index: str,
    foreign_key: bool,
) -> None:
    expected_builder = _relational_schema(
        """CREATE INDEX child_rank_idx
        ON child(parent_id COLLATE NOCASE,priority DESC) WHERE active=1;"""
    )
    expected = schema_contract_from_builder(expected_builder)
    with sqlite3.connect(":memory:") as connection:
        _relational_schema(actual_index, foreign_key=foreign_key)(connection)
        with pytest.raises(SQLiteSchemaContractError, match=r"incompatible|lacks"):
            validate_sqlite_schema_contract(
                connection,
                expected,
                label="relational fixture",
                exact=True,
            )


# endregion [04]
