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

import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

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
    SQLiteReadMode,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    SQLiteSnapshotReuseCache,
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
    # Retention is a diagnostic reader and may be interleaved with a writer;
    # always detach a bounded snapshot so a later commit cannot invalidate the
    # plan or make its close fence look like a reader failure.
    mode = SQLiteReadMode.SNAPSHOT_TEMP
    selected_budget = budget or SQLiteSnapshotBudget(
        prepare_timeout_seconds=5.0, cancellation_check=cancelled,
    )
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
        return version
    if store == "inventory":
        version = _metadata_version(connection, "dedup inventory")
        if version != INVENTORY_SCHEMA_VERSION:
            raise RuntimeError(
                f"dedup inventory schema is {version}; expected {INVENTORY_SCHEMA_VERSION}"
            )
        validate_inventory_schema(connection)
        return version
    version = _metadata_version(connection, "framework")
    if version != framework_schema.SCHEMA_VERSION:
        raise RuntimeError(
            f"framework schema is {version!r}; expected {framework_schema.SCHEMA_VERSION}"
        )
    framework_schema.validate_framework_schema_v22(connection)
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
               WHERE child.base_generation_id=g.generation_id) AS incoming_base,
        EXISTS(SELECT 1 FROM semantic_evidence evidence
               WHERE evidence.generation_id=g.generation_id) AS evidence_reference,
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
        elif bool(row["incoming_base"]):
            disposition = "blocked"
        elif bool(row["evidence_reference"]):
            disposition = "blocked"
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
        "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _semantic_holds(connection),
        after,
        next_after,
        truncated,
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
               WHERE child.base_generation_id=g.generation_id) AS incoming_base,
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
        elif bool(row["incoming_base"]):
            disposition = "blocked"
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
        "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _catalog_holds(connection),
        after,
        next_after,
        truncated,
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
        "blocked" if dependency_unverified else "ready",
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
            "framework retention dependency could not be validated"
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
            )
        ):
            disposition: Disposition = "protected"
        elif bool(row["source_reference"]):
            disposition = "blocked"
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
        "blocked" if dependency_unverified else "ready",
        snapshot.schema_version,
        database_bytes,
        wal_bytes,
        shm_bytes,
        items,
        _framework_holds(connection),
        after,
        next_after,
        truncated,
        ("catalog retention dependency could not be validated" if dependency_unverified else None),
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
                "storage": store.storage,
                "detail": store.detail,
                "eligible_bytes": store.eligible_bytes,
                "eligible_rows": store.eligible_rows,
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
    "plan_retention",
    "retention_plan_payload",
]
# endregion [02]
