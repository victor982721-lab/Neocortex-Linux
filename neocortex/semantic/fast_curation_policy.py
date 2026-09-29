"""Fail-safe policy for Fast Curation classification decisions.

Scores produced by an embedding model are similarities, not probabilities.
This owner turns bounded semantic and structural evidence into exactly one
organization disposition.  In particular, the provisional policy has no
automatic-safe path: a measured calibration manifest must be loaded before a
document can be classified.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


POLICY_VERSION = "fast-curation-policy-v1"
CALIBRATION_SCHEMA = "fast-curation-calibration/v1"
POLICY_BUNDLE_SCHEMA = "fast-curation-policy-bundle/v1"
CONTROLLED_FAMILIES = frozenset({"document_kind", "topic", "activity"})
FAMILY_ORDER = ("document_kind", "topic", "activity")


def _canonical_family(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("family must be a string")
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = {
        "documentkind": "document_kind",
        "document_kind": "document_kind",
        "topic": "topic",
        "activity": "activity",
    }.get(normalized, normalized)
    if normalized not in CONTROLLED_FAMILIES:
        raise ValueError("family is not controlled")
    return normalized


class CurationDecision(StrEnum):
    CLASSIFIED = "classified"
    ABSTAIN = "abstain"


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """Bounded semantic candidate; ``score`` remains a cosine-like score."""

    prototype_id: str
    concept_id: str
    family: str
    label: str
    score: float
    rank: int
    parent_id: str | None = None
    destination: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("prototype_id", self.prototype_id),
            ("concept_id", self.concept_id),
            ("family", self.family),
            ("label", self.label),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "family", _canonical_family(self.family))
        if not isinstance(self.rank, int) or isinstance(self.rank, bool) or self.rank < 1:
            raise ValueError("candidate rank must be positive")
        if not math.isfinite(float(self.score)) or not -1.0 <= float(self.score) <= 1.0:
            raise ValueError("candidate score must be a finite similarity in [-1, 1]")


@dataclass(frozen=True, slots=True)
class CalibrationParameters:
    """Measured thresholds and metadata for organization-safe classification."""

    calibration_version: str
    model_signature: str
    measured: bool
    min_score_by_family: Mapping[str, float]
    min_margin_by_family: Mapping[str, float]
    min_text_chars: int = 1
    min_evidence_count: int = 2
    min_views_agreement: float = 1.0
    confidence_floor: float = 0.0
    allow_single_candidate: bool = False
    escalation_enabled: bool = False
    escalation_justification: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.calibration_version, str) or not self.calibration_version.strip():
            raise ValueError("calibration_version must be non-empty")
        if not isinstance(self.model_signature, str) or not self.model_signature.strip():
            raise ValueError("model_signature must be non-empty")
        if not isinstance(self.measured, bool):
            raise ValueError("measured must be boolean")
        score_by_family = {
            _canonical_family(key): value for key, value in self.min_score_by_family.items()
        }
        margin_by_family = {
            _canonical_family(key): value for key, value in self.min_margin_by_family.items()
        }
        _validate_thresholds(score_by_family, lower=-1.0, upper=1.0)
        _validate_thresholds(margin_by_family, lower=0.0, upper=2.0)
        object.__setattr__(self, "min_score_by_family", score_by_family)
        object.__setattr__(self, "min_margin_by_family", margin_by_family)
        if not isinstance(self.min_text_chars, int) or isinstance(self.min_text_chars, bool):
            raise ValueError("min_text_chars must be an integer")
        if self.min_text_chars < 1:
            raise ValueError("min_text_chars must be positive")
        if not isinstance(self.min_evidence_count, int) or isinstance(
            self.min_evidence_count, bool
        ):
            raise ValueError("min_evidence_count must be an integer")
        if self.min_evidence_count < 1:
            raise ValueError("min_evidence_count must be positive")
        if not 0.0 <= float(self.min_views_agreement) <= 1.0:
            raise ValueError("min_views_agreement must be in [0,1]")
        if not 0.0 <= float(self.confidence_floor) <= 1.0:
            raise ValueError("confidence_floor must be in [0,1]")
        if self.escalation_enabled and not (
            isinstance(self.escalation_justification, str)
            and self.escalation_justification.strip()
        ):
            raise ValueError("measured escalation requires a justification")
        try:
            json.dumps(self.provenance, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("calibration provenance must be JSON compatible") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CalibrationParameters":
        """Load only an explicit measured calibration manifest.

        The method accepts both the nested ``thresholds`` shape and the flat
        fields emitted by early benchmark tools, but never invents missing
        thresholds.  Missing data leaves the policy provisional.
        """

        if not isinstance(value, Mapping):
            raise TypeError("calibration must be a mapping")
        schema = value.get("schema", CALIBRATION_SCHEMA)
        if schema != CALIBRATION_SCHEMA:
            raise ValueError("unsupported Fast Curation calibration schema")
        thresholds = value.get("thresholds")
        threshold_map = thresholds if isinstance(thresholds, Mapping) else value
        score = threshold_map.get("min_score_by_family", value.get("min_score_by_family", {}))
        margin = threshold_map.get(
            "min_margin_by_family", value.get("min_margin_by_family", {})
        )
        if (not score or not margin) and isinstance(thresholds, Mapping):
            paired: dict[str, tuple[object, object]] = {}
            for family, raw in thresholds.items():
                if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 2:
                    paired[str(family)] = (raw[0], raw[1])
            if paired:
                score = {family: raw[0] for family, raw in paired.items()}
                margin = {family: raw[1] for family, raw in paired.items()}
        if not isinstance(score, Mapping) or not isinstance(margin, Mapping):
            raise ValueError("calibration thresholds must be mappings")
        return cls(
            calibration_version=str(value.get("calibration_version", "")),
            model_signature=str(value.get("model_signature", "")),
            measured=value.get("measured") is True,
            min_score_by_family={str(key): float(raw) for key, raw in score.items()},
            min_margin_by_family={str(key): float(raw) for key, raw in margin.items()},
            min_text_chars=int(value.get("min_text_chars", 1)),
            min_evidence_count=int(value.get("min_evidence_count", 2)),
            min_views_agreement=float(value.get("min_views_agreement", 1.0)),
            confidence_floor=float(value.get("confidence_floor", 0.0)),
            allow_single_candidate=value.get("allow_single_candidate") is True,
            escalation_enabled=value.get("escalation_enabled") is True,
            escalation_justification=value.get("escalation_justification"),
            provenance=value.get("provenance") or {},
        )


def _validate_thresholds(values: Mapping[str, float], *, lower: float, upper: float) -> None:
    if not isinstance(values, Mapping):
        raise ValueError("thresholds must be mappings")
    for family, value in values.items():
        if family not in CONTROLLED_FAMILIES:
            raise ValueError("threshold family is not controlled")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("thresholds must contain numbers")
        if not math.isfinite(float(value)) or not lower <= float(value) <= upper:
            raise ValueError("threshold is outside its valid range")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One authoritative hybrid decision and bounded evidence."""

    decision: CurationDecision
    reason: str
    candidates: tuple[RankedCandidate, ...] = ()
    selected_by_family: Mapping[str, RankedCandidate] = field(default_factory=dict)
    top1_scores: Mapping[str, float] = field(default_factory=dict)
    top2_scores: Mapping[str, float | None] = field(default_factory=dict)
    margins: Mapping[str, float | None] = field(default_factory=dict)
    decision_confidence: float | None = None
    confidence_kind: str = "not_calibrated"
    calibrated: bool = False
    deterministic_evidence: Mapping[str, object] = field(default_factory=dict)
    semantic_evidence: Mapping[str, object] = field(default_factory=dict)
    structural_evidence: Mapping[str, object] = field(default_factory=dict)

    @property
    def disposition(self) -> str:
        return self.decision.value

    @property
    def organization_disposition(self) -> str:
        return self.decision.value.upper()

    @property
    def classified(self) -> bool:
        return self.decision is CurationDecision.CLASSIFIED

    @property
    def abstained(self) -> bool:
        return not self.classified

    @property
    def confidence(self) -> float | None:
        """Explicit calibrated decision confidence; never a raw similarity."""

        return self.decision_confidence


@dataclass(frozen=True, slots=True)
class FastCurationPolicy:
    """Single decision owner for semantic, metadata and structural evidence."""

    calibration: CalibrationParameters | None = None
    policy_version: str = POLICY_VERSION
    required_families: tuple[str, ...] = ()
    max_evidence: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")
        canonical_families = tuple(_canonical_family(family) for family in self.required_families)
        if len(set(canonical_families)) != len(canonical_families):
            raise ValueError("required families must be unique")
        object.__setattr__(self, "required_families", canonical_families)
        if not 1 <= self.max_evidence <= 5:
            raise ValueError("max_evidence must be between 1 and 5")

    @property
    def calibrated(self) -> bool:
        return self.calibration is not None and self.calibration.measured

    @property
    def model_signature(self) -> str | None:
        return None if self.calibration is None else self.calibration.model_signature

    @classmethod
    def from_calibration(
        cls,
        calibration: CalibrationParameters | Mapping[str, object],
        *,
        required_families: Sequence[str] = (),
        policy_version: str = POLICY_VERSION,
        max_evidence: int = 5,
    ) -> "FastCurationPolicy":
        parameters = (
            calibration
            if isinstance(calibration, CalibrationParameters)
            else CalibrationParameters.from_mapping(calibration)
        )
        return cls(
            calibration=parameters,
            policy_version=policy_version,
            required_families=tuple(required_families),
            max_evidence=max_evidence,
        )

    def evaluate(
        self,
        candidates: Sequence[RankedCandidate],
        *,
        text_chars: int,
        model_available: bool = True,
        model_signature: str | None = None,
        representation_quality: str = "adequate",
        view_agreement: float | None = None,
        deterministic_evidence: Mapping[str, object] | None = None,
        semantic_evidence: Mapping[str, object] | None = None,
        structural_evidence: Mapping[str, object] | None = None,
    ) -> PolicyDecision:
        """Evaluate bounded evidence without a heuristic fallback path."""

        grouped_candidates: dict[str, list[RankedCandidate]] = {}
        for candidate in candidates:
            grouped_candidates.setdefault(candidate.family, []).append(candidate)
        # ``max_evidence`` is a per-axis bound.  Keeping only the first few
        # candidates globally can silently remove Topic or Activity evidence
        # when DocumentKind has more prototypes.
        candidate_tuple = tuple(
            candidate
            for family in FAMILY_ORDER
            if family in grouped_candidates
            for candidate in grouped_candidates[family][: self.max_evidence]
        )
        deterministic = dict(deterministic_evidence or {})
        semantic = dict(semantic_evidence or {})
        structural = dict(structural_evidence or {})
        common = {
            "candidates": candidate_tuple,
            "deterministic_evidence": deterministic,
            "semantic_evidence": semantic,
            "structural_evidence": structural,
        }
        if not model_available:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "capability_unavailable",
                **common,
            )
        if not self.calibrated:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "calibration_unavailable",
                **common,
            )
        calibration = self.calibration
        assert calibration is not None
        if model_signature is not None and model_signature != calibration.model_signature:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "calibration_model_mismatch",
                **common,
            )
        if not isinstance(text_chars, int) or text_chars < calibration.min_text_chars:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "representation_insufficient",
                **common,
            )
        if representation_quality not in {"adequate", "rich"}:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "representation_insufficient",
                **common,
            )
        if view_agreement is not None and view_agreement < calibration.min_views_agreement:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "view_conflict",
                **common,
            )
        if _has_conflict(structural) or _has_conflict(deterministic):
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "evidence_conflict",
                **common,
            )
        if len(candidate_tuple) < calibration.min_evidence_count:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "insufficient_evidence",
                **common,
            )

        by_family: dict[str, list[RankedCandidate]] = {}
        for candidate in candidate_tuple:
            by_family.setdefault(candidate.family, []).append(candidate)
        if _kind_contradiction(deterministic, by_family):
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "document_kind_conflict",
                **common,
            )
        families = self.required_families or tuple(
            family for family in FAMILY_ORDER if family in by_family
        )
        if not families:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "unknown_category",
                **common,
            )
        selected: dict[str, RankedCandidate] = {}
        top1_scores: dict[str, float] = {}
        top2_scores: dict[str, float | None] = {}
        margins: dict[str, float | None] = {}
        confidence_values: list[float] = []
        for family in families:
            values = sorted(by_family.get(family, ()), key=lambda item: (-item.score, item.concept_id))
            if not values:
                return PolicyDecision(
                    CurationDecision.ABSTAIN,
                    f"missing_family:{family}",
                    **common,
                )
            top1 = values[0]
            top2 = values[1] if len(values) > 1 else None
            min_score = calibration.min_score_by_family.get(family)
            min_margin = calibration.min_margin_by_family.get(family)
            if min_score is None or min_margin is None:
                return PolicyDecision(
                    CurationDecision.ABSTAIN,
                    f"uncalibrated_family:{family}",
                    **common,
                )
            if top2 is None and not calibration.allow_single_candidate:
                return PolicyDecision(
                    CurationDecision.ABSTAIN,
                    f"insufficient_candidates:{family}",
                    **common,
                )
            margin = None if top2 is None else top1.score - top2.score
            if top1.score < min_score:
                return PolicyDecision(
                    CurationDecision.ABSTAIN,
                    f"score_below_calibration:{family}",
                    **common,
                    top1_scores={**top1_scores, family: top1.score},
                    top2_scores={**top2_scores, family: None if top2 is None else top2.score},
                    margins={**margins, family: margin},
                )
            if margin is not None and margin < min_margin:
                return PolicyDecision(
                    CurationDecision.ABSTAIN,
                    f"margin_below_calibration:{family}",
                    **common,
                    top1_scores={**top1_scores, family: top1.score},
                    top2_scores={**top2_scores, family: None if top2 is None else top2.score},
                    margins={**margins, family: margin},
                )
            selected[family] = top1
            top1_scores[family] = top1.score
            top2_scores[family] = None if top2 is None else top2.score
            margins[family] = margin
            score_norm = _normalize_floor(top1.score, min_score)
            margin_norm = (
                1.0 if margin is None else _normalize_floor(margin, min_margin)
            )
            confidence_values.append(0.5 * score_norm + 0.5 * margin_norm)
        confidence = min(confidence_values) if confidence_values else None
        if confidence is None or confidence < calibration.confidence_floor:
            return PolicyDecision(
                CurationDecision.ABSTAIN,
                "confidence_below_calibration",
                **common,
                selected_by_family=selected,
                top1_scores=top1_scores,
                top2_scores=top2_scores,
                margins=margins,
                decision_confidence=confidence,
                confidence_kind="score_margin_strength_not_probability",
                calibrated=True,
            )
        return PolicyDecision(
            CurationDecision.CLASSIFIED,
            "calibrated_score_and_margin",
            **common,
            selected_by_family=selected,
            top1_scores=top1_scores,
            top2_scores=top2_scores,
            margins=margins,
            decision_confidence=confidence,
            confidence_kind="score_margin_strength_not_probability",
            calibrated=True,
        )


def _normalize_floor(value: float, floor: float) -> float:
    if floor >= 1.0:
        return 1.0 if value >= floor else 0.0
    return max(0.0, min(1.0, (float(value) - floor) / (1.0 - floor)))


def _has_conflict(value: Mapping[str, object]) -> bool:
    if value.get("conflict") is True or value.get("contradiction") is True:
        return True
    conflicts = value.get("conflicts")
    return isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and bool(conflicts)


def _kind_contradiction(
    evidence: Mapping[str, object],
    candidates: Mapping[str, Sequence[RankedCandidate]],
) -> bool:
    """Apply only explicit/strong document-kind vetoes, never keyword scoring."""

    if evidence.get("document_kind_conflict") is True or evidence.get("kind_conflict") is True:
        return True
    if evidence.get("hard_veto") is True and evidence.get("veto_axis") in {
        "document_kind",
        "kind",
    }:
        return True
    status = evidence.get("document_kind_status", evidence.get("kind_status"))
    if status not in {"explicit", "strong", "catalog"}:
        return False
    expected = evidence.get("document_kind_concept_id", evidence.get("kind_concept_id"))
    if expected is None:
        expected = evidence.get("document_kind", evidence.get("kind"))
    if not isinstance(expected, str) or not expected.strip():
        return False
    values = candidates.get("document_kind", ())
    if not values:
        return False
    normalized = expected.strip().casefold()
    top = values[0]
    return normalized not in {top.concept_id.casefold(), top.label.casefold()}


__all__ = [
    "CALIBRATION_SCHEMA",
    "CONTROLLED_FAMILIES",
    "FAMILY_ORDER",
    "POLICY_BUNDLE_SCHEMA",
    "POLICY_VERSION",
    "CalibrationParameters",
    "CurationDecision",
    "FastCurationPolicy",
    "PolicyDecision",
    "RankedCandidate",
]
