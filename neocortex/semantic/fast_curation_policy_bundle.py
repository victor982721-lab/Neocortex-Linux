"""Pure measured-policy bundles for Fast Curation consumers.

This module validates version scopes and catalog decision records without loading
embedding models or opening Semantic state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .fast_curation_policy import (
    CALIBRATION_SCHEMA,
    POLICY_BUNDLE_SCHEMA,
    CalibrationParameters,
    FastCurationPolicy,
)

DEFAULT_POLICY_BUNDLE_DATA = Path(__file__).with_name("data") / "curation_policy_bundle.json"

@dataclass(frozen=True, slots=True)
class FastCurationPolicyBundle:
    """Pure version bundle consumed by Organization; it never loads a model."""

    policy: "FastCurationPolicy"
    model_signature: str
    representation_version: str
    ontology_version: str
    prototype_version: str
    policy_version: str
    calibration_version: str
    prototype_set_fingerprint: str = ""
    prototype_scope: str = ""
    bundle_version: str = POLICY_BUNDLE_SCHEMA
    required_families: tuple[str, ...] | None = None
    max_evidence: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "model_signature",
            "representation_version",
            "ontology_version",
            "prototype_version",
            "policy_version",
            "calibration_version",
            "prototype_set_fingerprint",
            "prototype_scope",
            "bundle_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.bundle_version != POLICY_BUNDLE_SCHEMA:
            raise ValueError("unsupported policy bundle schema")
        if len(self.prototype_set_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.prototype_set_fingerprint
        ):
            raise ValueError("prototype_set_fingerprint must be a SHA-256 hex digest")
        if not self.policy.calibrated:
            raise ValueError("policy bundle requires measured calibration")
        if self.policy.policy_version != self.policy_version:
            raise ValueError("policy and bundle policy versions differ")
        if self.policy.model_signature != self.model_signature:
            raise ValueError("policy and bundle model signatures differ")
        if self.policy.calibration is None or self.policy.calibration.calibration_version != self.calibration_version:
            raise ValueError("policy and bundle calibration versions differ")
        if self.required_families is None:
            object.__setattr__(self, "required_families", tuple(self.policy.required_families))
        if self.max_evidence is None:
            object.__setattr__(self, "max_evidence", self.policy.max_evidence)
        assert self.required_families is not None
        assert self.max_evidence is not None
        if tuple(self.policy.required_families) != tuple(self.required_families):
            raise ValueError("policy and bundle required families differ")
        if self.policy.max_evidence != self.max_evidence:
            raise ValueError("policy and bundle max evidence differs")
        if not 1 <= self.max_evidence <= 5:
            raise ValueError("max_evidence must be between 1 and 5")

    def as_dict(self) -> dict[str, object]:
        calibration = self.policy.calibration
        assert calibration is not None
        return {
            "schema": self.bundle_version,
            "model_signature": self.model_signature,
            "representation_version": self.representation_version,
            "ontology_version": self.ontology_version,
            "prototype_version": self.prototype_version,
            "policy_version": self.policy_version,
            "calibration_version": self.calibration_version,
            "prototype_set_fingerprint": self.prototype_set_fingerprint,
            "prototype_scope": self.prototype_scope,
            "required_families": list(self.required_families),
            "max_evidence": self.max_evidence,
            "calibration": {
                "schema": CALIBRATION_SCHEMA,
                "calibration_version": calibration.calibration_version,
                "model_signature": calibration.model_signature,
                "measured": calibration.measured,
                "min_score_by_family": dict(calibration.min_score_by_family),
                "min_margin_by_family": dict(calibration.min_margin_by_family),
                "min_text_chars": calibration.min_text_chars,
                "min_evidence_count": calibration.min_evidence_count,
                "min_views_agreement": calibration.min_views_agreement,
                "confidence_floor": calibration.confidence_floor,
                "allow_single_candidate": calibration.allow_single_candidate,
                "escalation_enabled": calibration.escalation_enabled,
                "escalation_justification": calibration.escalation_justification,
                "provenance": dict(calibration.provenance),
            },
        }


@dataclass(frozen=True, slots=True)
class DecisionVersionValidation:
    """Bounded result for a planner's current-record gate."""

    valid: bool
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.valid

    def __bool__(self) -> bool:
        return self.valid



def load_calibrated_policy(
    source: Mapping[str, object] | str | Path,
    *,
    required_versions: Mapping[str, str] | None = None,
) -> FastCurationPolicyBundle:
    """Load a measured policy bundle without importing or initializing a model."""

    if isinstance(source, (str, Path)):
        path = Path(source)
        raw = path.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 1_000_000:
            raise ValueError("policy bundle exceeds the bounded size")
        value = json.loads(raw)
    else:
        value = source
    if not isinstance(value, Mapping):
        raise ValueError("policy bundle must be a mapping")
    if value.get("schema", POLICY_BUNDLE_SCHEMA) != POLICY_BUNDLE_SCHEMA:
        raise ValueError("unsupported Fast Curation policy bundle schema")
    versions = value.get("versions")
    versions = versions if isinstance(versions, Mapping) else value
    calibration = value.get("calibration")
    if not isinstance(calibration, Mapping):
        raise ValueError("policy bundle requires a calibration manifest")
    parameters = CalibrationParameters.from_mapping(calibration)
    policy_version = _bundle_string(versions, "policy_version")
    model_signature = _bundle_string(versions, "model_signature")
    representation_version = _bundle_string(versions, "representation_version")
    ontology_version = _bundle_string(versions, "ontology_version")
    prototype_version = _bundle_string(versions, "prototype_version")
    calibration_version = _bundle_string(versions, "calibration_version")
    prototype_set_fingerprint = _bundle_string(versions, "prototype_set_fingerprint")
    prototype_scope = _bundle_string(versions, "prototype_scope")
    required_families_raw = versions.get("required_families", ())
    if not isinstance(required_families_raw, Sequence) or isinstance(required_families_raw, (str, bytes)):
        raise ValueError("policy bundle required_families must be a sequence")
    required_families = tuple(str(value) for value in required_families_raw)
    max_evidence = int(versions.get("max_evidence", 5))
    if model_signature != parameters.model_signature or calibration_version != parameters.calibration_version:
        raise ValueError("policy bundle and calibration versions differ")
    policy = FastCurationPolicy.from_calibration(
        parameters,
        policy_version=policy_version,
        required_families=required_families,
        max_evidence=max_evidence,
    )
    bundle = FastCurationPolicyBundle(
        policy=policy,
        model_signature=model_signature,
        representation_version=representation_version,
        ontology_version=ontology_version,
        prototype_version=prototype_version,
        policy_version=policy_version,
        calibration_version=calibration_version,
        prototype_set_fingerprint=prototype_set_fingerprint,
        prototype_scope=prototype_scope,
        required_families=required_families,
        max_evidence=max_evidence,
    )
    for key, expected in (required_versions or {}).items():
        observed = getattr(bundle, key, None)
        if observed != expected:
            raise ValueError(f"policy bundle version mismatch: {key}")
    return bundle


def default_calibrated_policy(
    source: Mapping[str, object] | str | Path | None = None,
    *,
    required_versions: Mapping[str, str] | None = None,
    packaged_path: str | Path | None = None,
) -> FastCurationPolicyBundle | None:
    """Return the explicit current bundle, or ``None`` when calibration is absent.

    Absence is deliberate: callers must keep the organization capability
    partial rather than guessing thresholds or silently selecting a retrieval
    calibration.
    """

    if source is None:
        source = packaged_path
    if source is None and DEFAULT_POLICY_BUNDLE_DATA.is_file():
        source = DEFAULT_POLICY_BUNDLE_DATA
    if source is None:
        return None
    return load_calibrated_policy(source, required_versions=required_versions)


def validate_record_versions(
    record: object,
    bundle: FastCurationPolicyBundle,
    *,
    source_input_signature: str | None = None,
) -> DecisionVersionValidation:
    """Check that one catalog decision belongs to the current policy bundle."""

    if not isinstance(bundle, FastCurationPolicyBundle):
        raise TypeError("bundle must be FastCurationPolicyBundle")
    for field_name in (
        "model_signature",
        "representation_version",
        "ontology_version",
        "prototype_version",
        "prototype_set_fingerprint",
        "policy_version",
        "calibration_version",
    ):
        observed = _record_value(record, field_name)
        expected = getattr(bundle, field_name)
        if observed != expected:
            return DecisionVersionValidation(False, f"stale_{field_name}")
    if source_input_signature is not None:
        observed = _record_value(record, "input_signature")
        if observed != source_input_signature:
            return DecisionVersionValidation(False, "stale_input_signature")
    return DecisionVersionValidation(True)


def _bundle_string(value: Mapping[str, object], name: str) -> str:
    candidate = value.get(name)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"policy bundle requires {name}")
    return candidate.strip()


def _record_value(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)




__all__ = [
    "DEFAULT_POLICY_BUNDLE_DATA",
    "POLICY_BUNDLE_SCHEMA",
    "DecisionVersionValidation",
    "FastCurationPolicyBundle",
    "default_calibrated_policy",
    "load_calibrated_policy",
    "validate_record_versions",
]
