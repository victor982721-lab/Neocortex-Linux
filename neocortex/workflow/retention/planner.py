"""Bounded, read-only retention planning across durable NeoCortex state.
# region [00] Contexto del módulo
# Módulo: neocortex/workflow/retention/planner.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The planner deliberately has no apply/delete path.  It inventories one keyset
page per selected store, protects publication and recovery invariants, and
reports lower-bound SQLite payload estimates.  A future deletion implementation
must define resumable batches and rollback independently of this diagnostic.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

from neocortex.deduplication.persistence.ddl import SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION
from neocortex.deduplication.persistence.validation import validate_inventory_schema
from neocortex.documents import document_catalog_schema
from neocortex.persistence import framework_schema
from neocortex.semantic import semantic_schema
from neocortex.workflow.review.review_task_repository import (
    MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
    audit_latest_review_task_source_publications_from_connection,
)
from neocortex.persistence.sqlite_immutable import (
    DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES,
    SQLiteReadMode,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    SQLiteSnapshotReuseCache,
    preferred_sqlite_read_mode,
)
from neocortex.persistence.sqlite_paths import readonly_sqlite_uri
from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract
# endregion [01]

# region [02] Implementación


RetentionStore = Literal["semantic", "catalog", "inventory", "framework"]
Disposition = Literal["eligible", "protected", "blocked"]
StoreStatus = Literal["ready", "absent", "blocked"]
RetentionObserver = Callable[[RetentionStore, str], None]

DEFAULT_RETENTION_SQL_TIMEOUT_SECONDS = 30.0

# Retention is a diagnostic reader.  Graph walks are deliberately bounded even
# though the current schemas use small integer keysets.  A malformed or future
# owner must never turn a read-only status query into an unbounded recursive
# query (or, worse, into an implicit cleanup attempt).
_MAX_REACHABILITY_DEPTH = 64
_MAX_REACHABILITY_NODES = 4_096

STORE_ORDER: tuple[RetentionStore, ...] = (
    "semantic",
    "catalog",
    "inventory",
    "framework",
)
STORE_DATABASES: Mapping[RetentionStore, str] = {
    "semantic": "semantic.sqlite3",
    "catalog": "document_catalog.sqlite3",
    "inventory": "dedup.sqlite3",
    "framework": "framework.sqlite3",
}

# Tests and embedders historically inject a sqlite-like module to exercise
# connection failures.  Production always uses the fenced kernel below; this
# identity marker keeps that compatibility seam explicit and bounded.
_CANONICAL_SQLITE_MODULE = sqlite3


class RetentionPlanningCancelled(RuntimeError):
    """The caller cancelled a read-only retention snapshot."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Explicit dry-run policy; ``None`` age never authorizes eligibility.

    Exactly two published states are retained because the current planner only
    proves the current/previous invariant.  A configurable depth would require
    owner-specific ranking queries and is deliberately not promised here.
    """

    minimum_age_ns: int | None = None
    keep_published: int = 2
    batch_size: int = 100
    snapshot_max_temporary_bytes: int = 256 * 1024 * 1024
    snapshot_prepare_timeout_seconds: float = 5.0
    # Aggregate product-history quotas.  A destructive cleaner must consume
    # an exact preview and apply these same values rather than inventing
    # per-stage copies.
    terminal_log_count: int = 2
    terminal_log_bytes: int = 512 * 1024 * 1024
    summary_count: int = 30
    summary_bytes: int = 64 * 1024 * 1024
    receipt_count: int = 30
    receipt_bytes: int = 128 * 1024 * 1024
    rollback_derived_count: int = 1

    def __post_init__(self) -> None:
        SQLiteSnapshotBudget(
            max_temporary_bytes=self.snapshot_max_temporary_bytes,
            prepare_timeout_seconds=self.snapshot_prepare_timeout_seconds,
        )
        if self.minimum_age_ns is not None and (
            isinstance(self.minimum_age_ns, bool)
            or not isinstance(self.minimum_age_ns, int)
            or self.minimum_age_ns < 0
        ):
            raise ValueError("minimum_age_ns must be a non-negative integer")
        if (
            isinstance(self.keep_published, bool)
            or not isinstance(self.keep_published, int)
            or self.keep_published != 2
        ):
            raise ValueError("keep_published must be exactly 2")
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or not 1 <= self.batch_size <= 1_000
        ):
            raise ValueError("retention batch_size must be between 1 and 1000")
        for name, value in (
            ("terminal_log_count", self.terminal_log_count),
            ("terminal_log_bytes", self.terminal_log_bytes),
            ("summary_count", self.summary_count),
            ("summary_bytes", self.summary_bytes),
            ("receipt_count", self.receipt_count),
            ("receipt_bytes", self.receipt_bytes),
            ("rollback_derived_count", self.rollback_derived_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.terminal_log_count != 2:
            raise ValueError("terminal_log_count must be exactly 2")
        if self.summary_count != 30:
            raise ValueError("summary_count must be exactly 30")
        if self.receipt_count != 30:
            raise ValueError("receipt_count must be exactly 30")
        if self.rollback_derived_count != 1:
            raise ValueError("rollback_derived_count must be exactly 1")


class _RetentionInspectionBudget:
    """Shared cooperative deadline for SQL planning over detached snapshots."""

    def __init__(self, timeout_seconds: float, cancelled: Callable[[], bool] | None) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.cancelled_callback = cancelled
        self.exhausted = False
        self.cancelled = False

    def progress(self) -> int:
        if self.cancelled_callback is not None and self.cancelled_callback():
            self.cancelled = True
            return 1
        if time.monotonic() >= self.deadline:
            self.exhausted = True
            return 1
        return 0

    def checkpoint(self) -> None:
        if self.progress():
            if self.cancelled:
                raise RetentionPlanningCancelled("retention planning was cancelled")
            raise _RetentionInspectionBudgetExceeded(
                "retention inspection SQL time budget exhausted"
            )


class _RetentionInspectionBudgetExceeded(RuntimeError):
    """Internal marker used when a planning query cannot finish in budget."""


@dataclass(frozen=True, slots=True)
class RetentionItem:
    """One generation/run classified within a bounded keyset page."""

    key: int
    entity: str
    scope: str
    recorded_status: str
    disposition: Disposition
    reasons: tuple[str, ...]
    estimated_rows: int
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionHold:
    """Rows intentionally excluded from generation/run eligibility."""

    name: str
    reason: str
    rows: int
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionStorePlan:
    store: RetentionStore
    database: Path
    status: StoreStatus
    schema_version: int | None
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    items: tuple[RetentionItem, ...]
    holds: tuple[RetentionHold, ...]
    after: int
    next_after: int | None
    truncated: bool
    detail: str | None = None
    storage: Mapping[str, int | None] | None = None

    @property
    def eligible_rows(self) -> int:
        return sum(item.estimated_rows for item in self.items if item.disposition == "eligible")

    @property
    def eligible_bytes(self) -> int:
        return sum(item.estimated_bytes for item in self.items if item.disposition == "eligible")

    @property
    def protected_rows(self) -> int:
        return sum(
            item.estimated_rows for item in self.items if item.disposition != "eligible"
        ) + sum(hold.rows for hold in self.holds)

    @property
    def protected_bytes(self) -> int:
        return sum(
            item.estimated_bytes for item in self.items if item.disposition != "eligible"
        ) + sum(hold.estimated_bytes for hold in self.holds)

    @property
    def observed_rows(self) -> int:
        """Logical payload rows observed in this bounded page.

        ``items`` are intentionally page-limited and each item accounts for
        its owner row plus the bounded child rows included by its query.
        Holds are included because they are observed owner data too.  This is
        not a count of every row in a database when ``truncated`` is true.
        """

        return sum(item.estimated_rows for item in self.items) + sum(
            hold.rows for hold in self.holds
        )

    @property
    def observed_bytes(self) -> int:
        """Logical lower-bound payload bytes observed by this page."""

        return sum(item.estimated_bytes for item in self.items) + sum(
            hold.estimated_bytes for hold in self.holds
        )

    @property
    def proposed_rows(self) -> int:
        """Rows proposed by the dry-run classifier, never a mutation count."""

        return self.eligible_rows

    @property
    def proposed_bytes(self) -> int:
        """Bytes proposed by the dry-run classifier, never physical reclaim."""

        return self.eligible_bytes

    @property
    def retired_rows(self) -> int:
        """Rows retired by this planner (always zero; it has no apply path)."""

        return 0

    @property
    def retired_bytes(self) -> int:
        """Bytes retired by this planner (always zero; it has no apply path)."""

        return 0

    @property
    def physically_recoverable_bytes(self) -> int | None:
        """Physical recovery is not verified by a logical retention plan."""

        return None

    @property
    def physical_recovery_status(self) -> str:
        """Stable explanation for the intentionally unknown physical value."""

        return "not_verified"

    @property
    def physical_reclaimable_bytes(self) -> int | None:
        """Compatibility spelling used by other read-only evidence APIs."""

        return None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """A per-database snapshot plan; deletion is intentionally unsupported."""

    now_ns: int
    policy: RetentionPolicy
    stores: tuple[RetentionStorePlan, ...]
    dry_run: bool = True
    deletion_supported: bool = False
    estimate_kind: str = "lower_bound_sqlite_text_blob_payload_bytes"
    snapshot_scope: str = "stable_per_database_not_cross_database_atomic"
    sqlite_read_snapshot_may_touch_shm: bool = True
    # Operational timing is not part of the identity of a repeatable dry-run.
    snapshot_metrics: Mapping[str, object] | None = field(default=None, compare=False)

    @property
    def observed_rows(self) -> int:
        return sum(store.observed_rows for store in self.stores)

    @property
    def observed_bytes(self) -> int:
        return sum(store.observed_bytes for store in self.stores)

    @property
    def proposed_rows(self) -> int:
        return sum(store.proposed_rows for store in self.stores)

    @property
    def proposed_bytes(self) -> int:
        return sum(store.proposed_bytes for store in self.stores)

    @property
    def retired_rows(self) -> int:
        return 0

    @property
    def retired_bytes(self) -> int:
        return 0

    @property
    def physically_recoverable_bytes(self) -> int | None:
        return None

    @property
    def physical_recovery_status(self) -> str:
        return "not_verified"

    @property
    def physical_reclaimable_bytes(self) -> int | None:
        return None

    @property
    def compaction_supported(self) -> bool:
        """The common retention planner never compacts SQLite owners."""

        return False


TerminalRetentionDisposition = Literal["eligible", "protected", "blocked"]
TerminalRetentionCategory = Literal[
    "workspace",
    "tombstone",
    "terminal_log",
    "summary",
    "receipt",
    "rollback_derived",
    "other",
]

_TERMINAL_RETENTION_CATEGORIES = frozenset(
    {
        "workspace",
        "tombstone",
        "terminal_log",
        "summary",
        "receipt",
        "rollback_derived",
        "other",
    }
)
_TERMINAL_RETENTION_STATES = frozenset(
    {
        "completed",
        "failed",
        "failed-retained",
        "cancelled",
        "abandoned",
        "retired",
    }
)
_TERMINAL_LIVE_STATES = frozenset(
    {
        "active",
        "committing",
        "running",
        "building",
        "partial",
        "recovering",
        "recovery",
        "recovery_required",
    }
)
_MAX_TERMINAL_RETENTION_RECORDS = 100_000
_MAX_TERMINAL_RETENTION_BYTES = 4 * 1024 * 1024 * 1024
_MAX_TERMINAL_RECORD_ID_BYTES = 256


def _validate_terminal_quota(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return value


def _validate_terminal_non_negative(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class TerminalRetentionPolicy:
    """Bounded policy for terminal activity evidence and tombstones.

    This policy is intentionally a read-only planning contract.  It does not
    grant a caller permission to remove a workspace or a manifest.  An item
    can be eligible only after its owner has recorded reconciliation and an
    explicit release authorization; age, a missing PID, or a TTL on its own is
    never sufficient.

    The four product-history quotas mirror the existing common retention
    vocabulary.  ``workspace_*`` and ``tombstone_*`` bound activity-owned
    terminal material and registry evidence respectively.  A quota is applied
    only to records that have already passed the safety gates, so an active,
    pinned, replay-required, or otherwise uncertain record cannot be evicted
    merely because a count is full.
    """

    minimum_age_ns: int | None = None
    max_records: int = _MAX_TERMINAL_RETENTION_RECORDS
    max_bytes: int = _MAX_TERMINAL_RETENTION_BYTES
    workspace_count: int = 2
    workspace_bytes: int = 512 * 1024 * 1024
    tombstone_count: int = 2
    tombstone_bytes: int = 64 * 1024 * 1024
    terminal_log_count: int = 2
    terminal_log_bytes: int = 512 * 1024 * 1024
    summary_count: int = 30
    summary_bytes: int = 64 * 1024 * 1024
    receipt_count: int = 30
    receipt_bytes: int = 128 * 1024 * 1024
    rollback_derived_count: int = 1
    rollback_derived_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.minimum_age_ns is not None:
            _validate_terminal_non_negative(
                self.minimum_age_ns,
                label="terminal minimum_age_ns",
                maximum=(1 << 63) - 1,
            )
        _validate_terminal_quota(
            self.max_records,
            label="terminal max_records",
            maximum=_MAX_TERMINAL_RETENTION_RECORDS,
        )
        _validate_terminal_non_negative(
            self.max_bytes,
            label="terminal max_bytes",
            maximum=_MAX_TERMINAL_RETENTION_BYTES,
        )
        for name, value in (
            ("workspace_count", self.workspace_count),
            ("tombstone_count", self.tombstone_count),
            ("terminal_log_count", self.terminal_log_count),
            ("summary_count", self.summary_count),
            ("receipt_count", self.receipt_count),
            ("rollback_derived_count", self.rollback_derived_count),
        ):
            _validate_terminal_quota(
                value,
                label=f"terminal {name}",
                maximum=_MAX_TERMINAL_RETENTION_RECORDS,
            )
        for name, value in (
            ("workspace_bytes", self.workspace_bytes),
            ("tombstone_bytes", self.tombstone_bytes),
            ("terminal_log_bytes", self.terminal_log_bytes),
            ("summary_bytes", self.summary_bytes),
            ("receipt_bytes", self.receipt_bytes),
            ("rollback_derived_bytes", self.rollback_derived_bytes),
        ):
            _validate_terminal_non_negative(
                value,
                label=f"terminal {name}",
                maximum=_MAX_TERMINAL_RETENTION_BYTES,
            )

    @classmethod
    def from_retention_policy(cls, policy: RetentionPolicy) -> "TerminalRetentionPolicy":
        """Adapt the common planner's explicit product-history quotas.

        ``RetentionPolicy.minimum_age_ns=None`` deliberately remains
        unconfigured here.  This preserves the common planner's safe default:
        an operator must opt into an age before a terminal record can become a
        candidate.
        """

        if not isinstance(policy, RetentionPolicy):
            raise TypeError("retention policy is invalid")
        return cls(
            minimum_age_ns=policy.minimum_age_ns,
            max_records=min(policy.batch_size * 1_000, _MAX_TERMINAL_RETENTION_RECORDS),
            max_bytes=min(policy.snapshot_max_temporary_bytes, _MAX_TERMINAL_RETENTION_BYTES),
            terminal_log_count=policy.terminal_log_count,
            terminal_log_bytes=policy.terminal_log_bytes,
            summary_count=policy.summary_count,
            summary_bytes=policy.summary_bytes,
            receipt_count=policy.receipt_count,
            receipt_bytes=policy.receipt_bytes,
            rollback_derived_count=policy.rollback_derived_count,
        )

    def quota(self, category: TerminalRetentionCategory) -> tuple[int, int]:
        """Return the count and apparent-byte quota for one category."""

        if category == "workspace":
            return self.workspace_count, self.workspace_bytes
        if category == "tombstone":
            return self.tombstone_count, self.tombstone_bytes
        if category == "terminal_log":
            return self.terminal_log_count, self.terminal_log_bytes
        if category == "summary":
            return self.summary_count, self.summary_bytes
        if category == "receipt":
            return self.receipt_count, self.receipt_bytes
        if category == "rollback_derived":
            return self.rollback_derived_count, self.rollback_derived_bytes
        return self.workspace_count, self.workspace_bytes

    def to_dict(self) -> dict[str, int | None]:
        return {
            "minimum_age_ns": self.minimum_age_ns,
            "max_records": self.max_records,
            "max_bytes": self.max_bytes,
            "workspace_count": self.workspace_count,
            "workspace_bytes": self.workspace_bytes,
            "tombstone_count": self.tombstone_count,
            "tombstone_bytes": self.tombstone_bytes,
            "terminal_log_count": self.terminal_log_count,
            "terminal_log_bytes": self.terminal_log_bytes,
            "summary_count": self.summary_count,
            "summary_bytes": self.summary_bytes,
            "receipt_count": self.receipt_count,
            "receipt_bytes": self.receipt_bytes,
            "rollback_derived_count": self.rollback_derived_count,
            "rollback_derived_bytes": self.rollback_derived_bytes,
        }


@dataclass(frozen=True, slots=True)
class TerminalRetentionRecord:
    """A bounded, owner-provided terminal observation.

    This is a projection, not a second registry.  Owners may construct it
    directly or pass a mapping/object with the same fields to
    :func:`plan_terminal_retention`.  The planner never follows ``path`` or
    invokes owner methods; the owner remains responsible for identity,
    recovery and physical-effect revalidation.
    """

    record_id: str
    status: str
    category: TerminalRetentionCategory = "workspace"
    terminal_ns: int | None = None
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    physical_identity: tuple[int, int, int] | None = None
    reconciled: bool = False
    recovery_required: bool = False
    replay_required: bool = False
    pinned: bool = False
    grant_active: bool = False
    authorization_active: bool = False
    release_authorized: bool = False
    evidence_required: bool = False
    retain_until_ns: int | None = None
    tombstone: bool = False
    owner: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise ValueError("terminal record_id must be non-empty text")
        if len(self.record_id.encode("utf-8")) > _MAX_TERMINAL_RECORD_ID_BYTES:
            raise ValueError("terminal record_id exceeds the durable size limit")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("terminal status must be non-empty text")
        if self.category not in _TERMINAL_RETENTION_CATEGORIES:
            raise ValueError(f"unsupported terminal retention category: {self.category!r}")
        for name, value in (
            ("terminal_ns", self.terminal_ns),
            ("retain_until_ns", self.retain_until_ns),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"terminal {name} must be a non-negative integer or None")
        for name, value in (
            ("apparent_bytes", self.apparent_bytes),
            ("allocated_bytes", self.allocated_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"terminal {name} must be a non-negative integer")
        for name in (
            "reconciled",
            "recovery_required",
            "replay_required",
            "pinned",
            "grant_active",
            "authorization_active",
            "release_authorized",
            "evidence_required",
            "tombstone",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"terminal {name} must be a boolean")
        if self.physical_identity is not None:
            if (
                not isinstance(self.physical_identity, (tuple, list))
                or len(self.physical_identity) != 3
                or any(type(value) is not int for value in self.physical_identity)
                or self.physical_identity[0] < 0
                or self.physical_identity[1] < 0
                or self.physical_identity[2] < -1
            ):
                raise ValueError("terminal physical_identity is invalid")
            object.__setattr__(self, "physical_identity", tuple(self.physical_identity))
        if self.owner is not None and (
            not isinstance(self.owner, str) or not self.owner.strip()
        ):
            raise ValueError("terminal owner must be non-empty text or None")
        if self.category == "tombstone" and not self.tombstone:
            object.__setattr__(self, "tombstone", True)

    @property
    def status_normalized(self) -> str:
        return self.status.strip().casefold()

    @property
    def terminal(self) -> bool:
        return self.status_normalized in _TERMINAL_RETENTION_STATES

    @property
    def logical_bytes(self) -> int:
        return self.apparent_bytes

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "status": self.status,
            "category": self.category,
            "terminal_ns": self.terminal_ns,
            "apparent_bytes": self.apparent_bytes,
            "allocated_bytes": self.allocated_bytes,
            "physical_identity": (
                None if self.physical_identity is None else list(self.physical_identity)
            ),
            "reconciled": self.reconciled,
            "recovery_required": self.recovery_required,
            "replay_required": self.replay_required,
            "pinned": self.pinned,
            "grant_active": self.grant_active,
            "authorization_active": self.authorization_active,
            "release_authorized": self.release_authorized,
            "evidence_required": self.evidence_required,
            "retain_until_ns": self.retain_until_ns,
            "tombstone": self.tombstone,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class TerminalRetentionItem:
    """Classification of one terminal observation."""

    record: TerminalRetentionRecord
    disposition: TerminalRetentionDisposition
    reasons: tuple[str, ...]

    @property
    def record_id(self) -> str:
        return self.record.record_id

    @property
    def apparent_bytes(self) -> int:
        return self.record.apparent_bytes

    @property
    def allocated_bytes(self) -> int:
        return self.record.allocated_bytes

    def to_dict(self) -> dict[str, object]:
        payload = self.record.to_dict()
        payload.update({"disposition": self.disposition, "reasons": list(self.reasons)})
        return payload


@dataclass(frozen=True, slots=True)
class TerminalRetentionPlan:
    """Read-only bounded terminal-retention decision and accounting.

    ``eligible`` is a proposal only.  There is intentionally no apply method:
    the owning ScratchManager/ArtifactRegistry must reacquire its own lock,
    revalidate identity and commit a durable effect/recovery receipt.
    """

    now_ns: int
    policy: TerminalRetentionPolicy
    items: tuple[TerminalRetentionItem, ...]
    status: str = "ready"
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    omitted_records: int | None = None
    baseline_record_ids: tuple[str, ...] = ()
    free_bytes: int | None = None
    physical_accounting_complete: bool = True
    fingerprint: str = ""

    @property
    def records(self) -> tuple[TerminalRetentionRecord, ...]:
        return tuple(item.record for item in self.items)

    @property
    def eligible_items(self) -> tuple[TerminalRetentionItem, ...]:
        return tuple(item for item in self.items if item.disposition == "eligible")

    @property
    def protected_items(self) -> tuple[TerminalRetentionItem, ...]:
        return tuple(item for item in self.items if item.disposition == "protected")

    @property
    def blocked_items(self) -> tuple[TerminalRetentionItem, ...]:
        return tuple(item for item in self.items if item.disposition == "blocked")

    @property
    def observed_count(self) -> int:
        return len(self.items)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_items)

    @property
    def protected_count(self) -> int:
        return len(self.protected_items)

    @property
    def blocked_count(self) -> int:
        return len(self.blocked_items)

    @property
    def observed_apparent_bytes(self) -> int:
        return sum(item.apparent_bytes for item in self.items)

    @property
    def observed_allocated_bytes(self) -> int:
        return sum(item.allocated_bytes for item in self.items)

    @property
    def logical_apparent_bytes(self) -> int:
        """Apparent bytes, including duplicate owner projections."""

        return self.observed_apparent_bytes

    @property
    def eligible_apparent_bytes(self) -> int:
        return sum(item.apparent_bytes for item in self.eligible_items)

    @property
    def protected_apparent_bytes(self) -> int:
        return sum(item.apparent_bytes for item in self.protected_items)

    @property
    def blocked_apparent_bytes(self) -> int:
        return sum(item.apparent_bytes for item in self.blocked_items)

    @property
    def recovery_pending_count(self) -> int:
        return sum(
            item.record.recovery_required
            or item.record.replay_required
            or item.record.status_normalized in {"recovery_required", "recovering", "recovery"}
            for item in self.items
        )

    @property
    def tombstone_count(self) -> int:
        return sum(item.record.tombstone for item in self.items)

    @property
    def unique_workspace_count(self) -> int:
        """Count physical workspace identities, not registry projections."""

        return len(
            {
                item.record.physical_identity
                for item in self.items
                if item.record.category == "workspace"
                and item.record.physical_identity is not None
            }
        )

    @property
    def preserved_terminal_count(self) -> int:
        return self.protected_count + self.blocked_count

    @property
    def terminal_overhead_apparent_bytes(self) -> int:
        """Apparent bytes of durable tombstone/terminal evidence observed."""

        return sum(
            item.apparent_bytes
            for item in self.items
            if item.record.tombstone
            or item.record.category
            in {"terminal_log", "summary", "receipt", "rollback_derived"}
        )

    @property
    def new_terminal_records(self) -> int:
        known = set(self.baseline_record_ids)
        return sum(
            item.record.terminal and item.record.record_id not in known for item in self.items
        )

    @property
    def pending_recovery_bytes(self) -> int:
        return sum(
            item.apparent_bytes
            for item in self.items
            if item.record.recovery_required
            or item.record.replay_required
            or item.record.status_normalized in {"recovery_required", "recovering", "recovery"}
        )

    def _unique_bytes(self, items: Sequence[TerminalRetentionItem]) -> tuple[int, int, bool, int]:
        seen: dict[tuple[int, int, int], tuple[int, int]] = {}
        missing = 0
        for item in items:
            identity = item.record.physical_identity
            if identity is None:
                missing += 1
                continue
            current = seen.get(identity)
            apparent = item.apparent_bytes
            allocated = item.allocated_bytes
            if current is None:
                seen[identity] = (apparent, allocated)
            else:
                # A projection may report different observations for the same
                # object.  Max is conservative and prevents double-counting
                # while avoiding an under-report caused by an older projection.
                seen[identity] = (max(current[0], apparent), max(current[1], allocated))
        return (
            sum(value[0] for value in seen.values()),
            sum(value[1] for value in seen.values()),
            missing == 0,
            missing,
        )

    @property
    def unique_apparent_bytes(self) -> int:
        return self._unique_bytes(self.items)[0]

    @property
    def unique_allocated_bytes(self) -> int:
        return self._unique_bytes(self.items)[1]

    @property
    def physical_unique_count(self) -> int:
        identities = {
            item.record.physical_identity
            for item in self.items
            if item.record.physical_identity is not None
        }
        return len(identities)

    @property
    def physical_recovery_status(self) -> str:
        return "not_verified"

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            for reason in item.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def bytes(self) -> dict[str, int | None]:
        return {
            "observed_apparent": self.observed_apparent_bytes,
            "observed_allocated": self.observed_allocated_bytes,
            "logical_apparent": self.logical_apparent_bytes,
            "unique_apparent": self.unique_apparent_bytes,
            "unique_allocated": self.unique_allocated_bytes,
            "eligible_apparent": self.eligible_apparent_bytes,
            "protected_apparent": self.protected_apparent_bytes,
            "blocked_apparent": self.blocked_apparent_bytes,
            "preserved_apparent": self.protected_apparent_bytes + self.blocked_apparent_bytes,
            "planned_release_apparent": self.eligible_apparent_bytes,
            "terminal_overhead_apparent": self.terminal_overhead_apparent_bytes,
            "free": self.free_bytes,
        }

    @property
    def counts(self) -> dict[str, int | None]:
        return {
            "observed": self.observed_count,
            "eligible": self.eligible_count,
            "protected": self.protected_count,
            "blocked": self.blocked_count,
            "tombstones": self.tombstone_count,
            "unique_workspaces": self.unique_workspace_count,
            "preserved_terminal": self.preserved_terminal_count,
            "recovery_pending": self.recovery_pending_count,
            "new_terminal_records": self.new_terminal_records,
            "omitted": self.omitted_records,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "neocortex.terminal-retention/v1",
            "status": self.status,
            "now_ns": self.now_ns,
            "read_only": True,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "omitted_records": self.omitted_records,
            "policy": self.policy.to_dict(),
            "counts": self.counts,
            "bytes": self.bytes,
            "physical_unique_count": self.physical_unique_count,
            "physical_accounting_complete": self.physical_accounting_complete,
            "physical_recovery_status": self.physical_recovery_status,
            "baseline_record_ids": list(self.baseline_record_ids),
            "reason_counts": self.reason_counts,
            "fingerprint": self.fingerprint,
            "items": [item.to_dict() for item in self.items],
        }


def _terminal_value(source: object, *names: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return default
    for name in names:
        try:
            value = getattr(source, name)
        except AttributeError:
            continue
        if value is not None:
            return value
    return default


def _terminal_bool(source: object, *names: str, default: bool = False) -> bool:
    value = _terminal_value(source, *names, default=default)
    if type(value) is not bool:
        raise ValueError(f"terminal {names[0]} must be a boolean")
    return value


def _terminal_int(source: object, *names: str, default: int | None = None) -> int | None:
    value = _terminal_value(source, *names, default=default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"terminal {names[0]} must be a non-negative integer or None")
    return value


def _coerce_terminal_record(source: object) -> TerminalRetentionRecord:
    if isinstance(source, TerminalRetentionRecord):
        return source
    record_id = _terminal_value(
        source,
        "record_id",
        "artifact_id",
        "workspace_id",
        "id",
    )
    status = _terminal_value(source, "status", "state", "recorded_status")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("terminal record must provide a non-empty record_id")
    if not isinstance(status, str) or not status.strip():
        raise ValueError(f"terminal record {record_id!r} must provide status")
    category_value = _terminal_value(source, "category", "kind", "role", default="workspace")
    if category_value in {"temporary", "cache", "rebuildable"}:
        category_value = "workspace"
    if category_value in {"canonical", "operational", "external"}:
        category_value = "other"
    if category_value == "registry_tombstone":
        category_value = "tombstone"
    if category_value not in _TERMINAL_RETENTION_CATEGORIES:
        raise ValueError(f"unsupported terminal retention category: {category_value!r}")
    category = cast(TerminalRetentionCategory, category_value)
    terminal_ns = _terminal_int(
        source,
        "terminal_ns",
        "completed_ns",
        "finished_ns",
        "retired_ns",
        "updated_ns",
    )
    apparent = _terminal_int(
        source,
        "apparent_bytes",
        "size_bytes",
        "payload_size_bytes",
        "estimated_bytes",
        default=0,
    )
    allocated = _terminal_int(source, "allocated_bytes", "physical_bytes", default=0)
    identity = _terminal_value(
        source,
        "physical_identity",
        "path_identity",
        "identity",
    )
    if identity is not None:
        if not isinstance(identity, (tuple, list)) or len(identity) != 3:
            raise ValueError("terminal physical_identity is invalid")
        identity = tuple(identity)
    tombstone = _terminal_bool(source, "tombstone", default=category_value == "tombstone")
    evidence_value = _terminal_value(source, "evidence_required", default=None)
    evidence_required = tombstone if evidence_value is None else _terminal_bool(
        source, "evidence_required"
    )
    return TerminalRetentionRecord(
        record_id=record_id,
        status=status,
        category=category,
        terminal_ns=terminal_ns,
        apparent_bytes=0 if apparent is None else apparent,
        allocated_bytes=0 if allocated is None else allocated,
        physical_identity=identity,
        reconciled=_terminal_bool(source, "reconciled", "reconciled_ok"),
        recovery_required=_terminal_bool(source, "recovery_required", "recovery_pending"),
        replay_required=_terminal_bool(source, "replay_required", "replay_pending"),
        pinned=_terminal_bool(source, "pinned", "pin"),
        grant_active=_terminal_bool(source, "grant_active", "grant", "lease_active"),
        authorization_active=_terminal_bool(
            source, "authorization_active", "retention_authorized", "authorization"
        ),
        release_authorized=_terminal_bool(source, "release_authorized", "releasable"),
        evidence_required=evidence_required,
        retain_until_ns=_terminal_int(source, "retain_until_ns", "retire_after_ns"),
        tombstone=tombstone,
        owner=cast(str | None, _terminal_value(source, "owner")),
    )


def _terminal_fingerprint(
    items: Sequence[TerminalRetentionItem], policy: TerminalRetentionPolicy
) -> str:
    claims = {
        "policy": policy.to_dict(),
        "items": [
            {
                "record": item.record.to_dict(),
                "disposition": item.disposition,
                "reasons": list(item.reasons),
            }
            for item in sorted(
                items,
                key=lambda value: (
                    value.record.record_id,
                    value.record.status_normalized,
                    value.record.terminal_ns if value.record.terminal_ns is not None else -1,
                ),
            )
        ],
    }
    encoded = json.dumps(
        claims,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def plan_terminal_retention(
    records: Iterable[TerminalRetentionRecord | Mapping[str, object] | object],
    *,
    now_ns: int,
    policy: TerminalRetentionPolicy | RetentionPolicy | None = None,
    baseline_record_ids: Collection[str] | None = None,
    free_bytes: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TerminalRetentionPlan:
    """Classify terminal owner observations under one bounded dry-run policy.

    ``records`` must be a projection obtained from the canonical owner.  This
    function does not scan filesystem paths, read manifests, infer liveness
    from PIDs, or mutate anything.  A count/byte fence stops observation before
    the next record and marks the whole selection incomplete; no item is then
    eligible.  Owners must pass explicit ``reconciled`` and
    ``release_authorized`` claims for any terminal cleanup to be considered.
    """

    if isinstance(policy, RetentionPolicy):
        selected_policy = TerminalRetentionPolicy.from_retention_policy(policy)
    elif policy is None:
        selected_policy = TerminalRetentionPolicy()
    elif isinstance(policy, TerminalRetentionPolicy):
        selected_policy = policy
    else:
        raise TypeError("terminal retention policy is invalid")
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
        raise ValueError("terminal retention now_ns must be a non-negative integer")
    if free_bytes is not None and (
        isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0
    ):
        raise ValueError("terminal free_bytes must be a non-negative integer or None")
    baseline = tuple(sorted(set(baseline_record_ids or ())))
    if any(
        not isinstance(value, str) or not value.strip() for value in baseline
    ):
        raise ValueError("terminal baseline_record_ids must contain non-empty text")
    if any(len(value.encode("utf-8")) > _MAX_TERMINAL_RECORD_ID_BYTES for value in baseline):
        raise ValueError("terminal baseline_record_ids contain an oversized identifier")

    bounded: list[TerminalRetentionRecord] = []
    omitted: int | None = None
    truncated = False
    truncation_reasons: list[str] = []
    observed_budget = 0
    source_is_sequence = isinstance(records, Sequence) and not isinstance(records, (str, bytes))
    known_total = len(records) if source_is_sequence else None  # type: ignore[arg-type]
    iterator = iter(records)
    while True:
        if cancelled is not None and cancelled():
            raise RetentionPlanningCancelled("terminal retention planning was cancelled")
        if len(bounded) >= selected_policy.max_records:
            truncated = True
            truncation_reasons.append("record_limit")
            if known_total is not None:
                omitted = max(0, known_total - len(bounded))
            break
        try:
            raw = next(iterator)
        except StopIteration:
            break
        record = _coerce_terminal_record(raw)
        observation_cost = record.apparent_bytes + record.allocated_bytes
        if observation_cost > selected_policy.max_bytes - observed_budget:
            truncated = True
            truncation_reasons.append("byte_limit")
            if known_total is not None:
                omitted = max(0, known_total - len(bounded))
            break
        bounded.append(record)
        observed_budget += observation_cost
    if truncated and omitted is None and known_total is not None:
        omitted = max(0, known_total - len(bounded))

    # Duplicate IDs are an invalid observation boundary.  Keep the records in
    # the evidence, but prevent either projection from becoming eligible.
    id_counts: dict[str, int] = {}
    for record in bounded:
        id_counts[record.record_id] = id_counts.get(record.record_id, 0) + 1

    candidates_by_category: dict[TerminalRetentionCategory, list[int]] = {}
    preliminary: dict[int, tuple[TerminalRetentionDisposition, list[str]]] = {}
    for index, record in enumerate(bounded):
        reasons: list[str] = []
        status = record.status_normalized
        if id_counts[record.record_id] > 1:
            reasons.append("duplicate_record_id")
        if status in _TERMINAL_LIVE_STATES:
            reasons.append("state_not_terminal")
        elif status not in _TERMINAL_RETENTION_STATES:
            reasons.append("unknown_status")
        if record.recovery_required or status in {"recovery_required", "recovering", "recovery"}:
            reasons.append("recovery_required")
        if record.replay_required:
            reasons.append("replay_required")
        if record.pinned:
            reasons.append("pinned")
        if record.grant_active:
            reasons.append("grant_active")
        if record.authorization_active:
            reasons.append("authorization_active")
        if record.evidence_required:
            reasons.append("evidence_required")
        if not record.reconciled:
            reasons.append("reconciliation_required")
        if not record.release_authorized:
            reasons.append("release_not_authorized")
        if record.terminal_ns is None:
            reasons.append("terminal_timestamp_missing")
        elif selected_policy.minimum_age_ns is None:
            reasons.append("age_policy_not_configured")
        elif (
            record.terminal_ns > now_ns
            or now_ns - record.terminal_ns < selected_policy.minimum_age_ns
        ):
            reasons.append("minimum_age_not_reached")
        if record.retain_until_ns is not None and now_ns < record.retain_until_ns:
            reasons.append("retention_active")
        if reasons:
            preliminary[index] = (
                "protected" if "unknown_status" not in reasons else "blocked",
                reasons,
            )
            continue
        category: TerminalRetentionCategory = (
            "tombstone" if record.tombstone else record.category
        )
        candidates_by_category.setdefault(category, []).append(index)
        preliminary[index] = ("protected", ["policy_window"])

    # Quotas keep the newest safe records.  Sorting ties by ID gives a stable
    # decision independent of input order, which is important for replay.
    for category, indexes in candidates_by_category.items():
        count_limit, byte_limit = selected_policy.quota(category)
        ranked = sorted(
            indexes,
            key=lambda index: (
                bounded[index].terminal_ns if bounded[index].terminal_ns is not None else -1,
                bounded[index].record_id,
            ),
            reverse=True,
        )
        kept_count = 0
        kept_bytes = 0
        keep: set[int] = set()
        for index in ranked:
            record = bounded[index]
            if kept_count >= count_limit or (
                kept_count > 0 and kept_bytes + record.apparent_bytes > byte_limit
            ):
                continue
            keep.add(index)
            kept_count += 1
            kept_bytes += record.apparent_bytes
        for index in ranked:
            if index in keep:
                continue
            preliminary[index] = ("eligible", ["terminal_retention_expired"])

    items: list[TerminalRetentionItem] = []
    for index, record in enumerate(bounded):
        disposition, reasons = preliminary[index]
        if truncated:
            disposition = "blocked"
            reasons = list(dict.fromkeys(("observation_incomplete", *reasons)))
        items.append(TerminalRetentionItem(record, disposition, tuple(reasons)))
    status = (
        "blocked"
        if truncated or any(item.disposition == "blocked" for item in items)
        else "ready"
    )
    complete = all(item.record.physical_identity is not None for item in items)
    plan = TerminalRetentionPlan(
        now_ns=now_ns,
        policy=selected_policy,
        items=tuple(items),
        status=status,
        truncated=truncated,
        truncation_reasons=tuple(dict.fromkeys(truncation_reasons)),
        omitted_records=omitted,
        baseline_record_ids=baseline,
        free_bytes=free_bytes,
        physical_accounting_complete=complete,
    )
    return replace(plan, fingerprint=_terminal_fingerprint(plan.items, selected_policy))


def terminal_retention_plan_payload(plan: TerminalRetentionPlan) -> dict[str, object]:
    """Return the bounded JSON envelope for terminal retention evidence."""

    return plan.to_dict()


@dataclass(slots=True)
class _StoreSnapshot:
    store: RetentionStore
    database: Path
    status: StoreStatus
    schema_version: int | None
    connection: sqlite3.Connection | None
    detail: str | None
    storage: Mapping[str, int | None] | None = None


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise RetentionPlanningCancelled("retention planning was cancelled")


@contextmanager
def _readonly_snapshot(
    database: Path,
    cancelled: Callable[[], bool] | None,
    *,
    cache: SQLiteSnapshotReuseCache | None = None,
    budget: SQLiteSnapshotBudget | None = None,
    generation: object | None = None,
    inspection_budget: _RetentionInspectionBudget | None = None,
) -> Iterator[sqlite3.Connection]:
    if sqlite3 is not _CANONICAL_SQLITE_MODULE:
        connection = sqlite3.connect(
            readonly_sqlite_uri(database),
            uri=True,
            timeout=5.0,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("retention snapshot could not enforce foreign_keys")
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("retention snapshot could not enforce query_only")
            if cancelled is not None:
                connection.set_progress_handler(lambda: int(cancelled()), 1_000)
            if inspection_budget is not None:
                connection.set_progress_handler(inspection_budget.progress, 1_000)
            connection.execute("BEGIN")
            yield connection
        finally:
            try:
                connection.set_progress_handler(None, 0)
            finally:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()
        return
    selected_budget = budget or SQLiteSnapshotBudget(
        prepare_timeout_seconds=5.0, cancellation_check=cancelled,
    )
    # Retention is a diagnostic reader and may be interleaved with a writer.
    # Keep the detached-copy path for ordinary owners so a later commit cannot
    # invalidate a page, but do not make a healthy, quiescent multi-gigabyte
    # owner pay a full temporary copy merely to inspect one bounded page.  The
    # immutable path is selected only above the canonical default copy budget;
    # it still requires an empty sidecar set and verifies the source fence on
    # close.  Active owners therefore remain on ``snapshot_temp`` and fail
    # closed when their bounded copy cannot fit.
    mode = SQLiteReadMode.SNAPSHOT_TEMP
    try:
        owner_bytes = database.stat().st_size
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                owner_bytes += Path(f"{database}{suffix}").stat().st_size
            except FileNotFoundError:
                continue
        oversized_owner = owner_bytes > DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES
    except OSError:
        oversized_owner = False
    if oversized_owner:
        mode = preferred_sqlite_read_mode(database)
    read = (
        SQLiteReadSession(
            database, mode=mode,
            timeout_seconds=selected_budget.prepare_timeout_seconds,
            budget=selected_budget,
        )
        if cache is None
        else cache.acquire(
            database, generation=generation, mode=mode,
            timeout_seconds=selected_budget.prepare_timeout_seconds,
            budget=selected_budget,
        )
    )
    with read as connection:
        # The kernel enables these safeguards, while retention keeps its
        # shorter bounded busy budget and cancellation progress hook.
        connection.execute("PRAGMA busy_timeout=5000")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("retention snapshot could not enforce foreign_keys")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("retention snapshot could not enforce query_only")
        if cancelled is not None:
            connection.set_progress_handler(lambda: int(cancelled()), 1_000)
        if inspection_budget is not None:
            connection.set_progress_handler(inspection_budget.progress, 1_000)
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            try:
                connection.set_progress_handler(None, 0)
            finally:
                if connection.in_transaction:
                    connection.rollback()


def _metadata_version(connection: sqlite3.Connection, label: str) -> int:
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"{label} metadata has no unique schema_version")
    raw = str(rows[0][0])
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{label} schema_version is not an integer") from exc
    if raw != str(value):
        raise RuntimeError(f"{label} schema_version is not canonical")
    return value


def _validate_snapshot(
    store: RetentionStore,
    connection: sqlite3.Connection,
) -> int:
    if store == "semantic":
        version = semantic_schema._read_schema_version(connection)
        if version != semantic_schema.SEMANTIC_SCHEMA_VERSION:
            raise RuntimeError(
                f"semantic schema is {version!r}; expected "
                f"{semantic_schema.SEMANTIC_SCHEMA_VERSION}"
            )
        semantic_schema._validate_version_contract(connection, version)
        return version
    if store == "catalog":
        version = _metadata_version(connection, "document catalog")
        if version != document_catalog_schema.CATALOG_SCHEMA_VERSION:
            raise RuntimeError(
                f"document catalog schema is {version}; expected "
                f"{document_catalog_schema.CATALOG_SCHEMA_VERSION}"
            )
        validate_sqlite_schema_contract(
            connection,
            document_catalog_schema.document_catalog_schema_contract(),
            label="document catalog retention source",
            exact=True,
        )
        pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if pragma_version > document_catalog_schema.CATALOG_SCHEMA_VERSION:
            raise RuntimeError(
                f"document catalog schema is newer than supported: {pragma_version}; "
                f"expected {document_catalog_schema.CATALOG_SCHEMA_VERSION}"
            )
        return version
    if store == "inventory":
        version = _metadata_version(connection, "dedup inventory")
        if version != INVENTORY_SCHEMA_VERSION:
            raise RuntimeError(
                f"dedup inventory schema is {version}; expected {INVENTORY_SCHEMA_VERSION}"
            )
        validate_inventory_schema(connection)
        pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if pragma_version > INVENTORY_SCHEMA_VERSION:
            raise RuntimeError(
                f"dedup inventory schema is newer than supported: {pragma_version}; "
                f"expected {INVENTORY_SCHEMA_VERSION}"
            )
        return version
    version = _metadata_version(connection, "framework")
    if version != framework_schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"framework schema is {version!r}; expected {framework_schema.SCHEMA_VERSION}"
        )
    framework_schema.validate_framework_schema(connection)
    pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if pragma_version > framework_schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"framework schema is newer than supported: {pragma_version}; "
            f"expected {framework_schema.SCHEMA_VERSION}"
        )
    return version


def _file_sizes(database: Path) -> tuple[int, int, int]:
    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    return (
        size(database),
        size(Path(f"{database}-wal")),
        size(Path(f"{database}-shm")),
    )


def _empty_store_plan(
    snapshot: _StoreSnapshot,
    *,
    after: int,
) -> RetentionStorePlan:
    database_bytes, wal_bytes, shm_bytes = _file_sizes(snapshot.database)
    return RetentionStorePlan(
        store=snapshot.store,
        database=snapshot.database,
        status=snapshot.status,
        schema_version=snapshot.schema_version,
        database_bytes=database_bytes,
        wal_bytes=wal_bytes,
        shm_bytes=shm_bytes,
        items=(),
        holds=(),
        after=after,
        next_after=None,
        truncated=False,
        detail=snapshot.detail,
    )


def _age_disposition(
    *,
    policy: RetentionPolicy,
    now_ns: int,
    terminal_ns: int | None,
) -> tuple[Disposition, tuple[str, ...]]:
    if policy.minimum_age_ns is None:
        return "protected", ("policy_not_configured",)
    if terminal_ns is None:
        return "blocked", ("terminal_timestamp_missing",)
    if terminal_ns > now_ns or now_ns - terminal_ns < policy.minimum_age_ns:
        return "protected", ("minimum_age_not_reached",)
    return "eligible", ("explicit_age_policy_matched", "dry_run_only")


def _page_result(
    rows: Sequence[sqlite3.Row],
    *,
    batch_size: int,
    build: Callable[[sqlite3.Row], RetentionItem],
) -> tuple[tuple[RetentionItem, ...], int | None, bool]:
    truncated = len(rows) > batch_size
    selected = rows[:batch_size]
    items = tuple(build(row) for row in selected)
    next_after = items[-1].key if truncated and items else None
    return items, next_after, truncated


@dataclass(frozen=True, slots=True)
class _ReachabilityResult:
    """Bounded result for a parent graph rooted at live references."""

    ancestors: frozenset[int]
    complete: bool


def _quoted_identifier(identifier: str) -> str:
    """Quote an internal SQL identifier before interpolating it."""

    return '"' + identifier.replace('"', '""') + '"'


def _bounded_parent_reachability(
    connection: sqlite3.Connection,
    *,
    table: str,
    node_column: str,
    parent_column: str,
    root_where: str,
    root_parameters: Sequence[object] = (),
    max_depth: int = _MAX_REACHABILITY_DEPTH,
    max_nodes: int = _MAX_REACHABILITY_NODES,
    depth_limit_is_incomplete: bool = True,
) -> _ReachabilityResult:
    """Collect ancestors of live roots without allowing an unbounded walk.

    The table/column names and predicate are module-owned constants; identifiers
    are quoted anyway so this helper cannot become a SQL injection seam if a
    future owner adds a new call.  ``complete=False`` is conservative evidence:
    callers must block eligibility rather than treating a truncated graph as
    unreachable.
    """

    if not 1 <= max_depth <= _MAX_REACHABILITY_DEPTH:
        raise ValueError("reachability depth is outside the planner bound")
    if not 1 <= max_nodes <= _MAX_REACHABILITY_NODES:
        raise ValueError("reachability node limit is outside the planner bound")
    if not isinstance(depth_limit_is_incomplete, bool):
        raise ValueError("reachability depth policy is invalid")
    table_sql = _quoted_identifier(table)
    node_sql = _quoted_identifier(node_column)
    parent_sql = _quoted_identifier(parent_column)
    # ``UNION ALL`` preserves depth evidence.  The depth guard is what makes a
    # malformed cycle finite; a node seen at the final depth is incomplete if
    # it still has a parent to follow.
    query = f"""
        WITH RECURSIVE ancestors(node_id, depth) AS (
            SELECT {parent_sql}, 1
            FROM {table_sql}
            WHERE {root_where} AND {parent_sql} IS NOT NULL
            UNION ALL
            SELECT parent.{parent_sql}, ancestors.depth + 1
            FROM {table_sql} AS parent
            JOIN ancestors ON parent.{node_sql}=ancestors.node_id
            WHERE ancestors.depth < ? AND parent.{parent_sql} IS NOT NULL
        )
        SELECT node_id, depth FROM ancestors LIMIT ?
    """
    rows = connection.execute(
        query,
        (*root_parameters, max_depth, max_nodes + 1),
    ).fetchall()
    truncated = len(rows) > max_nodes
    selected = rows[:max_nodes]
    ancestors = frozenset(int(row[0]) for row in selected if row[0] is not None)
    if ancestors:
        placeholders = ",".join("?" for _ in ancestors)
        existing = {
            int(row[0])
            for row in connection.execute(
                f"SELECT {node_sql} FROM {table_sql} "
                f"WHERE {node_sql} IN ({placeholders})",
                tuple(ancestors),
            ).fetchall()
        }
        # A dangling parent is not evidence of an unreachable historical
        # record.  It is an incomplete graph and therefore blocks eligibility.
        if existing != set(ancestors):
            truncated = True
    if depth_limit_is_incomplete and any(int(row[1]) >= max_depth for row in selected):
        truncated = True
    return _ReachabilityResult(ancestors, not truncated)


def _bounded_inventory_reachability(
    connection: sqlite3.Connection,
    *,
    max_depth: int = _MAX_REACHABILITY_DEPTH,
    max_nodes: int = _MAX_REACHABILITY_NODES,
) -> _ReachabilityResult:
    """Walk predecessor scans from every recorded successor, bounded."""

    if not 1 <= max_depth <= _MAX_REACHABILITY_DEPTH:
        raise ValueError("reachability depth is outside the planner bound")
    if not 1 <= max_nodes <= _MAX_REACHABILITY_NODES:
        raise ValueError("reachability node limit is outside the planner bound")
    query = """
        WITH RECURSIVE ancestors(scan_id, depth) AS (
            SELECT predecessor_scan_id, 1
            FROM inventory_scan_successors
            WHERE predecessor_scan_id IS NOT NULL
            UNION ALL
            SELECT edge.predecessor_scan_id, ancestors.depth + 1
            FROM inventory_scan_successors AS edge
            JOIN ancestors ON edge.successor_scan_id=ancestors.scan_id
            WHERE ancestors.depth < ? AND edge.predecessor_scan_id IS NOT NULL
        )
        SELECT scan_id, depth FROM ancestors LIMIT ?
    """
    rows = connection.execute(query, (max_depth, max_nodes + 1)).fetchall()
    truncated = len(rows) > max_nodes
    selected = rows[:max_nodes]
    ancestors = frozenset(int(row[0]) for row in selected if row[0] is not None)
    if ancestors:
        placeholders = ",".join("?" for _ in ancestors)
        existing = {
            int(row[0])
            for row in connection.execute(
                "SELECT scan_id FROM scans "
                f"WHERE scan_id IN ({placeholders})",
                tuple(ancestors),
            ).fetchall()
        }
        if existing != set(ancestors):
            truncated = True
    if any(int(row[1]) >= max_depth for row in selected):
        truncated = True
    return _ReachabilityResult(ancestors, not truncated)


def _status_is_partial_or_recovery(status: object) -> bool:
    """Recognize incomplete/uncertain owner states without broad inference."""

    return str(status).strip().casefold() in {
        "partial",
        "ready_partial",
        "recovery",
        "recovering",
        "recovery_required",
    }


_APPEND_ONLY_EXACT_ACCOUNTING_MAX_ROWS = 50_000


def _append_only_account(
    connection: sqlite3.Connection,
    *,
    table: str,
    byte_expression: str,
    minimum_bytes_per_row: int,
) -> tuple[int, int]:
    """Return a bounded lower-bound account for an append-only table.

    Counting rows is cheap on the owner index, while summing large JSON
    payloads can consume the entire retention SQL budget.  Keep exact text
    accounting for ordinary-sized tables and use a schema-derived minimum
    lower bound for larger tables, never pretending that the latter is a
    physical-reclaim estimate.
    """

    rows = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if rows == 0:
        return 0, 0
    if rows > _APPEND_ONLY_EXACT_ACCOUNTING_MAX_ROWS:
        return rows, rows * minimum_bytes_per_row
    estimated = int(
        connection.execute(
            f"SELECT COALESCE(SUM({byte_expression}),0) FROM {table}"
        ).fetchone()[0]
    )
    return rows, estimated


def _semantic_holds(connection: sqlite3.Connection) -> tuple[RetentionHold, ...]:
    model_registry = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM vector_spaces)+
        (SELECT COUNT(*) FROM embedding_models)+
        (SELECT COUNT(*) FROM label_prototypes),
        (SELECT COALESCE(SUM(length(vector_space)+length(distance)+
            length(normalization)),0) FROM vector_spaces)+
        (SELECT COALESCE(SUM(length(model_signature)+length(vector_space)+
            length(modality)+length(model_id)+length(model_version)+
            length(provider)+length(supported_roles_json)+length(vector_dtype)+
            length(normalization)+length(distance)+length(provenance_json)),0)
         FROM embedding_models)+
        (SELECT COALESCE(SUM(length(prototype_id)+length(ontology_id)+
            length(ontology_version)+length(concept_id)+
            length(prototype_version)+length(model_signature)+
            length(vector_space)+length(prototype_text)+
            length(content_xxh3_128)+length(content_xxh3_64_guard)+
            length(vector_dtype)+length(vector_blob)+length(calibration_status)+
            COALESCE(length(feedback_reference),0)+length(provenance_json)),0)
         FROM label_prototypes)"""
    ).fetchone()
    source_content = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM semantic_items)+
        (SELECT COUNT(*) FROM text_chunks)+
        (SELECT COUNT(*) FROM text_channel_revisions),
        (SELECT COALESCE(SUM(length(item_id)+length(source_kind)+
            length(source_identity)+length(identity_version)+
            COALESCE(length(path),0)+length(content_xxh3_128)+
            length(content_xxh3_64_guard)+length(provenance_json)+
            COALESCE(length(refresh_token),0)+length(source_revision_json)),0)
         FROM semantic_items)+
        (SELECT COALESCE(SUM(length(chunk_id)+length(item_id)+
            length(section_kind)+length(section_id)+length(text_zlib)+
            length(content_xxh3_128)+length(content_xxh3_64_guard)+
            length(chunking_signature)+length(provenance_json)+
            length(refresh_token)),0) FROM text_chunks)+
        (SELECT COALESCE(SUM(length(item_id)+length(channel)+
            length(revision_token)),0) FROM text_channel_revisions)"""
    ).fetchone()
    evidence = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM vector_payloads)+
        (SELECT COUNT(*) FROM semantic_item_revisions)+
        (SELECT COUNT(*) FROM semantic_chunk_revisions)+
        (SELECT COUNT(*) FROM semantic_evidence),
        (SELECT COALESCE(SUM(length(vector_blob)+length(provenance_json)),0)
         FROM vector_payloads)+
        (SELECT COALESCE(SUM(length(item_id)+length(source_kind)+
            length(source_identity)+length(identity_version)+COALESCE(length(path),0)+
            length(provenance_json)+length(source_revision_json)),0)
         FROM semantic_item_revisions)+
        (SELECT COALESCE(SUM(length(chunk_id)+length(item_id)+length(text_zlib)+
            length(provenance_json)),0) FROM semantic_chunk_revisions)+
        (SELECT COALESCE(SUM(length(item_id)+length(source_entity_id)+
            length(ontology_id)+length(ontology_version)+length(concept_id)+
            length(prototype_id)+length(query_model_signature)+
            length(indexed_model_signature)+length(vector_space)+
            length(provenance_json)+length(refresh_token)),0)
         FROM semantic_evidence)"""
    ).fetchone()
    lineage_rows, lineage_bytes = _append_only_account(
        connection,
        table="semantic_work_receipts",
        byte_expression=(
            "length(receipt_key)+length(contract_version)+length(stage_id)+"
            "length(stage_version)+length(processing_signature)+length(status)+"
            "length(execution_mode)+length(reproducibility_class)+length(entity_kind)+"
            "length(entity_id)+length(receipt_json)"
        ),
        minimum_bytes_per_row=11,
    )
    derivation_rows, derivation_bytes = _append_only_account(
        connection,
        table="semantic_chunk_derivations",
        byte_expression="length(refresh_token)",
        minimum_bytes_per_row=1,
    )
    outbox_rows, outbox_bytes = _append_only_account(
        connection,
        table="semantic_derivation_outbox",
        byte_expression=(
            "length(event_kind)+length(aggregate_kind)+length(aggregate_id)+"
            "length(payload_json)"
        ),
        minimum_bytes_per_row=4,
    )
    return (
        RetentionHold(
            "semantic_model_registry",
            "model_spaces_and_prototypes_require_an_independent_policy",
            int(model_registry[0]),
            int(model_registry[1]),
        ),
        RetentionHold(
            "shared_semantic_source_content",
            "active_items_and_chunks_are_shared_across_generations",
            int(source_content[0]),
            int(source_content[1]),
        ),
        RetentionHold(
            "shared_semantic_payload_and_evidence",
            "shared_rows_require_reference_proof_before_pruning",
            int(evidence[0]),
            int(evidence[1]),
        ),
        RetentionHold(
            "semantic_lineage_and_outbox",
            "append_only_lineage_and_delivery_evidence_requires_retention",
            lineage_rows + derivation_rows + outbox_rows,
            lineage_bytes + derivation_bytes + outbox_bytes,
        ),
    )


def _plan_semantic(
    snapshot: _StoreSnapshot,
    *,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
) -> RetentionStorePlan:
    connection = snapshot.connection
    assert connection is not None
    active_builder_reachability = _bounded_parent_reachability(
        connection,
        table="embedding_generations",
        node_column="generation_id",
        parent_column="base_generation_id",
        root_where="status='building' AND base_clone_complete=0",
        # A generation's base is required only while its direct child is
        # cloning.  Once that parent is available, walking older terminal
        # bases would retain an entire historical chain without proving a
        # live consumer.  The edge itself is still bounded and fail-closed.
        max_depth=1,
        depth_limit_is_incomplete=False,
    )
    rows = connection.execute(
        """SELECT g.generation_id,g.model_signature,g.status,g.started_ns,
        g.completed_ns,
        EXISTS(SELECT 1 FROM published_embedding_heads h
               WHERE h.generation_id=g.generation_id) AS current_head,
        EXISTS(SELECT 1 FROM published_embedding_heads h
               WHERE h.model_signature=g.model_signature AND
               g.generation_id=(SELECT MAX(previous.generation_id)
                 FROM embedding_generations previous
                 WHERE previous.model_signature=h.model_signature
                   AND previous.status='ready'
                   AND previous.generation_id<h.generation_id)) AS previous_head,
        EXISTS(SELECT 1 FROM embedding_generations child
               WHERE child.base_generation_id=g.generation_id
                 AND child.status='building'
                 AND child.base_clone_complete=0) AS incoming_base,
        EXISTS(SELECT 1 FROM semantic_evidence evidence
               WHERE evidence.generation_id=g.generation_id) AS evidence_reference,
        EXISTS(SELECT 1 FROM semantic_work_receipts receipt
               WHERE receipt.generation_id=g.generation_id) AS lineage_reference,
        EXISTS(SELECT 1 FROM embedding_jobs live
               WHERE live.generation_id=g.generation_id AND live.status='leased'
                 AND live.lease_until_ns>?) AS live_lease,
        1+(SELECT COUNT(*) FROM embedding_jobs j
           WHERE j.generation_id=g.generation_id)+
          (SELECT COUNT(*) FROM embedding_generation_members m
           WHERE m.generation_id=g.generation_id)+
          (SELECT COUNT(*) FROM text_embeddings t
           WHERE t.generation_id=g.generation_id)+
          (SELECT COUNT(*) FROM image_embeddings i
           WHERE i.generation_id=g.generation_id)+
          (SELECT COUNT(*) FROM published_embedding_heads h
           WHERE h.generation_id=g.generation_id) AS estimated_rows,
        length(g.model_signature)+length(g.processing_signature)+length(g.status)+
        length(g.provenance_json)+length(g.cursor_json)+
          (SELECT COALESCE(SUM(length(role)+length(entity_kind)+length(entity_id)+
             length(item_id)+length(content_xxh3_128)+
             length(content_xxh3_64_guard)+COALESCE(length(lease_owner),0)+
             COALESCE(length(error_type),0)+COALESCE(length(error_message),0)),0)
           FROM embedding_jobs j WHERE j.generation_id=g.generation_id)+
          (SELECT COALESCE(SUM(length(model_signature)+length(entity_kind)+
             length(entity_id)+length(item_id)+length(content_xxh3_128)+
             length(content_xxh3_64_guard)+length(provenance_json)),0)
           FROM embedding_generation_members m
           WHERE m.generation_id=g.generation_id)+
          (SELECT COALESCE(SUM(length(chunk_id)+length(model_signature)+
             length(content_xxh3_128)+length(content_xxh3_64_guard)+
             length(provenance_json)),0) FROM text_embeddings t
           WHERE t.generation_id=g.generation_id)+
          (SELECT COALESCE(SUM(length(item_id)+length(model_signature)+
             length(content_xxh3_128)+length(content_xxh3_64_guard)+
             length(provenance_json)),0) FROM image_embeddings i
           WHERE i.generation_id=g.generation_id)+
          (SELECT COALESCE(SUM(length(model_signature)),0)
           FROM published_embedding_heads h
           WHERE h.generation_id=g.generation_id) AS estimated_bytes
        FROM embedding_generations g WHERE g.generation_id>?
        ORDER BY g.generation_id LIMIT ?""",
        (now_ns, after, policy.batch_size + 1),
    ).fetchall()

    def build(row: sqlite3.Row) -> RetentionItem:
        reasons: list[str] = []
        if bool(row["current_head"]):
            reasons.append("current_published_generation")
        if bool(row["previous_head"]):
            reasons.append("previous_published_generation")
        if bool(row["live_lease"]):
            reasons.append("live_worker_lease")
        if str(row["status"]) == "building":
            reasons.append("resumable_builder_no_durable_owner")
        if bool(row["incoming_base"]):
            reasons.append("referenced_as_generation_base")
        if bool(row["evidence_reference"]):
            reasons.append("referenced_by_semantic_evidence")
        if bool(row["lineage_reference"]):
            reasons.append("referenced_by_semantic_work_receipt")
        if int(row["generation_id"]) in active_builder_reachability.ancestors:
            # Keep the historical reason stable for callers while extending
            # the check from one parent edge to the bounded live-builder graph.
            if "referenced_as_generation_base" not in reasons:
                reasons.append("referenced_as_generation_base")
        if any(
            reason in reasons
            for reason in (
                "current_published_generation",
                "previous_published_generation",
                "live_worker_lease",
                "resumable_builder_no_durable_owner",
            )
        ):
            disposition: Disposition = "protected"
        elif not active_builder_reachability.complete:
            disposition = "blocked"
            reasons.append("bounded_reachability_incomplete")
        elif bool(row["incoming_base"]):
            disposition = "blocked"
        elif bool(row["evidence_reference"]):
            disposition = "blocked"
        elif bool(row["lineage_reference"]):
            disposition = "blocked"
        elif _status_is_partial_or_recovery(row["status"]):
            disposition = "blocked"
            reasons.append("partial_or_recovery_state")
        elif str(row["status"]) not in {"ready", "ready_partial", "failed"}:
            disposition = "blocked"
            reasons.append("unexpected_generation_status")
        else:
            disposition, age_reasons = _age_disposition(
                policy=policy,
                now_ns=now_ns,
                terminal_ns=(None if row["completed_ns"] is None else int(row["completed_ns"])),
            )
            reasons.extend(age_reasons)
        return RetentionItem(
            key=int(row["generation_id"]),
            entity="embedding_generation",
            scope=str(row["model_signature"]),
            recorded_status=str(row["status"]),
            disposition=disposition,
            reasons=tuple(reasons),
            estimated_rows=int(row["estimated_rows"]),
            estimated_bytes=int(row["estimated_bytes"]),
        )

    items, next_after, truncated = _page_result(
        rows,
        batch_size=policy.batch_size,
        build=build,
    )
    database_bytes, wal_bytes, shm_bytes = _file_sizes(snapshot.database)
    return RetentionStorePlan(
        snapshot.store,
        snapshot.database,
        "blocked" if not active_builder_reachability.complete else "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _semantic_holds(connection),
        after,
        next_after,
        truncated,
        detail=(
            "bounded semantic generation reachability is incomplete"
            if not active_builder_reachability.complete
            else None
        ),
    )


def _catalog_holds(connection: sqlite3.Connection) -> tuple[RetentionHold, ...]:
    history = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(length(source_kind)+length(file_key)+
        length(processing_signature)+length(text_fingerprint)+
        length(classifier_signature)+length(path)+length(classification_json)),0)
        FROM classification_history"""
    ).fetchone()
    uncertain = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(length(source_path)+
        COALESCE(length(destination_path),0)+length(evidence_json)),0)
        FROM organization_plans
        WHERE status IN ('applying','moved_cache_pending','recovery_required')"""
    ).fetchone()
    return (
        RetentionHold(
            "classification_history",
            "classification_provenance_is_not_generation_owned",
            int(history[0]),
            int(history[1]),
        ),
        RetentionHold(
            "uncertain_organization_actions",
            "uncertain_mutation_evidence_is_never_a_retention_candidate",
            int(uncertain[0]),
            int(uncertain[1]),
        ),
    )


def _plan_catalog(
    snapshot: _StoreSnapshot,
    *,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
) -> RetentionStorePlan:
    connection = snapshot.connection
    assert connection is not None
    active_builder_reachability = _bounded_parent_reachability(
        connection,
        table="catalog_generations",
        node_column="generation_id",
        parent_column="base_generation_id",
        root_where="status='building'",
        max_depth=1,
        depth_limit_is_incomplete=False,
    )
    rows = connection.execute(
        """SELECT g.generation_id,g.source_kind,g.status,g.started_ns,
        g.completed_ns,g.catalog_run_id,
        EXISTS(SELECT 1 FROM catalog_publications p
               WHERE p.generation_id=g.generation_id) AS current_head,
        EXISTS(SELECT 1 FROM catalog_publications p
               WHERE p.source_kind=g.source_kind AND
               g.generation_id=(SELECT MAX(previous.generation_id)
                 FROM catalog_generations previous
                 WHERE previous.source_kind=p.source_kind
                   AND previous.status='published'
                   AND previous.generation_id<p.generation_id)) AS previous_head,
        EXISTS(SELECT 1 FROM catalog_generations child
               WHERE child.base_generation_id=g.generation_id
                 AND child.status='building') AS incoming_base,
        EXISTS(SELECT 1 FROM organization_plans plan
               WHERE plan.catalog_run_id=g.catalog_run_id AND plan.status IN
               ('applying','moved_cache_pending','recovery_required')) AS uncertain_action,
        1+(SELECT COUNT(*) FROM catalog_generation_documents d
           WHERE d.generation_id=g.generation_id) AS estimated_rows,
        length(g.source_kind)+length(g.status)+COALESCE(length(g.error_type),0)+
          COALESCE(length(g.error_message),0)+
          (SELECT COALESCE(SUM(length(source_kind)+length(file_key)+length(path)+
             length(volume_id)+length(file_id)+length(source_status)+
             length(processing_signature)+COALESCE(length(text_fingerprint),0)+
             length(classifier_signature)+length(primary_kind)+
             length(classification_json)+length(standard_references_json)+
             length(organizations_json)+length(topics_json)),0)
           FROM catalog_generation_documents d
           WHERE d.generation_id=g.generation_id) AS estimated_bytes
        FROM catalog_generations g WHERE g.generation_id>?
        ORDER BY g.generation_id LIMIT ?""",
        (after, policy.batch_size + 1),
    ).fetchall()

    def build(row: sqlite3.Row) -> RetentionItem:
        reasons: list[str] = []
        if bool(row["current_head"]):
            reasons.append("current_published_generation")
        if bool(row["previous_head"]):
            reasons.append("previous_published_generation")
        if str(row["status"]) == "building":
            reasons.append("builder_liveness_unverifiable")
        if bool(row["uncertain_action"]):
            reasons.append("uncertain_organization_action")
        if bool(row["incoming_base"]):
            reasons.append("referenced_as_generation_base")
        if int(row["generation_id"]) in active_builder_reachability.ancestors:
            if "referenced_as_generation_base" not in reasons:
                reasons.append("referenced_as_generation_base")
        if any(
            reason in reasons
            for reason in (
                "current_published_generation",
                "previous_published_generation",
                "builder_liveness_unverifiable",
                "uncertain_organization_action",
            )
        ):
            disposition: Disposition = "protected"
        elif not active_builder_reachability.complete:
            disposition = "blocked"
            reasons.append("bounded_reachability_incomplete")
        elif bool(row["incoming_base"]):
            disposition = "blocked"
        elif _status_is_partial_or_recovery(row["status"]):
            disposition = "blocked"
            reasons.append("partial_or_recovery_state")
        elif str(row["status"]) not in {
            "published",
            "failed",
            "cancelled",
            "superseded",
            "abandoned",
        }:
            disposition = "blocked"
            reasons.append("unexpected_generation_status")
        else:
            disposition, age_reasons = _age_disposition(
                policy=policy,
                now_ns=now_ns,
                terminal_ns=(None if row["completed_ns"] is None else int(row["completed_ns"])),
            )
            reasons.extend(age_reasons)
        return RetentionItem(
            int(row["generation_id"]),
            "catalog_generation",
            str(row["source_kind"]),
            str(row["status"]),
            disposition,
            tuple(reasons),
            int(row["estimated_rows"]),
            int(row["estimated_bytes"]),
        )

    items, next_after, truncated = _page_result(
        rows,
        batch_size=policy.batch_size,
        build=build,
    )
    database_bytes, wal_bytes, shm_bytes = _file_sizes(snapshot.database)
    return RetentionStorePlan(
        snapshot.store,
        snapshot.database,
        "blocked" if not active_builder_reachability.complete else "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _catalog_holds(connection),
        after,
        next_after,
        truncated,
        "bounded catalog generation reachability is incomplete"
        if not active_builder_reachability.complete
        else None,
    )


def _inventory_holds(connection: sqlite3.Connection) -> tuple[RetentionHold, ...]:
    row = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(length(volume_id)+length(file_id)+
        length(algorithm)+length(digest)),0) FROM fingerprints"""
    ).fetchone()
    return (
        RetentionHold(
            "shared_fingerprints",
            "fingerprints_are_shared_across_inventory_generations",
            int(row[0]),
            int(row[1]),
        ),
    )


def _referenced_inventory_scans(
    framework: _StoreSnapshot | None,
    scan_ids: Sequence[int],
) -> tuple[set[int], bool]:
    if framework is None or framework.status == "absent":
        return set(), False
    if framework.status != "ready" or framework.connection is None:
        return set(), True
    if not scan_ids:
        return set(), False
    placeholders = ",".join("?" for _ in scan_ids)
    rows = framework.connection.execute(
        f"SELECT DISTINCT scan_id FROM initial_runs WHERE scan_id IN ({placeholders})",
        tuple(scan_ids),
    ).fetchall()
    return {int(row[0]) for row in rows}, False


def _plan_inventory(
    snapshot: _StoreSnapshot,
    *,
    framework: _StoreSnapshot | None,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
) -> RetentionStorePlan:
    connection = snapshot.connection
    assert connection is not None
    successor_reachability = _bounded_inventory_reachability(connection)
    rows = connection.execute(
        """SELECT s.scan_id,s.root,s.status,s.started_ns,s.completed_ns,
        EXISTS(SELECT 1 FROM inventory_checkpoints c
               WHERE c.scan_id=s.scan_id AND c.valid=1) AS current_head,
        EXISTS(SELECT 1 FROM inventory_checkpoints c
               WHERE c.root=s.root AND c.valid=1 AND
               s.scan_id=(SELECT MAX(previous.scan_id) FROM scans previous
                 WHERE previous.root=s.root AND previous.status='complete'
                   AND previous.scan_id<c.scan_id)) AS previous_head,
        EXISTS(SELECT 1 FROM inventory_checkpoints c
               WHERE c.scan_id=s.scan_id) AS checkpoint_reference,
        EXISTS(SELECT 1 FROM inventory_checkpoints c
               WHERE c.root=s.root AND c.valid=1 AND s.status='complete'
                 AND s.scan_id>c.scan_id) AS publication_candidate,
        s.status='complete' AND
        (SELECT COUNT(*) FROM scans newer WHERE newer.root=s.root
          AND newer.status='complete' AND newer.scan_id>s.scan_id)<2
          AS latest_complete_without_head,
        1+(SELECT COUNT(*) FROM files f WHERE f.scan_id=s.scan_id)+
          (SELECT COUNT(*) FROM duplicate_plan_summaries summary
           WHERE summary.scan_id=s.scan_id)+
          (SELECT COUNT(*) FROM planned_duplicate_groups groups_
           WHERE groups_.scan_id=s.scan_id)+
          (SELECT COUNT(*) FROM planned_duplicate_members member
           JOIN planned_duplicate_groups groups_ USING(group_id)
           WHERE groups_.scan_id=s.scan_id) AS estimated_rows,
        length(s.root)+length(s.status)+
          (SELECT COALESCE(SUM(length(path)+length(volume_id)+length(file_id)),0)
           FROM files f WHERE f.scan_id=s.scan_id)+
          (SELECT COALESCE(SUM(length(keep_path)+length(full_fingerprint)),0)
           FROM planned_duplicate_groups groups_ WHERE groups_.scan_id=s.scan_id)+
          (SELECT COALESCE(SUM(length(member.path)+length(member.volume_id)+
             length(member.file_id)+length(member.role)),0)
           FROM planned_duplicate_members member
           JOIN planned_duplicate_groups groups_ USING(group_id)
           WHERE groups_.scan_id=s.scan_id) AS estimated_bytes
        FROM scans s WHERE s.scan_id>? ORDER BY s.scan_id LIMIT ?""",
        (after, policy.batch_size + 1),
    ).fetchall()
    selected_rows = rows[: policy.batch_size]
    references, dependency_unverified = _referenced_inventory_scans(
        framework,
        tuple(int(row["scan_id"]) for row in selected_rows),
    )

    def build(row: sqlite3.Row) -> RetentionItem:
        scan_id = int(row["scan_id"])
        reasons: list[str] = []
        if bool(row["current_head"]):
            reasons.append("current_published_inventory")
        if bool(row["previous_head"]):
            reasons.append("previous_published_inventory")
        if bool(row["checkpoint_reference"]):
            reasons.append("checkpoint_reference")
        if bool(row["publication_candidate"]):
            reasons.append("complete_publication_candidate")
        if str(row["status"]) == "building":
            reasons.append("active_inventory_builder")
        if scan_id in references:
            reasons.append("referenced_by_framework_run")
        if scan_id in successor_reachability.ancestors:
            reasons.append("referenced_by_inventory_successor")
        if (
            not bool(row["current_head"])
            and not bool(row["previous_head"])
            and not bool(row["checkpoint_reference"])
            and not bool(row["publication_candidate"])
            and bool(row["latest_complete_without_head"])
        ):
            reasons.append("latest_complete_without_published_head")
        if reasons:
            disposition: Disposition = "protected"
        elif not successor_reachability.complete:
            disposition = "blocked"
            reasons.append("bounded_reachability_incomplete")
        elif dependency_unverified:
            disposition = "blocked"
            reasons.append("framework_dependency_unverified")
        elif str(row["status"]) not in {"complete", "partial"}:
            disposition = "blocked"
            reasons.append("unexpected_scan_status")
        else:
            disposition, age_reasons = _age_disposition(
                policy=policy,
                now_ns=now_ns,
                terminal_ns=(None if row["completed_ns"] is None else int(row["completed_ns"])),
            )
            reasons.extend(age_reasons)
        return RetentionItem(
            scan_id,
            "inventory_scan",
            str(row["root"]),
            str(row["status"]),
            disposition,
            tuple(reasons),
            int(row["estimated_rows"]),
            int(row["estimated_bytes"]),
        )

    items = tuple(build(row) for row in selected_rows)
    truncated = len(rows) > policy.batch_size
    next_after = items[-1].key if truncated and items else None
    database_bytes, wal_bytes, shm_bytes = _file_sizes(snapshot.database)
    return RetentionStorePlan(
        snapshot.store,
        snapshot.database,
        "blocked"
        if (dependency_unverified or not successor_reachability.complete)
        else "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _inventory_holds(connection),
        after,
        next_after,
        truncated,
        (
            "bounded inventory scan reachability is incomplete"
            if not successor_reachability.complete
            else "framework retention dependency could not be validated"
            if dependency_unverified
            else None
        ),
    )


def _framework_holds(connection: sqlite3.Connection) -> tuple[RetentionHold, ...]:
    review_source_audit = audit_latest_review_task_source_publications_from_connection(
        connection,
        limit=MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
    )
    human = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM review_candidates)+
        (SELECT COUNT(*) FROM review_decisions)+
        (SELECT COUNT(*) FROM review_evidence_examples)+
        (SELECT COUNT(*) FROM review_tasks)+
        (SELECT COUNT(*) FROM review_task_events
         WHERE actor_kind='human'),
        (SELECT COALESCE(SUM(length(path)+length(evidence_json)),0)
         FROM review_candidates)+
        (SELECT COALESCE(SUM(length(path)+COALESCE(length(evidence_json),0)+
            length(provenance_json)+COALESCE(length(note),0)),0)
         FROM review_decisions)+
        (SELECT COALESCE(SUM(length(path)+COALESCE(length(evidence_json),0)+
            length(provenance_json)+COALESCE(length(note),0)),0)
         FROM review_evidence_examples)+
        (SELECT COALESCE(SUM(length(task_id)+length(logical_key)+
            length(task_type)+length(scope)+length(source_kind)+
            length(source_input_id)+length(source_ref_json)+
            length(source_snapshot_fingerprint)+length(snapshot_json)+
            length(evidence_json)+length(reason_code)+length(uncertainty_json)+
            length(priority_algorithm)+length(suggestions_json)+length(batch_id)+
            COALESCE(length(supersedes_task_id),0)),0)
         FROM review_tasks)+
        (SELECT COALESCE(SUM(length(event_id)+length(event_key)+length(task_id)+
            COALESCE(length(previous_event_id),0)+COALESCE(length(from_state),0)+
            length(to_state)+length(actor_kind)+length(actor_id)+
            length(provenance_json)+COALESCE(length(decision_json),0)+
            COALESCE(length(note),0)),0)
         FROM review_task_events WHERE actor_kind='human')"""
    ).fetchone()
    action_evidence = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM file_actions)+
        (SELECT COUNT(*) FROM file_action_events)+
        (SELECT COUNT(*) FROM file_action_reconciliation_events),
        (SELECT COALESCE(SUM(length(action_type)+length(source_path)+
            COALESCE(length(target_path),0)+COALESCE(length(detected_mime),0)+
            COALESCE(length(evidence),0)+COALESCE(length(detail),0)+
            COALESCE(length(idempotency_key),0)+
            COALESCE(length(expected_identity_json),0)+
            COALESCE(length(effect_receipt_json),0)),0)
         FROM file_actions)+
        (SELECT COALESCE(SUM(length(to_status)+length(stage)+
            COALESCE(length(from_status),0)+COALESCE(length(detail),0)+
            COALESCE(length(evidence_json),0)),0)
         FROM file_action_events)+
        (SELECT COALESCE(SUM(length(reconciliation_key)+length(action_status)+
            length(reconciler_signature)+length(actor)+length(provenance_json)+
            length(classification)+length(recommendation)+length(detail)+
            length(evidence_json)),0)
         FROM file_action_reconciliation_events)"""
    ).fetchone()
    return (
        RetentionHold(
            "human_review_evidence",
            "human_decisions_and_training_evidence_are_permanent_holds",
            int(human[0]),
            int(human[1]),
        ),
        RetentionHold(
            "file_action_audit_evidence",
            "mutation_and_reconciliation_evidence_is_a_permanent_hold",
            int(action_evidence[0]),
            int(action_evidence[1]),
        ),
        RetentionHold(
            "published_review_task_state",
            "current_review_task_source_heads_and_receipts_are_protected",
            len(review_source_audit.publications)
            + review_source_audit.batch_count
            + review_source_audit.membership_count
            + review_source_audit.progress_count,
            review_source_audit.estimated_bytes,
        ),
    )


def _referenced_framework_runs(
    catalog: _StoreSnapshot | None,
    run_ids: Sequence[int],
) -> tuple[set[int], bool]:
    if catalog is None or catalog.status == "absent":
        return set(), False
    if catalog.status != "ready" or catalog.connection is None:
        return set(), True
    if not run_ids:
        return set(), False
    placeholders = ",".join("?" for _ in run_ids)
    rows = catalog.connection.execute(
        f"""SELECT DISTINCT framework_run_id FROM catalog_runs
        WHERE framework_run_id IN ({placeholders})""",
        tuple(run_ids),
    ).fetchall()
    return {int(row[0]) for row in rows}, False


def _plan_framework(
    snapshot: _StoreSnapshot,
    *,
    catalog: _StoreSnapshot | None,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
) -> RetentionStorePlan:
    connection = snapshot.connection
    assert connection is not None
    source_reachability = _bounded_parent_reachability(
        connection,
        table="initial_runs",
        node_column="run_id",
        parent_column="source_run_id",
        root_where="source_run_id IS NOT NULL",
    )
    rows = connection.execute(
        """SELECT run.run_id,run.root,run.status,run.started_ns,run.completed_ns,
        (SELECT COUNT(*) FROM initial_runs newer WHERE newer.root=run.root
         AND newer.run_id>run.run_id)<2 AS latest_two,
        run.status='completed' AND NOT EXISTS(
            SELECT 1 FROM initial_runs completed
            WHERE completed.root=run.root AND completed.status='completed'
              AND completed.run_id>run.run_id) AS last_completed,
        EXISTS(SELECT 1 FROM initial_runs child
               WHERE child.source_run_id=run.run_id) AS source_reference,
        EXISTS(SELECT 1 FROM file_actions action
               WHERE action.run_id=run.run_id AND action.status IN
               ('started','applying','recovery_required')) AS uncertain_action,
        EXISTS(SELECT 1 FROM file_actions action
               WHERE action.run_id=run.run_id) AS action_evidence,
        EXISTS(SELECT 1 FROM route_runs route
               WHERE route.run_id=run.run_id AND route.status IN
               ('running','partial','recovery','recovering','recovery_required'))
               AS live_or_incomplete_route,
        EXISTS(SELECT 1 FROM route_phase_runs phase
               WHERE phase.run_id=run.run_id AND phase.status IN
               ('running','partial','recovery','recovering','recovery_required'))
               AS live_or_incomplete_phase,
        EXISTS(SELECT 1 FROM review_candidates review
               WHERE review.last_seen_run_id=run.run_id
                  OR review.resolved_run_id=run.run_id) OR
        EXISTS(SELECT 1 FROM review_decisions decision
               WHERE decision.candidate_generation=run.run_id) OR
        EXISTS(SELECT 1 FROM review_evidence_examples evidence
               WHERE evidence.candidate_generation=run.run_id) AS human_reference,
        1+(SELECT COUNT(*) FROM run_events event WHERE event.run_id=run.run_id)+
          (SELECT COUNT(*) FROM route_runs route WHERE route.run_id=run.run_id)+
          (SELECT COUNT(*) FROM route_phase_runs phase WHERE phase.run_id=run.run_id)+
          (SELECT COUNT(*) FROM run_actions action WHERE action.run_id=run.run_id)+
          (SELECT COUNT(*) FROM route_candidates candidate
           WHERE candidate.run_id=run.run_id) AS estimated_rows,
        length(run.root)+length(run.status)+length(run.run_kind)+
          (SELECT COALESCE(SUM(length(level)+length(phase)+length(message)+
             COALESCE(length(details_json),0)),0) FROM run_events event
           WHERE event.run_id=run.run_id)+
          (SELECT COALESCE(SUM(length(route_name)+length(status)+
             COALESCE(length(summary_json),0)+COALESCE(length(error_message),0)),0)
           FROM route_runs route WHERE route.run_id=run.run_id)+
          (SELECT COALESCE(SUM(length(route_name)+length(phase_name)+length(status)+
             COALESCE(length(summary_json),0)+COALESCE(length(error_message),0)),0)
           FROM route_phase_runs phase WHERE phase.run_id=run.run_id)+
          (SELECT COALESCE(SUM(length(mime)+length(path)+length(volume_id)+
             length(file_id)),0) FROM route_candidates candidate
           WHERE candidate.run_id=run.run_id) AS estimated_bytes
        FROM initial_runs run WHERE run.run_id>? ORDER BY run.run_id LIMIT ?""",
        (after, policy.batch_size + 1),
    ).fetchall()
    selected_rows = rows[: policy.batch_size]
    catalog_references, dependency_unverified = _referenced_framework_runs(
        catalog,
        tuple(int(row["run_id"]) for row in selected_rows),
    )

    def build(row: sqlite3.Row) -> RetentionItem:
        run_id = int(row["run_id"])
        reasons: list[str] = []
        if bool(row["latest_two"]):
            reasons.append("latest_and_previous_run")
        if bool(row["last_completed"]):
            reasons.append("last_completed_run")
        if str(row["status"]) == "running":
            reasons.append("active_framework_run")
        if bool(row["uncertain_action"]):
            reasons.append("uncertain_file_action")
        if bool(row["action_evidence"]):
            reasons.append("file_action_audit_evidence")
        if bool(row["human_reference"]):
            reasons.append("human_evidence_provenance")
        if run_id in catalog_references:
            reasons.append("referenced_by_catalog_run")
        if bool(row["source_reference"]):
            reasons.append("referenced_as_source_run")
        if run_id in source_reachability.ancestors:
            if "referenced_as_source_run" not in reasons:
                reasons.append("referenced_as_source_run")
        if bool(row["live_or_incomplete_route"]):
            reasons.append("live_or_incomplete_route")
        if bool(row["live_or_incomplete_phase"]):
            reasons.append("live_or_incomplete_phase")
        if any(
            reason in reasons
            for reason in (
                "latest_and_previous_run",
                "last_completed_run",
                "active_framework_run",
                "uncertain_file_action",
                "file_action_audit_evidence",
                "human_evidence_provenance",
                "referenced_by_catalog_run",
                "live_or_incomplete_route",
                "live_or_incomplete_phase",
            )
        ):
            disposition: Disposition = "protected"
        elif not source_reachability.complete:
            disposition = "blocked"
            reasons.append("bounded_reachability_incomplete")
        elif bool(row["source_reference"]):
            disposition = "blocked"
        elif _status_is_partial_or_recovery(row["status"]):
            disposition = "blocked"
            reasons.append("partial_or_recovery_state")
        elif dependency_unverified:
            disposition = "blocked"
            reasons.append("catalog_dependency_unverified")
        elif row["completed_ns"] is None:
            disposition = "blocked"
            reasons.append("run_completion_unverified")
        else:
            disposition, age_reasons = _age_disposition(
                policy=policy,
                now_ns=now_ns,
                terminal_ns=int(row["completed_ns"]),
            )
            reasons.extend(age_reasons)
        return RetentionItem(
            run_id,
            "framework_run",
            str(row["root"]),
            str(row["status"]),
            disposition,
            tuple(reasons),
            int(row["estimated_rows"]),
            int(row["estimated_bytes"]),
        )

    items = tuple(build(row) for row in selected_rows)
    truncated = len(rows) > policy.batch_size
    next_after = items[-1].key if truncated and items else None
    database_bytes, wal_bytes, shm_bytes = _file_sizes(snapshot.database)
    return RetentionStorePlan(
        snapshot.store,
        snapshot.database,
        "blocked"
        if (dependency_unverified or not source_reachability.complete)
        else "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _framework_holds(connection),
        after,
        next_after,
        truncated,
        (
            "bounded framework source reachability is incomplete"
            if not source_reachability.complete
            else "catalog retention dependency could not be validated"
            if dependency_unverified
            else None
        ),
    )


def _selected_stores(stores: Sequence[str] | None) -> tuple[RetentionStore, ...]:
    if stores is None:
        return STORE_ORDER
    requested = tuple(stores)
    invalid = sorted(set(requested) - set(STORE_ORDER))
    if invalid:
        raise ValueError(f"unknown retention store: {invalid[0]}")
    if len(set(requested)) != len(requested):
        raise ValueError("retention stores must be unique")
    if not requested:
        raise ValueError("at least one retention store is required")
    return tuple(store for store in STORE_ORDER if store in requested)


def _validated_snapshot(
    store: RetentionStore,
    state_directory: Path,
    stack: ExitStack,
    *,
    cancelled: Callable[[], bool] | None,
    observer: RetentionObserver | None,
    cache: SQLiteSnapshotReuseCache | None = None,
    budget: SQLiteSnapshotBudget | None = None,
    generation: object | None = None,
    inspection_budget: _RetentionInspectionBudget | None = None,
) -> _StoreSnapshot:
    database = state_directory / STORE_DATABASES[store]
    if not database.is_file():
        return _StoreSnapshot(store, database, "absent", None, None, None)
    if inspection_budget is not None:
        try:
            inspection_budget.checkpoint()
        except _RetentionInspectionBudgetExceeded as exc:
            return _StoreSnapshot(
                store,
                database,
                "blocked",
                None,
                None,
                f"eligibility unknown: {exc}",
            )
    try:
        connection = stack.enter_context(_readonly_snapshot(
            database, cancelled, cache=cache, budget=budget, generation=generation,
            inspection_budget=inspection_budget,
        ))
        version = _validate_snapshot(store, connection)
        foreign_key_violation = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        if foreign_key_violation is not None:
            raise RuntimeError(
                f"{store} retention source has an inconsistent foreign-key graph"
            )
        page_size, page_count, freelist_count = (
            int(connection.execute(f"PRAGMA {name}").fetchone()[0])
            for name in ("page_size", "page_count", "freelist_count")
        )
        storage: Mapping[str, int | None] = {
            "page_size": page_size,
            "allocated_pages": page_count,
            "freelist_pages": freelist_count,
            "allocated_page_bytes": page_size * page_count,
            "freelist_page_bytes": page_size * freelist_count,
            "physically_recoverable_bytes": None,
        }
        if inspection_budget is not None:
            inspection_budget.checkpoint()
        if observer is not None:
            observer(store, "snapshot_opened")
        return _StoreSnapshot(store, database, "ready", version, connection, None, storage)
    except RetentionPlanningCancelled:
        raise
    except _RetentionInspectionBudgetExceeded as exc:
        return _StoreSnapshot(
            store,
            database,
            "blocked",
            None,
            None,
            f"eligibility unknown: {exc}",
        )
    except SQLiteSnapshotBudgetExceeded as exc:
        if exc.reason == "cancelled":
            raise RetentionPlanningCancelled("retention planning was cancelled") from exc
        return _StoreSnapshot(store, database, "blocked", None, None, str(exc))
    except sqlite3.OperationalError as exc:
        if inspection_budget is not None and inspection_budget.exhausted:
            return _StoreSnapshot(
                store,
                database,
                "blocked",
                None,
                None,
                "eligibility unknown: retention inspection SQL time budget exhausted",
            )
        if cancelled is not None and cancelled() and "interrupt" in str(exc).lower():
            raise RetentionPlanningCancelled("retention planning was cancelled") from exc
        return _StoreSnapshot(store, database, "blocked", None, None, str(exc)[:1000])
    except Exception as exc:
        return _StoreSnapshot(store, database, "blocked", None, None, str(exc)[:1000])


def _retention_request(
    *,
    policy: RetentionPolicy | None,
    after: Mapping[str, int] | None,
    stores: Sequence[str] | None,
    now_ns: int,
) -> tuple[tuple[RetentionStore, ...], RetentionPolicy, dict[str, int]]:
    selected = _selected_stores(stores)
    selected_policy = RetentionPolicy() if policy is None else policy
    cursors = dict(after or {})
    invalid_cursors = sorted(set(cursors) - set(STORE_ORDER))
    if invalid_cursors:
        raise ValueError(f"unknown retention cursor: {invalid_cursors[0]}")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in cursors.values()
    ):
        raise ValueError("retention cursors must be non-negative integers")
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
        raise ValueError("now_ns must be a non-negative integer")
    return selected, selected_policy, cursors


def _required_retention_stores(
    selected: tuple[RetentionStore, ...],
) -> set[RetentionStore]:
    required = set(selected)
    if "inventory" in selected:
        required.add("framework")
    if "framework" in selected:
        required.add("catalog")
    return required


def _open_retention_snapshots(
    state_directory: Path,
    stack: ExitStack,
    required: set[RetentionStore],
    *,
    cancelled: Callable[[], bool] | None,
    observer: RetentionObserver | None,
    cache: SQLiteSnapshotReuseCache | None = None,
    budget: SQLiteSnapshotBudget | None = None,
    generation: object | None = None,
    inspection_budget: _RetentionInspectionBudget | None = None,
) -> dict[RetentionStore, _StoreSnapshot]:
    return {
        store: _validated_snapshot(
            store,
            state_directory,
            stack,
            cancelled=cancelled,
            observer=observer,
            cache=cache,
            budget=budget,
            generation=generation,
            inspection_budget=inspection_budget,
        )
        for store in STORE_ORDER
        if store in required
    }


def _plan_retention_store(
    store: RetentionStore,
    snapshot: _StoreSnapshot,
    snapshots: Mapping[RetentionStore, _StoreSnapshot],
    *,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
) -> RetentionStorePlan:
    if snapshot.status != "ready":
        return _empty_store_plan(snapshot, after=after)
    if store == "semantic":
        return _plan_semantic(
            snapshot,
            policy=policy,
            after=after,
            now_ns=now_ns,
        )
    if store == "catalog":
        return _plan_catalog(
            snapshot,
            policy=policy,
            after=after,
            now_ns=now_ns,
        )
    if store == "inventory":
        return _plan_inventory(
            snapshot,
            framework=snapshots.get("framework"),
            policy=policy,
            after=after,
            now_ns=now_ns,
        )
    return _plan_framework(
        snapshot,
        catalog=snapshots.get("catalog"),
        policy=policy,
        after=after,
        now_ns=now_ns,
    )


def _plan_retention_store_with_cancellation(
    store: RetentionStore,
    snapshot: _StoreSnapshot,
    snapshots: Mapping[RetentionStore, _StoreSnapshot],
    *,
    policy: RetentionPolicy,
    after: int,
    now_ns: int,
    cancelled: Callable[[], bool] | None,
    inspection_budget: _RetentionInspectionBudget | None,
) -> RetentionStorePlan:
    try:
        if inspection_budget is not None:
            inspection_budget.checkpoint()
        return _plan_retention_store(
            store,
            snapshot,
            snapshots,
            policy=policy,
            after=after,
            now_ns=now_ns,
        )
    except _RetentionInspectionBudgetExceeded as exc:
        return replace(
            _empty_store_plan(snapshot, after=after),
            status="blocked",
            detail=f"eligibility unknown: {exc}",
        )
    except sqlite3.OperationalError as exc:
        if inspection_budget is not None:
            if inspection_budget.cancelled:
                raise RetentionPlanningCancelled("retention planning was cancelled") from exc
            if inspection_budget.exhausted:
                return replace(
                    _empty_store_plan(snapshot, after=after),
                    status="blocked",
                    detail="eligibility unknown: retention inspection SQL time budget exhausted",
                )
        if cancelled is not None and cancelled() and "interrupt" in str(exc).lower():
            raise RetentionPlanningCancelled("retention planning was cancelled") from exc
        raise


def _plan_selected_retention_stores(
    selected: tuple[RetentionStore, ...],
    snapshots: Mapping[RetentionStore, _StoreSnapshot],
    cursors: Mapping[str, int],
    *,
    policy: RetentionPolicy,
    now_ns: int,
    cancelled: Callable[[], bool] | None,
    observer: RetentionObserver | None,
    inspection_budget: _RetentionInspectionBudget | None,
) -> tuple[RetentionStorePlan, ...]:
    plans: list[RetentionStorePlan] = []
    for store in selected:
        _check_cancelled(cancelled)
        plans.append(
            replace(_plan_retention_store_with_cancellation(
                store,
                snapshots[store],
                snapshots,
                policy=policy,
                after=cursors.get(store, 0),
                now_ns=now_ns,
                cancelled=cancelled,
                inspection_budget=inspection_budget,
            ), storage=snapshots[store].storage)
        )
        if observer is not None:
            observer(store, "planned")
    return tuple(plans)


def plan_retention(
    state_directory: Path,
    *,
    policy: RetentionPolicy | None = None,
    after: Mapping[str, int] | None = None,
    stores: Sequence[str] | None = None,
    now_ns: int,
    cancelled: Callable[[], bool] | None = None,
    observer: RetentionObserver | None = None,
) -> RetentionPlan:
    """Return one stable, bounded dry-run page without creating or migrating state."""

    selected, selected_policy, cursors = _retention_request(
        policy=policy,
        after=after,
        stores=stores,
        now_ns=now_ns,
    )
    _check_cancelled(cancelled)
    cache = SQLiteSnapshotReuseCache(
        max_temporary_bytes=selected_policy.snapshot_max_temporary_bytes,
    )
    budget = SQLiteSnapshotBudget(
        max_temporary_bytes=selected_policy.snapshot_max_temporary_bytes,
        prepare_timeout_seconds=selected_policy.snapshot_prepare_timeout_seconds,
        cancellation_check=cancelled,
    )
    inspection_budget = _RetentionInspectionBudget(
        DEFAULT_RETENTION_SQL_TIMEOUT_SECONDS,
        cancelled,
    )
    with ExitStack() as stack:
        stack.enter_context(cache)
        snapshots = _open_retention_snapshots(
            Path(state_directory),
            stack,
            _required_retention_stores(selected),
            cancelled=cancelled,
            observer=observer,
            cache=cache,
            budget=budget,
            generation=now_ns,
            inspection_budget=inspection_budget,
        )
        plans = _plan_selected_retention_stores(
            selected,
            snapshots,
            cursors,
            policy=selected_policy,
            now_ns=now_ns,
            cancelled=cancelled,
            observer=observer,
            inspection_budget=inspection_budget,
        )
        _check_cancelled(cancelled)
    return RetentionPlan(
        now_ns, selected_policy, plans, snapshot_metrics=asdict(cache.snapshot_metrics),
    )


def retention_plan_payload(plan: RetentionPlan) -> dict[str, object]:
    """Return a stable JSON-ready representation of a retention plan."""

    return {
        "accounting": {
            "observed_rows": plan.observed_rows,
            "observed_bytes": plan.observed_bytes,
            "eligible_rows": plan.proposed_rows,
            "eligible_bytes": plan.proposed_bytes,
            "proposed_rows": plan.proposed_rows,
            "proposed_bytes": plan.proposed_bytes,
            "retired_rows": plan.retired_rows,
            "retired_bytes": plan.retired_bytes,
            "physically_recoverable_bytes": plan.physically_recoverable_bytes,
            "physical_reclaimable_bytes": plan.physical_reclaimable_bytes,
            "physical_recovery_status": plan.physical_recovery_status,
        },
        "compaction_supported": plan.compaction_supported,
        "deletion_supported": plan.deletion_supported,
        "dry_run": plan.dry_run,
        "estimate_kind": plan.estimate_kind,
        "now_ns": plan.now_ns,
        "policy": {
            "batch_size": plan.policy.batch_size,
            "keep_published": plan.policy.keep_published,
            "minimum_age_ns": plan.policy.minimum_age_ns,
            "snapshot_max_temporary_bytes": plan.policy.snapshot_max_temporary_bytes,
            "snapshot_prepare_timeout_seconds": plan.policy.snapshot_prepare_timeout_seconds,
            "terminal_log_count": plan.policy.terminal_log_count,
            "terminal_log_bytes": plan.policy.terminal_log_bytes,
            "summary_count": plan.policy.summary_count,
            "summary_bytes": plan.policy.summary_bytes,
            "receipt_count": plan.policy.receipt_count,
            "receipt_bytes": plan.policy.receipt_bytes,
            "rollback_derived_count": plan.policy.rollback_derived_count,
        },
        "snapshot_scope": plan.snapshot_scope,
        "sqlite_read_snapshot_may_touch_shm": (plan.sqlite_read_snapshot_may_touch_shm),
        "source_sidecars_touched": False,
        "snapshot_metrics": plan.snapshot_metrics,
        "snapshot_budget_scope": "aggregate_retained_temporary_bytes_per_operation",
        "stores": [
            {
                "after": store.after,
                "database": str(store.database),
                "database_bytes": store.database_bytes,
                "observed_rows": store.observed_rows,
                "observed_bytes": store.observed_bytes,
                "eligible_rows": store.eligible_rows,
                "eligible_bytes": store.eligible_bytes,
                "proposed_rows": store.proposed_rows,
                "proposed_bytes": store.proposed_bytes,
                "retired_rows": store.retired_rows,
                "retired_bytes": store.retired_bytes,
                "physically_recoverable_bytes": store.physically_recoverable_bytes,
                "physical_reclaimable_bytes": store.physical_reclaimable_bytes,
                "physical_recovery_status": store.physical_recovery_status,
                "compaction_supported": False,
                "storage": store.storage,
                "detail": store.detail,
                "holds": [
                    {
                        "estimated_bytes": hold.estimated_bytes,
                        "name": hold.name,
                        "reason": hold.reason,
                        "rows": hold.rows,
                    }
                    for hold in store.holds
                ],
                "items": [
                    {
                        "disposition": item.disposition,
                        "entity": item.entity,
                        "estimated_bytes": item.estimated_bytes,
                        "estimated_rows": item.estimated_rows,
                        "key": item.key,
                        "reasons": list(item.reasons),
                        "recorded_status": item.recorded_status,
                        "scope": item.scope,
                    }
                    for item in store.items
                ],
                "next_after": store.next_after,
                "protected_bytes": store.protected_bytes,
                "protected_rows": store.protected_rows,
                "schema_version": store.schema_version,
                "shm_bytes": store.shm_bytes,
                "status": store.status,
                "store": store.store,
                "truncated": store.truncated,
                "wal_bytes": store.wal_bytes,
            }
            for store in plan.stores
        ],
    }


__all__ = [
    "STORE_DATABASES",
    "STORE_ORDER",
    "RetentionHold",
    "RetentionItem",
    "RetentionPlan",
    "RetentionPlanningCancelled",
    "RetentionPolicy",
    "RetentionStorePlan",
    "TerminalRetentionCategory",
    "TerminalRetentionDisposition",
    "TerminalRetentionItem",
    "TerminalRetentionPlan",
    "TerminalRetentionPolicy",
    "TerminalRetentionRecord",
    "plan_retention",
    "plan_terminal_retention",
    "retention_plan_payload",
    "terminal_retention_plan_payload",
]
# endregion [02]
