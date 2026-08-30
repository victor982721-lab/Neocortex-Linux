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

from .document_catalog import connect_document_catalog
from neocortex.persistence.framework_connection import connect_existing_framework
# endregion [01]

# region [02] Implementación

DEFAULT_ORGANIZATION_DIRECTORY_NAME = "Consulta_Tecnica_Organizada"
ORGANIZATION_APPLY_BATCH_SIZE = 100
ORGANIZATION_PROGRESS_INTERVAL = 10
ORGANIZATION_FILENAME_LIMIT = 240


@dataclass(frozen=True, slots=True)
class OrganizationPlanSummary:
    catalog_run_id: int
    considered: int = 0
    planned: int = 0
    review_required: int = 0
    blocked: int = 0
    already_organized: int = 0


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


@dataclass(frozen=True, slots=True)
class OrganizationApplyProgress:
    selected: int = 0
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    cache_synced: int = 0


@dataclass(frozen=True, slots=True)
class _ApplyRowOutcome:
    status: str
    cache_synced: bool = False
    cache_pending: bool = False


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
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(initial_runs)")
            }
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
    connection = connect_document_catalog(catalog_path, readonly=True)
    try:
        predicate = "" if status is None else "WHERE status=?"
        parameters: tuple[object, ...] = () if status is None else (status,)
        rows = connection.execute(
            f"""SELECT plan_id,source_kind,source_path,destination_path,
            primary_kind,confidence,status,reason,detail
            FROM organization_plans {predicate}
            ORDER BY plan_id DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        return tuple(
            OrganizationPlanView(
                plan_id=int(row["plan_id"]),
                source_kind=str(row["source_kind"]),
                source_path=str(row["source_path"]),
                destination_path=(
                    None
                    if row["destination_path"] is None
                    else str(row["destination_path"])
                ),
                primary_kind=str(row["primary_kind"]),
                confidence=float(row["confidence"]),
                status=str(row["status"]),
                reason=str(row["reason"]),
                detail=None if row["detail"] is None else str(row["detail"]),
            )
            for row in rows
        )
    finally:
        connection.close()


def _begin_organization_run(
    connection: sqlite3.Connection,
    mode: str,
    root: Path,
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
        (mode, time.time_ns(), json.dumps({"organization_root": str(root)})),
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
