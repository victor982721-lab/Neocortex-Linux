"""Fresh-process public review identity for canonical Code validation."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .code_review import (
    _current_versions_match_latest_run,
    _latest_run,
    _review_freshness_fence,
    review_code_state,
)
from .code_review_epistemics import CodeReviewEvidenceResolutionError
from .code_review_models import CodeReviewResult
from .code_schema import validate_code_schema
from .external_evidence_models import AnalysisProfile, ExternalProviderStatus
from .external_evidence_store import read_external_validation_provider_statuses
from .self_analysis_status import (
    CodeRunStatusEvidence,
    QuiescentSQLiteUnavailable,
    quiescent_sqlite_database,
    require_sqlite_sidecars_absent,
)
from .semantic_models import canonical_json


PUBLIC_REVIEW_IDENTITY_SCHEMA = "neocortex.code-validation-public-review/v1"
VALIDATION_STABLE_PUBLIC_REVIEW_SCHEMA = "neocortex.code-validation-public-review-stable/v1"
_PUBLIC_REVIEW_IDENTITY_FIELDS = frozenset(
    {
        "schema",
        "status",
        "reason",
        "snapshot",
        "digest",
        "external_profile",
        "providers",
        "experiment_receipt_ids",
        "question_evaluations",
        "materialization_limit",
        "mutation_authority",
    }
)
_VALIDATION_STABLE_FIELDS = (
    "status",
    "reason",
    "snapshot",
    "external_profile",
    "providers",
    "materialization_limit",
    "mutation_authority",
)


def code_review_identity(result: CodeReviewResult) -> dict[str, object]:
    """Project one typed review onto the stable public validation boundary."""

    snapshot = result.snapshot
    suite = result.external_evidence_suite
    return {
        "schema": PUBLIC_REVIEW_IDENTITY_SCHEMA,
        "status": result.status,
        "reason": result.reason,
        "snapshot": (
            None
            if snapshot is None
            else {
                "analysis_run_id": snapshot.analysis_run_id,
                "processing_signature": snapshot.processing_signature,
                "freshness": snapshot.freshness,
            }
        ),
        "digest": None if result.digest is None else asdict(result.digest),
        "external_profile": None if suite is None else suite.profile,
        "providers": (
            []
            if suite is None
            else [
                item.as_payload()
                for item in sorted(suite.providers, key=lambda value: value.provider_id)
            ]
        ),
        "experiment_receipt_ids": sorted(
            item.receipt.receipt_id for item in result.experiment_receipts
        ),
        "question_evaluations": len(result.question_evaluations),
        "materialization_limit": result.materialization_limit,
        "mutation_authority": False,
    }


def public_review_identity(state_directory: Path) -> dict[str, object]:
    """Read and project one review in a fresh process."""

    return code_review_identity(review_code_state(Path(state_directory), limit=50))


def validation_stable_public_review_identity(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Project only Code-validation authority, excluding operational views.

    Review digest, question materialization, and experiment-receipt visibility
    may legitimately change after an installed ``--all`` advances owner state.
    They remain present in the full gate evidence, but cannot invalidate the
    already-proven source SHA or prevent the required replay run.
    """

    if set(payload) != _PUBLIC_REVIEW_IDENTITY_FIELDS:
        raise ValueError("public review identity fields are incompatible")
    if payload.get("schema") != PUBLIC_REVIEW_IDENTITY_SCHEMA:
        raise ValueError("public review identity schema is incompatible")
    return {
        "schema": VALIDATION_STABLE_PUBLIC_REVIEW_SCHEMA,
        "public_review_schema": PUBLIC_REVIEW_IDENTITY_SCHEMA,
        **{field: payload[field] for field in _VALIDATION_STABLE_FIELDS},
    }


def _abstained_validation_identity(reason: str) -> dict[str, object]:
    return validation_stable_public_review_identity(
        {
            "schema": PUBLIC_REVIEW_IDENTITY_SCHEMA,
            "status": "abstained",
            "reason": reason,
            "snapshot": None,
            "digest": None,
            "external_profile": None,
            "providers": [],
            "experiment_receipt_ids": [],
            "question_evaluations": 0,
            "materialization_limit": 50,
            "mutation_authority": False,
        }
    )


def _validation_provider_statuses_for_run(
    database: Path,
    latest_run: CodeRunStatusEvidence,
) -> tuple[AnalysisProfile, tuple[ExternalProviderStatus, ...]]:
    with quiescent_sqlite_database(database, timeout_seconds=60) as connection:
        validate_code_schema(connection)
        latest = _latest_run(connection)
        if latest != latest_run:
            raise CodeReviewEvidenceResolutionError(
                "latest_code_run_changed_during_validation_identity"
            )
        if not _current_versions_match_latest_run(connection, latest_run):
            raise CodeReviewEvidenceResolutionError(
                "current_code_projection_not_owned_by_latest_completed_run"
            )
        return read_external_validation_provider_statuses(
            connection,
            latest_run.analysis_run_id,
            enforce_current_runtime=True,
        )


def _ready_validation_identity(
    *,
    snapshot: Mapping[str, object],
    profile: AnalysisProfile,
    providers: Sequence[ExternalProviderStatus],
) -> dict[str, object]:
    return validation_stable_public_review_identity(
        {
            "schema": PUBLIC_REVIEW_IDENTITY_SCHEMA,
            "status": "ready",
            "reason": None,
            "snapshot": dict(snapshot),
            "digest": None,
            "external_profile": profile,
            "providers": [item.as_payload() for item in providers],
            "experiment_receipt_ids": [],
            "question_evaluations": 0,
            "materialization_limit": 50,
            "mutation_authority": False,
        }
    )


def validation_stable_review_identity(state_directory: Path) -> dict[str, object]:
    """Read only receipt-bound review evidence with bounded resident memory."""

    state_directory = Path(state_directory)
    database = state_directory / "code.sqlite3"
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        return _abstained_validation_identity("code_state_missing")
    try:
        fence, reason = _review_freshness_fence(state_directory, database)
        if reason is not None:
            return _abstained_validation_identity(reason)
        if fence is None:
            raise CodeReviewEvidenceResolutionError("code review freshness fence is missing")
        profile, providers = _validation_provider_statuses_for_run(
            database,
            fence.latest_run,
        )
        post_fence, post_reason = _review_freshness_fence(state_directory, database)
        if post_reason is not None:
            return _abstained_validation_identity(post_reason)
        if post_fence != fence:
            return _abstained_validation_identity(
                "self_analysis_freshness_changed_during_validation_identity"
            )
    except (
        CodeReviewEvidenceResolutionError,
        OSError,
        QuiescentSQLiteUnavailable,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.DatabaseError,
    ):
        return _abstained_validation_identity("code_review_evidence_unresolvable")
    return _ready_validation_identity(
        snapshot={
            "analysis_run_id": fence.latest_run.analysis_run_id,
            "processing_signature": fence.latest_run.processing_signature,
            "freshness": fence.freshness,
        },
        profile=profile,
        providers=providers,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--validation-stable", action="store_true")
    namespace = parser.parse_args(arguments)
    identity = (
        validation_stable_review_identity(namespace.state_directory)
        if namespace.validation_stable
        else public_review_identity(namespace.state_directory)
    )
    print(canonical_json(identity), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())


__all__ = [
    "PUBLIC_REVIEW_IDENTITY_SCHEMA",
    "VALIDATION_STABLE_PUBLIC_REVIEW_SCHEMA",
    "code_review_identity",
    "main",
    "public_review_identity",
    "validation_stable_public_review_identity",
    "validation_stable_review_identity",
]
