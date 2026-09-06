"""Bounded evidence contracts for federated, advisory asset diagnosis.

These projections do not own state. A processing error, index gap, policy gate,
and a condition described *inside* a document refer to different subjects.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_DIAGNOSTIC_OBSERVATIONS = 32
MAX_DIAGNOSTIC_REFERENCES = 32


class AssetProblemScope(StrEnum):
    FILE = "file"
    PROCESSING = "processing"
    INDEX = "index"
    POLICY = "policy"
    DOCUMENT_CONDITION = "document_condition"


class AssetDiagnosticCertainty(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class AssetDiagnosticObservationKind(StrEnum):
    DUPLICATE_CONTENT = "duplicate_content"
    LOGICAL_FORMAT = "logical_format"
    DOCUMENT_CONDITION = "document_condition"


def _text(label: str, value: object, maximum: int = 1024) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be bounded non-blank text")


def _codes(label: str, values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > 32:
        raise ValueError(f"{label} must be a bounded immutable tuple")
    if any(not isinstance(value, str) or _CODE.fullmatch(value) is None for value in values):
        raise ValueError(f"{label} must contain canonical codes")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{label} must contain unique sorted codes")


@dataclass(frozen=True, slots=True)
class AssetDiagnosticEvidenceRef:
    """An existing owner's immutable projection, not copied corpus content."""

    owner: str
    record_id: str
    resource_id: str
    snapshot_id: str
    projection_digest: str
    publication_id: str | None = None

    def __post_init__(self) -> None:
        _codes("owner", (self.owner,))
        for label, value in (
            ("record_id", self.record_id),
            ("resource_id", self.resource_id),
            ("snapshot_id", self.snapshot_id),
        ):
            _text(label, value)
        if not isinstance(self.projection_digest, str) or _DIGEST.fullmatch(
            self.projection_digest
        ) is None:
            raise ValueError("projection_digest must be a lowercase SHA-256 digest")
        if self.publication_id is not None:
            _text("publication_id", self.publication_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "record_id": self.record_id,
            "resource_id": self.resource_id,
            "snapshot_id": self.snapshot_id,
            "projection_digest": self.projection_digest,
            "publication_id": self.publication_id,
        }


def _refs(values: tuple[AssetDiagnosticEvidenceRef, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > MAX_DIAGNOSTIC_REFERENCES or any(
        not isinstance(value, AssetDiagnosticEvidenceRef) for value in values
    ):
        raise ValueError("evidence_refs must be bounded typed immutable references")
    if len(set(values)) != len(values):
        raise ValueError("evidence_refs cannot repeat")


@dataclass(frozen=True, slots=True)
class AssetDiagnosticObservation:
    """A typed owner observation consumed by the pure diagnosis projection.

`duplicate_content` means an owner validated a complete full-content group;
paths, suffixes, partial fingerprints and similarity do not qualify. Logical
format is inferred from container structure, not from its name. Domain facts
describe a document's claim, not independently verified physical events.
"""

    kind: AssetDiagnosticObservationKind
    resource_id: str
    code: str
    evidence_refs: tuple[AssetDiagnosticEvidenceRef, ...]
    member_resource_ids: tuple[str, ...] = ()
    missing_checks: tuple[str, ...] = ()
    selection_basis: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AssetDiagnosticObservationKind):
            raise ValueError("kind must be an AssetDiagnosticObservationKind")
        _text("resource_id", self.resource_id)
        _codes("code", (self.code,))
        _codes("missing_checks", self.missing_checks)
        if self.selection_basis is not None:
            _codes("selection_basis", (self.selection_basis,))
            if self.kind is not AssetDiagnosticObservationKind.DUPLICATE_CONTENT:
                raise ValueError("keeper selection basis only applies to duplicate groups")
        _refs(self.evidence_refs)
        if not self.evidence_refs or not any(
            ref.resource_id == self.resource_id for ref in self.evidence_refs
        ):
            raise ValueError("observation requires evidence bound to its resource")
        if not isinstance(self.member_resource_ids, tuple) or len(self.member_resource_ids) > 100:
            raise ValueError("member_resource_ids must be a bounded immutable tuple")
        for member in self.member_resource_ids:
            _text("member_resource_id", member)
        if tuple(sorted(set(self.member_resource_ids))) != self.member_resource_ids:
            raise ValueError("member_resource_ids must be unique and sorted")
        if self.kind is AssetDiagnosticObservationKind.DUPLICATE_CONTENT and (
            len(self.member_resource_ids) < 2 or self.resource_id not in self.member_resource_ids
            or self.code != "byte_for_byte_equal"
        ):
            raise ValueError("duplicate evidence needs byte-for-byte proof of distinct physical resources")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "resource_id": self.resource_id,
            "code": self.code,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "member_resource_ids": list(self.member_resource_ids),
            "missing_checks": list(self.missing_checks),
            "selection_basis": self.selection_basis,
        }


@dataclass(frozen=True, slots=True)
class AssetDiagnosticFinding:
    scope: AssetProblemScope
    code: str
    certainty: AssetDiagnosticCertainty
    evidence_refs: tuple[AssetDiagnosticEvidenceRef, ...] = ()
    missing_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AssetProblemScope) or not isinstance(
            self.certainty, AssetDiagnosticCertainty
        ):
            raise ValueError("finding requires typed scope and certainty")
        _codes("code", (self.code,))
        _codes("missing_checks", self.missing_checks)
        _refs(self.evidence_refs)
        if self.certainty is not AssetDiagnosticCertainty.UNKNOWN and not self.evidence_refs:
            raise ValueError("observed or inferred findings require evidence references")

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "code": self.code,
            "certainty": self.certainty.value,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "missing_checks": list(self.missing_checks),
        }


@dataclass(frozen=True, slots=True)
class AssetDiagnosticRecommendation:
    scope: AssetProblemScope
    action: str
    evidence_refs: tuple[AssetDiagnosticEvidenceRef, ...]
    missing_checks: tuple[str, ...]
    preconditions: tuple[str, ...]
    executable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AssetProblemScope):
            raise ValueError("recommendation requires a typed problem scope")
        _codes("action", (self.action,))
        _refs(self.evidence_refs)
        if not self.evidence_refs:
            raise ValueError("a recommendation requires evidence references")
        _codes("missing_checks", self.missing_checks)
        _codes("preconditions", self.preconditions)
        if self.executable is not False:
            raise ValueError("diagnosis cannot claim that an advisory recommendation is executable")

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "action": self.action,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "missing_checks": list(self.missing_checks),
            "preconditions": list(self.preconditions),
            "executable": self.executable,
            "mutation_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeAssetDiagnosis:
    resource_id: str
    findings: tuple[AssetDiagnosticFinding, ...]
    recommendations: tuple[AssetDiagnosticRecommendation, ...]
    observations: tuple[AssetDiagnosticObservation, ...] = ()

    def __post_init__(self) -> None:
        _text("resource_id", self.resource_id)
        for label, values, expected, maximum in (
            ("findings", self.findings, AssetDiagnosticFinding, 128),
            ("recommendations", self.recommendations, AssetDiagnosticRecommendation, 64),
            ("observations", self.observations, AssetDiagnosticObservation, MAX_DIAGNOSTIC_OBSERVATIONS),
        ):
            if not isinstance(values, tuple) or len(values) > maximum or any(
                not isinstance(value, expected) for value in values
            ):
                raise ValueError(f"{label} must be a bounded typed immutable tuple")
        if any(item.resource_id != self.resource_id for item in self.observations):
            raise ValueError("diagnosis observation belongs to another resource")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "neocortex.knowledge-asset-diagnosis/v1",
            "resource_id": self.resource_id,
            "health_semantics": "published_pipeline_alignment_not_file_integrity",
            "score_semantics": "no_calibrated_probability",
            "read_only": True,
            "advisory_only": True,
            "mutation_authorized": False,
            "findings": [item.to_dict() for item in self.findings],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "observations": [item.to_dict() for item in self.observations],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload["diagnosis_id"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        return payload
