"""Current Fast Curation gate for physical document organization.

Organization consumes a Catalog-owned, calibrated decision.  This module is
deliberately a pure validation layer: it never classifies content, loads a
model, derives a label from a path, or writes a Catalog row.
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .curation_state import CurationDecisionRecord, read_current_curation_decision
from .document_kind_destinations import (
    COMPACT_KIND_DIRECTORIES, FAST_KIND_ROUTING_ALIASES, REVIEW_ONLY_KINDS,
)
from .document_resource_binding import parse_resource_binding
from neocortex.semantic.fast_curation_policy_bundle import (
    FastCurationPolicyBundle,
    default_calibrated_policy,
    load_calibrated_policy,
    validate_record_versions,
)


FastCurationPolicySource = FastCurationPolicyBundle | Mapping[str, object] | str | Path


@dataclass(frozen=True, slots=True)
class FastOrganizationCurationGate:
    """Validation result used by both planning and the apply frontier."""

    eligible: bool
    reason: str
    document_kind: str | None = None
    decision: CurationDecisionRecord | None = None
    bundle: FastCurationPolicyBundle | None = None

    @property
    def accepted(self) -> bool:
        return self.eligible


def resolve_fast_curation_policy_bundle(
    source: FastCurationPolicySource | None,
) -> FastCurationPolicyBundle | None:
    """Resolve an explicit or packaged measured bundle without loading a model."""

    if source is None:
        return default_calibrated_policy()
    if isinstance(source, FastCurationPolicyBundle):
        return source
    return load_calibrated_policy(source)


def validate_current_fast_curation_decision(
    connection: Any,
    *,
    source_kind: str,
    file_key: str,
    policy_bundle: FastCurationPolicySource | None,
    expected_binding: Mapping[str, object] | None = None,
    expected_path: str | None = None,
    expected_identity: tuple[object, object, object, object, object] | None = None,
) -> FastOrganizationCurationGate:
    """Validate the current decision and its exact Catalog resource binding.

    ``expected_binding``/``expected_path`` are the plan snapshot fences.  The
    optional identity tuple is ``(volume_id, file_id, size, mtime_ns,
    birthtime_ns)`` and is used at apply time to catch replacement before the
    filesystem effect.
    """

    try:
        bundle = resolve_fast_curation_policy_bundle(policy_bundle)
    except (OSError, TypeError, ValueError) as exc:
        return FastOrganizationCurationGate(
            False, f"fast_curation_policy_invalid:{type(exc).__name__}"
        )
    if bundle is None:
        return FastOrganizationCurationGate(False, "fast_curation_policy_missing")

    try:
        row = connection.execute(
            "SELECT * FROM documents WHERE source_kind=? AND file_key=?",
            (source_kind, file_key),
        ).fetchone()
    except Exception as exc:  # SQLite owner errors are a review outcome here.
        return FastOrganizationCurationGate(
            False, f"catalog_current_document_unreadable:{type(exc).__name__}"
        )
    if row is None:
        return FastOrganizationCurationGate(False, "catalog_current_document_missing")
    if _row_value(row, "active") != 1:
        return FastOrganizationCurationGate(False, "catalog_current_document_inactive")

    current_path = _row_text(row, "path")
    raw_binding = _row_value(row, "resource_binding_json")
    try:
        binding = parse_resource_binding(raw_binding)
    except (TypeError, ValueError, KeyError):
        return FastOrganizationCurationGate(False, "catalog_resource_binding_invalid")
    if binding.get("source_kind") != source_kind or binding.get("file_key") != file_key:
        return FastOrganizationCurationGate(False, "catalog_resource_binding_identity_mismatch")
    if binding.get("physical_anchor_path") != current_path:
        return FastOrganizationCurationGate(False, "catalog_resource_binding_path_mismatch")
    if expected_binding is not None and dict(expected_binding) != binding:
        return FastOrganizationCurationGate(False, "organization_plan_resource_binding_stale")
    if expected_path is not None and _absolute(expected_path) != _absolute(current_path):
        return FastOrganizationCurationGate(False, "organization_plan_source_path_stale")
    if expected_identity is not None:
        try:
            expected_snapshot = _normalize_identity(expected_identity)
            current_snapshot = _row_identity(row)
        except (TypeError, ValueError, OverflowError):
            return FastOrganizationCurationGate(False, "organization_source_identity_stale")
        if expected_snapshot != current_snapshot:
            return FastOrganizationCurationGate(False, "organization_source_identity_stale")
        if not _current_identity_matches(row, Path(current_path)):
            return FastOrganizationCurationGate(False, "organization_source_identity_stale")

    try:
        decision = read_current_curation_decision(
            connection,
            source_kind=source_kind,
            file_key=file_key,
        )
    except (TypeError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
        return FastOrganizationCurationGate(
            False, f"fast_curation_decision_invalid:{type(exc).__name__}"
        )
    if decision is None:
        return FastOrganizationCurationGate(False, "fast_curation_decision_missing")
    if decision.source_kind != source_kind or decision.file_key != file_key:
        return FastOrganizationCurationGate(False, "fast_curation_decision_identity_mismatch")
    if dict(decision.source_binding) != binding:
        return FastOrganizationCurationGate(False, "fast_curation_decision_binding_stale")
    if decision.role != "document":
        return FastOrganizationCurationGate(False, "fast_curation_decision_role_uncontrolled")
    if decision.decision != "CLASSIFIED":
        return FastOrganizationCurationGate(
            False, f"fast_curation_decision_{decision.decision.lower()}"
        )

    source_input_signature = _source_input_signature(row)
    if source_input_signature is None:
        return FastOrganizationCurationGate(False, "catalog_source_input_signature_missing")
    version_check = validate_record_versions(
        decision,
        bundle,
        source_input_signature=source_input_signature,
    )
    if not version_check.valid:
        return FastOrganizationCurationGate(False, f"fast_curation_{version_check.reason}")
    if not decision.evidence or not decision.context_provenance:
        return FastOrganizationCurationGate(False, "fast_curation_decision_evidence_missing")
    confidence_kind = decision.evidence.get("confidence_kind")
    if confidence_kind == "not_calibrated" or decision.evidence.get("calibrated") is False:
        return FastOrganizationCurationGate(False, "fast_curation_decision_uncalibrated")
    if not _finite_decision_values(decision):
        return FastOrganizationCurationGate(False, "fast_curation_decision_scores_invalid")
    if not decision.top_k or decision.top_k[0].label != decision.top1_label:
        return FastOrganizationCurationGate(False, "fast_curation_decision_top_k_invalid")
    document_kind = _decision_document_kind(decision)
    if document_kind is None:
        return FastOrganizationCurationGate(False, "fast_curation_document_kind_uncontrolled")
    return FastOrganizationCurationGate(
        True,
        "fast_curation_classified_current",
        document_kind=document_kind,
        decision=decision,
        bundle=bundle,
    )


def controlled_document_kind(label: object) -> str | None:
    """Map only explicit Fast Curation document-kind labels to known kinds."""

    if not isinstance(label, str) or not label.strip():
        return None
    normalized = _controlled_text(label)
    if normalized.startswith("document_kind_"):
        normalized = normalized[len("document_kind_") :]
    if normalized.startswith("document_kind."):
        normalized = normalized[len("document_kind.") :]
    if normalized.startswith("document_kind") and normalized[len("document_kind") :].startswith("_"):
        normalized = normalized[len("document_kind_") :]
    normalized = FAST_KIND_ROUTING_ALIASES.get(normalized, normalized)
    allowed = _controlled_kind_names()
    return normalized if normalized in allowed else None


def _decision_document_kind(decision: CurationDecisionRecord) -> str | None:
    """Prefer an explicit persisted concept ID, then accept a controlled label."""

    evidence = decision.evidence
    candidates: list[object] = []
    if isinstance(evidence, Mapping):
        for key in ("top1_concept_id", "document_kind_concept_id", "kind_concept_id"):
            if key in evidence:
                candidates.append(evidence[key])
        selected = evidence.get("selected_by_family")
        if isinstance(selected, Mapping):
            candidates.append(selected.get("document_kind"))
    candidates.append(decision.top1_label)
    for candidate in candidates:
        resolved = controlled_document_kind(candidate)
        if resolved is not None:
            return resolved
    return None


def _controlled_kind_names() -> frozenset[str]:
    return frozenset(("normativa", *COMPACT_KIND_DIRECTORIES, *REVIEW_ONLY_KINDS))


def _controlled_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return "".join(char if char.isalnum() else "_" for char in folded).strip("_")


def _row_value(row: Any, key: str, default: object = None) -> object:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        try:
            return getattr(row, key)
        except AttributeError:
            return default


def _row_text(row: Any, key: str) -> str:
    value = _row_value(row, key)
    return value if isinstance(value, str) else str(value)


def _source_input_signature(row: Any) -> str | None:
    for key in ("source_input_signature", "text_fingerprint", "processing_signature"):
        value = _row_value(row, key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _absolute(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _finite_decision_values(decision: CurationDecisionRecord) -> bool:
    values = (decision.top1_score, decision.top2_score, decision.margin)
    return all(value is not None and math.isfinite(float(value)) for value in values)


def _current_identity_matches(row: Any, path: Path) -> bool:
    try:
        observed = path.lstat()
        if path.resolve(strict=True) != path or not stat.S_ISREG(observed.st_mode):
            return False
        expected_volume, expected_file, expected_size, expected_mtime, expected_birth = (
            _row_identity(row)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    observed_birth = getattr(observed, "st_birthtime_ns", -1)
    birth_matches = observed_birth == expected_birth or (
        observed_birth == -1 and expected_birth >= 0 and observed.st_ctime_ns == expected_birth
    )
    return (
        observed.st_dev == expected_volume
        and observed.st_ino == expected_file
        and observed.st_size == expected_size
        and observed.st_mtime_ns == expected_mtime
        and birth_matches
    )


def _normalize_identity(value: tuple[object, object, object, object, object]) -> tuple[int, ...]:
    if len(value) != 5:
        raise ValueError("identity snapshot must contain five fields")
    return tuple(int(str(item)) for item in value)


def _row_identity(row: Any) -> tuple[int, int, int, int, int]:
    return _normalize_identity(
        (
            _row_value(row, "volume_id"),
            _row_value(row, "file_id"),
            _row_value(row, "size"),
            _row_value(row, "mtime_ns"),
            _row_value(row, "birthtime_ns"),
        )
    )  # type: ignore[return-value]


__all__ = [
    "FastCurationPolicySource",
    "FastOrganizationCurationGate",
    "controlled_document_kind",
    "resolve_fast_curation_policy_bundle",
    "validate_current_fast_curation_decision",
]
