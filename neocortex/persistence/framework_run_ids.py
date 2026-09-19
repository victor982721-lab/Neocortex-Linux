"""Read-only allocation helpers for Framework run identifiers.

The Framework reset path may leave a high-water mark in ``metadata`` after
removing visible run history.  This module is deliberately limited to reading
that marker and the run identifiers that are still present in the owner.  It
does not plan, stage, copy, reset, or otherwise mutate SQLite state; callers
must perform their insert in the same writer transaction as this calculation.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from neocortex.persistence.framework_schema import validate_framework_schema


FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY: Final = "framework_run_id_floor"
"""Legacy metadata key containing the Framework run-id high-water mark."""

_MAX_SQLITE_INT: Final = 9223372036854775807

# These are scalar references that can outlive the row in ``initial_runs``.
# A value in any of them must remain below the next allocated identifier.
_REFERENCE_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("file_actions", "run_id"),
    ("content_type_cache", "last_seen_run_id"),
    ("review_candidates", "last_seen_run_id"),
    ("review_candidates", "resolved_run_id"),
    ("initial_runs", "source_run_id"),
    ("route_runs", "source_run_id"),
    ("route_phase_runs", "source_run_id"),
)


class FrameworkRunIdError(RuntimeError):
    """Base error for a failed read-only Framework run-id allocation."""


class FrameworkRunIdSchemaError(FrameworkRunIdError):
    """The Framework owner or one of its run-id values is malformed."""


class FrameworkRunIdOverflowError(FrameworkRunIdError):
    """No representable SQLite integer remains for a new run identifier."""


def _quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise FrameworkRunIdSchemaError("invalid Framework identifier")
    return '"' + value.replace('"', '""') + '"'


def _validate_read_connection(connection: sqlite3.Connection) -> None:
    """Validate the current owner without changing it."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    try:
        attached = tuple(
            str(row[1])
            for row in connection.execute("PRAGMA database_list")
            if str(row[1]) not in {"main", "temp"}
        )
    except sqlite3.Error as exc:
        raise FrameworkRunIdSchemaError(
            "Framework attached databases cannot be inspected"
        ) from exc
    if attached:
        raise FrameworkRunIdSchemaError(
            "Framework run-id allocation refuses attached databases"
        )
    try:
        validate_framework_schema(connection)
    except (sqlite3.Error, RuntimeError) as exc:
        raise FrameworkRunIdSchemaError(
            "Framework schema cannot be validated for run-id allocation"
        ) from exc


def _read_floor(connection: sqlite3.Connection) -> int | None:
    try:
        rows = connection.execute(
            "SELECT value FROM main.metadata WHERE key=?",
            (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise FrameworkRunIdSchemaError("Framework run-id floor cannot be read") from exc
    if len(rows) > 1:
        raise FrameworkRunIdSchemaError("Framework run-id floor is not unique")
    if not rows:
        return None
    value = rows[0][0]
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise FrameworkRunIdSchemaError("Framework run-id floor is malformed")
    parsed = int(value)
    if parsed < 0 or parsed >= _MAX_SQLITE_INT:
        raise FrameworkRunIdSchemaError("Framework run-id floor is out of range")
    return parsed


def read_framework_run_id_floor(connection: sqlite3.Connection) -> int | None:
    """Read the legacy allocator floor without changing the connection."""

    _validate_read_connection(connection)
    return _read_floor(connection)


def _read_max_run_id(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT MIN(run_id), MAX(run_id) FROM main.initial_runs"
        ).fetchone()
    except sqlite3.Error as exc:
        raise FrameworkRunIdSchemaError(
            "Framework run identifiers cannot be read"
        ) from exc
    if row is None or (row[0] is None and row[1] is None):
        return None
    minimum, maximum = row
    if type(minimum) is not int or type(maximum) is not int or minimum < 0:
        raise FrameworkRunIdSchemaError("Framework run identifiers are malformed")
    return maximum


def _all_referenced_max(connection: sqlite3.Connection) -> int | None:
    """Read the highest run-id retained by a non-ledger reference."""

    highest: int | None = None
    for table, column in _REFERENCE_SPECS:
        try:
            rows = connection.execute(
                f"SELECT {_quote_identifier(column)} "
                f"FROM main.{_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL"
            )
        except sqlite3.Error as exc:
            raise FrameworkRunIdSchemaError(
                f"Framework references cannot be read: {table}.{column}"
            ) from exc
        try:
            for row in rows:
                value = row[0]
                if type(value) is not int or value < 0:
                    raise FrameworkRunIdSchemaError(
                        f"Framework reference is not integer-valued: {table}.{column}"
                    )
                # Zero is the documented empty/sentinel value for cache links.
                if value and (highest is None or value > highest):
                    highest = value
        except sqlite3.Error as exc:
            raise FrameworkRunIdSchemaError(
                f"Framework references cannot be read: {table}.{column}"
            ) from exc
    return highest


def framework_next_run_id(connection: sqlite3.Connection) -> int:
    """Return the next collision-free Framework run identifier.

    The calculation is read-only.  A Framework writer must call this helper
    after entering its write transaction and insert the returned identifier in
    that same transaction; this function does not reserve or persist it.
    """

    _validate_read_connection(connection)
    floor = _read_floor(connection)
    max_run = _read_max_run_id(connection)
    referenced_max = _all_referenced_max(connection)
    current = max(
        (value for value in (floor, max_run, referenced_max) if value is not None),
        default=0,
    )
    if current >= _MAX_SQLITE_INT - 1:
        raise FrameworkRunIdOverflowError("Framework run identifier space is exhausted")
    return current + 1


__all__ = [
    "FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY",
    "FrameworkRunIdError",
    "FrameworkRunIdOverflowError",
    "FrameworkRunIdSchemaError",
    "framework_next_run_id",
    "read_framework_run_id_floor",
]
