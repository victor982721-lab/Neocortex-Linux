"""Shared structural validation for route-local SQLite state."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest
from neocortex import sqlite_schema_contract as shared_schema_contract

from _04_Nucleo_Operativo.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    capture_sqlite_schema_contract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


def _build_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version','2');
        CREATE TABLE parent(
            parent_id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        CREATE TABLE child(
            child_id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            FOREIGN KEY(parent_id) REFERENCES parent(parent_id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE UNIQUE INDEX child_parent_ordinal_idx
            ON child(parent_id,ordinal);
        """
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@pytest.mark.parametrize(
    ("source", "start", "value", "end"),
    (
        ("0", 0, "0", 1),
        ("123 rest", 0, "123", 3),
        ("1_2_3", 0, "1_2_3", 5),
        ("0xCA_FE!", 0, "0xca_fe", 7),
        ("0X", 0, "0x", 2),
        ("0x_G", 0, "0x_", 3),
        ("0x1g", 0, "0x1", 3),
        (".5,", 0, ".5", 2),
        ("1.", 0, "1.", 2),
        ("12.34E+05tail", 0, "12.34e+05", 9),
        ("1E-9", 0, "1e-9", 4),
        ("1e+", 0, "1", 1),
        ("1e+x", 0, "1", 1),
        ("1e_", 0, "1e_", 3),
        ("1e+_", 0, "1e+_", 4),
        ("12..3", 0, "12.", 3),
        ("00x12", 0, "00", 2),
        ("١٢.٣E٤", 0, "١٢.٣e٤", 6),
        ("xx12.5yy", 2, "12.5", 6),
        ("-12", 1, "12", 3),
        ("", 0, "", 0),
        ("1", 1, "", 1),
        ("1", 2, "", 2),
    ),
)
def test_numeric_sql_token_signature_determinism_and_edge_semantics(
    source: str,
    start: int,
    value: str,
    end: int,
) -> None:
    assert str(inspect.signature(shared_schema_contract._read_numeric_sql_token)) == (
        "(source: 'str', start: 'int') -> 'tuple[_SQLToken, int]'"
    )
    expected = (shared_schema_contract._SQLToken("number", value), end)

    assert shared_schema_contract._read_numeric_sql_token(source, start) == expected
    assert shared_schema_contract._read_numeric_sql_token(source, start) == expected


def test_numeric_sql_tokens_preserve_caller_boundaries() -> None:
    tokens = shared_schema_contract._tokenize_schema_sql(
        "VALUES(-1,+.5,6.02E23,0XCA_FE,1e+,1e_,12..3)"
    )

    assert tuple((token.kind, token.value) for token in tokens) == (
        ("word", "VALUES"),
        ("symbol", "("),
        ("symbol", "-"),
        ("number", "1"),
        ("symbol", ","),
        ("symbol", "+"),
        ("number", ".5"),
        ("symbol", ","),
        ("number", "6.02e23"),
        ("symbol", ","),
        ("number", "0xca_fe"),
        ("symbol", ","),
        ("number", "1"),
        ("word", "e"),
        ("symbol", "+"),
        ("symbol", ","),
        ("number", "1e_"),
        ("symbol", ","),
        ("number", "12."),
        ("number", ".3"),
        ("symbol", ")"),
    )


def test_matching_contract_and_canonical_version_are_accepted() -> None:
    expected = schema_contract_from_builder(_build_schema)
    with _connection() as connection:
        _build_schema(connection)
        validate_sqlite_schema_contract(connection, expected, label="fixture")
        assert read_metadata_schema_version(connection, label="fixture") == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "DROP TABLE child",
        "DROP INDEX child_parent_ordinal_idx",
    ),
)
def test_missing_required_objects_are_rejected(mutation: str) -> None:
    expected = schema_contract_from_builder(_build_schema)
    with _connection() as connection:
        _build_schema(connection)
        connection.execute(mutation)
        with pytest.raises(SQLiteSchemaContractError, match="schema contract"):
            validate_sqlite_schema_contract(connection, expected, label="fixture")


def test_incompatible_columns_and_table_options_are_rejected() -> None:
    expected = schema_contract_from_builder(_build_schema)
    with _connection() as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            INSERT INTO metadata VALUES('schema_version','2');
            CREATE TABLE parent(parent_id TEXT PRIMARY KEY,label TEXT NOT NULL);
            CREATE TABLE child(
                child_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL,
                ordinal TEXT NOT NULL
            );
            CREATE UNIQUE INDEX child_parent_ordinal_idx
                ON child(parent_id,ordinal);
            """
        )
        with pytest.raises(SQLiteSchemaContractError, match="incompatible"):
            validate_sqlite_schema_contract(connection, expected, label="fixture")


def test_same_named_index_with_different_collation_is_incompatible() -> None:
    expected = schema_contract_from_builder(_build_schema)
    with _connection() as connection:
        _build_schema(connection)
        connection.executescript(
            """
            DROP INDEX child_parent_ordinal_idx;
            CREATE UNIQUE INDEX child_parent_ordinal_idx
                ON child(parent_id COLLATE NOCASE,ordinal);
            """
        )
        with pytest.raises(
            SQLiteSchemaContractError,
            match="incompatible indexes child_parent_ordinal_idx",
        ):
            validate_sqlite_schema_contract(
                connection,
                expected,
                label="fixture",
                exact=True,
            )


@pytest.mark.parametrize("raw", ("02", "-1", "two", " 2"))
def test_noncanonical_metadata_versions_are_rejected(raw: str) -> None:
    with _connection() as connection:
        connection.executescript(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO metadata VALUES('schema_version',?)", (raw,))
        with pytest.raises(SQLiteSchemaContractError, match="canonical"):
            read_metadata_schema_version(connection, label="fixture")


def test_absent_metadata_version_is_distinct_from_zero() -> None:
    with _connection() as connection:
        assert read_metadata_schema_version(connection, label="fixture") is None
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        assert read_metadata_schema_version(connection, label="fixture") is None


def test_malformed_metadata_table_is_rejected_explicitly() -> None:
    with _connection() as connection:
        connection.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY)")
        with pytest.raises(SQLiteSchemaContractError, match="metadata table"):
            read_metadata_schema_version(connection, label="fixture")


# region [02] Canonical table-definition contracts


def _contract_from_sql(sql: str):
    def build(connection: sqlite3.Connection) -> None:
        connection.executescript(sql)

    return schema_contract_from_builder(build)


def _assert_incompatible_definition(expected_sql: str, actual_sql: str) -> None:
    expected = _contract_from_sql(expected_sql)
    with _connection() as connection:
        connection.executescript(actual_sql)
        with pytest.raises(
            SQLiteSchemaContractError,
            match="incompatible definition",
        ):
            validate_sqlite_schema_contract(connection, expected, label="fixture")


@pytest.mark.parametrize(
    ("expected_sql", "actual_sql"),
    (
        (
            "CREATE TABLE fixture(value INTEGER NOT NULL CHECK(value>0))",
            "CREATE TABLE fixture(value INTEGER NOT NULL)",
        ),
        (
            """CREATE TABLE fixture(
                label TEXT COLLATE NOCASE,
                ordinal INTEGER CHECK(ordinal>=0)
            )""",
            "CREATE TABLE fixture(label TEXT,ordinal INTEGER)",
        ),
        (
            """CREATE TABLE fixture(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL ON CONFLICT FAIL CHECK(length(label)>0)
            )""",
            """CREATE TABLE fixture(
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL
            )""",
        ),
        (
            """CREATE TABLE fixture(
                source INTEGER NOT NULL,
                derived INTEGER GENERATED ALWAYS AS (source+1) STORED
            )""",
            """CREATE TABLE fixture(
                source INTEGER NOT NULL,
                derived INTEGER GENERATED ALWAYS AS (source+2) STORED
            )""",
        ),
        (
            """CREATE TABLE parent(parent_id INTEGER PRIMARY KEY);
            CREATE TABLE fixture(
                parent_id INTEGER REFERENCES parent(parent_id)
                    DEFERRABLE INITIALLY DEFERRED
            )""",
            """CREATE TABLE parent(parent_id INTEGER PRIMARY KEY);
            CREATE TABLE fixture(
                parent_id INTEGER REFERENCES parent(parent_id)
            )""",
        ),
    ),
    ids=(
        "check",
        "collation-and-check",
        "autoincrement-on-conflict-and-check",
        "generated-expression",
        "foreign-key-deferrability",
    ),
)
def test_ordinary_table_semantics_are_contractual(
    expected_sql: str,
    actual_sql: str,
) -> None:
    _assert_incompatible_definition(expected_sql, actual_sql)


def test_ordinary_column_order_and_sql_spelling_are_not_contractual() -> None:
    expected = _contract_from_sql(
        """CREATE TABLE fixture(
            label TEXT COLLATE NOCASE DEFAULT '-- /* literal */ "quoted"',
            ordinal INTEGER CONSTRAINT ordinal_positive CHECK(ordinal>0),
            amount DECIMAL (10, 2) DEFAULT (1 + 2),
            CONSTRAINT expected_name CHECK(length(label)>0)
        )"""
    )
    with _connection() as connection:
        connection.executescript(
            """CrEaTe TaBlE IF NOT EXISTS "fixture"(
                [ordinal] integer /* outside literal */ CHECK ([ordinal] > 0),
                "label" text collate nocase
                    DEFAULT '-- /* literal */ "quoted"',
                amount decimal(10,2) default(1+2),
                CONSTRAINT actual_name CHECK (length([label]) > 0)
            )"""
        )
        validate_sqlite_schema_contract(connection, expected, label="fixture")


def test_alter_added_column_position_and_quoting_are_not_contractual() -> None:
    expected = _contract_from_sql(
        """CREATE TABLE fixture(
            identity INTEGER PRIMARY KEY,
            resolved_run_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('open','resolved')),
            note TEXT
        )"""
    )
    with _connection() as connection:
        connection.executescript(
            """CREATE TABLE fixture(
                identity INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('open','resolved')),
                note TEXT
            );
            ALTER TABLE fixture ADD COLUMN "resolved_run_id" INTEGER;
            """
        )

        validate_sqlite_schema_contract(
            connection,
            expected,
            label="fixture",
            exact=True,
        )


def test_ordinary_column_order_does_not_renumber_inline_foreign_keys() -> None:
    expected = _contract_from_sql(
        """CREATE TABLE parent_a(id INTEGER PRIMARY KEY);
        CREATE TABLE parent_b(id INTEGER PRIMARY KEY);
        CREATE TABLE fixture(
            alpha INTEGER REFERENCES parent_a(id),
            beta INTEGER REFERENCES parent_b(id)
        )"""
    )
    with _connection() as connection:
        connection.executescript(
            """CREATE TABLE parent_a(id INTEGER PRIMARY KEY);
            CREATE TABLE parent_b(id INTEGER PRIMARY KEY);
            CREATE TABLE fixture(
                beta INTEGER REFERENCES parent_b(id),
                alpha INTEGER REFERENCES parent_a(id)
            )"""
        )

        validate_sqlite_schema_contract(connection, expected, label="fixture")


def test_comment_markers_and_quotes_inside_literals_remain_contractual() -> None:
    _assert_incompatible_definition(
        "CREATE TABLE fixture(value TEXT DEFAULT '-- /* \"expected\" */')",
        "CREATE TABLE fixture(value TEXT DEFAULT '-- /* \"changed\" */')",
    )


_FTS_EXPECTED = """CREATE VIRTUAL TABLE document_fts USING fts5(
    file_key UNINDEXED,
    path UNINDEXED,
    page_number UNINDEXED,
    text,
    tokenize="unicode61 remove_diacritics 2 tokenchars '-_'",
    prefix='2 3',
    detail=full
)"""


@pytest.mark.parametrize(
    "actual_sql",
    (
        "CREATE VIRTUAL TABLE document_fts USING fts4(text)",
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
            tokenize='porter',prefix='2 3',detail=full
        )""",
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            text,file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,
            tokenize='unicode61 remove_diacritics 2',prefix='2 3',detail=full
        )""",
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            file_key,path UNINDEXED,page_number UNINDEXED,text,
            tokenize='unicode61 remove_diacritics 2',prefix='2 3',detail=full
        )""",
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
            tokenize="unicode61 remove_diacritics 2 tokenchars '-'",
            prefix='2 4',detail=column
        )""",
    ),
    ids=(
        "module",
        "tokenizer",
        "column-order",
        "unindexed",
        "tokenchars-prefix-and-detail",
    ),
)
def test_virtual_table_module_columns_and_options_are_contractual(
    actual_sql: str,
) -> None:
    _assert_incompatible_definition(_FTS_EXPECTED, actual_sql)


def test_fts_option_order_and_tokenizer_whitespace_are_not_contractual() -> None:
    expected = _contract_from_sql(_FTS_EXPECTED)
    with _connection() as connection:
        connection.executescript(
            """create virtual table "document_fts" using FTS5(
                "file_key" unindexed,
                [path] UNINDEXED,
                page_number unindexed,
                text,
                DETAIL = full,
                tokenize = 'unicode61   remove_diacritics  2 tokenchars ''-_''',
                PREFIX = '2 3'
            )"""
        )
        validate_sqlite_schema_contract(connection, expected, label="fixture")


def test_fts_external_content_and_rowid_options_are_contractual() -> None:
    _assert_incompatible_definition(
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            text,content='documents',content_rowid='document_id'
        )""",
        """CREATE VIRTUAL TABLE document_fts USING fts5(
            text,content='documents',content_rowid='other_id'
        )""",
    )


def test_readonly_validation_does_not_change_database_bytes(tmp_path: Path) -> None:
    database = tmp_path / "schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value INTEGER CHECK(value>=0))")
    before = database.read_bytes()
    expected = _contract_from_sql("CREATE TABLE fixture(value INTEGER CHECK(value>=0))")

    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        validate_sqlite_schema_contract(connection, expected, label="fixture")

    assert database.read_bytes() == before


def test_table_definition_capture_rejects_oversized_persisted_sql() -> None:
    with _connection() as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='fixture'",
            ("X" * (2 * 1024 * 1024 + 1),),
        )
        connection.execute("PRAGMA writable_schema=OFF")

        with pytest.raises(SQLiteSchemaContractError, match="SQL text limit"):
            capture_sqlite_schema_contract(connection)


def test_fts_duplicate_options_are_rejected_as_noncanonical() -> None:
    with _connection() as connection:
        connection.execute(
            """CREATE VIRTUAL TABLE document_fts USING fts5(
                text,detail=full,detail=column
            )"""
        )

        with pytest.raises(SQLiteSchemaContractError, match="duplicate FTS option"):
            capture_sqlite_schema_contract(connection)


# endregion [02]
