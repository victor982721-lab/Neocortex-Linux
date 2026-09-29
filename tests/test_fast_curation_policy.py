"""Fail-safe policy tests for Fast Curation."""

from __future__ import annotations

import pytest

from neocortex.semantic.fast_curation_policy import (
    CalibrationParameters,
    CurationDecision,
    FastCurationPolicy,
    RankedCandidate,
)


def _candidate(
    prototype_id: str,
    concept_id: str,
    score: float,
    rank: int,
    *,
    family: str = "document_kind",
    label: str | None = None,
) -> RankedCandidate:
    return RankedCandidate(
        prototype_id=prototype_id,
        concept_id=concept_id,
        family=family,
        label=label or concept_id,
        score=score,
        rank=rank,
    )


def _policy() -> FastCurationPolicy:
    return FastCurationPolicy.from_calibration(
        CalibrationParameters(
            calibration_version="cal-v1",
            model_signature="model-v1",
            measured=True,
            min_score_by_family={"document_kind": 0.70},
            min_margin_by_family={"document_kind": 0.10},
            min_text_chars=8,
            min_evidence_count=2,
        )
    )


def test_default_policy_is_provisional_and_never_autosafe() -> None:
    decision = FastCurationPolicy().evaluate(
        (_candidate("p", "report", 0.99, 1),),
        text_chars=100,
        model_available=True,
        model_signature="model-v1",
    )
    assert decision.decision is CurationDecision.ABSTAIN
    assert decision.reason == "calibration_unavailable"
    assert decision.decision_confidence is None


def test_score_and_margin_are_required_and_similarity_is_not_probability() -> None:
    policy = _policy()
    classified = policy.evaluate(
        (
            _candidate("report", "report", 0.90, 1),
            _candidate("manual", "manual", 0.30, 2),
        ),
        text_chars=100,
        model_available=True,
        model_signature="model-v1",
    )
    assert classified.decision is CurationDecision.CLASSIFIED
    assert classified.margins["document_kind"] == pytest.approx(0.60)
    assert classified.confidence_kind == "score_margin_strength_not_probability"
    assert classified.confidence is not None
    assert classified.confidence != classified.top1_scores["document_kind"]

    ambiguous = policy.evaluate(
        (
            _candidate("report", "report", 0.90, 1),
            _candidate("manual", "manual", 0.85, 2),
        ),
        text_chars=100,
        model_available=True,
        model_signature="model-v1",
    )
    assert ambiguous.decision is CurationDecision.ABSTAIN
    assert ambiguous.reason == "margin_below_calibration:document_kind"


def test_wrong_model_and_explicit_document_kind_conflict_abstain() -> None:
    policy = _policy()
    wrong_model = policy.evaluate(
        (
            _candidate("report", "report", 0.9, 1),
            _candidate("manual", "manual", 0.2, 2),
        ),
        text_chars=100,
        model_available=True,
        model_signature="different-model",
    )
    assert wrong_model.reason == "calibration_model_mismatch"

    conflict = policy.evaluate(
        (
            _candidate("report", "report", 0.9, 1),
            _candidate("manual", "manual", 0.2, 2),
        ),
        text_chars=100,
        model_available=True,
        model_signature="model-v1",
        deterministic_evidence={
            "document_kind_status": "explicit",
            "document_kind_concept_id": "invoice",
        },
    )
    assert conflict.decision is CurationDecision.ABSTAIN
    assert conflict.reason == "document_kind_conflict"


def test_family_order_keeps_document_kind_primary() -> None:
    policy = FastCurationPolicy.from_calibration(
        CalibrationParameters(
            "cal-v1",
            "model-v1",
            True,
            {"document_kind": 0.4, "activity": 0.4},
            {"document_kind": 0.1, "activity": 0.1},
        )
    )
    decision = policy.evaluate(
        (
            _candidate("testing", "testing", 0.99, 1, family="activity"),
            _candidate("maintenance", "maintenance", 0.20, 2, family="activity"),
            _candidate("report", "report", 0.80, 1, family="document_kind"),
            _candidate("manual", "manual", 0.20, 2, family="document_kind"),
        ),
        text_chars=100,
        model_available=True,
        model_signature="model-v1",
    )
    assert tuple(decision.selected_by_family) == ("document_kind", "activity")
