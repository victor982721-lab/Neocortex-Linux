"""Bounded generational retention for the Code SQLite owner.

Code analysis stores immutable file versions and provider evidence.  File
versions remain the durable identity history, while run-scoped provider
projections are disposable once they are no longer needed for the current or
rollback publication, an incident window, an experiment receipt, or a replay
source.  This module keeps that distinction explicit and applies only bounded
keyset batches inside the Code owner's existing writer transaction.
"""

from __future__ import annotations
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import sqlite3

CODE_RETENTION_POLICY_ID = "code-owner-generational-retention-v1"
CODE_RETENTION_DAY_NS = 86_400_000_000_000
CODE_RETENTION_MAX_RUNS = 64
CODE_RETENTION_MAX_BATCH = 100

_CHILD_TABLES = (
    "external_findings",
    "external_metrics",
    "external_relations",
    "external_run_inputs",
    "external_run_counters",
    "external_run_replays",
    "external_run_contracts",
)
_REQUIRED_TABLES = frozenset(
    {
        "analysis_runs",
        "external_tool_runs",
        "external_run_replays",
        "external_run_contracts",
        "external_run_inputs",
        "external_findings",
        "external_metrics",
        "external_relations",
        "external_run_counters",
        "code_experiment_receipts",
    }
)


@dataclass(frozen=True, slots=True)
class CodeRetentionPolicy:
    """Automatic Code-owner policy applied at the next run boundary.

    Two completed runs are retained for current/rollback behavior, four
    terminal incident runs are retained for short-lived diagnostics, and a
    hard terminal-run ceiling prevents repeated analysis from growing the
    owner without limit.  ``minimum_age_ns`` is an optional reader grace
    period; the ceiling remains authoritative when it is exceeded.  The batch
    bound keeps one ordinary run from becoming an unbounded maintenance
    transaction.
    """

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
        if isinstance(self.max_terminal_runs, bool) or not 1 <= self.max_terminal_runs <= (
            CODE_RETENTION_MAX_RUNS
        ):
            raise ValueError(
                f"max_terminal_runs must be between 1 and {CODE_RETENTION_MAX_RUNS}"
            )
        if isinstance(self.batch_size, bool) or not 1 <= self.batch_size <= CODE_RETENTION_MAX_BATCH:
            raise ValueError(f"batch_size must be between 1 and {CODE_RETENTION_MAX_BATCH}")


DEFAULT_CODE_RETENTION_POLICY = CodeRetentionPolicy()


@dataclass(frozen=True, slots=True)
class CodeRetentionCandidate:
    """One terminal analysis run considered for bounded pruning."""

    analysis_run_id: int
    status: str
    tool_run_ids: tuple[int, ...]
    held_by_replay: bool
    deletable: bool


@dataclass(frozen=True, slots=True)
class CodeRetentionPlan:
    """Read-only classification used by automatic and administrative paths."""

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
    """Bounded mutation receipt returned after a successful owner transaction."""

    policy_id: str
    dry_run: bool
    protected_run_ids: tuple[int, ...]
    considered_run_ids: tuple[int, ...]
    held_run_ids: tuple[int, ...]
    deleted_run_ids: tuple[int, ...]
    deleted_tool_runs: int
    deleted_rows: int


def _placeholders(values: Iterable[object]) -> str:
    values = tuple(values)
    if not values:
        raise ValueError("retention SQL values cannot be empty")
    return ",".join("?" for _ in values)


def _validate_owner_tables(connection: sqlite3.Connection) -> None:
    observed = frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    missing = _REQUIRED_TABLES - observed
    if missing:
        raise RuntimeError(
            "Code retention requires the current Code owner schema; "
            f"missing={','.join(sorted(missing))}"
        )


def _latest_run_ids(
    connection: sqlite3.Connection,
    *,
    status: Literal["completed"] | Literal["incident"],
    limit: int,
) -> tuple[int, ...]:
    if status == "completed":
        rows = connection.execute(
            "SELECT analysis_run_id FROM analysis_runs "
            "WHERE status='completed' ORDER BY analysis_run_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT analysis_run_id FROM analysis_runs "
            "WHERE status IN ('partial','failed','cancelled','interrupted') "
            "ORDER BY COALESCE(completed_ns,started_ns) DESC,analysis_run_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return tuple(int(row[0]) for row in rows)


def _replay_source_runs(
    connection: sqlite3.Connection,
    protected: set[int],
) -> None:
    """Close the replay-source closure without loading any provider payload."""

    while protected:
        placeholders = _placeholders(protected)
        rows = connection.execute(
            f"""SELECT DISTINCT source.analysis_run_id
            FROM external_run_replays replay
            JOIN external_tool_runs replay_tool
              ON replay_tool.tool_run_id=replay.tool_run_id
            JOIN external_tool_runs source_tool
              ON source_tool.tool_run_id=replay.source_tool_run_id
            JOIN analysis_runs source
              ON source.analysis_run_id=source_tool.analysis_run_id
            WHERE replay_tool.analysis_run_id IN ({placeholders})""",
            tuple(protected),
        ).fetchall()
        additions = {int(row[0]) for row in rows} - protected
        if not additions:
            return
        protected.update(additions)


def _candidate_tool_runs(
    connection: sqlite3.Connection,
    analysis_run_id: int,
) -> tuple[int, ...]:
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT tool_run_id FROM external_tool_runs "
            "WHERE analysis_run_id=? ORDER BY tool_run_id",
            (analysis_run_id,),
        ).fetchall()
    )


def _has_external_replay_hold(
    connection: sqlite3.Connection,
    tool_run_ids: tuple[int, ...],
) -> bool:
    if not tool_run_ids:
        return False
    placeholders = _placeholders(tool_run_ids)
    return (
        connection.execute(
            f"""SELECT 1 FROM external_run_replays replay
            WHERE replay.source_tool_run_id IN ({placeholders})
            AND replay.tool_run_id NOT IN ({placeholders}) LIMIT 1""",
            (*tool_run_ids, *tool_run_ids),
        ).fetchone()
        is not None
    )


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
    _validate_owner_tables(connection)
    observed_ns = time.time_ns() if now_ns is None else now_ns
    if isinstance(observed_ns, bool) or not isinstance(observed_ns, int) or observed_ns < 0:
        raise ValueError("retention now_ns must be a non-negative integer")

    protected = set(_latest_run_ids(connection, status="completed", limit=policy.keep_completed_runs))
    protected.update(_latest_run_ids(connection, status="incident", limit=policy.keep_incident_runs))
    receipt_rows = connection.execute(
        "SELECT DISTINCT analysis_run_id FROM code_experiment_receipts"
    ).fetchall()
    protected.update(int(row[0]) for row in receipt_rows)
    if current_run_id is not None:
        if isinstance(current_run_id, bool) or not isinstance(current_run_id, int) or current_run_id < 1:
            raise ValueError("current_run_id must be a positive integer")
        protected.add(current_run_id)
    _replay_source_runs(connection, protected)

    cutoff = observed_ns - policy.minimum_age_ns
    protected_sql = _placeholders(protected) if protected else ""
    terminal_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE status<>'running'"
        ).fetchone()[0]
    )
    overflow = max(0, terminal_count - policy.max_terminal_runs)
    parameters: tuple[object, ...] = (overflow, cutoff, *protected) if protected else (
        overflow,
        cutoff,
    )
    rows = connection.execute(
        "WITH ranked AS ("
        "SELECT analysis_run_id,status,COALESCE(completed_ns,started_ns) AS finished_ns,"
        "ROW_NUMBER() OVER (ORDER BY COALESCE(completed_ns,started_ns),analysis_run_id) AS ordinal "
        "FROM analysis_runs WHERE status<>'running'"
        ") SELECT analysis_run_id,status FROM ranked "
        "WHERE (ordinal<=? OR finished_ns<=?) "
        + (f"AND analysis_run_id NOT IN ({protected_sql}) " if protected else "")
        + "ORDER BY ordinal LIMIT ?",
        (*parameters, policy.batch_size),
    ).fetchall()
    candidates: list[CodeRetentionCandidate] = []
    for row in rows:
        run_id = int(row[0])
        tools = _candidate_tool_runs(connection, run_id)
        held = _has_external_replay_hold(connection, tools)
        candidates.append(CodeRetentionCandidate(run_id, str(row[1]), tools, held, not held))
    return CodeRetentionPlan(
        CODE_RETENTION_POLICY_ID,
        current_run_id,
        tuple(sorted(protected)),
        tuple(candidates),
        observed_ns,
    )


def _delete_tool_run_rows(
    connection: sqlite3.Connection,
    tool_run_ids: tuple[int, ...],
) -> tuple[int, int]:
    if not tool_run_ids:
        return 0, 0
    placeholders = _placeholders(tool_run_ids)
    deleted_rows = 0
    for table in _CHILD_TABLES:
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE tool_run_id IN ({placeholders})",
            tool_run_ids,
        )
        deleted_rows += max(0, int(cursor.rowcount))
    cursor = connection.execute(
        f"DELETE FROM external_tool_runs WHERE tool_run_id IN ({placeholders})",
        tool_run_ids,
    )
    return max(0, int(cursor.rowcount)), deleted_rows


def apply_code_retention(
    connection: sqlite3.Connection,
    *,
    current_run_id: int | None = None,
    policy: CodeRetentionPolicy = DEFAULT_CODE_RETENTION_POLICY,
    now_ns: int | None = None,
    dry_run: bool = False,
) -> CodeRetentionResult:
    """Apply one bounded plan inside the caller's existing writer transaction.

    The function never opens a connection, starts a second transaction, runs
    ``VACUUM`` or checkpoints.  A production Code route calls it while its
    completion transaction is open, so any retention failure rolls the whole
    route back atomically.
    """

    plan = plan_code_retention(
        connection,
        current_run_id=current_run_id,
        policy=policy,
        now_ns=now_ns,
    )
    considered = tuple(item.analysis_run_id for item in plan.candidates)
    held = tuple(item.analysis_run_id for item in plan.candidates if item.held_by_replay)
    if dry_run:
        return CodeRetentionResult(
            plan.policy_id,
            True,
            plan.protected_run_ids,
            considered,
            held,
            (),
            0,
            0,
        )
    if not connection.in_transaction:
        raise RuntimeError("Code retention mutation requires the owner writer transaction")
    deleted_run_ids: list[int] = []
    deleted_tool_runs = 0
    deleted_rows = 0
    for candidate in plan.candidates:
        if not candidate.deletable:
            continue
        if candidate.tool_run_ids:
            placeholders = _placeholders(candidate.tool_run_ids)
            replay_cursor = connection.execute(
                f"DELETE FROM external_run_replays WHERE tool_run_id IN ({placeholders})",
                candidate.tool_run_ids,
            )
            deleted_rows += max(0, int(replay_cursor.rowcount))
            tools, rows = _delete_tool_run_rows(connection, candidate.tool_run_ids)
            deleted_tool_runs += tools
            deleted_rows += rows
        removed = connection.execute(
            "DELETE FROM analysis_runs WHERE analysis_run_id=? "
            "AND NOT EXISTS(SELECT 1 FROM external_tool_runs "
            "WHERE analysis_run_id=?) AND NOT EXISTS(SELECT 1 "
            "FROM code_experiment_receipts WHERE analysis_run_id=?)",
            (candidate.analysis_run_id, candidate.analysis_run_id, candidate.analysis_run_id),
        )
        if removed.rowcount == 1:
            deleted_run_ids.append(candidate.analysis_run_id)
    return CodeRetentionResult(
        plan.policy_id,
        False,
        plan.protected_run_ids,
        considered,
        held,
        tuple(deleted_run_ids),
        deleted_tool_runs,
        deleted_rows,
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
