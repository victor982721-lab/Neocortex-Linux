"""Raster-document detection is an observation, not an individual review task."""

from __future__ import annotations

import json
from typing import Mapping


def project_raster_document_candidate(row: Mapping[str, object]) -> dict[str, object] | None:
    """Expose the same semantics for old cached and freshly extracted evidence."""

    if not row.get("document_candidate"):
        return None
    try:
        payload = json.loads(str(row.get("evidence_json") or "{}"))
    except (TypeError, ValueError):
        payload = {}
    document = payload.get("document_candidate", {}) if isinstance(payload, dict) else {}
    details = document if isinstance(document, dict) else {}
    return {
        "owner": "image",
        "record_id": str(row.get("file_key") or ""),
        "reason_code": "image_raster_document_candidate",
        "evidence_kind": "inference",
        "heuristic_score": row.get("document_candidate_score"),
        "score_semantics": "uncalibrated_heuristic",
        "severity": "informational",
        "uncertainty": row.get("document_candidate_uncertainty") or "unknown",
        "suggested_action": "extract_or_associate",
        "human_review_required": False,
        "logical_document_status": "unverified",
        "organization_allowed": False,
        "deletion_allowed": False,
        "signals": details.get("evidence", ()),
        "kinds": details.get("kinds", ()),
        "provenance": details.get("provenance", ()),
    }
