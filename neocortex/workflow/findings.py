"""Small, non-authorizing finding contracts used by format routes.

Format routes need to preserve bounded diagnostics for a source snapshot.  They
do not need the durable human-review workflow that used to own these values.
This module is intentionally limited to the value object, its recommendation
type, and the canonical JSON/identifier validation shared by the route-state
writer.

A finding is advisory data only: constructing one never authorizes a mutation,
creates a task, or grants an effect.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from neocortex.deduplication import FileSnapshot


Recommendation = Literal[
    "retry",
    "keep_protected",
    "manual_review",
    "deletion_candidate",
]
# Keep the descriptive name used by the format-route diagnostics while moving
# its owner out of the removed workflow/review package.
ReviewRecommendation = Recommendation

FINDING_RECOMMENDATIONS = frozenset(
    {"retry", "keep_protected", "manual_review", "deletion_candidate"}
)
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_IDENTIFIER_CHARS = 256
MAX_RECONCILIATION_REASONS = 256


def _validated_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"finding {field_name} must be non-empty and trimmed")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(
            f"finding {field_name} exceeds {MAX_IDENTIFIER_CHARS} characters"
        )
    return value


def _serialized_mapping(
    value: Mapping[str, object],
    *,
    field_name: str,
    maximum_bytes: int,
) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"finding {field_name} must be a mapping")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"finding {field_name} is not canonical JSON") from exc
    if len(payload.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"finding {field_name} exceeds the {maximum_bytes}-byte limit")
    return payload


def serialized_evidence(evidence: Mapping[str, object]) -> str:
    """Serialize bounded evidence without NaN or silent truncation."""

    return _serialized_mapping(
        evidence,
        field_name="evidence",
        maximum_bytes=MAX_EVIDENCE_BYTES,
    )


def validated_reason_codes(reason_codes: object) -> tuple[str, ...]:
    """Return a stable, bounded reason set suitable for a SQL predicate."""

    if isinstance(reason_codes, (str, bytes)):
        raise TypeError("finding reason codes must be an iterable of strings")
    try:
        values = tuple(reason_codes)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("finding reason codes must be an iterable of strings") from exc
    if len(values) > MAX_RECONCILIATION_REASONS:
        raise ValueError(
            "finding reconciliation exceeds "
            f"{MAX_RECONCILIATION_REASONS} reason codes"
        )
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("finding reason codes must contain only strings")
        normalized.add(_validated_identifier(value, field_name="reason_code"))
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """One bounded route finding; it never authorizes an action."""

    route_name: str
    snapshot: FileSnapshot
    reason_code: str
    source_status: str
    recommendation: ReviewRecommendation
    retryable: bool
    confidence: float
    evidence: Mapping[str, object]
    detector_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, FileSnapshot):
            raise TypeError("finding snapshot must be a FileSnapshot")
        for field_name, value in (
            ("route_name", self.route_name),
            ("reason_code", self.reason_code),
            ("source_status", self.source_status),
            ("detector_version", self.detector_version),
        ):
            _validated_identifier(value, field_name=field_name)
        if self.recommendation not in FINDING_RECOMMENDATIONS:
            raise ValueError(f"invalid finding recommendation: {self.recommendation}")
        if not isinstance(self.retryable, bool):
            raise TypeError("finding retryable must be a boolean")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("finding confidence must be finite and between zero and one")
        serialized_evidence(self.evidence)


__all__ = [
    "FINDING_RECOMMENDATIONS",
    "MAX_EVIDENCE_BYTES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_RECONCILIATION_REASONS",
    "Recommendation",
    "ReviewCandidate",
    "ReviewRecommendation",
    "serialized_evidence",
    "validated_reason_codes",
]
