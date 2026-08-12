"""Calibration and anti-Goodhart regressions for Code review epistemics."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import _04_Nucleo_Operativo.code_review as code_review_module
from _04_Nucleo_Operativo.code_review import review_code_state
from _04_Nucleo_Operativo.code_review_actionability import (
    CodeReviewActionabilityInput,
    CodeReviewEpistemicState,
    assess_code_review_actionability,
)
from _04_Nucleo_Operativo.code_review_models import (
    CodeReviewRecommendation,
    CodeReviewResult,
    CodeReviewWorkPackage,
    CodeReviewWorkPackageStep,
    build_code_review_recommendations,
)
from _04_Nucleo_Operativo.code_review_work_packages import build_code_review_work_packages
from _04_Nucleo_Operativo.code_unused_analysis import (
    UnusedConsensusCandidate,
    UnusedEvidenceSignals,
)
from _04_Nucleo_Operativo.semantic_models import canonical_json, fingerprint_text
from tests.test_code_review import _build_state, _status


def _input(
    *,
    path: str = "/repo/pkg/semantic_lineage_repository.py",
    symbol: str = "semantic_lineage_repository.explain_text_chunk_lineage",
    outgoing_calls: tuple[str, ...] = (),
) -> CodeReviewActionabilityInput:
    return CodeReviewActionabilityInput(
        path=path,
        symbol=symbol,
        root="/repo",
        complexity_ratio_basis_points=58_000,
        length_ratio_basis_points=22_000,
        path_convention_production_callers=2,
        path_convention_test_callers=1,
        path_convention_fixture_callers=1,
        path_convention_tool_callers=0,
        path_convention_compatibility_callers=0,
        resolved_static_consumer_files=3,
        outgoing_calls=outgoing_calls,
    )


def _semantic_projection(assessment: object) -> tuple[object, ...]:
    return (
        assessment.construction,
        assessment.actionability,
        assessment.change_risk,
        assessment.recommended_change,
        assessment.epistemic_state,
        assessment.contracts_to_preserve,
        assessment.recommended_validation,
    )


@pytest.mark.parametrize(
    ("path", "symbol", "outgoing_calls"),
    (
        (
            "/repo/pkg/semantic_lineage_reader.py",
            "semantic_lineage_reader.lookup_text_chunk_lineage",
            (),
        ),
        (
            "/repo/pkg/semantic_lineage_store.py",
            "semantic_lineage_store.build_text_chunk_lineage",
            (),
        ),
        (
            "/repo/pkg/semantic_lineage_repository.py",
            "semantic_lineage_repository.run_text_chunk_lineage",
            (),
        ),
        (
            "/repo/pkg/state_writer.py",
            "state_writer.persist_and_enqueue",
            ("begin", "commit", "executemany", "rollback"),
        ),
        (
            "/repo/pkg/value_review_tasks.py",
            "value_review_tasks.read_value_review_task_queue",
            (),
        ),
        (
            "/repo/pkg/value_review_repository.py",
            "value_review_repository._observation",
            (),
        ),
        (
            "/repo/tests/test_semantic_lineage.py",
            "test_semantic_lineage.compute",
            ("git.commit",),
        ),
    ),
)
def test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state(
    path: str,
    symbol: str,
    outgoing_calls: tuple[str, ...],
) -> None:
    baseline = assess_code_review_actionability(_input())
    transformed = assess_code_review_actionability(
        _input(path=path, symbol=symbol, outgoing_calls=outgoing_calls)
    )

    assert _semantic_projection(transformed) == _semantic_projection(baseline)
    assert transformed.construction == "unknown"
    assert transformed.actionability == "characterize_first"
    assert transformed.recommended_change is False


def test_wrapper_name_and_outgoing_call_proxy_cannot_create_a_change_decision() -> None:
    direct = assess_code_review_actionability(
        _input(
            symbol="workflow.publish_generation",
            outgoing_calls=("sqlite3.Connection.execute",),
        )
    )
    wrapper = assess_code_review_actionability(
        _input(
            symbol="workflow.commit_and_store_wrapper",
            outgoing_calls=("begin", "commit", "rollback", "store"),
        )
    )

    assert _semantic_projection(wrapper) == _semantic_projection(direct)
    assert wrapper.epistemic_state.decision_readiness == "experiment_required"
    assert wrapper.epistemic_state.decision is None
    assert wrapper.epistemic_state.mutation_authority is False


def test_structural_hotspot_exposes_observation_question_and_missing_decision_evidence() -> None:
    assessment = assess_code_review_actionability(_input())
    epistemic = assessment.epistemic_state

    assert epistemic.observation_status == "confirmed"
    assert epistemic.observations == (
        "cyclomatic_complexity_threshold_met_or_exceeded:58000bp",
        "function_length_threshold_met_or_exceeded:22000bp",
        "path_convention_production_callers:2",
        "path_convention_test_or_fixture_callers:2",
        "resolved_static_consumer_files:3",
    )
    assert epistemic.inference_status == "abstained"
    assert epistemic.inferences == ()
    assert epistemic.hypotheses == (
        "structural_hotspot_may_increase_maintenance_cost",
        "structure_may_be_intentional_or_cohesive",
    )
    assert epistemic.question_readiness == "ready"
    assert epistemic.question_reason is None
    assert epistemic.decision_readiness == "experiment_required"
    assert epistemic.decision is None
    assert epistemic.decision_reason == ("structural_observation_alone_cannot_justify_change")
    assert epistemic.missing_evidence == (
        "behavior_or_contract_problem_observed",
        "counterevidence_evaluated",
        "discriminating_experiment_result",
    )
    assert epistemic.counterevidence_to_seek
    assert epistemic.next_actions
    assert epistemic.authority == "advisory"
    assert epistemic.mutation_authority is False
    assert assessment.actionability != "act_now"
    assert assessment.recommended_change is False


def test_structural_state_rejects_a_self_declared_ready_decision() -> None:
    with pytest.raises(
        ValueError,
        match="has no evidence resolver and cannot issue decisions",
    ):
        CodeReviewEpistemicState(
            question_id="maintenance.forged",
            question_version="v1",
            observation_status="confirmed",
            observations=("anything",),
            inference_status="abstained",
            inferences=(),
            hypotheses=("a", "b"),
            question_readiness="ready",
            question_reason=None,
            required_evidence=("anything",),
            satisfied_evidence=("anything",),
            missing_evidence=(),
            counterevidence_status="evaluated",
            counterevidence_to_seek=("anything",),
            decision_readiness="ready",
            decision="recommend_change",
            decision_reason="self_declared",
            next_actions=(),
        )


def test_structural_state_rejects_a_self_declared_semantic_inference() -> None:
    baseline = assess_code_review_actionability(_input()).epistemic_state

    with pytest.raises(ValueError, match="has no semantic inference resolver"):
        replace(
            baseline,
            inference_status="supported",
            inferences=("anything",),
        )


def test_structural_state_rejects_an_arbitrary_action_disguised_as_an_experiment() -> None:
    baseline = assess_code_review_actionability(_input()).epistemic_state

    with pytest.raises(ValueError, match="next actions must be canonical"):
        replace(baseline, next_actions=("delete_or_refactor_symbol_now",))


def test_structural_state_rejects_forged_question_or_evidence_semantics() -> None:
    baseline = assess_code_review_actionability(_input()).epistemic_state

    with pytest.raises(ValueError, match="question identity must be canonical"):
        replace(baseline, question_id="maintenance.delete_symbol_now")
    with pytest.raises(ValueError, match="state must be canonical"):
        replace(
            baseline,
            hypotheses=("delete_symbol_now", "safe_to_delete"),
        )


def test_structural_state_rejects_empty_or_duplicated_evidence_contracts() -> None:
    baseline = assess_code_review_actionability(_input()).epistemic_state

    with pytest.raises(ValueError, match="requires an evidence contract"):
        replace(
            baseline,
            required_evidence=(),
            satisfied_evidence=(),
            missing_evidence=(),
        )
    with pytest.raises(ValueError, match="missing evidence cannot contain duplicates"):
        replace(
            baseline,
            missing_evidence=(*baseline.missing_evidence, baseline.missing_evidence[0]),
        )


def test_valid_structural_findings_cannot_be_promoted_by_legacy_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    finding = review_code_state(state_directory).findings[0]

    assert build_code_review_recommendations((finding,), limit=1) == ()
    with pytest.raises(ValueError, match="cannot authorize a change recommendation"):
        replace(finding, actionability="act_now", recommended_change=True)


def test_public_recommendation_constructor_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="cannot construct semantic change recommendations"):
        CodeReviewRecommendation(
            1,
            "finding",
            "hotspot",
            1,
            "pkg/module.py",
            "pkg.module.symbol",
            "unknown",
            "unknown",
            0,
            0,
            (),
            (),
            (),
        )


def test_public_hotspot_package_factory_and_constructor_are_fail_closed() -> None:
    assert build_code_review_work_packages((), (), ()) == ()
    with pytest.raises(ValueError, match="only supports unused-code characterization"):
        CodeReviewWorkPackage(
            package_rank=1,
            package_id="forged",
            title="forged",
            objective="change_hotspot",
            primary_finding_id="finding",
            primary_hotspot_id="hotspot",
            primary_symbol="pkg.module.symbol",
            primary_module="pkg.module",
            change_risk="unknown",
            members=(),
            members_truncated=False,
            consumer_module_examples=(),
            import_chains=(),
            affected_architecture_contracts=(),
            test_coverage=None,
            test_coverage_scope=None,
            contracts_to_preserve=(),
            steps=(CodeReviewWorkPackageStep(1, "characterize", "symbol", "inspect"),),
            recommended_validation=(),
            acceptance_gates=(),
            evidence=(),
            limitations=(),
            confidence="primary_finding_only",
            package_kind="hotspot_maintenance",  # type: ignore[arg-type]
            requires_human_confirmation=True,
        )


def _public_unused_package(**overrides: object) -> CodeReviewWorkPackage:
    signals = UnusedEvidenceSignals(
        vulture_reported=True,
        vulture_confidence=1.0,
        pyright_reported=True,
        vulture_complete=True,
        pyright_complete=True,
        providers_aligned=True,
        graph_references=0,
        graph_calls=0,
        graph_imports=0,
        in_all=False,
        reexported=False,
        entry_point=False,
        callback=False,
        registry=False,
        fixture=False,
        protocol=False,
        special=False,
        coverage_observed=False,
        coverage_status="missing",
        evidence_ids=("provider-finding-1", "provider-finding-2"),
    )
    candidate = UnusedConsensusCandidate(
        candidate_id="unused-candidate",
        version_id=1,
        symbol_id=1,
        relative_path="pkg/module.py",
        module_id="pkg.module",
        symbol="pkg.module.maybe_unused",
        name="maybe_unused",
        kind="function",
        start_line=1,
        end_line=2,
        state="probable_unused_high_consensus",
        provider_ids=("vulture-unused-static", "pyright-trusted-project"),
        signals=signals,
        reasons=(
            "vulture_high_confidence",
            "pyright_reported_unused",
            "no_observed_usage_or_dynamic_contract",
        ),
        evidence=signals.evidence_ids,
        limitations=("dynamic_usage_not_observed",),
    )
    target = candidate.symbol or candidate.name
    package_id = (
        "code-unused-work-package-v1:xxh3_128:"
        + fingerprint_text(
            canonical_json(
                {
                    "planning": "unused-characterization-work-packages-v1",
                    "candidate_id": candidate.candidate_id,
                }
            )
        ).xxh3_128
    )
    values: dict[str, object] = {
        "package_rank": 1,
        "package_id": package_id,
        "title": "pkg.module.maybe_unused unused-code characterization",
        "objective": "characterize_high_consensus_unused_candidate_without_mutation",
        "primary_finding_id": "unused-candidate",
        "primary_hotspot_id": "unused-candidate",
        "primary_symbol": "pkg.module.maybe_unused",
        "primary_module": "pkg.module",
        "change_risk": "unknown",
        "members": (),
        "members_truncated": False,
        "consumer_module_examples": (),
        "import_chains": (),
        "affected_architecture_contracts": (),
        "test_coverage": None,
        "test_coverage_scope": None,
        "contracts_to_preserve": (
            "public_import_and_reexport_surface",
            "callbacks_registries_protocols_and_entry_points",
            "runtime_and_test_fixture_behavior",
        ),
        "steps": tuple(
            CodeReviewWorkPackageStep(index, "characterize", target, requirement)
            for index, requirement in enumerate(
                (
                    "verify_import_reexport_callback_registry_protocol_and_entry_point_usage",
                    "run_targeted_tests_and_public_import_smoke_without_mutating_code",
                    "record_explicit_human_confirmation_or_reclassify_with_new_evidence",
                    "require_comparable_unused_analysis_replay_before_any_separate_change",
                ),
                start=1,
            )
        ),
        "recommended_validation": (
            "inspect_import_reexport_and___all___usage",
            "inspect_callbacks_registries_protocols_and_entry_points",
            "run_targeted_tests_and_public_import_smoke",
            "record_human_confirmation_before_any_separate_change",
        ),
        "acceptance_gates": (
            "unused_analysis_comparable",
            "candidate_remains_probable_unused_high_consensus",
            "dynamic_usage_ruled_out_by_human_review",
            "human_confirmation_recorded",
            "tests_passed",
            "public_import_surface_preserved",
            "architecture_contracts_not_degraded",
            "no_new_import_cycles",
            "unused_coverage_status_honest",
        ),
        "evidence": (
            "unused_candidate:unused-candidate:probable_unused_high_consensus",
            "provider:vulture-unused-static",
            "provider:pyright-trusted-project",
        ),
        "limitations": (
            "characterization_package_is_advice_not_change_authorization",
            "candidate_requires_explicit_human_confirmation",
            "dynamic_usage_may_remain_unobserved",
            "coverage_can_explain_usage_but_never_strengthens_missing_evidence",
            "package_has_zero_delete_or_mutation_authority",
            "supply_chain_evidence_is_advisory_and_has_zero_mutation_authority",
        ),
        "confidence": "unused_high_consensus_advisory",
        "unused_candidates": (candidate,),
        "requires_human_confirmation": True,
    }
    values.update(overrides)
    return CodeReviewWorkPackage(**values)  # type: ignore[arg-type]


def test_public_unused_package_requires_steps_and_calibrated_candidate_evidence() -> None:
    with pytest.raises(ValueError, match="characterization steps only"):
        _public_unused_package(steps=())
    with pytest.raises(ValueError, match="requires one calibrated high-consensus candidate"):
        _public_unused_package(unused_candidates=())
    with pytest.raises((AttributeError, ValueError)):
        _public_unused_package(unused_candidates=(SimpleNamespace(state="insufficient_evidence"),))


@pytest.mark.parametrize(
    "changes",
    (
        {"title": "DELETE production now"},
        {"objective": "delete_candidate_without_further_evidence"},
        {"recommended_validation": ("delete_and_commit_now",)},
        {"contracts_to_preserve": ("none_delete_now",)},
        {"evidence": ("human_approved_delete",)},
        {"limitations": ("safe_to_delete",)},
    ),
)
def test_public_unused_package_rejects_arbitrary_semantic_instructions(
    changes: dict[str, object],
) -> None:
    package = _public_unused_package()

    with pytest.raises(ValueError):
        replace(package, **changes)


def test_result_readiness_cannot_claim_absent_recommendations_or_packages() -> None:
    with pytest.raises(ValueError, match="recommendation status must abstain"):
        CodeReviewResult(
            database="fixture",
            status="ready",
            reason=None,
            ranking="fixture",
            actionability_version="fixture",
            recommendation_status="ready",
            recommendation_reason=None,
            planning_version="fixture",
            work_package_status="abstained",
            work_package_reason="none",
            snapshot=None,
            coverage=None,
            findings=(),
            recommendations=(),
            work_packages=(),
            external_evidence=None,
            external_evidence_suite=None,
            architecture=None,
            test_coverage=None,
            limitations=(),
            digest=None,
        )
    with pytest.raises(ValueError, match="readiness must match published packages"):
        CodeReviewResult(
            database="fixture",
            status="ready",
            reason=None,
            ranking="fixture",
            actionability_version="fixture",
            recommendation_status="abstained",
            recommendation_reason="none",
            planning_version="fixture",
            work_package_status="ready",
            work_package_reason=None,
            snapshot=None,
            coverage=None,
            findings=(),
            recommendations=(),
            work_packages=(),
            external_evidence=None,
            external_evidence_suite=None,
            architecture=None,
            test_coverage=None,
            limitations=(),
            digest=None,
        )


def test_missing_structural_signal_abstains_without_a_question_or_decision() -> None:
    assessment = assess_code_review_actionability(
        CodeReviewActionabilityInput(
            path="/repo/pkg/plain.py",
            symbol="plain.unit",
            root="/repo",
            complexity_ratio_basis_points=0,
            length_ratio_basis_points=0,
            path_convention_production_callers=0,
            path_convention_test_callers=0,
            path_convention_fixture_callers=0,
            path_convention_tool_callers=0,
            path_convention_compatibility_callers=0,
            resolved_static_consumer_files=0,
        )
    )
    epistemic = assessment.epistemic_state

    assert epistemic.observation_status == "abstained"
    assert epistemic.observations == ()
    assert epistemic.question_readiness == "abstained"
    assert epistemic.question_reason == "confirmed_structural_hotspot_missing"
    assert epistemic.decision_readiness == "abstained"
    assert epistemic.decision is None
    assert assessment.actionability == "insufficient_evidence"
    assert assessment.recommended_change is False


def test_ratio_below_declared_threshold_cannot_be_published_as_exceeded() -> None:
    assessment = assess_code_review_actionability(
        replace(
            _input(),
            complexity_ratio_basis_points=9_999,
            length_ratio_basis_points=0,
        )
    )

    assert assessment.epistemic_state.observation_status == "abstained"
    assert assessment.epistemic_state.observations == ()


@pytest.mark.parametrize("invalid", (-1, 1.5, "1", True))
def test_actionability_input_rejects_non_integer_or_negative_counts(invalid: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        replace(  # type: ignore[arg-type]
            _input(),
            path_convention_production_callers=invalid,
        )


def test_required_diagnostic_cannot_claim_a_value_below_its_threshold() -> None:
    with pytest.raises(ValueError, match="does not meet its declared threshold"):
        code_review_module._diagnostic_threshold(
            json.dumps({"value": 4, "threshold": 5}),
            expected_value=4,
            required=True,
            label="fixture",
        )


def test_review_keeps_observations_without_recommendations_or_hotspot_work_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )

    result = review_code_state(state_directory)

    assert result.status == "ready"
    assert result.findings
    assert all(
        finding.epistemic_state.observation_status == "confirmed" for finding in result.findings
    )
    assert all(
        finding.epistemic_state.decision_readiness == "experiment_required"
        for finding in result.findings
    )
    assert all(finding.epistemic_state.decision is None for finding in result.findings)
    assert all(finding.actionability != "act_now" for finding in result.findings)
    assert not any(finding.recommended_change for finding in result.findings)
    assert result.recommendation_status == "abstained"
    assert result.recommendation_reason == (
        "no_evidence_ready_change_decision_within_bounded_findings"
    )
    assert result.recommendations == ()
    assert result.work_package_status == "abstained"
    assert result.work_packages == ()


def test_ready_review_rejects_unlinked_observations_and_stale_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    result = review_code_state(state_directory)
    finding = result.findings[0]

    assert result.digest is not None
    assert finding.diagnostics
    assert all(diagnostic.diagnostic_id > 0 for diagnostic in finding.diagnostics)
    with pytest.raises(ValueError, match="requires diagnostic evidence"):
        replace(finding, diagnostics=())
    with pytest.raises(ValueError, match="observations must be derived"):
        replace(
            finding,
            epistemic_state=replace(
                finding.epistemic_state,
                observations=("invented_observation",),
            ),
        )
    with pytest.raises(ValueError, match="digest disagrees"):
        replace(result, limitations=(*result.limitations, "invented_limitation"))
    with pytest.raises(ValueError, match="lacks evidence required"):
        replace(result, snapshot=None)
    with pytest.raises(ValueError, match="invalid code-review result status"):
        replace(result, status="invented")  # type: ignore[arg-type]
