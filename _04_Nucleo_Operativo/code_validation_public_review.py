"""Fresh-process public review identity for canonical Code validation."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .code_review import review_code_state
from .code_review_models import CodeReviewResult
from .semantic_models import canonical_json


PUBLIC_REVIEW_IDENTITY_SCHEMA = "neocortex.code-validation-public-review/v1"


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


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-directory", required=True, type=Path)
    namespace = parser.parse_args(arguments)
    print(canonical_json(public_review_identity(namespace.state_directory)), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())


__all__ = [
    "PUBLIC_REVIEW_IDENTITY_SCHEMA",
    "code_review_identity",
    "main",
    "public_review_identity",
]
