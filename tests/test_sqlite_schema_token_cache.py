"""Lexical reuse must never reuse database acceptance or unbounded storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from neocortex.persistence import sqlite_schema_contract as schema


@pytest.fixture(autouse=True)
def empty_token_cache() -> Iterator[None]:
    with schema._SCHEMA_TOKEN_CACHE_LOCK:
        schema._SCHEMA_TOKEN_CACHE.clear()
        schema._SCHEMA_TOKEN_CACHE_BYTES = 0
    yield
    with schema._SCHEMA_TOKEN_CACHE_LOCK:
        schema._SCHEMA_TOKEN_CACHE.clear()
        schema._SCHEMA_TOKEN_CACHE_BYTES = 0


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT CHECK(length(title)<100));
        CREATE INDEX title_idx ON items(lower(title));
        CREATE VIEW labels AS SELECT title || 'Keep Case' AS label FROM items;
        """
    )
    return connection


def test_warm_validation_reuses_lexing_but_repeats_every_database_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[str] = []
    original = schema._tokenize_schema_sql_uncached

    def observe(source: str) -> tuple[schema._SQLToken, ...]:
        parsed.append(source)
        return original(source)

    monkeypatch.setattr(schema, "_tokenize_schema_sql_uncached", observe)
    with _database() as connection:
        expected = schema.capture_sqlite_schema_contract(connection)
        assert parsed
        parsed.clear()
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        schema.validate_sqlite_schema_contract(connection, expected, label="first", exact=True)
        first_observations = tuple(statements)
        statements.clear()
        schema.validate_sqlite_schema_contract(connection, expected, label="second", exact=True)
        assert tuple(statements) == first_observations
        assert any("table_xinfo" in statement for statement in statements)
        assert any("sqlite_master" in statement for statement in statements)
        assert parsed == []
    connection.close()


@pytest.mark.parametrize(
    "drift",
    (
        "DROP INDEX title_idx; CREATE INDEX title_idx ON items(upper(title));",
        "DROP VIEW labels; CREATE VIEW labels AS SELECT title || 'keep case' AS label FROM items;",
        "DROP TABLE items; CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT CHECK(length(title)<200));"
        "CREATE INDEX title_idx ON items(lower(title));",
    ),
)
def test_warm_cache_rejects_changed_sql_even_with_restored_schema_version(drift: str) -> None:
    with _database() as connection:
        expected = schema.capture_sqlite_schema_contract(connection)
        version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        schema.validate_sqlite_schema_contract(connection, expected, label="warm", exact=True)
        connection.executescript(drift)
        connection.execute(f"PRAGMA schema_version={version}")
        with pytest.raises(schema.SQLiteSchemaContractError):
            schema.validate_sqlite_schema_contract(connection, expected, label="changed", exact=True)
    connection.close()


def test_cache_bounds_and_eviction_preserve_exact_tokens() -> None:
    small = [f"SELECT 'value {index}'" for index in range(300)]
    for source in small:
        assert schema._tokenize_schema_sql(source) == schema._tokenize_schema_sql_uncached(source)
    assert len(schema._SCHEMA_TOKEN_CACHE) == schema._SCHEMA_TOKEN_CACHE_MAX_ENTRIES
    assert small[0] not in schema._SCHEMA_TOKEN_CACHE
    for index in range(300):
        source = f"SELECT {index}, " + ", ".join(f"'column {column}'" for column in range(200))
        assert schema._tokenize_schema_sql(source) == schema._tokenize_schema_sql_uncached(source)
        assert schema._SCHEMA_TOKEN_CACHE_BYTES <= schema._SCHEMA_TOKEN_CACHE_MAX_BYTES
    assert len(schema._SCHEMA_TOKEN_CACHE) < schema._SCHEMA_TOKEN_CACHE_MAX_ENTRIES
    assert schema._SCHEMA_TOKEN_CACHE_BYTES == sum(charge for _, charge in schema._SCHEMA_TOKEN_CACHE.values())
    assert schema._tokenize_schema_sql(small[0]) == schema._tokenize_schema_sql_uncached(small[0])


def test_large_or_invalid_sql_is_not_retained() -> None:
    source = " " * schema._SCHEMA_TOKEN_CACHE_MAX_SOURCE_CHARS + "SELECT 'large'"
    assert schema._tokenize_schema_sql(source) == schema._tokenize_schema_sql_uncached(source)
    assert not schema._SCHEMA_TOKEN_CACHE
    for invalid in ("SELECT 'unterminated", " " * (schema._MAX_SCHEMA_SQL_CHARS + 1)):
        with pytest.raises(schema.SQLiteSchemaContractError):
            schema._tokenize_schema_sql(invalid)
    assert not schema._SCHEMA_TOKEN_CACHE
    assert schema._SCHEMA_TOKEN_CACHE_BYTES == 0


def test_parallel_reuse_preserves_quoted_values_and_accounting() -> None:
    sources = [f"SELECT 'Case {index % 7}', \"case {index % 7}\"" for index in range(400)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        actual = list(workers.map(schema._tokenize_schema_sql, sources))
    expected = [schema._tokenize_schema_sql_uncached(source) for source in sources]
    assert actual == expected
    assert len(schema._SCHEMA_TOKEN_CACHE) == 7
    assert schema._SCHEMA_TOKEN_CACHE_BYTES == sum(charge for _, charge in schema._SCHEMA_TOKEN_CACHE.values())


def test_parser_limits_remain_enforced_on_cache_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "SELECT one, two"
    assert len(schema._tokenize_schema_sql(source)) == 4
    monkeypatch.setattr(schema, "_MAX_SCHEMA_SQL_TOKENS", 3)
    with pytest.raises(schema.SQLiteSchemaContractError, match="token limit"):
        schema._tokenize_schema_sql(source)
