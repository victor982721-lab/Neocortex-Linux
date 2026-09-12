"""Staged, run-history-only maintenance for the Framework owner.

This module deliberately does not know about the state-directory replacement
protocol.  A caller is expected to make a verified backup and a disposable
SQLite staging copy first, then invoke :func:`apply_framework_run_reset` on
that staging connection.  The apply function refuses an unmarked connection
so a caller cannot accidentally turn a preview into a destructive operation
against a live writer owner.

Only the append-only *orchestration* tables are candidates for removal.  The
content-type cache, Review tables, file-action/recovery tables, and metadata
are preserved.  ``initial_runs`` is treated specially: rows named by any
preserved action/review/cache/provenance reference remain in place (and rows
needed by an uncertain file action therefore remain available for recovery).
For rows that are removed, the allocator floor in ``metadata`` keeps
future Framework run identifiers above every identifier still present in a
preserved reference.  The floor is an explicit companion contract for
``FrameworkState``; this module exposes the reader so the writer can adopt it
without making this maintenance path allocate a run itself.

No filesystem path is opened here.  This keeps the core transform useful for
SQLite backup/staging implementations and makes the fixture tests incapable
of touching the productive owner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from neocortex.persistence.framework_schema import SCHEMA_VERSION


FRAMEWORK_RUN_RESET_SCHEMA: Final = "neocortex.framework-run-reset/v1"
"""Schema identifier for plans and receipts emitted by this module."""

FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY: Final = "framework_run_id_floor"
"""Metadata key consumed by the Framework run-id allocator.

The value is a canonical non-negative decimal integer.  It represents the
largest run identifier that must not be reused, including identifiers that
only survive in a cache/recovery reference after a reset.
"""

FRAMEWORK_RUN_RESET_METADATA_KEY: Final = "framework_last_run_reset"
"""Metadata key containing a bounded audit marker for the latest reset."""

_RUN_HISTORY_TABLES: Final[tuple[str, ...]] = (
    "run_events",
    "route_phase_runs",
    "route_runs",
    "run_actions",
    "route_candidates",
    "initial_runs",
)

# These tables are intentionally not part of the deletion allow-list.  The
# list is also used for schema checks, so a future table cannot silently become
# resettable merely because it contains a ``run_id`` column.
_PRESERVED_TABLES: Final[tuple[str, ...]] = (
    "metadata",
    "content_type_cache",
    "review_candidates",
    "review_decisions",
    "review_evidence_examples",
    "review_evidence_progress",
    "review_task_batches",
    "review_tasks",
    "review_task_batch_memberships",
    "review_task_events",
    "review_task_scan_progress",
    "review_task_source_publications",
    "file_actions",
    "file_action_events",
    "file_action_reconciliation_events",
)

_REFERENCE_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("file_actions", "run_id"),
    ("content_type_cache", "last_seen_run_id"),
    ("review_candidates", "last_seen_run_id"),
    ("review_candidates", "resolved_run_id"),
    # These are run-to-run provenance links.  They are reported even though
    # the owning row is in a resettable table; a retained run must keep its
    # source run in the retained set as well.
    ("initial_runs", "source_run_id"),
    ("route_runs", "source_run_id"),
    ("route_phase_runs", "source_run_id"),
)

_REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "metadata": ("key", "value"),
    "initial_runs": ("run_id", "status"),
    "run_events": ("event_id", "run_id"),
    "route_runs": ("run_id", "route_name", "status"),
    "route_phase_runs": ("run_id", "route_name", "phase_name", "status"),
    "run_actions": ("run_id",),
    "route_candidates": ("run_id",),
    "content_type_cache": ("last_seen_run_id",),
    "review_candidates": ("last_seen_run_id", "resolved_run_id"),
    "review_decisions": ("decision_id",),
    "review_evidence_examples": ("decision_id",),
    "review_evidence_progress": ("pipeline_key",),
    "review_task_batches": ("batch_id",),
    "review_tasks": ("task_id",),
    "review_task_batch_memberships": ("membership_id",),
    "review_task_events": ("event_id",),
    "review_task_scan_progress": ("progress_id",),
    "review_task_source_publications": ("publication_id",),
    "file_actions": ("action_id", "run_id", "status"),
    "file_action_events": ("event_id", "action_id"),
    "file_action_reconciliation_events": (
        "reconciliation_event_id",
        "action_id",
    ),
}

_TARGET_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "run_events": ("event_id",),
    "route_phase_runs": ("run_id", "route_name", "phase_name"),
    "route_runs": ("run_id", "route_name"),
    "run_actions": ("run_id",),
    "route_candidates": ("run_id", "path"),
    "initial_runs": ("run_id",),
}

_MAX_SQLITE_INT: Final = 9223372036854775807


class FrameworkRunResetError(RuntimeError):
    """Base class for fail-closed Framework run-history reset errors."""


class FrameworkRunResetSchemaError(FrameworkRunResetError):
    """The connection does not expose the current Framework schema."""


class FrameworkRunResetStagingError(FrameworkRunResetError):
    """A destructive apply was attempted without an explicit staging gate."""


class FrameworkRunResetBusyError(FrameworkRunResetError):
    """An active run, route, phase, or filesystem-action frontier blocks reset."""

    def __init__(
        self,
        *,
        active_run_ids: Sequence[int] = (),
        active_route_count: int = 0,
        active_phase_count: int = 0,
        active_action_ids: Sequence[int] = (),
    ) -> None:
        self.active_run_ids = tuple(active_run_ids)
        self.active_route_count = int(active_route_count)
        self.active_phase_count = int(active_phase_count)
        self.active_action_ids = tuple(active_action_ids)
        detail = [
            *(f"run={value}" for value in self.active_run_ids[:16]),
            *([f"active_routes={self.active_route_count}"] if self.active_route_count else []),
            *([f"active_phases={self.active_phase_count}"] if self.active_phase_count else []),
            *(f"action={value}" for value in self.active_action_ids[:16]),
        ]
        if len(self.active_run_ids) > 16:
            detail.append(f"active_run_count={len(self.active_run_ids)}")
        if len(self.active_action_ids) > 16:
            detail.append(f"active_action_count={len(self.active_action_ids)}")
        super().__init__(
            "Framework run reset is blocked by active state"
            + (f": {', '.join(detail)}" if detail else "")
        )


class FrameworkRunResetChangedError(FrameworkRunResetError):
    """The staged state no longer matches the read-only reset plan."""


class FrameworkRunResetReferenceError(FrameworkRunResetError):
    """A preserved recovery reference would be invalidated by the reset."""


class FrameworkRunResetIntegrityError(FrameworkRunResetError):
    """The staged owner failed post-apply integrity or residual checks."""


@dataclass(frozen=True, slots=True)
class FrameworkRunReference:
    """A scalar run-id reference observed while planning the reset."""

    table: str
    column: str
    run_ids: tuple[int, ...]
    row_count: int
    deleted_run_ids: tuple[int, ...]
    retained_run_ids: tuple[int, ...]
    orphan_run_ids: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    def as_payload(self) -> dict[str, object]:
        return {
            "table": self.table,
            "column": self.column,
            "run_ids": list(self.run_ids),
            "row_count": self.row_count,
            "deleted_run_ids": list(self.deleted_run_ids),
            "retained_run_ids": list(self.retained_run_ids),
            "orphan_run_ids": list(self.orphan_run_ids),
        }


@dataclass(frozen=True, slots=True)
class FrameworkRunResetPlan:
    """Read-only, digest-bound plan for removing Framework run history."""

    schema_version: int
    requested_run_ids: tuple[int, ...]
    keep_run_ids: tuple[int, ...]
    delete_run_ids: tuple[int, ...]
    retained_run_ids: tuple[int, ...]
    recovery_run_ids: tuple[int, ...]
    active_run_ids: tuple[int, ...]
    active_route_count: int
    active_phase_count: int
    active_action_ids: tuple[int, ...]
    delete_counts: tuple[tuple[str, int], ...]
    references: tuple[FrameworkRunReference, ...]
    observed_max_run_id: int | None
    observed_max_referenced_run_id: int | None
    previous_run_id_floor: int | None
    next_run_id: int
    source_fingerprint: str
    plan_digest: str

    @property
    def rows_to_delete(self) -> int:
        return sum(value for _table, value in self.delete_counts)

    @property
    def cross_references(self) -> tuple[FrameworkRunReference, ...]:
        """Descriptive alias used by state-maintenance callers."""

        return self.references

    @property
    def delete_counts_by_table(self) -> dict[str, int]:
        return dict(self.delete_counts)

    @property
    def run_tables(self) -> tuple[str, ...]:
        """Tables with at least one row selected for deletion."""

        return tuple(table for table, count in self.delete_counts if count)

    @property
    def blocked(self) -> bool:
        return bool(
            self.active_run_ids
            or self.active_route_count
            or self.active_phase_count
            or self.active_action_ids
        )

    def as_payload(self, *, mode: str = "preview") -> dict[str, object]:
        return {
            "schema": FRAMEWORK_RUN_RESET_SCHEMA,
            "mode": mode,
            "schema_version": self.schema_version,
            "requested_run_ids": list(self.requested_run_ids),
            "keep_run_ids": list(self.keep_run_ids),
            "delete_run_ids": list(self.delete_run_ids),
            "retained_run_ids": list(self.retained_run_ids),
            "recovery_run_ids": list(self.recovery_run_ids),
            "active_run_ids": list(self.active_run_ids),
            "active_route_count": self.active_route_count,
            "active_phase_count": self.active_phase_count,
            "active_action_ids": list(self.active_action_ids),
            "delete_counts": dict(self.delete_counts),
            "run_tables": list(self.run_tables),
            "rows_to_delete": self.rows_to_delete,
            "references": [item.as_payload() for item in self.references],
            "observed_max_run_id": self.observed_max_run_id,
            "observed_max_referenced_run_id": self.observed_max_referenced_run_id,
            "previous_run_id_floor": self.previous_run_id_floor,
            "next_run_id": self.next_run_id,
            "source_fingerprint": self.source_fingerprint,
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True, slots=True)
class FrameworkRunResetResult:
    """Verified result of applying a plan to a staged connection."""

    plan: FrameworkRunResetPlan
    deleted_counts: tuple[tuple[str, int], ...]
    deleted_run_ids: tuple[int, ...]
    retained_run_ids: tuple[int, ...]
    next_run_id: int
    audit_metadata_key: str
    verified: bool

    @property
    def deleted_rows(self) -> int:
        return sum(value for _table, value in self.deleted_counts)

    def as_payload(self) -> dict[str, object]:
        payload = self.plan.as_payload(mode="applied")
        payload.update(
            {
                "deleted_counts": dict(self.deleted_counts),
                "deleted_rows": self.deleted_rows,
                "deleted_run_ids": list(self.deleted_run_ids),
                "retained_run_ids": list(self.retained_run_ids),
                "next_run_id": self.next_run_id,
                "audit_metadata_key": self.audit_metadata_key,
                "verified": self.verified,
            }
        )
        return payload


def _quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FrameworkRunResetSchemaError("invalid Framework identifier")
    return '"' + value.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM main.sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework schema cannot be inspected") from exc


def _validate_current_schema(connection: sqlite3.Connection) -> None:
    """Validate the minimal current-owner contract without running migrations."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    try:
        metadata_type = connection.execute(
            "SELECT type FROM main.sqlite_master WHERE name='metadata'"
        ).fetchone()
        if metadata_type is None or str(metadata_type[0]) != "table":
            raise FrameworkRunResetSchemaError("Framework metadata table is missing")
        rows = connection.execute(
            "SELECT value FROM main.metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
        if len(rows) != 1:
            raise FrameworkRunResetSchemaError("Framework schema version is not unique")
        try:
            version = int(str(rows[0][0]))
        except (TypeError, ValueError) as exc:
            raise FrameworkRunResetSchemaError("Framework schema version is malformed") from exc
        if str(rows[0][0]) != str(version) or version != SCHEMA_VERSION:
            raise FrameworkRunResetSchemaError(
                f"Framework run reset requires schema {SCHEMA_VERSION}; observed {version}"
            )
        tables = _table_names(connection)
        required = set(_REQUIRED_COLUMNS)
        missing = sorted(required - tables)
        if missing:
            raise FrameworkRunResetSchemaError(
                f"Framework schema is missing required table: {missing[0]}"
            )
        for table, columns in _REQUIRED_COLUMNS.items():
            actual = {
                str(row[1])
                for row in connection.execute(f"PRAGMA main.table_info({_quote_identifier(table)})")
            }
            missing_columns = sorted(set(columns) - actual)
            if missing_columns:
                raise FrameworkRunResetSchemaError(
                    f"Framework table {table!r} is missing column {missing_columns[0]!r}"
                )
        known_tables = (
            set(_RUN_HISTORY_TABLES) | set(_PRESERVED_TABLES) | {"curation_authorization_grants"}
        )
        for table in sorted(tables - known_tables):
            actual_columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA main.table_info({_quote_identifier(table)})")
            }
            if "run_id" in actual_columns or "source_run_id" in actual_columns:
                raise FrameworkRunResetSchemaError(
                    f"Framework table {table!r} has an unclassified run reference"
                )
    except FrameworkRunResetError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise FrameworkRunResetSchemaError("Framework schema cannot be validated") from exc


def _validate_read_connection(connection: sqlite3.Connection) -> None:
    _validate_current_schema(connection)
    try:
        attached = tuple(
            str(row[1])
            for row in connection.execute("PRAGMA database_list")
            if str(row[1]) not in {"main", "temp"}
        )
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError(
            "Framework attached databases cannot be inspected"
        ) from exc
    if attached:
        raise FrameworkRunResetSchemaError(
            "Framework run reset refuses connections with attached databases"
        )


def _validate_write_connection(connection: sqlite3.Connection, *, staged: bool) -> None:
    if staged is not True:
        raise FrameworkRunResetStagingError(
            "Framework run reset requires staged=True; live owners are never writable here"
        )
    _validate_read_connection(connection)
    try:
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise FrameworkRunResetSchemaError("Framework foreign-key mode cannot be verified") from exc
    if foreign_keys != 1:
        raise FrameworkRunResetSchemaError(
            "Framework run reset requires PRAGMA foreign_keys=ON on the staged owner"
        )
    if connection.in_transaction:
        raise FrameworkRunResetChangedError(
            "Framework staged connection has an uncommitted transaction"
        )


def _normalise_ids(values: Iterable[int], *, label: str) -> tuple[int, ...]:
    result: list[int] = []
    for value in values:
        if type(value) is not int or value < 0:
            raise ValueError(f"{label} must contain non-negative integers")
        result.append(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot contain duplicate identifiers")
    return tuple(sorted(result))


def _selected_run_ids(
    connection: sqlite3.Connection,
    run_ids: Sequence[int] | None,
) -> tuple[int, ...]:
    try:
        existing = tuple(
            int(row[0])
            for row in connection.execute("SELECT run_id FROM main.initial_runs ORDER BY run_id")
        )
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework runs cannot be enumerated") from exc
    if run_ids is None:
        return existing
    selected = _normalise_ids(run_ids, label="run_ids")
    missing = sorted(set(selected) - set(existing))
    if missing:
        raise FrameworkRunResetChangedError(
            f"Framework run does not exist in the reset plan: {missing[0]}"
        )
    return selected


def _keep_ids(
    selected: tuple[int, ...],
    keep_run_ids: Sequence[int],
) -> tuple[int, ...]:
    keep = _normalise_ids(keep_run_ids, label="keep_run_ids")
    outside = sorted(set(keep) - set(selected))
    if outside:
        raise ValueError(
            f"keep_run_ids must be selected for reset; unexpected identifier {outside[0]}"
        )
    return keep


def _ids_sql(values: Sequence[int]) -> tuple[str, tuple[int, ...]]:
    if not values:
        return "NULL", ()
    return ",".join("?" for _ in values), tuple(values)


def _count_for_runs(
    connection: sqlite3.Connection,
    table: str,
    run_ids: Sequence[int],
) -> int:
    placeholders, params = _ids_sql(run_ids)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM main.{_quote_identifier(table)} "
            f"WHERE run_id IN ({placeholders})",
            params,
        ).fetchone()
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError(f"Framework table {table!r} cannot be counted") from exc
    if row is None or type(row[0]) is not int:
        raise FrameworkRunResetSchemaError(f"Framework table {table!r} count is invalid")
    return int(row[0])


def _reference(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    deleted: tuple[int, ...],
    retained: tuple[int, ...],
    existing_run_ids: tuple[int, ...],
) -> FrameworkRunReference:
    try:
        rows = connection.execute(
            f"SELECT {_quote_identifier(column)} FROM main.{_quote_identifier(table)} "
            f"WHERE {_quote_identifier(column)} IS NOT NULL "
            f"ORDER BY {_quote_identifier(column)}",
        )
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError(
            f"Framework reference {table}.{column} cannot be inspected"
        ) from exc
    run_values_set: set[int] = set()
    row_count = 0
    for row in rows:
        if type(row[0]) is not int or int(row[0]) < 0:
            raise FrameworkRunResetSchemaError(
                f"Framework reference {table}.{column} is not integer-valued"
            )
        row_count += 1
        run_values_set.add(int(row[0]))
    run_values = tuple(sorted(run_values_set))
    return FrameworkRunReference(
        table=table,
        column=column,
        run_ids=run_values,
        row_count=row_count,
        deleted_run_ids=tuple(value for value in run_values if value in deleted),
        retained_run_ids=tuple(value for value in run_values if value in retained),
        # ``0`` is the documented empty/sentinel value for cache run links;
        # do not turn old cache rows with that default into a new reset gate.
        orphan_run_ids=tuple(
            value for value in run_values if value != 0 and value not in existing_run_ids
        ),
    )


def _recovery_run_ids(connection: sqlite3.Connection) -> tuple[int, ...]:
    try:
        rows = connection.execute(
            "SELECT DISTINCT run_id FROM main.file_actions "
            "WHERE status IN ('applying','recovery_required') "
            "ORDER BY run_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError(
            "Framework recovery frontier cannot be inspected"
        ) from exc
    for row in rows:
        if type(row[0]) is not int or int(row[0]) < 0:
            raise FrameworkRunResetSchemaError("Framework recovery run identifier is invalid")
    return tuple(int(row[0]) for row in rows)


_SOURCE_REFERENCE_SPECS: Final[tuple[tuple[str, str, str], ...]] = (
    ("initial_runs", "run_id", "source_run_id"),
    ("route_runs", "run_id", "source_run_id"),
    ("route_phase_runs", "run_id", "source_run_id"),
)


def _source_dependencies(
    connection: sqlite3.Connection,
    owner_run_ids: set[int],
) -> set[int]:
    """Return source runs needed by retained run-owned rows."""

    if not owner_run_ids:
        return set()
    placeholders, params = _ids_sql(tuple(sorted(owner_run_ids)))
    dependencies: set[int] = set()
    for table, owner_column, source_column in _SOURCE_REFERENCE_SPECS:
        try:
            rows = connection.execute(
                f"SELECT {_quote_identifier(source_column)} "
                f"FROM main.{_quote_identifier(table)} "
                f"WHERE {_quote_identifier(owner_column)} IN ({placeholders}) "
                f"AND {_quote_identifier(source_column)} IS NOT NULL",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            raise FrameworkRunResetSchemaError(
                f"Framework source references cannot be inspected: {table}"
            ) from exc
        for row in rows:
            if type(row[0]) is not int or int(row[0]) < 0:
                raise FrameworkRunResetSchemaError(
                    f"Framework source reference is not integer-valued: {table}.{source_column}"
                )
            dependencies.add(int(row[0]))
    return dependencies


def _active_state(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, ...], int, int, tuple[int, ...]]:
    try:
        run_rows = connection.execute(
            "SELECT run_id FROM main.initial_runs WHERE status='running' ORDER BY run_id"
        )
        runs_values: list[int] = []
        for row in run_rows:
            if type(row[0]) is not int or int(row[0]) < 0:
                raise FrameworkRunResetSchemaError("Framework active run identifier is invalid")
            runs_values.append(int(row[0]))
        runs = tuple(runs_values)
        route_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM main.route_runs WHERE status='running'"
            ).fetchone()[0]
        )
        phase_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM main.route_phase_runs WHERE status='running'"
            ).fetchone()[0]
        )
        action_rows = connection.execute(
            "SELECT action_id FROM main.file_actions "
            "WHERE status IN ('started','applying') ORDER BY action_id"
        )
        action_values: list[int] = []
        for row in action_rows:
            if type(row[0]) is not int or int(row[0]) < 0:
                raise FrameworkRunResetSchemaError("Framework active action identifier is invalid")
            action_values.append(int(row[0]))
        action_ids = tuple(action_values)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise FrameworkRunResetSchemaError("Framework active state cannot be inspected") from exc
    return runs, route_count, phase_count, action_ids


def _read_floor(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value FROM main.metadata WHERE key=?",
            (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework run-id floor cannot be read") from exc
    if row is None:
        return None
    value = str(row[0])
    if not value.isascii() or not value.isdecimal():
        raise FrameworkRunResetSchemaError("Framework run-id floor is malformed")
    parsed = int(value)
    if parsed < 0 or parsed >= _MAX_SQLITE_INT:
        raise FrameworkRunResetSchemaError("Framework run-id floor is out of range")
    return parsed


def read_framework_run_id_floor(connection: sqlite3.Connection) -> int | None:
    """Read the reset allocator floor without changing the connection."""

    _validate_read_connection(connection)
    return _read_floor(connection)


def framework_next_run_id(connection: sqlite3.Connection) -> int:
    """Return the next collision-free Framework run identifier.

    ``FrameworkState`` should perform its insert and any accompanying floor
    update in the same ``BEGIN IMMEDIATE`` transaction.  This helper is a
    read-only calculation and intentionally does not allocate the identifier.
    """

    _validate_read_connection(connection)
    try:
        max_run = connection.execute("SELECT MAX(run_id) FROM main.initial_runs").fetchone()[0]
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework run identifiers cannot be read") from exc
    floor = _read_floor(connection)
    referenced_max = _all_referenced_max(connection)
    candidates = [
        value
        for value in (
            floor,
            None if max_run is None else int(max_run),
            referenced_max,
        )
        if value is not None
    ]
    current = max(candidates, default=0)
    if current >= _MAX_SQLITE_INT - 1:
        raise FrameworkRunResetError("Framework run identifier space is exhausted")
    return current + 1


def _referenced_max(
    connection: sqlite3.Connection,
    selected: tuple[int, ...],
    references: tuple[FrameworkRunReference, ...],
) -> int | None:
    values = list(selected)
    for reference in references:
        values.extend(reference.run_ids)
    return max(values, default=None)


def _all_referenced_max(connection: sqlite3.Connection) -> int | None:
    """Read the highest run-id retained by a cache/recovery/provenance row."""

    values: list[int] = []
    for table, column in _REFERENCE_SPECS:
        try:
            rows = connection.execute(
                f"SELECT {_quote_identifier(column)} FROM main.{_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL"
            )
        except sqlite3.Error as exc:
            raise FrameworkRunResetSchemaError(
                f"Framework references cannot be read: {table}.{column}"
            ) from exc
        for row in rows:
            if type(row[0]) is not int or int(row[0]) < 0:
                raise FrameworkRunResetSchemaError(
                    f"Framework reference is not integer-valued: {table}.{column}"
                )
            if int(row[0]) != 0:
                values.append(int(row[0]))
    return max(values, default=None)


def _row_values(
    connection: sqlite3.Connection, query: str, params: Sequence[int]
) -> Iterable[tuple[Any, ...]]:
    try:
        cursor = connection.execute(query, tuple(params))
        for row in cursor:
            yield tuple(row)
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework reset fingerprint could not be read") from exc


def _canonical_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bytes):
        return "bytes:" + value.hex()
    return f"{type(value).__name__}:{value!r}"


def _fingerprint(connection: sqlite3.Connection, selected: tuple[int, ...]) -> str:
    """Hash only reset candidates and their preserved scalar references."""

    digest = hashlib.sha256()
    placeholders, params = _ids_sql(selected)
    for table in _RUN_HISTORY_TABLES:
        order = ",".join(_quote_identifier(column) for column in _TARGET_ORDER[table])
        query = (
            f"SELECT * FROM main.{_quote_identifier(table)} "
            f"WHERE run_id IN ({placeholders}) ORDER BY {order}"
        )
        digest.update(f"target:{table}\n".encode("ascii"))
        for row in _row_values(connection, query, params):
            digest.update(
                ("|".join(_canonical_value(value) for value in row) + "\n").encode(
                    "utf-8",
                    "surrogatepass",
                )
            )
    for table, column in _REFERENCE_SPECS:
        order = _quote_identifier(column)
        query = (
            f"SELECT * FROM main.{_quote_identifier(table)} "
            f"WHERE {_quote_identifier(column)} IS NOT NULL "
            f"AND {_quote_identifier(column)} IN ({placeholders}) ORDER BY {order}"
        )
        digest.update(f"reference:{table}.{column}\n".encode("ascii"))
        for row in _row_values(connection, query, params):
            digest.update(
                ("|".join(_canonical_value(value) for value in row) + "\n").encode(
                    "utf-8",
                    "surrogatepass",
                )
            )
    floor = _read_floor(connection)
    digest.update(f"floor:{floor!r}\n".encode("ascii"))
    return digest.hexdigest()


def _digest_payload(payload: dict[str, object]) -> str:
    without_digest = dict(payload)
    without_digest.pop("plan_digest", None)
    raw = json.dumps(
        without_digest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _ids_digest(values: Sequence[int]) -> str:
    raw = json.dumps(list(values), separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def plan_framework_run_reset(
    connection: sqlite3.Connection,
    *,
    run_ids: Sequence[int] | None = None,
    keep_run_ids: Sequence[int] = (),
) -> FrameworkRunResetPlan:
    """Build a read-only plan for deleting Framework run history.

    ``run_ids=None`` selects all currently present ``initial_runs`` rows.
    Any parent named by a preserved cache, Review, action or provenance row is
    retained so the reset cannot publish an orphan reference.  Recovery
    frontiers are additionally reported in ``recovery_run_ids``.  Review,
    cache and recovery rows themselves are never candidates for deletion.
    """

    _validate_read_connection(connection)
    selected = _selected_run_ids(connection, run_ids)
    keep = _keep_ids(selected, keep_run_ids)
    recovery = _recovery_run_ids(connection)
    selected_set = set(selected)
    # A preserved cache/review/action row still needs the run it names.  Keep
    # those parent runs rather than publishing an orphaned reference.  Source
    # links are closed transitively for retained operational rows.
    retained_set = (set(keep) | (set(recovery) & selected_set)) & selected_set
    try:
        existing_rows = connection.execute("SELECT run_id FROM main.initial_runs ORDER BY run_id")
    except sqlite3.Error as exc:
        raise FrameworkRunResetSchemaError("Framework run identifiers cannot be read") from exc
    existing_values: list[int] = []
    for row in existing_rows:
        if type(row[0]) is not int or int(row[0]) < 0:
            raise FrameworkRunResetSchemaError("Framework run identifier is invalid")
        existing_values.append(int(row[0]))
    existing_run_ids = tuple(existing_values)
    while True:
        tentative_delete = tuple(sorted(selected_set - retained_set))
        tentative_retained = tuple(sorted(retained_set))
        references = tuple(
            _reference(
                connection,
                table,
                column,
                tentative_delete,
                tentative_retained,
                existing_run_ids,
            )
            for table, column in _REFERENCE_SPECS
        )
        referenced_parents = {
            value
            for reference in references
            for value in reference.run_ids
            if value in selected_set
        }
        source_parents = _source_dependencies(connection, retained_set) & selected_set
        additions = (referenced_parents | source_parents) - retained_set
        if not additions:
            break
        retained_set.update(additions)
    recovery_selected = tuple(value for value in recovery if value in selected_set)
    delete = tuple(sorted(selected_set - retained_set))
    retained = tuple(sorted(retained_set))
    active_runs, active_route_count, active_phase_count, active_action_ids = _active_state(
        connection
    )
    references = tuple(
        _reference(
            connection,
            table,
            column,
            delete,
            retained,
            existing_run_ids,
        )
        for table, column in _REFERENCE_SPECS
    )
    delete_counts = tuple(
        (table, _count_for_runs(connection, table, delete)) for table in _RUN_HISTORY_TABLES
    )
    observed_max = max(existing_run_ids, default=None)
    referenced_max = _referenced_max(connection, selected, references)
    previous_floor = _read_floor(connection)
    floor = (
        max(value for value in (previous_floor, observed_max, referenced_max) if value is not None)
        if any(value is not None for value in (previous_floor, observed_max, referenced_max))
        else 0
    )
    if floor >= _MAX_SQLITE_INT - 1:
        raise FrameworkRunResetError("Framework run identifier space is exhausted")
    next_run_id = floor + 1
    source_fingerprint = _fingerprint(connection, selected)
    provisional = FrameworkRunResetPlan(
        schema_version=SCHEMA_VERSION,
        requested_run_ids=selected,
        keep_run_ids=keep,
        delete_run_ids=delete,
        retained_run_ids=retained,
        recovery_run_ids=recovery_selected,
        active_run_ids=active_runs,
        active_route_count=active_route_count,
        active_phase_count=active_phase_count,
        active_action_ids=active_action_ids,
        delete_counts=delete_counts,
        references=references,
        observed_max_run_id=observed_max,
        observed_max_referenced_run_id=referenced_max,
        previous_run_id_floor=previous_floor,
        next_run_id=next_run_id,
        source_fingerprint=source_fingerprint,
        plan_digest="",
    )
    digest = _digest_payload(provisional.as_payload())
    return FrameworkRunResetPlan(
        schema_version=provisional.schema_version,
        requested_run_ids=provisional.requested_run_ids,
        keep_run_ids=provisional.keep_run_ids,
        delete_run_ids=provisional.delete_run_ids,
        retained_run_ids=provisional.retained_run_ids,
        recovery_run_ids=provisional.recovery_run_ids,
        active_run_ids=provisional.active_run_ids,
        active_route_count=provisional.active_route_count,
        active_phase_count=provisional.active_phase_count,
        active_action_ids=provisional.active_action_ids,
        delete_counts=provisional.delete_counts,
        references=provisional.references,
        observed_max_run_id=provisional.observed_max_run_id,
        observed_max_referenced_run_id=provisional.observed_max_referenced_run_id,
        previous_run_id_floor=provisional.previous_run_id_floor,
        next_run_id=provisional.next_run_id,
        source_fingerprint=provisional.source_fingerprint,
        plan_digest=digest,
    )


def _raise_if_busy(plan: FrameworkRunResetPlan) -> None:
    if plan.blocked:
        raise FrameworkRunResetBusyError(
            active_run_ids=plan.active_run_ids,
            active_route_count=plan.active_route_count,
            active_phase_count=plan.active_phase_count,
            active_action_ids=plan.active_action_ids,
        )


def _raise_if_orphan_references(plan: FrameworkRunResetPlan) -> None:
    orphaned = tuple(
        f"{reference.key}={value}"
        for reference in plan.references
        for value in reference.orphan_run_ids
    )
    if orphaned:
        raise FrameworkRunResetReferenceError(
            "Framework state already contains orphaned run references: " + ",".join(orphaned)
        )


def _write_reset_markers(
    connection: sqlite3.Connection,
    plan: FrameworkRunResetPlan,
    *,
    occurred_ns: int,
) -> None:
    floor = plan.next_run_id - 1
    connection.execute(
        "INSERT INTO main.metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY, str(floor)),
    )
    marker = {
        "schema": FRAMEWORK_RUN_RESET_SCHEMA,
        "plan_digest": plan.plan_digest,
        "occurred_ns": occurred_ns,
        "deleted_run_count": len(plan.delete_run_ids),
        "deleted_rows": plan.rows_to_delete,
        "retained_run_count": len(plan.retained_run_ids),
        "recovery_run_count": len(plan.recovery_run_ids),
        "recovery_run_ids_sha256": _ids_digest(plan.recovery_run_ids),
        "next_run_id": plan.next_run_id,
        "orphan_reference_count": sum(
            len(reference.orphan_run_ids) for reference in plan.references
        ),
        "references": {
            reference.key: {
                "row_count": reference.row_count,
                "deleted_run_count": len(reference.deleted_run_ids),
                "retained_run_count": len(reference.retained_run_ids),
            }
            for reference in plan.references
        },
    }
    connection.execute(
        "INSERT INTO main.metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            FRAMEWORK_RUN_RESET_METADATA_KEY,
            json.dumps(marker, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        ),
    )


def _delete_run_rows(
    connection: sqlite3.Connection,
    delete_run_ids: Sequence[int],
) -> tuple[tuple[str, int], ...]:
    counts: list[tuple[str, int]] = []
    if not delete_run_ids:
        return tuple((table, 0) for table in _RUN_HISTORY_TABLES)
    # Child-like run-history tables are removed before initial_runs.  Current
    # Framework has no FK from these tables to initial_runs, but the order is
    # intentional and remains safe if a future migration adds one.
    for table in _RUN_HISTORY_TABLES:
        placeholders, params = _ids_sql(delete_run_ids)
        cursor = connection.execute(
            f"DELETE FROM main.{_quote_identifier(table)} WHERE run_id IN ({placeholders})",
            params,
        )
        counts.append((table, int(cursor.rowcount)))
    return tuple(counts)


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    try:
        fk = connection.execute("PRAGMA foreign_key_check").fetchone()
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    except sqlite3.Error as exc:
        raise FrameworkRunResetIntegrityError("Framework staged integrity check failed") from exc
    return fk is None and integrity == ("ok",)


def apply_framework_run_reset(
    connection: sqlite3.Connection,
    plan: FrameworkRunResetPlan,
    *,
    staged: bool = False,
) -> FrameworkRunResetResult:
    """Apply a digest-bound plan to an explicitly staged Framework database.

    The function owns one ``BEGIN IMMEDIATE`` transaction and never opens,
    replaces, or deletes a filesystem path.  The caller remains responsible
    for verified backup, stage promotion, rollback, locks and the publication
    receipt around that transaction.
    """

    if not isinstance(plan, FrameworkRunResetPlan):
        raise TypeError("plan must be a FrameworkRunResetPlan")
    _validate_write_connection(connection, staged=staged)
    if plan.schema_version != SCHEMA_VERSION:
        raise FrameworkRunResetSchemaError("Framework reset plan schema version is unsupported")
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = plan_framework_run_reset(
            connection,
            run_ids=plan.requested_run_ids,
            keep_run_ids=plan.keep_run_ids,
        )
        if current.plan_digest != plan.plan_digest:
            raise FrameworkRunResetChangedError(
                "Framework staged state changed; create a new reset preview"
            )
        _raise_if_busy(current)
        _raise_if_orphan_references(current)
        # The planner closes all preserved file-action/review/cache parents,
        # but keep this assertion adjacent to the destructive boundary so a
        # future planner change cannot accidentally orphan one.
        if any(
            reference.deleted_run_ids
            for reference in current.references
            if reference.table in {"file_actions", "content_type_cache", "review_candidates"}
        ):
            raise FrameworkRunResetReferenceError(
                "Framework reset would orphan a preserved run reference"
            )
        deleted_counts = _delete_run_rows(connection, current.delete_run_ids)
        _write_reset_markers(connection, current, occurred_ns=time.time_ns())
        if not _integrity_ok(connection):
            raise FrameworkRunResetIntegrityError(
                "Framework staged owner failed integrity verification before commit"
            )
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise

    # Verify the committed staging copy without re-planning with deleted IDs
    # (an exact plan intentionally rejects IDs that no longer exist).
    residual_values: list[tuple[str, int]] = []
    for table in _RUN_HISTORY_TABLES:
        count = _count_for_runs(connection, table, current.delete_run_ids)
        if count:
            residual_values.append((table, count))
    residual = tuple(residual_values)
    if residual:
        raise FrameworkRunResetIntegrityError(
            f"Framework reset left residual run-history rows: {residual!r}"
        )
    if not _integrity_ok(connection):
        raise FrameworkRunResetIntegrityError(
            "Framework staged owner failed post-commit integrity verification"
        )
    return FrameworkRunResetResult(
        plan=current,
        deleted_counts=deleted_counts,
        deleted_run_ids=current.delete_run_ids,
        retained_run_ids=current.retained_run_ids,
        next_run_id=current.next_run_id,
        audit_metadata_key=FRAMEWORK_RUN_RESET_METADATA_KEY,
        verified=True,
    )


# Discoverable aliases for adapters that use "prepare/execute" vocabulary.
prepare_framework_run_reset = plan_framework_run_reset
execute_framework_run_reset = apply_framework_run_reset


__all__ = (
    "FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY",
    "FRAMEWORK_RUN_RESET_METADATA_KEY",
    "FRAMEWORK_RUN_RESET_SCHEMA",
    "FrameworkRunReference",
    "FrameworkRunResetBusyError",
    "FrameworkRunResetChangedError",
    "FrameworkRunResetError",
    "FrameworkRunResetIntegrityError",
    "FrameworkRunResetPlan",
    "FrameworkRunResetReferenceError",
    "FrameworkRunResetResult",
    "FrameworkRunResetSchemaError",
    "FrameworkRunResetStagingError",
    "apply_framework_run_reset",
    "execute_framework_run_reset",
    "framework_next_run_id",
    "plan_framework_run_reset",
    "prepare_framework_run_reset",
    "read_framework_run_id_floor",
)
