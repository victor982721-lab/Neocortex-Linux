"""Structural contracts for versioned SQLite application databases.

The module is deliberately dependency-free so lower pipeline layers can validate
their own durable state without importing the operational orchestration layer.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass


# region [01] Immutable schema descriptions


class SQLiteSchemaContractError(RuntimeError):
    """Persisted SQLite state does not match its declared application schema."""


@dataclass(frozen=True, slots=True, order=True)
class ColumnContract:
    name: str
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_ordinal: int
    hidden: int


@dataclass(frozen=True, slots=True, order=True)
class ForeignKeyContract:
    identifier: int
    sequence: int
    target_table: str
    source_column: str | None
    target_column: str | None
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True, slots=True, order=True)
class IndexContract:
    name: str | None
    unique: bool
    origin: str
    partial: bool
    columns: tuple[str, ...]
    descending: tuple[bool, ...]
    collations: tuple[str, ...]
    normalized_sql: str | None


type CanonicalSQLTokens = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrdinaryTableDefinition:
    """Canonical ordinary-table DDL with order-independent columns."""

    columns: tuple[tuple[str, CanonicalSQLTokens], ...]
    constraints: tuple[CanonicalSQLTokens, ...]


@dataclass(frozen=True, slots=True)
class VirtualTableDefinition:
    """Canonical virtual-table module, ordered arguments and named options."""

    module: str
    arguments: tuple[CanonicalSQLTokens, ...]
    options: tuple[tuple[str, CanonicalSQLTokens], ...]


@dataclass(frozen=True, slots=True)
class TableContract:
    name: str
    table_type: str
    without_rowid: bool
    strict: bool
    columns: tuple[ColumnContract, ...]
    foreign_keys: tuple[ForeignKeyContract, ...]
    indexes: tuple[IndexContract, ...]
    definition: OrdinaryTableDefinition | VirtualTableDefinition | None


@dataclass(frozen=True, slots=True, order=True)
class SQLObjectContract:
    object_type: str
    name: str
    normalized_sql: str


@dataclass(frozen=True, slots=True)
class SQLiteSchemaContract:
    tables: tuple[TableContract, ...]
    sql_objects: tuple[SQLObjectContract, ...]


# endregion [01]


# region [02] Read-only schema introspection


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_type(value: object) -> str:
    source = str(value or "")
    return "" if not source else _serialized_canonical_tokens(source)


def _normalized_sql(value: object) -> str:
    """Canonicalize object DDL without merging tokens or changing quoted data.

    Quoted identifiers retain their case here because SQLite's legacy DQS
    behavior can interpret a double-quoted expression as a string literal.
    This deliberately prefers rejecting a spelling change over accepting a
    different view/trigger/index expression as equivalent.
    """
    tokens = _tokenize_schema_sql(str(value or ""))
    if tokens and _word_is(tokens[0], "create"):
        position = 1
        if position < len(tokens) and any(
            _word_is(tokens[position], word) for word in ("temp", "temporary", "unique")
        ):
            position += 1
        if position < len(tokens) and any(
            _word_is(tokens[position], word) for word in ("index", "view", "trigger")
        ):
            position += 1
            optional = tokens[position:position + 3]
            if len(optional) == 3 and all(
                _word_is(token, word) for token, word in zip(optional, ("if", "not", "exists"), strict=True)
            ):
                tokens = tokens[:position] + tokens[position + 3:]
    canonical = (
        f"quoted:{token.value}" if token.kind == "identifier" else _canonical_token(token)
        for token in tokens
    )
    return "".join(f"{len(token)}:{token}" for token in canonical)


def _serialized_canonical_tokens(source: str) -> str:
    """Serialize canonical tokens without delimiter-collision ambiguity."""

    return "".join(
        f"{len(token)}:{token}" for token in _canonical_tokens(_tokenize_schema_sql(source))
    )


def _normalized_index_sql(value: object) -> str | None:
    """Use the same bounded token contract for explicit indexes and objects."""
    return None if value is None else _normalized_sql(value)


# region [03] Bounded canonical CREATE TABLE parsing


_MAX_SCHEMA_SQL_CHARS = 2 * 1024 * 1024
_MAX_SCHEMA_SQL_TOKENS = 200_000
_MAX_SCHEMA_NESTING = 256
_MAX_TABLE_DEFINITION_PARTS = 32_768

_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_CASEFOLD = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)

# SQLite keywords are context-sensitive.  Classifying the complete public set
# lets quoted identifiers remain distinct from an identically-spelled keyword
# while accepting equivalent quoted and unquoted ordinary identifiers.
_SQLITE_KEYWORDS = frozenset(
    """abort action add after all alter always analyze and as asc attach
    autoincrement before begin between by cascade case cast check collate column
    commit conflict constraint create cross current current_date current_time
    current_timestamp database default deferrable deferred delete desc detach
    distinct do drop each else end escape except exclude exclusive exists explain
    fail filter first following for foreign from full generated glob group groups
    having if ignore immediate in index indexed initially inner insert instead
    intersect into is isnull join key last left like limit match materialized
    natural no not nothing notnull null nulls of offset on or order others outer
    over partition plan pragma preceding primary query raise range recursive
    references regexp reindex release rename replace restrict returning right
    rollback row rows savepoint select set stored strict table temp temporary then
    ties to transaction trigger unbounded union unique update using vacuum values
    view virtual when where window with without""".split()
)
_TABLE_CONSTRAINT_START = frozenset({"primary", "unique", "check", "foreign"})
_MULTI_CHARACTER_OPERATORS = (
    "->>",
    "||",
    "<<",
    ">>",
    "<=",
    ">=",
    "==",
    "!=",
    "<>",
    "->",
)
_SQL_SYMBOLS = frozenset("(),;.+-*/%<>=~|&!?:")


@dataclass(frozen=True, slots=True)
class _SQLToken:
    kind: str
    value: str


# Cache only immutable lexical results keyed by the exact observed SQL. Database
# acceptance, PRAGMA reads and structural comparisons always run afresh. The
# payload charge includes strings/tokens and a conservative per-entry allowance;
# entry and source limits also bound bookkeeping and cache-miss accounting work.
_SCHEMA_TOKEN_CACHE_MAX_BYTES = 2 * 1024 * 1024
_SCHEMA_TOKEN_CACHE_MAX_ENTRIES = 256
_SCHEMA_TOKEN_CACHE_MAX_SOURCE_CHARS = 4096
_SCHEMA_TOKEN_CACHE: OrderedDict[str, tuple[tuple[_SQLToken, ...], int]] = OrderedDict()
_SCHEMA_TOKEN_CACHE_BYTES = 0
_SCHEMA_TOKEN_CACHE_LOCK = threading.Lock()


def _ascii_casefold(value: str) -> str:
    return value.translate(_ASCII_CASEFOLD)


def _schema_definition_error(detail: str) -> SQLiteSchemaContractError:
    return SQLiteSchemaContractError(f"invalid SQLite table definition: {detail}")


def _append_sql_token(tokens: list[_SQLToken], token: _SQLToken) -> None:
    tokens.append(token)
    if len(tokens) > _MAX_SCHEMA_SQL_TOKENS:
        raise _schema_definition_error("token limit exceeded")


def _read_quoted_sql_token(
    source: str,
    start: int,
) -> tuple[_SQLToken, int]:
    opener = source[start]
    closer = "]" if opener == "[" else opener
    kind = "string" if opener == "'" else "identifier"
    index = start + 1
    decoded: list[str] = []
    while index < len(source):
        character = source[index]
        if character != closer:
            decoded.append(character)
            index += 1
            continue
        if opener != "[" and index + 1 < len(source):
            if source[index + 1] == closer:
                decoded.append(closer)
                index += 2
                continue
        return _SQLToken(kind, "".join(decoded)), index + 1
    raise _schema_definition_error("unterminated quoted token")


def _consume_decimal_sql_digits(source: str, start: int) -> int:
    index = start
    while index < len(source) and (source[index].isdigit() or source[index] == "_"):
        index += 1
    return index


def _consume_hexadecimal_sql_digits(source: str, start: int) -> int:
    index = start
    while index < len(source) and (
        source[index].isdigit() or source[index].lower() in "abcdef" or source[index] == "_"
    ):
        index += 1
    return index


def _consume_decimal_sql_fraction(source: str, index: int) -> int:
    if index >= len(source) or source[index] != ".":
        return index
    return _consume_decimal_sql_digits(source, index + 1)


def _consume_decimal_sql_exponent(source: str, index: int) -> int:
    if index >= len(source) or source[index] not in {"e", "E"}:
        return index
    exponent = index + 1
    if exponent < len(source) and source[exponent] in {"+", "-"}:
        exponent += 1
    digit_start = exponent
    exponent = _consume_decimal_sql_digits(source, exponent)
    return exponent if exponent > digit_start else index


def _read_numeric_sql_token(source: str, start: int) -> tuple[_SQLToken, int]:
    """Read one SQLite decimal or hexadecimal numeric literal."""

    if source.startswith(("0x", "0X"), start):
        index = _consume_hexadecimal_sql_digits(source, start + 2)
    else:
        index = _consume_decimal_sql_digits(source, start)
        index = _consume_decimal_sql_fraction(source, index)
        index = _consume_decimal_sql_exponent(source, index)
    return _SQLToken("number", _ascii_casefold(source[start:index])), index


def _tokenize_schema_sql(source: str) -> tuple[_SQLToken, ...]:
    """Tokenize SQLite DDL while discarding only insignificant trivia."""

    if len(source) > _MAX_SCHEMA_SQL_CHARS:
        raise _schema_definition_error("SQL text limit exceeded")
    if len(source) > _SCHEMA_TOKEN_CACHE_MAX_SOURCE_CHARS:
        return _tokenize_schema_sql_uncached(source)
    with _SCHEMA_TOKEN_CACHE_LOCK:
        cached = _SCHEMA_TOKEN_CACHE.get(source)
        if cached is not None:
            if len(cached[0]) > _MAX_SCHEMA_SQL_TOKENS:
                raise _schema_definition_error("SQL token limit exceeded")
            _SCHEMA_TOKEN_CACHE.move_to_end(source)
            return cached[0]
    tokens = _tokenize_schema_sql_uncached(source)
    charge = sys.getsizeof(source) + sys.getsizeof(tokens) + 256 + sum(
        sys.getsizeof(token) + sys.getsizeof(token.kind) + sys.getsizeof(token.value)
        for token in tokens
    )
    if charge > _SCHEMA_TOKEN_CACHE_MAX_BYTES:
        return tokens
    global _SCHEMA_TOKEN_CACHE_BYTES
    with _SCHEMA_TOKEN_CACHE_LOCK:
        # Another thread may have parsed this SQL while the lock was released.
        if source in _SCHEMA_TOKEN_CACHE:
            _SCHEMA_TOKEN_CACHE.move_to_end(source)
            return _SCHEMA_TOKEN_CACHE[source][0]
        while _SCHEMA_TOKEN_CACHE and (
            len(_SCHEMA_TOKEN_CACHE) >= _SCHEMA_TOKEN_CACHE_MAX_ENTRIES
            or _SCHEMA_TOKEN_CACHE_BYTES + charge > _SCHEMA_TOKEN_CACHE_MAX_BYTES
        ):
            _, (_, removed_charge) = _SCHEMA_TOKEN_CACHE.popitem(last=False)
            _SCHEMA_TOKEN_CACHE_BYTES -= removed_charge
        _SCHEMA_TOKEN_CACHE[source] = (tokens, charge)
        _SCHEMA_TOKEN_CACHE_BYTES += charge
    return tokens


def _tokenize_schema_sql_uncached(source: str) -> tuple[_SQLToken, ...]:
    tokens: list[_SQLToken] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise _schema_definition_error("unterminated block comment")
            index = end + 2
            continue
        if character in {"'", '"', "`", "["}:
            token, index = _read_quoted_sql_token(source, index)
            _append_sql_token(tokens, token)
            continue
        if character.isdigit() or (
            character == "." and index + 1 < len(source) and source[index + 1].isdigit()
        ):
            token, index = _read_numeric_sql_token(source, index)
            _append_sql_token(tokens, token)
            continue
        operator = next(
            (value for value in _MULTI_CHARACTER_OPERATORS if source.startswith(value, index)),
            None,
        )
        if operator is not None:
            _append_sql_token(tokens, _SQLToken("symbol", operator))
            index += len(operator)
            continue
        if character in _SQL_SYMBOLS:
            _append_sql_token(tokens, _SQLToken("symbol", character))
            index += 1
            continue
        end = index + 1
        while end < len(source):
            candidate = source[end]
            if (
                candidate.isspace()
                or candidate in {"'", '"', "`", "["}
                or candidate in _SQL_SYMBOLS
            ):
                break
            end += 1
        _append_sql_token(tokens, _SQLToken("word", source[index:end]))
        index = end
    return tuple(tokens)


def _word_is(token: _SQLToken, value: str) -> bool:
    return token.kind == "word" and _ascii_casefold(token.value) == value


def _identifier_value(token: _SQLToken) -> str:
    if token.kind not in {"word", "identifier"}:
        raise _schema_definition_error("identifier expected")
    return _ascii_casefold(token.value)


def _canonical_token(token: _SQLToken) -> str:
    if token.kind == "word":
        value = _ascii_casefold(token.value)
        prefix = "keyword" if value in _SQLITE_KEYWORDS else "identifier"
        return f"{prefix}:{value}"
    if token.kind == "identifier":
        return f"identifier:{_ascii_casefold(token.value)}"
    if token.kind == "string":
        return f"string:{token.value}"
    if token.kind == "number":
        return f"number:{token.value}"
    return f"symbol:{token.value}"


def _canonical_tokens(tokens: tuple[_SQLToken, ...]) -> CanonicalSQLTokens:
    return tuple(_canonical_token(token) for token in tokens)


def _expect_keyword(
    tokens: tuple[_SQLToken, ...],
    index: int,
    value: str,
) -> int:
    if index >= len(tokens) or not _word_is(tokens[index], value):
        raise _schema_definition_error(f"expected keyword {value!r}")
    return index + 1


def _skip_qualified_identifier(
    tokens: tuple[_SQLToken, ...],
    index: int,
) -> int:
    if index >= len(tokens):
        raise _schema_definition_error("table name is missing")
    _identifier_value(tokens[index])
    index += 1
    if index < len(tokens) and tokens[index] == _SQLToken("symbol", "."):
        index += 1
        if index >= len(tokens):
            raise _schema_definition_error("qualified table name is incomplete")
        _identifier_value(tokens[index])
        index += 1
    return index


def _matching_body(
    tokens: tuple[_SQLToken, ...],
    opening_index: int,
) -> tuple[tuple[_SQLToken, ...], tuple[_SQLToken, ...]]:
    if opening_index >= len(tokens) or tokens[opening_index] != _SQLToken("symbol", "("):
        raise _schema_definition_error("parenthesized table body is missing")
    depth = 0
    for index in range(opening_index, len(tokens)):
        token = tokens[index]
        if token == _SQLToken("symbol", "("):
            depth += 1
            if depth > _MAX_SCHEMA_NESTING:
                raise _schema_definition_error("nesting limit exceeded")
        elif token == _SQLToken("symbol", ")"):
            depth -= 1
            if depth == 0:
                return tokens[opening_index + 1 : index], tokens[index + 1 :]
            if depth < 0:
                break
    raise _schema_definition_error("unbalanced table body")


def _split_top_level_parts(
    tokens: tuple[_SQLToken, ...],
) -> tuple[tuple[_SQLToken, ...], ...]:
    parts: list[tuple[_SQLToken, ...]] = []
    start = 0
    depth = 0
    for index, token in enumerate(tokens):
        if token == _SQLToken("symbol", "("):
            depth += 1
            if depth > _MAX_SCHEMA_NESTING:
                raise _schema_definition_error("nesting limit exceeded")
        elif token == _SQLToken("symbol", ")"):
            depth -= 1
            if depth < 0:
                raise _schema_definition_error("unbalanced nested expression")
        elif token == _SQLToken("symbol", ",") and depth == 0:
            if index == start:
                raise _schema_definition_error("empty table definition item")
            parts.append(tokens[start:index])
            start = index + 1
            if len(parts) > _MAX_TABLE_DEFINITION_PARTS:
                raise _schema_definition_error("table item limit exceeded")
    if depth != 0:
        raise _schema_definition_error("unbalanced nested expression")
    if start == len(tokens):
        raise _schema_definition_error("empty table definition item")
    parts.append(tokens[start:])
    if len(parts) > _MAX_TABLE_DEFINITION_PARTS:
        raise _schema_definition_error("table item limit exceeded")
    return tuple(parts)


def _remove_constraint_names(
    tokens: tuple[_SQLToken, ...],
) -> tuple[_SQLToken, ...]:
    normalized: list[_SQLToken] = []
    index = 0
    while index < len(tokens):
        if _word_is(tokens[index], "constraint"):
            if index + 1 >= len(tokens):
                raise _schema_definition_error("CONSTRAINT name is missing")
            _identifier_value(tokens[index + 1])
            index += 2
            continue
        normalized.append(tokens[index])
        index += 1
    return tuple(normalized)


def _ordinary_prefix_body(
    tokens: tuple[_SQLToken, ...],
) -> tuple[_SQLToken, ...]:
    index = _expect_keyword(tokens, 0, "create")
    if index < len(tokens) and (
        _word_is(tokens[index], "temp") or _word_is(tokens[index], "temporary")
    ):
        index += 1
    index = _expect_keyword(tokens, index, "table")
    if index < len(tokens) and _word_is(tokens[index], "if"):
        index = _expect_keyword(tokens, index, "if")
        index = _expect_keyword(tokens, index, "not")
        index = _expect_keyword(tokens, index, "exists")
    index = _skip_qualified_identifier(tokens, index)
    body, _suffix = _matching_body(tokens, index)
    return body


def _ordinary_table_definition(
    sql: str,
    column_names: tuple[str, ...],
) -> OrdinaryTableDefinition:
    tokens = _tokenize_schema_sql(sql)
    parts = _split_top_level_parts(_ordinary_prefix_body(tokens))
    known_columns = {_ascii_casefold(name): name for name in column_names}
    columns: list[tuple[str, CanonicalSQLTokens]] = []
    constraints: list[CanonicalSQLTokens] = []
    seen_columns: set[str] = set()
    for raw_part in parts:
        part = _remove_constraint_names(raw_part)
        if not part:
            raise _schema_definition_error("empty constraint after normalization")
        first_name = _identifier_value(part[0]) if part[0].kind in {"word", "identifier"} else None
        if first_name is not None and first_name in known_columns:
            if first_name in seen_columns:
                raise _schema_definition_error(f"duplicate column {first_name!r}")
            seen_columns.add(first_name)
            columns.append((first_name, _canonical_tokens(part[1:])))
            continue
        if part[0].kind != "word" or _ascii_casefold(part[0].value) not in _TABLE_CONSTRAINT_START:
            raise _schema_definition_error("unrecognized table definition item")
        constraints.append(_canonical_tokens(part))
    if seen_columns != set(known_columns):
        raise _schema_definition_error("DDL columns do not match table_xinfo")
    return OrdinaryTableDefinition(
        columns=tuple(sorted(columns, key=lambda item: item[0])),
        constraints=tuple(sorted(constraints)),
    )


def _canonical_fts_tokenizer_spec(value: str) -> CanonicalSQLTokens:
    """Normalize only separator whitespace in one decoded FTS tokenizer spec."""

    if len(value) > _MAX_SCHEMA_SQL_CHARS:
        raise _schema_definition_error("FTS tokenizer specification is too large")
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index].isspace():
            index += 1
            continue
        if value[index] in {"'", '"'}:
            opener = value[index]
            index += 1
            decoded: list[str] = []
            while index < len(value):
                character = value[index]
                if character != opener:
                    decoded.append(character)
                    index += 1
                    continue
                if index + 1 < len(value) and value[index + 1] == opener:
                    decoded.append(opener)
                    index += 2
                    continue
                index += 1
                break
            else:
                raise _schema_definition_error("unterminated FTS tokenizer quote")
            result.append("quoted:" + "".join(decoded))
        else:
            end = index + 1
            while end < len(value) and not value[end].isspace():
                if value[end] in {"'", '"'}:
                    break
                end += 1
            result.append("bare:" + _ascii_casefold(value[index:end]))
            index = end
        if len(result) > _MAX_SCHEMA_SQL_TOKENS:
            raise _schema_definition_error("FTS tokenizer token limit exceeded")
    return tuple(result)


def _top_level_equal_index(tokens: tuple[_SQLToken, ...]) -> int | None:
    depth = 0
    found: int | None = None
    for index, token in enumerate(tokens):
        if token == _SQLToken("symbol", "("):
            depth += 1
        elif token == _SQLToken("symbol", ")"):
            depth -= 1
        elif token == _SQLToken("symbol", "=") and depth == 0:
            if found is not None:
                raise _schema_definition_error("virtual option has multiple equals")
            found = index
    return found


def _canonical_fts_option_value(
    key: str,
    tokens: tuple[_SQLToken, ...],
) -> CanonicalSQLTokens:
    if not tokens:
        raise _schema_definition_error(f"FTS option {key!r} has no value")
    if key == "tokenize" and len(tokens) == 1:
        return (
            "fts-tokenizer",
            *_canonical_fts_tokenizer_spec(tokens[0].value),
        )
    if len(tokens) == 1 and tokens[0].kind in {"string", "identifier"}:
        return (f"value:{tokens[0].value}",)
    return _canonical_tokens(tokens)


def _virtual_table_definition(sql: str) -> VirtualTableDefinition:
    tokens = _tokenize_schema_sql(sql)
    index = _expect_keyword(tokens, 0, "create")
    if index < len(tokens) and (
        _word_is(tokens[index], "temp") or _word_is(tokens[index], "temporary")
    ):
        index += 1
    index = _expect_keyword(tokens, index, "virtual")
    index = _expect_keyword(tokens, index, "table")
    if index < len(tokens) and _word_is(tokens[index], "if"):
        index = _expect_keyword(tokens, index, "if")
        index = _expect_keyword(tokens, index, "not")
        index = _expect_keyword(tokens, index, "exists")
    index = _skip_qualified_identifier(tokens, index)
    index = _expect_keyword(tokens, index, "using")
    if index >= len(tokens):
        raise _schema_definition_error("virtual-table module is missing")
    module = _identifier_value(tokens[index])
    body, _suffix = _matching_body(tokens, index + 1)
    parts = _split_top_level_parts(body)
    if module != "fts5":
        return VirtualTableDefinition(
            module=module,
            arguments=tuple(_canonical_tokens(part) for part in parts),
            options=(),
        )

    arguments: list[CanonicalSQLTokens] = []
    options: list[tuple[str, CanonicalSQLTokens]] = []
    option_names: set[str] = set()
    for part in parts:
        equal_index = _top_level_equal_index(part)
        if equal_index is None:
            arguments.append(_canonical_tokens(part))
            continue
        key_tokens = part[:equal_index]
        if len(key_tokens) != 1:
            raise _schema_definition_error("FTS option name is not canonical")
        key = _identifier_value(key_tokens[0])
        if key in option_names:
            raise _schema_definition_error(f"duplicate FTS option {key!r}")
        option_names.add(key)
        options.append((key, _canonical_fts_option_value(key, part[equal_index + 1 :])))
    return VirtualTableDefinition(
        module=module,
        arguments=tuple(arguments),
        options=tuple(sorted(options)),
    )


def _captured_table_definition(
    connection: sqlite3.Connection,
    table: str,
    table_type: str,
    columns: tuple[ColumnContract, ...],
) -> OrdinaryTableDefinition | VirtualTableDefinition | None:
    # Views have their own SQLObjectContract, and virtual-table shadow DDL is
    # generated by SQLite rather than by the application schema.
    if table_type in {"view", "shadow"}:
        return None
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or row[0] is None:
        raise _schema_definition_error(f"SQL is unavailable for table {table!r}")
    sql = str(row[0])
    try:
        if table_type == "virtual":
            return _virtual_table_definition(sql)
        if table_type == "table":
            return _ordinary_table_definition(
                sql,
                tuple(column.name for column in columns),
            )
    except SQLiteSchemaContractError as exc:
        raise SQLiteSchemaContractError(
            f"table {table!r} has an invalid persisted definition: {exc}"
        ) from exc
    raise _schema_definition_error(f"unsupported table type {table_type!r}")


# endregion [03]


def _application_table_options(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, bool, bool]]:
    return {
        str(row[1]): (str(row[2]), bool(row[4]), bool(row[5]))
        for row in connection.execute("PRAGMA table_list")
        if not str(row[1]).startswith("sqlite_")
    }


def _column_contracts(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[ColumnContract, ...]:
    rows = connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})").fetchall()
    return tuple(
        ColumnContract(
            name=str(row[1]),
            declared_type=_normalized_type(row[2]),
            not_null=bool(row[3]),
            default_sql=(None if row[4] is None else _serialized_canonical_tokens(str(row[4]))),
            primary_key_ordinal=int(row[5]),
            hidden=int(row[6]),
        )
        for row in rows
    )


def _foreign_key_contracts(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[ForeignKeyContract, ...]:
    rows = connection.execute(f"PRAGMA foreign_key_list({_quoted_identifier(table)})").fetchall()
    groups: dict[int, list[ForeignKeyContract]] = {}
    for row in rows:
        identifier = int(row[0])
        groups.setdefault(identifier, []).append(
            ForeignKeyContract(
                identifier=0,
                sequence=int(row[1]),
                target_table=str(row[2]),
                source_column=None if row[3] is None else str(row[3]),
                target_column=None if row[4] is None else str(row[4]),
                on_update=str(row[5]),
                on_delete=str(row[6]),
                match=str(row[7]),
            )
        )
    ordered_groups = sorted(
        (tuple(sorted(group, key=lambda item: item.sequence)) for group in groups.values()),
        key=lambda group: tuple(
            (
                item.sequence,
                item.target_table,
                item.source_column or "",
                item.target_column or "",
                item.on_update,
                item.on_delete,
                item.match,
            )
            for item in group
        ),
    )
    return tuple(
        ForeignKeyContract(
            identifier=identifier,
            sequence=item.sequence,
            target_table=item.target_table,
            source_column=item.source_column,
            target_column=item.target_column,
            on_update=item.on_update,
            on_delete=item.on_delete,
            match=item.match,
        )
        for identifier, group in enumerate(ordered_groups)
        for item in group
    )


def _index_details(
    connection: sqlite3.Connection,
    index: str,
) -> tuple[tuple[str, ...], tuple[bool, ...], tuple[str, ...]]:
    rows = connection.execute(f"PRAGMA index_xinfo({_quoted_identifier(index)})").fetchall()
    key_rows = [row for row in rows if bool(row[5])]
    columns = tuple(
        str(row[2]) if row[2] is not None else f"<expression:{int(row[1])}>" for row in key_rows
    )
    descending = tuple(bool(row[3]) for row in key_rows)
    collations = tuple(str(row[4] or "BINARY").upper() for row in key_rows)
    return columns, descending, collations


def _index_sql(connection: sqlite3.Connection, index: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index,),
    ).fetchone()
    return None if row is None else _normalized_index_sql(row[0])


def _index_contracts(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[IndexContract, ...]:
    contracts: list[IndexContract] = []
    rows = connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})").fetchall()
    for row in rows:
        index_name = str(row[1])
        columns, descending, collations = _index_details(connection, index_name)
        contracts.append(
            IndexContract(
                name=(None if index_name.startswith("sqlite_autoindex_") else index_name),
                unique=bool(row[2]),
                origin=str(row[3]),
                partial=bool(row[4]),
                columns=columns,
                descending=descending,
                collations=collations,
                normalized_sql=_index_sql(connection, index_name),
            )
        )
    return tuple(
        sorted(
            contracts,
            key=lambda item: (
                item.name is not None,
                item.name or "",
                item.unique,
                item.origin,
                item.partial,
                item.columns,
                item.descending,
                item.collations,
                item.normalized_sql or "",
            ),
        )
    )


def _table_contract(
    connection: sqlite3.Connection,
    name: str,
    table_type: str,
    without_rowid: bool,
    strict: bool,
) -> TableContract:
    columns = _column_contracts(connection, name)
    return TableContract(
        name=name,
        table_type=table_type,
        without_rowid=without_rowid,
        strict=strict,
        columns=columns,
        foreign_keys=_foreign_key_contracts(connection, name),
        indexes=_index_contracts(connection, name),
        definition=_captured_table_definition(
            connection,
            name,
            table_type,
            columns,
        ),
    )


def capture_sqlite_schema_contract(
    connection: sqlite3.Connection,
) -> SQLiteSchemaContract:
    """Capture all non-internal tables, views and triggers on one connection."""

    options = _application_table_options(connection)
    tables = tuple(
        _table_contract(
            connection,
            name,
            table_type,
            without_rowid,
            strict,
        )
        for name, (table_type, without_rowid, strict) in sorted(options.items())
    )
    objects = tuple(
        SQLObjectContract(str(row[0]), str(row[1]), _normalized_sql(row[2]))
        for row in connection.execute(
            """SELECT type,name,sql FROM sqlite_master
            WHERE type IN ('view','trigger') AND name NOT LIKE 'sqlite_%'
            ORDER BY type,name"""
        )
    )
    return SQLiteSchemaContract(tables=tables, sql_objects=objects)


def schema_contract_from_builder(
    builder: Callable[[sqlite3.Connection], None],
) -> SQLiteSchemaContract:
    """Build a canonical contract in memory from one route's DDL function."""

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        builder(connection)
        return capture_sqlite_schema_contract(connection)
    finally:
        connection.close()


# endregion [02]


# region [04] Contract and version validation


def _table_errors(
    actual: TableContract,
    expected: TableContract,
    *,
    exact: bool,
    allowed_extra_indexes: frozenset[str],
) -> list[str]:
    errors: list[str] = []
    if (
        actual.table_type != expected.table_type
        or actual.without_rowid != expected.without_rowid
        or actual.strict != expected.strict
    ):
        errors.append(f"table {expected.name!r} has incompatible options")
    actual_columns = {column.name: column for column in actual.columns}
    expected_columns = {column.name: column for column in expected.columns}
    if actual_columns != expected_columns:
        errors.append(f"table {expected.name!r} has incompatible columns")
    if actual.foreign_keys != expected.foreign_keys:
        errors.append(f"table {expected.name!r} has incompatible foreign keys")
    if actual.definition != expected.definition:
        errors.append(f"table {expected.name!r} has incompatible definition")
    missing_indexes = set(expected.indexes) - set(actual.indexes)
    actual_only_indexes = set(actual.indexes) - set(expected.indexes)
    incompatible_names = {index.name for index in missing_indexes if index.name is not None} & {
        index.name for index in actual_only_indexes if index.name is not None
    }
    if incompatible_names:
        errors.append(
            f"table {expected.name!r} has incompatible indexes "
            + ", ".join(sorted(incompatible_names))
        )
        missing_indexes = {
            index for index in missing_indexes if index.name not in incompatible_names
        }
        actual_only_indexes = {
            index for index in actual_only_indexes if index.name not in incompatible_names
        }
    extra_indexes = (
        {
            index
            for index in actual_only_indexes
            if index.name not in allowed_extra_indexes
        }
        if exact
        else set()
    )
    if missing_indexes:
        names = sorted(index.name or "<automatic>" for index in missing_indexes)
        errors.append(f"table {expected.name!r} lacks indexes {', '.join(names)}")
    if extra_indexes:
        names = sorted(index.name or "<automatic>" for index in extra_indexes)
        errors.append(f"table {expected.name!r} has unexpected indexes {', '.join(names)}")
    return errors


def validate_sqlite_schema_contract(
    connection: sqlite3.Connection,
    expected: SQLiteSchemaContract,
    *,
    label: str,
    exact: bool = False,
    allowed_extra_tables: Collection[str] = (),
    allowed_extra_indexes: Collection[str] = (),
    allowed_extra_objects: Collection[str] = (),
) -> None:
    """Reject incompatible required objects without changing persisted state."""

    allowed_tables = frozenset(allowed_extra_tables)
    allowed_indexes = frozenset(allowed_extra_indexes)
    allowed_objects = frozenset(allowed_extra_objects)

    # Detect missing tables before introspecting virtual tables.  A damaged FTS
    # shadow set can make ``PRAGMA table_xinfo`` fail while constructing the
    # parent virtual table, obscuring the actual missing durable object behind
    # a raw sqlite3.DatabaseError.
    expected_names = {table.name for table in expected.tables}
    observed_names = {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"""
        )
    }
    missing_names = sorted(expected_names - observed_names)
    if missing_names:
        raise SQLiteSchemaContractError(
            f"{label} schema contract is invalid: "
            + "; ".join(f"missing table {name!r}" for name in missing_names)
        )
    if exact:
        actual = capture_sqlite_schema_contract(connection)
    else:
        options = _application_table_options(connection)
        actual = SQLiteSchemaContract(
            tables=tuple(
                _table_contract(connection, name, *options[name]) for name in sorted(expected_names)
            ),
            sql_objects=tuple(
                SQLObjectContract(str(row[0]), str(row[1]), _normalized_sql(row[2]))
                for row in connection.execute(
                    """SELECT type,name,sql FROM sqlite_master
                    WHERE type IN ('view','trigger') AND name NOT LIKE 'sqlite_%'
                    ORDER BY type,name"""
                )
            ),
        )
    actual_tables = {table.name: table for table in actual.tables}
    expected_tables = {table.name: table for table in expected.tables}
    errors: list[str] = []
    for name, expected_table in expected_tables.items():
        actual_table = actual_tables.get(name)
        if actual_table is None:
            errors.append(f"missing table {name!r}")
            continue
        errors.extend(
            _table_errors(
                actual_table,
                expected_table,
                exact=exact,
                allowed_extra_indexes=allowed_indexes,
            )
        )
    if exact:
        errors.extend(
            f"unexpected table {name!r}"
            for name in sorted(set(actual_tables) - set(expected_tables) - allowed_tables)
        )
    missing_objects = set(expected.sql_objects) - set(actual.sql_objects)
    if missing_objects:
        errors.extend(
            f"missing or incompatible {item.object_type} {item.name!r}"
            for item in sorted(missing_objects)
        )
    if exact:
        extra_objects = set(actual.sql_objects) - set(expected.sql_objects)
        errors.extend(
            f"unexpected {item.object_type} {item.name!r}"
            for item in sorted(extra_objects, key=lambda value: (value.object_type, value.name))
            if item.name not in allowed_objects
        )
    if errors:
        raise SQLiteSchemaContractError(f"{label} schema contract is invalid: " + "; ".join(errors))


def read_metadata_schema_version(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> int | None:
    """Read one canonical unsigned decimal ``metadata.schema_version`` value."""

    metadata_exists = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='metadata'"""
    ).fetchone()
    if metadata_exists is None:
        return None
    try:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SQLiteSchemaContractError(f"{label} metadata table is incompatible: {exc}") from exc
    if not rows:
        return None
    if len(rows) != 1:
        raise SQLiteSchemaContractError(f"{label} metadata has no unique schema_version")
    raw = str(rows[0][0])
    if not raw.isascii() or not raw.isdecimal() or (raw != "0" and raw.startswith("0")):
        raise SQLiteSchemaContractError(
            f"{label} schema version is not canonical unsigned decimal text"
        )
    return int(raw)


def read_application_schema_version(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> int | None:
    """Read the mandatory version of a non-empty application database."""

    has_objects = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' LIMIT 1"""
    ).fetchone()
    if has_objects is None:
        return None
    metadata_type = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='metadata'"
    ).fetchone()
    if metadata_type is None or str(metadata_type[0]) != "table":
        raise SQLiteSchemaContractError(
            f"{label} database contains objects but no valid metadata table"
        )
    version = read_metadata_schema_version(connection, label=label)
    if version is None:
        raise SQLiteSchemaContractError(f"{label} metadata lacks schema_version")
    return version


__all__ = [
    "SQLiteSchemaContract",
    "SQLiteSchemaContractError",
    "capture_sqlite_schema_contract",
    "read_application_schema_version",
    "read_metadata_schema_version",
    "schema_contract_from_builder",
    "validate_sqlite_schema_contract",
]


# endregion [04]
