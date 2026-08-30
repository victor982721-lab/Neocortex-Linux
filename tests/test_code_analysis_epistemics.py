"""Adversarial tests for the generic Code question/evidence contract."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from neocortex.code.code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    analysis_questions_payload,
    parse_analysis_questions_payload,
    validate_analysis_question_evaluation,
)


def _spec() -> AnalysisQuestionSpec:
    return AnalysisQuestionSpec(
        question_id="structural.fixture",
        version="v1",
        subject_kinds=("class",),
        requirements=(
            AnalysisEvidenceRequirementSpec(
                "observed",
                "question",
                "supporting",
                ("internal_metric",),
            ),
            AnalysisEvidenceRequirementSpec(
                "counterevidence",
                "decision",
                "counterevidence",
                ("internal_fact",),
            ),
            AnalysisEvidenceRequirementSpec(
                "experiment",
                "decision",
                "experiment_result",
                ("experiment_result",),
            ),
        ),
        hypotheses=("accidental_structure", "intentional_structure"),
        counterevidence_rules=("cohesive_declared_role",),
        next_actions=(
            AnalysisNextActionSpec("characterize", "characterization", "Measure cohesion."),
            AnalysisNextActionSpec("seek_counter", "counterevidence_search", "Seek role evidence."),
            AnalysisNextActionSpec("experiment", "experiment", "Run a discriminating probe."),
        ),
    )


def _subject() -> AnalysisSubjectRef:
    return AnalysisSubjectRef(
        subject_kind="class",
        subject_key="class:physical-id:span",
        display_name="Fixture",
        source_owner_id="code",
        snapshot_id="run:1:signature",
        snapshot_freshness="current",
        revision_id="revision:1",
    )


def _evidence(
    *,
    evidence_id: str = "evidence:1",
    role: str = "supporting",
    kind: str = "internal_metric",
    completeness: str = "complete",
    truncated: bool = False,
) -> AnalysisEvidenceRef:
    return AnalysisEvidenceRef(
        evidence_id=evidence_id,
        subject_key="class:physical-id:span",
        role=role,  # type: ignore[arg-type]
        evidence_kind=kind,  # type: ignore[arg-type]
        source_owner_id="code",
        producer_id="fixture-resolver",
        producer_version="v1",
        source_schema="fixture/v1",
        source_record_kind="metric",
        source_record_id="record:1",
        source_projection_digest=analysis_identity("projection", {"value": 500}),
        snapshot_id="run:1:signature",
        revision_id="revision:1",
        facts=(AnalysisFact("class_span_lines", 500, "lines"),),
        completeness=completeness,  # type: ignore[arg-type]
        bounded=False,
        truncated=truncated,
        resolver_id="fixture-resolver",
        resolver_version="v1",
        limitations=("metric_does_not_prove_harm",),
    )


def _evaluation() -> AnalysisQuestionEvaluation:
    spec = _spec()
    evidence = _evidence()
    return AnalysisQuestionEvaluation(
        evaluation_id="evaluation:1",
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=1,
        subject=_subject(),
        evidence=(evidence,),
        requirements=(
            AnalysisRequirementEvaluation("observed", "satisfied", (evidence.evidence_id,), "ok"),
            AnalysisRequirementEvaluation("counterevidence", "not_evaluated", (), "not_run"),
            AnalysisRequirementEvaluation("experiment", "missing", (), "missing"),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions),
        limitations=("semantics_not_resolved",),
    )


def test_generic_question_derives_readiness_from_linked_evidence() -> None:
    spec = _spec()
    evaluation = _evaluation()

    validate_analysis_question_evaluation(spec, evaluation)
    payload = analysis_questions_payload((spec,), (evaluation,))

    assert payload["schema"] == "neocortex.code-analysis-epistemics/v1"
    assert payload["specs"][0]["spec_fingerprint"] == analysis_question_spec_fingerprint(spec)
    assert payload["evaluations"][0]["decision"] is None
    assert payload["evaluations"][0]["mutation_authority"] is False


def test_generic_question_rejects_nominal_ready_or_unlinked_evidence() -> None:
    spec = _spec()
    evaluation = _evaluation()

    with pytest.raises(ValueError, match="readiness is not derived"):
        validate_analysis_question_evaluation(
            spec,
            replace(evaluation, decision_readiness="human_review_required"),
        )
    with pytest.raises(ValueError, match="unreferenced evidence"):
        validate_analysis_question_evaluation(
            spec,
            replace(
                evaluation,
                evidence=(evaluation.evidence[0], _evidence(evidence_id="evidence:extra")),
            ),
        )


def test_generic_question_rejects_wrong_snapshot_revision_or_spec() -> None:
    spec = _spec()
    evaluation = _evaluation()

    with pytest.raises(ValueError, match="subject snapshot"):
        replace(
            evaluation,
            evidence=(replace(evaluation.evidence[0], snapshot_id="run:other"),),
        )
    with pytest.raises(ValueError, match="subject revision"):
        replace(
            evaluation,
            evidence=(replace(evaluation.evidence[0], revision_id="revision:other"),),
        )
    with pytest.raises(ValueError, match="spec fingerprint mismatch"):
        validate_analysis_question_evaluation(
            spec,
            replace(evaluation, question_spec_fingerprint="forged"),
        )


def test_generic_question_rejects_incomplete_or_truncated_support() -> None:
    spec = _spec()
    evaluation = _evaluation()

    partial = replace(evaluation.evidence[0], completeness="partial")
    with pytest.raises(ValueError, match="epistemically incomplete"):
        validate_analysis_question_evaluation(spec, replace(evaluation, evidence=(partial,)))
    truncated = replace(
        evaluation.evidence[0],
        completeness="partial",
        truncated=True,
    )
    permissive_spec = replace(
        spec,
        requirements=(
            replace(
                spec.requirements[0],
                accepted_completeness=("complete", "partial"),
            ),
            *spec.requirements[1:],
        ),
    )
    permissive_eval = replace(
        evaluation,
        question_spec_fingerprint=analysis_question_spec_fingerprint(permissive_spec),
        evidence=(truncated,),
    )
    with pytest.raises(ValueError, match="rejects truncated evidence"):
        validate_analysis_question_evaluation(permissive_spec, permissive_eval)


def test_external_evidence_requires_a_provider_run_receipt() -> None:
    with pytest.raises(ValueError, match="requires a provider run"):
        _evidence(kind="external_metric")


def test_generic_question_never_owns_a_human_decision() -> None:
    with pytest.raises(ValueError, match="never owns a human decision"):
        replace(_evaluation(), decision="recommend_change")  # type: ignore[arg-type]


def test_generic_question_wire_roundtrip_is_strict_and_revalidates_links() -> None:
    spec = _spec()
    evaluation = _evaluation()
    payload = json.loads(json.dumps(analysis_questions_payload((spec,), (evaluation,))))

    assert parse_analysis_questions_payload(payload) == ((spec,), (evaluation,))

    payload["evaluations"][0]["requirements"][0]["evidence_ids"] = ["forged"]
    with pytest.raises(ValueError, match="unknown evidence"):
        parse_analysis_questions_payload(payload)


def test_generic_question_wire_rejects_unknown_fields_and_spec_tampering() -> None:
    payload = json.loads(json.dumps(analysis_questions_payload((_spec(),), (_evaluation(),))))
    payload["unknown"] = True
    with pytest.raises(ValueError, match="fields are invalid"):
        parse_analysis_questions_payload(payload)

    payload = json.loads(json.dumps(analysis_questions_payload((_spec(),), (_evaluation(),))))
    payload["specs"][0]["hypotheses"][0] = "tampered"
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        parse_analysis_questions_payload(payload)
