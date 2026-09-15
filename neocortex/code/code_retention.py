"""Bounded retention for the product Code owner.

Only completed/incident ``analysis_runs`` are managed here.  Historical
provider and experiment tables, when encountered in a legacy database, are
treated as read-only holds so this product path cannot delete their parents.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


CODE_RETENTION_POLICY_ID = "code-owner-generational-retention-v2"
CODE_RETENTION_DAY_NS = 86_400_000_000_000
CODE_RETENTION_MAX_RUNS = 64
CODE_RETENTION_MAX_BATCH = 100
_REQUIRED_TABLES = frozenset({"analysis_runs"})
_LEGACY_CHILD_TABLES = (
    "external_tool_runs",
    "code_experiment_receipts",
)
_GRAPH_LINEAGE_TABLES = frozenset(
    {
        "graph_input_snapshots",
        "graph_generations",
        "graph_heads",
    }
)


@dataclass(frozen=True, slots=True)
class CodeRetentionPolicy:
    """Limits applied at a Code run boundary."""

    keep_completed_runs: int = 2
    keep_incident_runs: int = 4
    minimum_age_ns: int = 0
    max_terminal_runs: int = CODE_RETENTION_MAX_RUNS
    batch_size: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.keep_completed_runs, bool) or not 1 <= self.keep_completed_runs <= 16:
            raise ValueError("keep_completed_runs must be between 1 and 16")
        if isinstance(self.keep_incident_runs, bool) or not 0 <= self.keep_incident_runs <= 16:
            raise ValueError("keep_incident_runs must be between 0 and 16")
        if isinstance(self.minimum_age_ns, bool) or not isinstance(self.minimum_age_ns, int):
            raise ValueError("minimum_age_ns must be an integer")
        if self.minimum_age_ns < 0:
            raise ValueError("minimum_age_ns must be non-negative")
        if isinstance(self.max_terminal_runs, bool) or not 1 <= self.max_terminal_runs <= CODE_RETENTION_MAX_RUNS:
            raise ValueError(f"max_terminal_runs must be between 1 and {CODE_RETENTION_MAX_RUNS}")
        if isinstance(self.batch_size, bool) or not 1 <= self.batch_size <= CODE_RETENTION_MAX_BATCH:
            raise ValueError(f"batch_size must be between 1 and {CODE_RETENTION_MAX_BATCH}")


DEFAULT_CODE_RETENTION_POLICY = CodeRetentionPolicy()


@dataclass(frozen=True, slots=True)
class CodeRetentionCandidate:
    """One terminal run considered for bounded pruning."""

    analysis_run_id: int
    status: str
    legacy_rows: int = 0
    deletable: bool = True

    @property
    def held_by_replay(self) -> bool:
        """Compatibility alias for callers that used the old hold field."""

        return self.legacy_rows > 0

    @property
    def tool_run_ids(self) -> tuple[int, ...]:
        """Compatibility alias; external tool rows are no longer managed."""

        return ()


@dataclass(frozen=True, slots=True)
class CodeRetentionPlan:
    policy_id: str
    current_run_id: int | None
    protected_run_ids: tuple[int, ...]
    candidates: tuple[CodeRetentionCandidate, ...]
    now_ns: int
    dry_run: bool = True

    @property
    def deletable_run_ids(self) -> tuple[int, ...]:
        return tuple(item.analysis_run_id for item in self.candidates if item.deletable)


@dataclass(frozen=True, slots=True)
class CodeRetentionResult:
    policy_id: str
    dry_run: bool
    protected_run_ids: tuple[int, ...]
    considered_run_ids: tuple[int, ...]
    held_run_ids: tuple[int, ...]
    deleted_run_ids: tuple[int, ...]
    deleted_tool_runs: int = 0
    deleted_rows: int = 0


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )


def _validate_owner_tables(connection: sqlite3.Connection) -> frozenset[str]:
    observed = _table_names(connection)
    missing = _REQUIRED_TABLES - observed
    if missing:
        raise RuntimeError(
            "Code retention requires the current Code owner schema; "
            f"missing={','.join(sorted(missing))}"
        )
    return observed


def _validate_graph_lineage_tables(observed_tables: frozenset[str]) -> bool:
    """Return whether the additive graph lineage owner can be inspected.

    A fresh Code owner has all three tables.  Older owners may have none of
    them, in which case retention keeps its legacy behaviour.  A partial
    graph owner is different: ignoring the tables could make an analysis run
    look unreferenced even though a head or a snapshot still depends on it.
    Refuse that ambiguous schema rather than mutating through an incomplete
    reachability view.
    """

    present = observed_tables.intersection(_GRAPH_LINEAGE_TABLES)
    if not present:
        return False
    missing = _GRAPH_LINEAGE_TABLES - present
    if missing:
        raise RuntimeError(
            "Code retention cannot validate graph lineage; schema is incomplete: "
            + ",".join(sorted(missing))
        )
    return True


def _metadata_source_run_id(
    value: object,
    *,
    table: str,
    identifier: str,
) -> int | None:
    """Read the optional source-run locator from validated graph metadata.

    The graph owner stores the source run in ``graph_input_snapshots`` as a
    typed column.  Generation metadata repeats that locator for portable
    lineage readers, so retention also checks the denormalized copy.  A
    malformed metadata document is an integrity ambiguity and must stop a
    mutation rather than silently discard a potentially reachable run.
    """

    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Code retention cannot validate graph lineage metadata: {table}/{identifier}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Code retention graph lineage metadata is not an object: {table}/{identifier}"
        )
    source_run_id = payload.get("source_run_id")
    if source_run_id is None:
        return None
    if isinstance(source_run_id, bool) or not isinstance(source_run_id, int) or source_run_id <= 0:
        raise RuntimeError(
            f"Code retention graph lineage source_run_id is invalid: {table}/{identifier}"
        )
    return source_run_id


def _graph_lineage_run_ids(
    connection: sqlite3.Connection,
    observed_tables: frozenset[str],
) -> set[int]:
    """Return analysis runs reachable from Code graph publication/lineage.

    ``graph_heads`` reaches a generation, which reaches an input snapshot.
    The snapshot's typed ``source_run_id`` is the durable lineage edge.  All
    snapshots are retained as lineage records, including building, aborted and
    pruned identities; pruning their child rows does not erase the source
    relationship.  Generation metadata is checked as a second, independent
    edge so a damaged or hand-created fixture cannot bypass the hold merely by
    setting the typed snapshot column to zero.
    """

    if not _validate_graph_lineage_tables(observed_tables):
        return set()

    reachable: set[int] = set()
    for row in connection.execute(
        "SELECT snapshot_id,source_run_id,metadata_json FROM graph_input_snapshots"
    ):
        snapshot_id = str(row[0])
        source_run_id = row[1]
        if isinstance(source_run_id, bool) or not isinstance(source_run_id, int) or source_run_id < 0:
            raise RuntimeError(
                f"Code retention graph lineage source_run_id is invalid: graph_input_snapshots/{snapshot_id}"
            )
        if source_run_id > 0:
            reachable.add(source_run_id)
        metadata_source = _metadata_source_run_id(
            row[2], table="graph_input_snapshots", identifier=snapshot_id
        )
        if metadata_source is not None:
            reachable.add(metadata_source)

    for row in connection.execute(
        "SELECT generation_id,metadata_json FROM graph_generations"
    ):
        generation_id = str(row[0])
        metadata_source = _metadata_source_run_id(
            row[1], table="graph_generations", identifier=generation_id
        )
        if metadata_source is not None:
            reachable.add(metadata_source)

    return reachable


def _latest_run_ids(connection: sqlite3.Connection, status: str, limit: int) -> tuple[int, ...]:
    if status == "completed":
        query = "SELECT analysis_run_id FROM analysis_runs WHERE status='completed' ORDER BY analysis_run_id DESC LIMIT ?"
    else:
        query = "SELECT analysis_run_id FROM analysis_runs WHERE status IN ('partial','failed','cancelled','interrupted') ORDER BY COALESCE(completed_ns,started_ns) DESC,analysis_run_id DESC LIMIT ?"
    return tuple(int(row[0]) for row in connection.execute(query, (limit,)).fetchall())


def _legacy_rows_for_run(
    connection: sqlite3.Connection,
    observed_tables: frozenset[str],
    run_id: int,
) -> int:
    total = 0
    if "external_tool_runs" in observed_tables:
        total += int(
            connection.execute(
                "SELECT COUNT(*) FROM external_tool_runs WHERE analysis_run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    if "code_experiment_receipts" in observed_tables:
        total += int(
            connection.execute(
                "SELECT COUNT(*) FROM code_experiment_receipts WHERE analysis_run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    return total


def plan_code_retention(
    connection: sqlite3.Connection,
    *,
    current_run_id: int | None = None,
    policy: CodeRetentionPolicy = DEFAULT_CODE_RETENTION_POLICY,
    now_ns: int | None = None,
) -> CodeRetentionPlan:
    """Classify old Code runs without changing the connection or sidecars."""

    import sqlite3

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("Code retention requires a sqlite3 connection")
    if not isinstance(policy, CodeRetentionPolicy):
        raise TypeError("Code retention policy is invalid")
    observed_tables = _validate_owner_tables(connection)
    observed_ns = time.time_ns() if now_ns is None else now_ns
    if isinstance(observed_ns, bool) or not isinstance(observed_ns, int) or observed_ns < 0:
        raise ValueError("retention now_ns must be a non-negative integer")
    if current_run_id is not None and (
        isinstance(current_run_id, bool) or not isinstance(current_run_id, int) or current_run_id < 1
    ):
        raise ValueError("current_run_id must be a positive integer")

    protected = set(_latest_run_ids(connection, "completed", policy.keep_completed_runs))
    protected.update(_latest_run_ids(connection, "incident", policy.keep_incident_runs))
    # Published graph heads and their input snapshots are durable Code
    # lineage.  Never let the run-retention policy orphan that evidence, even
    # when the source run is older than the normal completed-run window.
    protected.update(_graph_lineage_run_ids(connection, observed_tables))
    if current_run_id is not None:
        protected.add(current_run_id)

    cutoff = observed_ns - policy.minimum_age_ns
    terminal_count = int(
        connection.execute("SELECT COUNT(*) FROM analysis_runs WHERE status<>'running'").fetchone()[0]
    )
    overflow = max(0, terminal_count - policy.max_terminal_runs)
    params: tuple[object, ...] = (overflow, cutoff, *sorted(protected), policy.batch_size)
    exclusion = "" if not protected else " AND analysis_run_id NOT IN (" + ",".join("?" for _ in protected) + ")"
    rows = connection.execute(
        "WITH ranked AS (SELECT analysis_run_id,status,COALESCE(completed_ns,started_ns) AS finished_ns,"
        "ROW_NUMBER() OVER (ORDER BY COALESCE(completed_ns,started_ns),analysis_run_id) AS ordinal "
        "FROM analysis_runs WHERE status<>'running') "
        "SELECT analysis_run_id,status FROM ranked WHERE (ordinal<=? OR finished_ns<=?)"
        + exclusion
        + " ORDER BY ordinal LIMIT ?",
        params,
    ).fetchall()
    candidates = tuple(
        CodeRetentionCandidate(
            int(row[0]),
            str(row[1]),
            legacy_rows := _legacy_rows_for_run(connection, observed_tables, int(row[0])),
            legacy_rows == 0,
        )
        for row in rows
    )
    return CodeRetentionPlan(
        CODE_RETENTION_POLICY_ID,
        current_run_id,
        tuple(sorted(protected)),
        candidates,
        observed_ns,
    )


def apply_code_retention(
    connection: sqlite3.Connection,
    *,
    current_run_id: int | None = None,
    policy: CodeRetentionPolicy = DEFAULT_CODE_RETENTION_POLICY,
    now_ns: int | None = None,
    dry_run: bool = False,
) -> CodeRetentionResult:
    """Apply one bounded run-history plan in the caller's writer transaction."""

    plan = plan_code_retention(
        connection,
        current_run_id=current_run_id,
        policy=policy,
        now_ns=now_ns,
    )
    considered = tuple(item.analysis_run_id for item in plan.candidates)
    held = tuple(item.analysis_run_id for item in plan.candidates if not item.deletable)
    if dry_run:
        return CodeRetentionResult(
            plan.policy_id,
            True,
            plan.protected_run_ids,
            considered,
            held,
            (),
        )
    if not connection.in_transaction:
        raise RuntimeError("Code retention mutation requires the owner writer transaction")
    deleted: list[int] = []
    for candidate in plan.candidates:
        if not candidate.deletable:
            continue
        cursor = connection.execute(
            "DELETE FROM analysis_runs WHERE analysis_run_id=?",
            (candidate.analysis_run_id,),
        )
        if cursor.rowcount == 1:
            deleted.append(candidate.analysis_run_id)
    return CodeRetentionResult(
        plan.policy_id,
        False,
        plan.protected_run_ids,
        considered,
        held,
        tuple(deleted),
    )


__all__ = [
    "CODE_RETENTION_DAY_NS",
    "CODE_RETENTION_MAX_BATCH",
    "CODE_RETENTION_MAX_RUNS",
    "CODE_RETENTION_POLICY_ID",
    "DEFAULT_CODE_RETENTION_POLICY",
    "CodeRetentionCandidate",
    "CodeRetentionPlan",
    "CodeRetentionPolicy",
    "CodeRetentionResult",
    "apply_code_retention",
    "plan_code_retention",
]
