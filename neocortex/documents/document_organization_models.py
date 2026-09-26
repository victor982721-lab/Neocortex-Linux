"""Stable models and catalog queries for document organization."""
# region [00] Contexto del módulo
# Módulo: neocortex/document_organization_models.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .document_catalog import document_catalog_database
from .document_organization_scope import OrganizationInputScope
from neocortex.persistence.framework_connection import connect_existing_framework
# endregion [01]

# region [02] Implementación

DEFAULT_ORGANIZATION_DIRECTORY_NAME = "Consulta_Tecnica_Organizada"
ORGANIZATION_APPLY_BATCH_SIZE = 100
ORGANIZATION_PROGRESS_INTERVAL = 10
ORGANIZATION_FILENAME_LIMIT = 240
ADVISORY_ORGANIZATION_BLOCK_REASONS = frozenset(
    {
        "organization_plan_advisory_only",
        "organization_authorized_backend_unavailable",
    }
)


def is_advisory_organization_block(reason: str) -> bool:
    """Return whether one validated denial is an effect-free advisory abstention."""

    return reason in ADVISORY_ORGANIZATION_BLOCK_REASONS


@dataclass(frozen=True, slots=True)
class OrganizationPlanSummary:
    catalog_run_id: int
    considered: int = 0
    planned: int = 0
    review_required: int = 0
    blocked: int = 0
    already_organized: int = 0
    excluded_out_of_scope: int = 0
    unresolved_scope: int = 0
    excluded_components: int = 0
    source_scope_id: str | None = None
    source_root: str | None = None
    executable: int = 0


@dataclass(frozen=True, slots=True)
class OrganizationApplySummary:
    catalog_run_id: int
    selected: int = 0
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    cache_synced: int = 0
    cache_pending: int = 0
    batches: int = 1
    remaining: int = 0
    advisory_blocked: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.advisory_blocked) is not int
            or self.advisory_blocked < 0
            or self.advisory_blocked > self.blocked
        ):
            raise ValueError("advisory_blocked must be between zero and blocked")

    @property
    def has_unresolved(self) -> bool:
        """Whether outcomes still require recovery, validation, or authority."""

        return organization_apply_has_unresolved(self)


@dataclass(frozen=True, slots=True)
class OrganizationApplyProgress:
    selected: int = 0
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    cache_synced: int = 0
    advisory_blocked: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.advisory_blocked) is not int
            or self.advisory_blocked < 0
            or self.advisory_blocked > self.blocked
        ):
            raise ValueError("advisory_blocked must be between zero and blocked")


@dataclass(frozen=True, slots=True)
class _OrganizationPlanContractView:
    source_scope_id: str | None = None
    source_root: str | None = None
    classification_status: str = "unverified"
    taxonomy_status: str = "unverified"
    confidence_kind: str = "uncalibrated_heuristic"
    suggested_logical_location: str | None = None
    representation_kind: str = "unknown"
    operation_kind: str = "unresolved"
    eligibility_status: str = "unverified"
    executable: bool = False
    blockers: tuple[str, ...] = ("legacy_unscoped",)


@dataclass(frozen=True, slots=True)
class _ApplyRowOutcome:
    status: str
    cache_synced: bool = False
    cache_pending: bool = False
    advisory_blocked: bool = False

    def __post_init__(self) -> None:
        if self.advisory_blocked and self.status != "blocked":
            raise ValueError("advisory_blocked outcomes must have blocked status")


def organization_apply_has_unresolved(summary: OrganizationApplySummary) -> bool:
    """Exclude only validated advisory blocks from the unresolved gate."""

    return bool(
        summary.stale
        or summary.failed
        or summary.cache_pending
        or summary.remaining
        or summary.blocked > summary.advisory_blocked
    )


OrganizationApplyProgressCallback = Callable[[OrganizationApplyProgress], None]


@dataclass(frozen=True, slots=True)
class OrganizationPlanView:
    plan_id: int
    source_kind: str
    source_path: str
    destination_path: str | None
    primary_kind: str
    confidence: float
    status: str
    reason: str
    detail: str | None
    source_scope_id: str | None = None
    source_root: str | None = None
    classification_status: str = "unverified"
    taxonomy_status: str = "unverified"
    confidence_kind: str = "uncalibrated_heuristic"
    suggested_logical_location: str | None = None
    representation_kind: str = "unknown"
    operation_kind: str = "unresolved"
    eligibility_status: str = "unverified"
    executable: bool = False
    blockers: tuple[str, ...] = ("legacy_unscoped",)


def default_organization_root(
    framework_database: Path,
    *,
    analysis_root: Path | None = None,
) -> Path:
    """Place organized content under the explicit or latest analyzed root."""

    root = analysis_root
    if root is None:
        if not framework_database.is_file():
            raise FileNotFoundError(
                "framework state has no completed analysis root; provide --root or "
                "--organization-root"
            )
        connection = connect_existing_framework(
            framework_database, readonly=True, timeout_seconds=10
        )
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(initial_runs)")}
            if not {"root", "status", "run_id"}.issubset(columns):
                raise ValueError("framework state lacks a compatible analysis history")
            kind_predicate = "AND run_kind='initial'" if "run_kind" in columns else ""
            row = connection.execute(
                f"""SELECT root FROM initial_runs
                WHERE status='completed' {kind_predicate}
                ORDER BY run_id DESC LIMIT 1"""
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError(
                "framework state has no completed analysis root; provide --root or "
                "--organization-root"
            )
        root = Path(str(row[0]))
    normalized = Path(os.path.abspath(root.expanduser()))
    if not normalized.is_dir():
        raise ValueError(f"analyzed root is not an existing directory: {normalized}")
    return normalized / DEFAULT_ORGANIZATION_DIRECTORY_NAME


def list_organization_plans(
    catalog_path: Path,
    *,
    limit: int,
    status: str | None = None,
) -> tuple[OrganizationPlanView, ...]:
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with document_catalog_database(catalog_path, readonly=True) as connection:
        predicate = "" if status is None else "WHERE status=?"
        parameters: tuple[object, ...] = () if status is None else (status,)
        rows = connection.execute(
            f"""SELECT * FROM organization_plans {predicate}
            ORDER BY plan_id DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        views: list[OrganizationPlanView] = []
        for row in rows:
            contract = _organization_plan_contract_view(row)
            views.append(
                OrganizationPlanView(
                    plan_id=int(row["plan_id"]),
                    source_kind=str(row["source_kind"]),
                    source_path=str(row["source_path"]),
                    destination_path=(
                        None if row["destination_path"] is None else str(row["destination_path"])
                    ),
                    primary_kind=str(row["primary_kind"]),
                    confidence=float(row["confidence"]),
                    status=str(row["status"]),
                    reason=str(row["reason"]),
                    detail=None if row["detail"] is None else str(row["detail"]),
                    source_scope_id=contract.source_scope_id,
                    source_root=contract.source_root,
                    classification_status=contract.classification_status,
                    taxonomy_status=contract.taxonomy_status,
                    confidence_kind=contract.confidence_kind,
                    suggested_logical_location=contract.suggested_logical_location,
                    representation_kind=contract.representation_kind,
                    operation_kind=contract.operation_kind,
                    eligibility_status=contract.eligibility_status,
                    executable=contract.executable,
                    blockers=contract.blockers,
                )
            )
        return tuple(views)


def _organization_plan_contract_view(row: sqlite3.Row) -> _OrganizationPlanContractView:
    """Old proposals stay visible, but missing scope never implies executability."""

    columns = set(row.keys())
    base = _OrganizationPlanContractView()
    try:
        evidence = json.loads(row["evidence_json"])
        if isinstance(evidence, dict):
            suggested_logical_location = evidence.get("suggested_logical_location")
            base = _OrganizationPlanContractView(
                classification_status=str(evidence.get("classification_status", "unverified")),
                taxonomy_status=str(evidence.get("taxonomy_status", "unverified")),
                confidence_kind=str(
                    evidence.get("classification_score_kind", "uncalibrated_heuristic")
                ),
                suggested_logical_location=(
                    suggested_logical_location
                    if isinstance(suggested_logical_location, str)
                    else None
                ),
            )
    except (ValueError, TypeError, KeyError):
        pass
    if not {"source_scope_json", "source_scope_id", "blockers_json"}.issubset(columns):
        return base
    raw_scope = row["source_scope_json"]
    if raw_scope is None:
        return base
    try:
        scope = OrganizationInputScope.from_json(raw_scope)
        if scope.scope_id != row["source_scope_id"]:
            raise ValueError("organization_scope_digest_mismatch")
        blockers = json.loads(row["blockers_json"])
        if not isinstance(blockers, list) or not all(isinstance(value, str) for value in blockers):
            raise ValueError("organization_blockers_invalid")
        return _OrganizationPlanContractView(
            source_scope_id=scope.scope_id,
            source_root=str(scope.root),
            classification_status=base.classification_status,
            taxonomy_status=base.taxonomy_status,
            confidence_kind=base.confidence_kind,
            suggested_logical_location=base.suggested_logical_location,
            representation_kind=str(row["representation_kind"] or "unknown"),
            operation_kind=str(row["operation_kind"] or "unresolved"),
            eligibility_status=str(row["eligibility_status"]),
            # This facade has no grant-consuming backend.  A persisted flag
            # alone cannot attest execution authority or backend availability.
            executable=False,
            blockers=tuple(
                sorted(set(blockers) | {"backend_unavailable", "authorization_required"})
            ),
        )
    except (ValueError, TypeError, KeyError):
        return _OrganizationPlanContractView(
            classification_status=base.classification_status,
            taxonomy_status=base.taxonomy_status,
            confidence_kind=base.confidence_kind,
            suggested_logical_location=base.suggested_logical_location,
            blockers=("organization_contract_invalid",),
        )


def _begin_organization_run(
    connection: sqlite3.Connection,
    mode: str,
    root: Path,
    *,
    source_scope: OrganizationInputScope | None = None,
) -> int:
    connection.execute(
        """UPDATE catalog_runs SET status='interrupted',completed_ns=?,
        error_type='InterruptedCatalogRun',
        error_message='exclusive framework lock was reacquired before completion'
        WHERE status='running' AND mode<>'classify'""",
        (time.time_ns(),),
    )
    cursor = connection.execute(
        """INSERT INTO catalog_runs(
        source_kind,mode,status,started_ns,summary_json)
        VALUES('all',?,'running',?,?)""",
        (
            mode,
            time.time_ns(),
            json.dumps(
                {
                    "organization_root": str(root),
                    "source_scope": None if source_scope is None else source_scope.to_dict(),
                }
            ),
        ),
    )
    connection.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("organization run insert did not return an identifier")
    return int(cursor.lastrowid)


def _complete_organization_run(
    connection: sqlite3.Connection,
    run_id: int,
    summary: OrganizationPlanSummary | OrganizationApplySummary,
) -> None:
    connection.execute(
        """UPDATE catalog_runs SET status='completed',completed_ns=?,summary_json=?
        WHERE catalog_run_id=?""",
        (
            time.time_ns(),
            json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")),
            run_id,
        ),
    )
    connection.commit()


def _fail_organization_run(
    connection: sqlite3.Connection,
    run_id: int,
    error: BaseException,
) -> None:
    connection.execute(
        """UPDATE catalog_runs SET status='failed',completed_ns=?,
        error_type=?,error_message=? WHERE catalog_run_id=?""",
        (time.time_ns(), type(error).__name__, str(error), run_id),
    )
    connection.commit()


# endregion [02]
