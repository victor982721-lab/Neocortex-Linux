"""Bounded immutable observability for the published Code SQLite owner.

This module reports storage shape and growth signals only.  It never creates,
migrates, checkpoints, vacuums, prunes, removes sidecars, or opens a writable
connection.  Row counts are lower bounds once their declared scan cap is hit.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .code_schema import CODE_SCHEMA_VERSION, validate_code_schema
from .sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    capture_sqlite_immutable_fence,
    immutable_sqlite_database,
)


CODE_STORAGE_ANALYSIS_SCHEMA = "neocortex.code-storage-analysis/v1"
CODE_STORAGE_ANALYSIS_POLICY = "immutable-bounded-storage-observability-v1"
CODE_STORAGE_MAX_TABLES = 64
CODE_STORAGE_MAX_PROVIDERS = 128
CODE_STORAGE_MAX_RUNS = 50
CODE_STORAGE_DEFAULT_ROW_SCAN_LIMIT = 250_000
CODE_STORAGE_MAX_ROW_SCAN_LIMIT = 1_000_000

StorageStatus = Literal["ready", "abstained"]
TemporalStatus = Literal["resolved", "not_applicable", "unresolved"]


def _required(label: str, value: object, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _strings(label: str, values: object, *, ordered: bool = False) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, item) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if ordered and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be canonically ordered")
    return result


@dataclass(frozen=True, slots=True)
class CodeStorageBoundedCount:
    observed_rows: int
    truncated: bool
    scan_limit: int

    def __post_init__(self) -> None:
        _nonnegative("observed storage rows", self.observed_rows)
        if not isinstance(self.truncated, bool):
            raise ValueError("bounded storage truncation must be boolean")
        if isinstance(self.scan_limit, bool) or not 1 <= self.scan_limit <= (
            CODE_STORAGE_MAX_ROW_SCAN_LIMIT
        ):
            raise ValueError("storage row scan limit is invalid")
        if self.truncated != (self.observed_rows == self.scan_limit):
            raise ValueError("bounded storage count truncation is inconsistent")


@dataclass(frozen=True, slots=True)
class CodeStorageTableObservation:
    table_name: str
    rows: CodeStorageBoundedCount
    temporal_status: TemporalStatus
    current_rows: CodeStorageBoundedCount | None
    historical_rows: CodeStorageBoundedCount | None
    temporal_basis: str | None

    def __post_init__(self) -> None:
        _required("Code storage table", self.table_name, 128)
        if self.temporal_status not in {"resolved", "not_applicable", "unresolved"}:
            raise ValueError("storage temporal status is invalid")
        if self.temporal_status == "resolved":
            if self.current_rows is None or self.historical_rows is None:
                raise ValueError("resolved temporal storage count is incomplete")
            _required("storage temporal basis", self.temporal_basis, 512)
            if (
                not self.rows.truncated
                and not self.current_rows.truncated
                and not self.historical_rows.truncated
                and self.rows.observed_rows
                != self.current_rows.observed_rows + self.historical_rows.observed_rows
            ):
                raise ValueError("resolved temporal storage rows are not a partition")
        elif (
            self.current_rows is not None
            or self.historical_rows is not None
            or self.temporal_basis is not None
        ):
            raise ValueError("unresolved temporal storage count asserts observations")


@dataclass(frozen=True, slots=True)
class CodeStorageProviderObservation:
    provider_id: str
    runs: CodeStorageBoundedCount
    current_runs: CodeStorageBoundedCount
    historical_runs: CodeStorageBoundedCount
    findings: CodeStorageBoundedCount
    metrics: CodeStorageBoundedCount
    relations: CodeStorageBoundedCount
    inputs: CodeStorageBoundedCount

    def __post_init__(self) -> None:
        _required("Code storage provider", self.provider_id, 256)
        if (
            not self.runs.truncated
            and not self.current_runs.truncated
            and not self.historical_runs.truncated
            and self.runs.observed_rows
            != self.current_runs.observed_rows + self.historical_runs.observed_rows
        ):
            raise ValueError("provider current and historical runs are not a partition")


@dataclass(frozen=True, slots=True)
class CodeStorageRunObservation:
    analysis_run_id: int
    status: str
    current: bool
    external_runs: CodeStorageBoundedCount
    findings: CodeStorageBoundedCount
    metrics: CodeStorageBoundedCount
    relations: CodeStorageBoundedCount
    inputs: CodeStorageBoundedCount
    experiment_receipts: CodeStorageBoundedCount

    def __post_init__(self) -> None:
        _nonnegative("Code storage analysis run id", self.analysis_run_id)
        _required("Code storage analysis run status", self.status, 64)
        if not isinstance(self.current, bool):
            raise ValueError("Code storage current run flag must be boolean")


@dataclass(frozen=True, slots=True)
class CodeStorageGrowthPreview:
    latest_completed_run_id: int | None
    previous_completed_run_id: int | None
    latest_external_rows: int | None
    previous_external_rows: int | None
    external_row_delta: int | None
    comparable: bool
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.comparable, bool):
            raise ValueError("storage growth comparability must be boolean")
        for value in (self.latest_completed_run_id, self.previous_completed_run_id):
            if value is not None:
                _nonnegative("storage growth run id", value)
        for value in (self.latest_external_rows, self.previous_external_rows):
            if value is not None:
                _nonnegative("storage growth external rows", value)
        if self.comparable:
            if (
                self.latest_completed_run_id is None
                or self.previous_completed_run_id is None
                or self.latest_external_rows is None
                or self.previous_external_rows is None
                or self.external_row_delta
                != self.latest_external_rows - self.previous_external_rows
                or self.reason is not None
            ):
                raise ValueError("comparable storage growth preview is incomplete")
        else:
            _required("storage growth limitation", self.reason, 256)
            if self.external_row_delta is not None:
                raise ValueError("non-comparable storage growth cannot publish a delta")


@dataclass(frozen=True, slots=True)
class CodeStorageRetentionPreview:
    retain_latest_completed_runs: int
    retained_run_ids: tuple[int, ...]
    historical_rows_observed: int
    observations_truncated: bool
    deletion_supported: Literal[False]
    action: Literal["preview_only"]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.retain_latest_completed_runs, bool) or not (
            1 <= self.retain_latest_completed_runs <= CODE_STORAGE_MAX_RUNS
        ):
            raise ValueError("storage retention run window is invalid")
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in self.retained_run_ids
            )
            or len(set(self.retained_run_ids)) != len(self.retained_run_ids)
            or self.retained_run_ids != tuple(sorted(self.retained_run_ids, reverse=True))
        ):
            raise ValueError("storage retained runs are invalid")
        _nonnegative("historical storage rows", self.historical_rows_observed)
        if not isinstance(self.observations_truncated, bool):
            raise ValueError("storage retention truncation must be boolean")
        if self.deletion_supported is not False or self.action != "preview_only":
            raise ValueError("storage retention analysis cannot authorize deletion")
        _strings("storage retention limitation", self.limitations)
        if not self.limitations:
            raise ValueError("storage retention preview requires limitations")


@dataclass(frozen=True, slots=True)
class CodeStorageAnalysis:
    database: str
    status: StorageStatus
    reason: str | None
    policy_id: str
    schema_version: int | None
    database_file_bytes: int
    page_size_bytes: int
    page_count: int
    freelist_pages: int
    allocated_bytes: int
    used_page_bytes: int
    tables: tuple[CodeStorageTableObservation, ...]
    providers: tuple[CodeStorageProviderObservation, ...]
    runs: tuple[CodeStorageRunObservation, ...]
    runs_truncated: bool
    growth: CodeStorageGrowthPreview | None
    retention: CodeStorageRetentionPreview | None
    source_fence: Mapping[str, object] | None
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("Code storage database", self.database)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("Code storage status is invalid")
        if self.policy_id != CODE_STORAGE_ANALYSIS_POLICY:
            raise ValueError("Code storage policy is invalid")
        for label, value in (
            ("database file bytes", self.database_file_bytes),
            ("page size bytes", self.page_size_bytes),
            ("page count", self.page_count),
            ("freelist pages", self.freelist_pages),
            ("allocated bytes", self.allocated_bytes),
            ("used page bytes", self.used_page_bytes),
        ):
            _nonnegative(label, value)
        if len(self.tables) > CODE_STORAGE_MAX_TABLES:
            raise ValueError("Code storage table bound exceeded")
        if len(self.providers) > CODE_STORAGE_MAX_PROVIDERS:
            raise ValueError("Code storage provider bound exceeded")
        if len(self.runs) > CODE_STORAGE_MAX_RUNS:
            raise ValueError("Code storage run bound exceeded")
        if not isinstance(self.runs_truncated, bool):
            raise ValueError("Code storage run truncation must be boolean")
        if tuple(item.table_name for item in self.tables) != tuple(
            sorted(item.table_name for item in self.tables)
        ):
            raise ValueError("Code storage tables are not ordered")
        if tuple(item.provider_id for item in self.providers) != tuple(
            sorted(item.provider_id for item in self.providers)
        ):
            raise ValueError("Code storage providers are not ordered")
        if tuple(item.analysis_run_id for item in self.runs) != tuple(
            sorted((item.analysis_run_id for item in self.runs), reverse=True)
        ):
            raise ValueError("Code storage runs are not newest-first")
        _strings("Code storage limitation", self.limitations)
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("Code storage analysis must remain advisory and non-mutating")
        if self.status == "ready":
            if (
                self.reason is not None
                or self.schema_version != CODE_SCHEMA_VERSION
                or self.page_size_bytes < 512
                or self.page_count < 1
                or self.freelist_pages > self.page_count
                or self.allocated_bytes != self.page_size_bytes * self.page_count
                or self.used_page_bytes
                != self.page_size_bytes * (self.page_count - self.freelist_pages)
                or self.source_fence is None
                or self.growth is None
                or self.retention is None
            ):
                raise ValueError("ready Code storage analysis is incomplete")
        else:
            _required("Code storage abstention reason", self.reason, 256)
            if (
                self.schema_version is not None
                or self.tables
                or self.providers
                or self.runs
                or self.growth is not None
                or self.retention is not None
                or self.source_fence is not None
            ):
                raise ValueError("abstained Code storage analysis asserts observations")

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": "code-storage-analysis",
            "schema": CODE_STORAGE_ANALYSIS_SCHEMA,
            **asdict(self),
        }


def _bounded_count(
    connection: sqlite3.Connection,
    select_sql: str,
    parameters: Sequence[object],
    *,
    scan_limit: int,
) -> CodeStorageBoundedCount:
    row = connection.execute(
        f"SELECT COUNT(*) FROM ({select_sql} LIMIT ?)",
        (*parameters, scan_limit),
    ).fetchone()
    if row is None:
        raise RuntimeError("bounded Code storage count returned no row")
    observed = int(row[0])
    return CodeStorageBoundedCount(observed, observed == scan_limit, scan_limit)


def _quoted_identifier(value: str) -> str:
    _required("SQLite identifier", value, 128)
    return '"' + value.replace('"', '""') + '"'


def _table_count(
    connection: sqlite3.Connection,
    table: str,
    *,
    scan_limit: int,
) -> CodeStorageBoundedCount:
    return _bounded_count(
        connection,
        f"SELECT 1 FROM {_quoted_identifier(table)}",
        (),
        scan_limit=scan_limit,
    )


def _temporal_queries(
    table: str,
    latest_run_id: int | None,
) -> tuple[str, tuple[object, ...], str, tuple[object, ...], str] | None:
    if table == "files":
        return (
            "SELECT 1 FROM files WHERE status='current'",
            (),
            "SELECT 1 FROM files WHERE status<>'current'",
            (),
            "files.status",
        )
    if table == "file_versions":
        return (
            "SELECT 1 FROM file_versions WHERE invalidated_ns IS NULL",
            (),
            "SELECT 1 FROM file_versions WHERE invalidated_ns IS NOT NULL",
            (),
            "file_versions.invalidated_ns",
        )
    if table == "projects":
        return (
            "SELECT 1 FROM projects WHERE status='current'",
            (),
            "SELECT 1 FROM projects WHERE status<>'current'",
            (),
            "projects.status",
        )
    if table == "embedding_links":
        return (
            "SELECT 1 FROM embedding_links WHERE active=1",
            (),
            "SELECT 1 FROM embedding_links WHERE active=0",
            (),
            "embedding_links.active",
        )
    if table == "invalidation_history":
        return (
            "SELECT 1 FROM invalidation_history WHERE 0",
            (),
            "SELECT 1 FROM invalidation_history",
            (),
            "invalidation_history_is_historical_by_contract",
        )
    if latest_run_id is not None and table in {
        "analysis_runs",
        "external_tool_runs",
        "code_experiment_receipts",
    }:
        column = "analysis_run_id"
        return (
            f"SELECT 1 FROM {_quoted_identifier(table)} WHERE {column}=?",
            (latest_run_id,),
            f"SELECT 1 FROM {_quoted_identifier(table)} WHERE {column}<>?",
            (latest_run_id,),
            f"{table}.analysis_run_id_vs_latest_completed",
        )
    return None


def _tables(
    connection: sqlite3.Connection,
    *,
    latest_run_id: int | None,
    scan_limit: int,
) -> tuple[CodeStorageTableObservation, ...]:
    names = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name LIMIT ?",
            (CODE_STORAGE_MAX_TABLES + 1,),
        )
    )
    if len(names) > CODE_STORAGE_MAX_TABLES:
        raise RuntimeError("Code storage table inventory exceeds its bound")
    result: list[CodeStorageTableObservation] = []
    for name in names:
        total = _table_count(connection, name, scan_limit=scan_limit)
        temporal = _temporal_queries(name, latest_run_id)
        if temporal is None:
            status: TemporalStatus = (
                "not_applicable" if name in {"metadata", "schema_migrations"} else "unresolved"
            )
            result.append(CodeStorageTableObservation(name, total, status, None, None, None))
            continue
        current_sql, current_parameters, historical_sql, historical_parameters, basis = temporal
        result.append(
            CodeStorageTableObservation(
                name,
                total,
                "resolved",
                _bounded_count(
                    connection,
                    current_sql,
                    current_parameters,
                    scan_limit=scan_limit,
                ),
                _bounded_count(
                    connection,
                    historical_sql,
                    historical_parameters,
                    scan_limit=scan_limit,
                ),
                basis,
            )
        )
    return tuple(result)


def _latest_completed_runs(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, ...]:
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT analysis_run_id FROM analysis_runs WHERE status='completed' "
            "ORDER BY analysis_run_id DESC LIMIT ?",
            (limit,),
        )
    )


def _run_external_count(
    connection: sqlite3.Connection,
    run_id: int,
    table: Literal[
        "external_tool_runs",
        "external_findings",
        "external_metrics",
        "external_relations",
        "external_run_inputs",
    ],
    *,
    scan_limit: int,
) -> CodeStorageBoundedCount:
    if table == "external_tool_runs":
        sql = "SELECT 1 FROM external_tool_runs WHERE analysis_run_id=?"
    else:
        sql = (
            f"SELECT 1 FROM {table} AS child JOIN external_tool_runs AS run "
            "ON run.tool_run_id=child.tool_run_id WHERE run.analysis_run_id=?"
        )
    return _bounded_count(connection, sql, (run_id,), scan_limit=scan_limit)


def _run_observation(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    current_run_id: int | None,
    scan_limit: int,
) -> CodeStorageRunObservation:
    status_row = connection.execute(
        "SELECT status FROM analysis_runs WHERE analysis_run_id=?",
        (run_id,),
    ).fetchone()
    if status_row is None:
        raise RuntimeError("Code storage run disappeared during immutable read")
    receipts = _bounded_count(
        connection,
        "SELECT 1 FROM code_experiment_receipts WHERE analysis_run_id=?",
        (run_id,),
        scan_limit=scan_limit,
    )
    return CodeStorageRunObservation(
        run_id,
        str(status_row[0]),
        run_id == current_run_id,
        _run_external_count(connection, run_id, "external_tool_runs", scan_limit=scan_limit),
        _run_external_count(connection, run_id, "external_findings", scan_limit=scan_limit),
        _run_external_count(connection, run_id, "external_metrics", scan_limit=scan_limit),
        _run_external_count(connection, run_id, "external_relations", scan_limit=scan_limit),
        _run_external_count(connection, run_id, "external_run_inputs", scan_limit=scan_limit),
        receipts,
    )


def _providers(
    connection: sqlite3.Connection,
    *,
    current_run_id: int | None,
    scan_limit: int,
) -> tuple[CodeStorageProviderObservation, ...]:
    provider_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT provider_id FROM external_run_contracts ORDER BY provider_id LIMIT ?",
            (CODE_STORAGE_MAX_PROVIDERS + 1,),
        )
    )
    if len(provider_ids) > CODE_STORAGE_MAX_PROVIDERS:
        raise RuntimeError("Code storage provider inventory exceeds its bound")
    result: list[CodeStorageProviderObservation] = []
    for provider_id in provider_ids:
        base = (
            "SELECT 1 FROM external_run_contracts AS contract "
            "JOIN external_tool_runs AS run ON run.tool_run_id=contract.tool_run_id "
            "WHERE contract.provider_id=?"
        )
        current = base + (" AND run.analysis_run_id=?" if current_run_id is not None else " AND 0")
        historical = base + (" AND run.analysis_run_id<>?" if current_run_id is not None else "")
        historical_parameters: tuple[object, ...] = (
            (provider_id, current_run_id) if current_run_id is not None else (provider_id,)
        )

        result.append(
            CodeStorageProviderObservation(
                provider_id,
                _bounded_count(connection, base, (provider_id,), scan_limit=scan_limit),
                _bounded_count(
                    connection,
                    current,
                    (
                        (provider_id, current_run_id)
                        if current_run_id is not None
                        else (provider_id,)
                    ),
                    scan_limit=scan_limit,
                ),
                _bounded_count(
                    connection,
                    historical,
                    historical_parameters,
                    scan_limit=scan_limit,
                ),
                _provider_child_count(
                    connection,
                    "external_findings",
                    provider_id,
                    scan_limit=scan_limit,
                ),
                _provider_child_count(
                    connection,
                    "external_metrics",
                    provider_id,
                    scan_limit=scan_limit,
                ),
                _provider_child_count(
                    connection,
                    "external_relations",
                    provider_id,
                    scan_limit=scan_limit,
                ),
                _provider_child_count(
                    connection,
                    "external_run_inputs",
                    provider_id,
                    scan_limit=scan_limit,
                ),
            )
        )
    return tuple(result)


def _provider_child_count(
    connection: sqlite3.Connection,
    table: Literal[
        "external_findings",
        "external_metrics",
        "external_relations",
        "external_run_inputs",
    ],
    provider_id: str,
    *,
    scan_limit: int,
) -> CodeStorageBoundedCount:
    return _bounded_count(
        connection,
        f"SELECT 1 FROM {table} AS child "
        "JOIN external_tool_runs AS run ON run.tool_run_id=child.tool_run_id "
        "JOIN external_run_contracts AS contract "
        "ON contract.tool_run_id=run.tool_run_id WHERE contract.provider_id=?",
        (provider_id,),
        scan_limit=scan_limit,
    )


def _external_rows(run: CodeStorageRunObservation) -> tuple[int, bool]:
    counts = (run.external_runs, run.findings, run.metrics, run.relations, run.inputs)
    return sum(item.observed_rows for item in counts), any(item.truncated for item in counts)


def _growth(runs: tuple[CodeStorageRunObservation, ...]) -> CodeStorageGrowthPreview:
    completed = tuple(item for item in runs if item.status == "completed")
    if len(completed) < 2:
        return CodeStorageGrowthPreview(
            completed[0].analysis_run_id if completed else None,
            None,
            None,
            None,
            None,
            False,
            "two_completed_runs_are_required_for_growth",
        )
    latest, previous = completed[:2]
    latest_rows, latest_truncated = _external_rows(latest)
    previous_rows, previous_truncated = _external_rows(previous)
    if latest_truncated or previous_truncated:
        return CodeStorageGrowthPreview(
            latest.analysis_run_id,
            previous.analysis_run_id,
            latest_rows,
            previous_rows,
            None,
            False,
            "bounded_run_counts_prevent_exact_growth_delta",
        )
    return CodeStorageGrowthPreview(
        latest.analysis_run_id,
        previous.analysis_run_id,
        latest_rows,
        previous_rows,
        latest_rows - previous_rows,
        True,
        None,
    )


def _retention(
    tables: tuple[CodeStorageTableObservation, ...],
    *,
    completed_run_ids: tuple[int, ...],
    retain_latest_completed_runs: int,
) -> CodeStorageRetentionPreview:
    historical = tuple(
        item.historical_rows
        for item in tables
        if item.temporal_status == "resolved" and item.historical_rows is not None
    )
    return CodeStorageRetentionPreview(
        retain_latest_completed_runs,
        completed_run_ids[:retain_latest_completed_runs],
        sum(item.observed_rows for item in historical),
        any(item.truncated for item in historical),
        False,
        "preview_only",
        (
            "historical_rows_are_observations_not_safe_delete_candidates",
            "foreign_keys_holds_replay_and_audit_require_owner_specific_retention",
            "no_prune_vacuum_checkpoint_or_sidecar_operation_is_supported",
        ),
    )


def _fence_payload(fence: SQLiteImmutableFence) -> dict[str, object]:
    return {
        "main": asdict(fence.main),
        "sidecars": [
            {"suffix": suffix, "identity": asdict(identity)} for suffix, identity in fence.sidecars
        ],
    }


def _abstained(database: Path, reason: str) -> CodeStorageAnalysis:
    size = 0
    try:
        metadata = database.stat()
    except OSError:
        pass
    else:
        if os.path.isfile(database) and not database.is_symlink():
            size = int(metadata.st_size)
    return CodeStorageAnalysis(
        str(database),
        "abstained",
        reason,
        CODE_STORAGE_ANALYSIS_POLICY,
        None,
        size,
        0,
        0,
        0,
        0,
        0,
        (),
        (),
        (),
        False,
        None,
        None,
        None,
        ("storage_observation_failed_closed_without_opening_a_writable_connection",),
    )


def analyze_code_storage(
    database: Path,
    *,
    run_limit: int = 20,
    row_scan_limit: int = CODE_STORAGE_DEFAULT_ROW_SCAN_LIMIT,
    retain_latest_completed_runs: int = 5,
) -> CodeStorageAnalysis:
    """Inspect one quiescent Code database without changing owner bytes or sidecars."""

    selected = Path(database)
    if isinstance(run_limit, bool) or not 1 <= run_limit <= CODE_STORAGE_MAX_RUNS:
        raise ValueError(f"storage run limit must be between 1 and {CODE_STORAGE_MAX_RUNS}")
    if isinstance(row_scan_limit, bool) or not 1 <= row_scan_limit <= (
        CODE_STORAGE_MAX_ROW_SCAN_LIMIT
    ):
        raise ValueError(
            f"storage row scan limit must be between 1 and {CODE_STORAGE_MAX_ROW_SCAN_LIMIT}"
        )
    if isinstance(retain_latest_completed_runs, bool) or not (
        1 <= retain_latest_completed_runs <= run_limit
    ):
        raise ValueError("storage retention window must fit inside the selected run window")
    if not selected.is_file() or selected.is_symlink():
        return _abstained(selected, "code_state_missing_or_not_regular")
    try:
        fence = capture_sqlite_immutable_fence(selected)
        with immutable_sqlite_database(selected) as connection:
            validate_code_schema(connection)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != CODE_SCHEMA_VERSION:
                raise RuntimeError("code_storage_schema_not_current")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or int(query_only[0]) != 1:
                raise RuntimeError("code_storage_query_only_not_enforced")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            completed_run_ids = _latest_completed_runs(
                connection,
                limit=max(run_limit, retain_latest_completed_runs),
            )
            current_run_id = completed_run_ids[0] if completed_run_ids else None
            all_run_rows = tuple(
                (int(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT analysis_run_id,status FROM analysis_runs "
                    "ORDER BY analysis_run_id DESC LIMIT ?",
                    (run_limit + 1,),
                )
            )
            runs_truncated = len(all_run_rows) > run_limit
            selected_runs = all_run_rows[:run_limit]
            tables = _tables(
                connection,
                latest_run_id=current_run_id,
                scan_limit=row_scan_limit,
            )
            providers = _providers(
                connection,
                current_run_id=current_run_id,
                scan_limit=row_scan_limit,
            )
            runs = tuple(
                _run_observation(
                    connection,
                    run_id,
                    current_run_id=current_run_id,
                    scan_limit=row_scan_limit,
                )
                for run_id, _status in selected_runs
            )
        after = capture_sqlite_immutable_fence(selected)
    except (
        ImmutableSQLiteUnavailable,
        OSError,
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _abstained(selected, f"code_storage_unavailable:{type(exc).__name__}:{exc}"[:256])
    if fence != after:
        return _abstained(selected, "code_storage_owner_changed_during_read")
    allocated = page_size * page_count
    used = page_size * (page_count - freelist_pages)
    limitations = (
        "row_counts_at_the_scan_limit_are_lower_bounds",
        "page_usage_is_database_level_not_attributed_to_individual_tables",
        "growth_compares_external_evidence_rows_not_historical_database_bytes",
        "retention_is_preview_only_and_never_authorizes_deletion",
        "immutable_reader_never_checkpoints_vacuums_prunes_or_removes_sidecars",
    )
    return CodeStorageAnalysis(
        str(selected),
        "ready",
        None,
        CODE_STORAGE_ANALYSIS_POLICY,
        schema_version,
        fence.main.size,
        page_size,
        page_count,
        freelist_pages,
        allocated,
        used,
        tables,
        providers,
        runs,
        runs_truncated,
        _growth(runs),
        _retention(
            tables,
            completed_run_ids=completed_run_ids,
            retain_latest_completed_runs=retain_latest_completed_runs,
        ),
        _fence_payload(fence),
        limitations,
    )


__all__ = [
    "CODE_STORAGE_ANALYSIS_POLICY",
    "CODE_STORAGE_ANALYSIS_SCHEMA",
    "CODE_STORAGE_DEFAULT_ROW_SCAN_LIMIT",
    "CODE_STORAGE_MAX_PROVIDERS",
    "CODE_STORAGE_MAX_ROW_SCAN_LIMIT",
    "CODE_STORAGE_MAX_RUNS",
    "CODE_STORAGE_MAX_TABLES",
    "CodeStorageAnalysis",
    "CodeStorageBoundedCount",
    "CodeStorageGrowthPreview",
    "CodeStorageProviderObservation",
    "CodeStorageRetentionPreview",
    "CodeStorageRunObservation",
    "CodeStorageTableObservation",
    "analyze_code_storage",
]
