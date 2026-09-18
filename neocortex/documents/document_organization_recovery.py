"""Read-only reconciliation of a bounded organization lifecycle checkpoint."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .document_catalog import document_catalog_database, read_catalog_publication_manifest
from .document_organization_models import OrganizationPlanSummary
from .document_organization_scope import OrganizationInputScope

CHECKPOINT_SCHEMA = "neocortex.organization-plan-checkpoint/v1"
_INTENT_COLUMNS = (
    "plan_id", "source_kind", "file_key", "source_path", "destination_path",
    "organization_root", "volume_id", "file_id", "size", "mtime_ns", "birthtime_ns",
    "classifier_signature", "primary_kind", "confidence", "planned_ns",
    "source_scope_id", "source_scope_json", "resource_binding_json",
    "representation_kind", "operation_kind", "executable", "blockers_json",
    "eligibility_status", "evidence_json",
)


class OrganizationRecoveryRequired(RuntimeError):
    """An outstanding organization obligation cannot safely be repeated."""


def _observe_plan(connection: sqlite3.Connection, scope: OrganizationInputScope, destination: Path) -> dict[str, object]:
    scope.verify(connection)
    for kind, generation_id in scope.publication_heads:
        if read_catalog_publication_manifest(connection, kind).generation_id != generation_id:
            raise OrganizationRecoveryRequired("organization catalog publication changed")
    digest = hashlib.sha256(b"NEOCORTEX_ORGANIZATION_PLAN_V1\0")
    count = cursor = 0
    statuses: dict[str, int] = {}
    for row in connection.execute(
        "SELECT " + ",".join(_INTENT_COLUMNS) + ",status FROM organization_plans "
        "WHERE organization_root=? AND source_scope_id=? AND status<>'superseded' ORDER BY plan_id",
        (str(destination), scope.scope_id),
    ):
        values = tuple(row[name] for name in _INTENT_COLUMNS)
        payload = repr(values).encode("utf-8", "surrogatepass")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
        cursor = int(row["plan_id"])
        status = str(row["status"])
        statuses[status] = statuses.get(status, 0) + 1
    digest.update(count.to_bytes(8, "big"))
    return {"plan_digest": digest.hexdigest(), "plan_count": count, "plan_cursor": cursor, "statuses": statuses}


def _verify_owner_receipt(connection: sqlite3.Connection, scope: OrganizationInputScope, summary: OrganizationPlanSummary) -> None:
    row = connection.execute(
        "SELECT mode,status,summary_json FROM catalog_runs WHERE catalog_run_id=?",
        (summary.catalog_run_id,),
    ).fetchone()
    if (
        row is None or row[0] != "plan" or row[1] != "completed"
        or json.loads(row[2]) != asdict(summary)
        or summary.source_scope_id != scope.scope_id
        or summary.source_root != str(scope.root)
    ):
        raise OrganizationRecoveryRequired("organization plan has no matching completed owner receipt")


def capture_organization_checkpoint(
    catalog_path: Path,
    source_scope: OrganizationInputScope,
    organization_root: Path,
    summary: OrganizationPlanSummary,
) -> dict[str, object]:
    with document_catalog_database(catalog_path, readonly=True) as connection:
        _verify_owner_receipt(connection, source_scope, summary)
        observed = _observe_plan(connection, source_scope, organization_root)
    return {
        "schema": CHECKPOINT_SCHEMA, "source_scope": source_scope.serialized,
        "organization_root": str(organization_root), "summary": asdict(summary),
        **observed,
    }


def inspect_organization_checkpoint(catalog_path: Path, checkpoint: Mapping[str, object], root: Path, organization_root: Path) -> tuple[OrganizationPlanSummary, dict[str, int]]:
    """Reconcile intent and current statuses without authorizing any effect."""

    expected = {"schema", "source_scope", "organization_root", "summary", "plan_digest", "plan_count", "plan_cursor", "statuses"}
    if set(checkpoint) != expected or checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise OrganizationRecoveryRequired("organization checkpoint schema is invalid")
    try:
        scope = OrganizationInputScope.from_json(checkpoint["source_scope"])
        if scope.root != root:
            raise OrganizationRecoveryRequired("organization checkpoint belongs to another corpus")
        destination = Path(str(checkpoint["organization_root"]))
        if destination != organization_root:
            raise OrganizationRecoveryRequired("organization checkpoint destination changed")
        raw_summary = checkpoint["summary"]
        if not isinstance(raw_summary, dict):
            raise ValueError("organization summary is invalid")
        summary = OrganizationPlanSummary(**raw_summary)
        with document_catalog_database(catalog_path, readonly=True) as connection:
            _verify_owner_receipt(connection, scope, summary)
            observed = _observe_plan(connection, scope, destination)
            for field in ("plan_digest", "plan_count", "plan_cursor"):
                if observed[field] != checkpoint[field]:
                    raise OrganizationRecoveryRequired("organization plan intent changed after checkpoint")
        statuses = observed["statuses"]
        assert isinstance(statuses, dict)
        return summary, {str(key): int(value) for key, value in statuses.items()}
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise OrganizationRecoveryRequired(f"organization checkpoint cannot be verified: {exc}") from exc
