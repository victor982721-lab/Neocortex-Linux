"""Bounded review-evidence commands for the canonical CLI."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
import json
import sqlite3
from dataclasses import asdict
from typing import Any

__all__ = [
    "run_review_evidence_list",
    "run_review_evidence_metrics",
    "run_review_evidence_sync",
]


# region [01] Stable output helpers


def _print_json(kind: str, value: Any) -> None:
    payload = {"kind": kind, **asdict(value)}
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def _common_filters(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "route_name": args.review_evidence_route,
        "reason_code": args.review_evidence_reason,
        "target_recommendation": args.review_evidence_recommendation,
        "detector_version": args.review_evidence_detector,
        "actor": args.review_evidence_actor,
    }


# endregion [01]


# region [02] Bounded materialization and queries


def run_review_evidence_sync(args: argparse.Namespace) -> int:
    """Materialize at most one explicitly bounded decision batch."""

    from neocortex.safety.internal_paths import canonical_internal_paths_policy
    from neocortex.integrations.inventory.inventory_boundary import (
        state_sqlite_mutation_paths,
        validate_authorized_state_path,
    )
    from neocortex.safety.protected_content import canonical_protected_content_policy
    from neocortex.workflow.review.review_evidence import materialize_review_evidence

    database = args.state_directory / "framework.sqlite3"
    if not database.is_file():
        print(f"ERROR review-evidence-sync state database does not exist: {database}")
        return 2
    try:
        validate_authorized_state_path(
            args.state_directory,
            internal_paths_policy=canonical_internal_paths_policy(),
            protected_content_policy=canonical_protected_content_policy(),
            mutation_paths=state_sqlite_mutation_paths(database),
        )
        result = materialize_review_evidence(
            database,
            batch_size=args.review_evidence_batch_size,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"ERROR review-evidence-sync {exc}")
        return 2
    if args.review_json:
        _print_json("review-evidence-sync", result)
    else:
        print(
            f"REVIEW_EVIDENCE_SYNC scanned={result.scanned_decisions} "
            f"materialized={result.materialized_examples} "
            f"last_decision_id={result.last_decision_id or '-'} "
            f"has_more={int(result.has_more)}"
        )
    return 0


def run_review_evidence_metrics(args: argparse.Namespace) -> int:
    """Print descriptive human-review outcome metrics without claiming calibration."""

    from neocortex.workflow.review.review_evidence import review_evidence_metrics

    database = args.state_directory / "framework.sqlite3"
    try:
        metrics = review_evidence_metrics(database, **_common_filters(args))
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"ERROR review-evidence-metrics {exc}")
        return 2
    if args.review_json:
        _print_json("review-evidence-metrics", metrics)
    else:
        reason = json.dumps(metrics.calibration_reason, ensure_ascii=False)
        print(
            f"REVIEW_EVIDENCE_METRICS decisions={metrics.total_decisions} "
            f"materialized={metrics.materialized_examples} "
            f"confirmed={metrics.confirmed_examples} "
            f"dismissed={metrics.dismissed_examples} "
            f"deferred={metrics.deferred_examples} "
            f"decisive={metrics.decisive_examples} "
            f"complete_evidence={metrics.complete_candidate_evidence} "
            f"materialization_coverage={_rate(metrics.materialization_coverage)} "
            f"decisive_label_coverage={_rate(metrics.decisive_label_coverage)} "
            f"candidate_evidence_coverage={_rate(metrics.candidate_evidence_coverage)} "
            f"acceptance_rate={_rate(metrics.acceptance_rate)} "
            f"rejection_rate={_rate(metrics.rejection_rate)} "
            f"abstention_rate={_rate(metrics.abstention_rate)} "
            f"evaluation_status={metrics.evaluation_status} "
            f"calibration_status={metrics.calibration_status} "
            f"calibration_reason={reason}"
        )
    return 0


def run_review_evidence_list(args: argparse.Namespace) -> int:
    """List a bounded filtered evidence view without mutating state."""

    from neocortex.workflow.review.review_evidence import list_review_evidence

    completeness = {
        None: None,
        "complete": True,
        "incomplete": False,
    }[args.review_evidence_completeness]
    database = args.state_directory / "framework.sqlite3"
    try:
        examples = list_review_evidence(
            database,
            limit=args.review_evidence_list,
            decision_status=args.review_evidence_status,
            require_complete_candidate_evidence=completeness,
            **_common_filters(args),
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"ERROR review-evidence-list {exc}")
        return 2
    for example in examples:
        if args.review_json:
            _print_json("review-evidence", example)
            continue
        recommendation = example.target_recommendation or "-"
        confidence = _rate(example.confidence)
        actor = json.dumps(example.actor, ensure_ascii=False)
        path = json.dumps(example.path, ensure_ascii=False)
        print(
            f"REVIEW_EVIDENCE id={example.decision_id} "
            f"outcome={example.outcome} status={example.decision_status} "
            f"route={example.route_name} generation={example.candidate_generation} "
            f"volume={example.volume_id:x} file={example.file_id:x} "
            f"reason={example.reason_code} recommendation={recommendation} "
            f"confidence={confidence} "
            f"evidence_complete={int(example.candidate_evidence_complete)} "
            f"actor={actor} path={path} reference={example.feedback_reference}"
        )
    return 0


# endregion [02]


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_review_evidence')
