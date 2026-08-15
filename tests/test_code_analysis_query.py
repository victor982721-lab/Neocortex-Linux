"""Functional, adversarial and property-style contracts for Code queries."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from _04_Nucleo_Operativo.code_analysis_query import (
    CODE_ANALYSIS_QUERY_MAX_SOURCE_SEQUENCE_ITEMS,
    CODE_ANALYSIS_QUERY_SCHEMA,
    CodeAnalysisQuery,
    query_code_analysis,
)

FIXTURE = Path(__file__).parent / "fixtures" / "code_analysis_query" / "public_surfaces_v1.json"


def _fixture() -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _surface(name: str) -> dict[str, object]:
    value = _fixture()[name]
    assert isinstance(value, dict)
    return value


def _closed_v20_experiment_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    import _04_Nucleo_Operativo.code_analysis_query as query_module
    import _04_Nucleo_Operativo.code_review as review_module
    import _04_Nucleo_Operativo.code_review_epistemics as epistemics_module
    from _04_Nucleo_Operativo.code_experiment_store import (
        code_review_digest_identity,
        record_code_experiment_receipt,
    )
    from tests.test_code_experiment_store import _question, _receipt
    from tests.test_code_review import PROCESSING_SIGNATURE, _build_state, _status

    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    spec, evaluation = _question(PROCESSING_SIGNATURE)

    def exact_questions(*_args: object, **_kwargs: object):
        return (spec,), (evaluation,)

    def no_questions(*_args: object, **_kwargs: object):
        return (), ()

    monkeypatch.setattr(review_module, "read_self_analysis_status", lambda *_: _status(tmp_path))
    monkeypatch.setattr(
        review_module,
        "expected_integrated_code_review_questions",
        exact_questions,
    )
    monkeypatch.setattr(
        epistemics_module,
        "expected_integrated_code_review_questions",
        exact_questions,
    )
    for name in (
        "architecture_questions",
        "interface_surface_questions",
        "state_projection_questions",
        "state_topology_questions",
        "retention_questions",
        "state_interaction_questions",
        "expected_code_change_evolution_questions",
        "assurance_questions",
        "invariant_assurance_questions",
        "framework_review_task_questions",
        "security_dependency_questions",
        "route_capability_questions",
        "analyzer_effectiveness_questions",
        "analyzer_calibration_questions",
    ):
        monkeypatch.setattr(query_module, name, no_questions)
    monkeypatch.setattr(query_module, "capability_reachability_questions", exact_questions)

    before = review_module.review_code_state(state_directory, limit=1)
    assert before.snapshot is not None
    assert before.digest is not None
    assert before.experiment_plan is not None
    executable = tuple(
        proposal
        for proposal in before.experiment_plan.proposals
        if proposal.planning_status == "planned" and proposal.runner_kind != "none"
    )
    assert len(executable) == 1
    proposal = executable[0]
    record_code_experiment_receipt(
        database,
        _receipt(proposal, source_version=PROCESSING_SIGNATURE),
        proposal,
        analysis_run_id=before.snapshot.analysis_run_id,
        processing_signature=PROCESSING_SIGNATURE,
        review_digest=code_review_digest_identity(before.digest),
        recorded_ns=123,
    )
    after = review_module.review_code_state(state_directory, limit=1)
    return json.loads(json.dumps(after.as_payload()))


def test_review_query_combines_all_dimensions_without_a_magic_score() -> None:
    query = CodeAnalysisQuery(
        surface="review",
        providers=("COSMIC-RAY-FOCAL-MUTATION",),
        categories=("mutation",),
        modules=("PKG.WORKER",),
        statuses=("passed",),
        work_packages=("WP-1",),
        limit=10,
    )

    first = query_code_analysis(_surface("review"), query)
    second = query_code_analysis(_surface("review"), query)

    assert first == second
    assert first["schema"] == CODE_ANALYSIS_QUERY_SCHEMA
    assert first["status"] == "ready"
    assert first["authority"] == "advisory"
    assert first["mutation_authority"] is False
    assert first["aggregate_score"] is None
    assert first["defect_probability"] is None
    assert first["filters"] == {
        "providers": ["cosmic-ray-focal-mutation"],
        "categories": ["mutation"],
        "modules": ["pkg.worker"],
        "statuses": ["passed"],
        "deltas": [],
        "work_packages": ["wp-1"],
    }
    assert first["counts"] == {
        "available": 4,
        "matched": 1,
        "returned": 1,
        "truncated": False,
    }
    matches = first["matches"]
    assert isinstance(matches, list)
    assert matches[0]["record_type"] == "work_package"
    assert matches[0]["facts"]["primary_module"] == "pkg.worker"
    serialized = json.dumps(first, sort_keys=True)
    assert '"aggregate_score": null' in serialized
    assert '"defect_probability": null' in serialized


def test_review_query_keeps_question_and_decision_readiness_dimensioned() -> None:
    payload = {
        "kind": "code-review",
        "schema": "neocortex.code-review/v11",
        "status": "ready",
        "reason": None,
        "recommendation_status": "abstained",
        "recommendation_reason": "no_evidence_ready_change_decision",
        "recommendations": [],
        "work_package_status": "abstained",
        "work_package_reason": "no_calibrated_characterization_candidate",
        "work_packages": [],
        "snapshot": {"processing_signature": "fixture"},
        "coverage": {"candidate_hotspots": 1},
        "digest": {"xxh3_128": "fixture"},
        "findings": [
            {
                "finding_id": "finding-1",
                "category": "long_function_hotspot",
                "actionability": "characterize_first",
                "construction": "unknown",
                "observation_confidence": "confirmed_static_evidence",
                "change_risk": "unknown",
                "recommended_change": False,
                "diagnostics": [{"code": "long_function"}],
                "epistemic_state": {
                    "observation_status": "confirmed",
                    "inference_status": "abstained",
                    "question_readiness": "ready",
                    "decision_readiness": "experiment_required",
                    "decision": None,
                    "authority": "advisory",
                    "mutation_authority": False,
                },
            }
        ],
    }

    question = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("question:ready",)),
    )
    bare_ready = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("ready",)),
    )
    decision = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("decision:experiment_required",)),
    )

    assert question["counts"]["matched"] == 1
    assert decision["counts"]["matched"] == 1
    assert bare_ready["counts"]["matched"] == 0


def test_review_query_rejects_a_forged_v11_change_package() -> None:
    payload = {
        "kind": "code-review",
        "schema": "neocortex.code-review/v11",
        "status": "ready",
        "reason": None,
        "recommendation_status": "abstained",
        "recommendation_reason": "no_evidence_ready_change_decision",
        "recommendations": [],
        "work_package_status": "ready",
        "work_package_reason": None,
        "snapshot": {"processing_signature": "forged"},
        "coverage": {"candidate_hotspots": 1},
        "digest": {"xxh3_128": "forged"},
        "findings": [],
        "work_packages": [
            {
                "package_id": "forged",
                "package_kind": "hotspot_maintenance",
                "title": "Delete now",
                "objective": "delete_production_symbol",
                "requires_human_confirmation": False,
                "mutation_authority": True,
                "steps": [{"phase": "change", "requirement": "delete"}],
            }
        ],
    }

    with pytest.raises(ValueError, match="characterization-only policy"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


@pytest.mark.parametrize("schema", (None, "neocortex.code-review/unknown"))
def test_review_query_rejects_missing_unknown_or_future_schemas(schema: object) -> None:
    payload = deepcopy(_surface("review"))
    if schema is None:
        payload.pop("schema", None)
    else:
        payload["schema"] = schema

    with pytest.raises(ValueError, match="unsupported code-review schema"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_accepts_and_indexes_source_linked_v13_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("maintenance.structural_hotspot_requires_change",),
            statuses=("decision:experiment_required",),
        ),
    )

    assert result["status"] == "ready"
    assert result["counts"]["matched"] == 1
    assert result["matches"][0]["record_type"] == "analysis_question"
    facts = result["matches"][0]["facts"]
    assert facts["mutation_authority"] is False
    assert facts["decision_readiness"] == "experiment_required"
    assert facts["requirements"]
    assert "behavior_or_contract_problem_observed" in facts["missing_requirement_ids"]
    assert facts["next_action_ids"]
    assert [item["action_id"] for item in facts["next_actions"]] == facts["next_action_ids"]
    assert {item["kind"] for item in facts["next_actions"]} == {
        "characterization",
        "counterevidence_search",
        "experiment",
    }
    assert all(item["description"] for item in facts["next_actions"])
    assert facts["proposal_id"]
    assert facts["proposal_executable"] is False
    assert facts["selected_action"]["action_id"] == facts["selected_action_id"]
    assert facts["template_limitations"]
    assert facts["manual_reason"] == "registered_template_has_no_allowlisted_runner"

    manual = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("maintenance.structural_hotspot_requires_change",),
            statuses=("execution:manual",),
            limit=10,
        ),
    )
    assert [item["record_type"] for item in manual["matches"]] == ["analysis_question"]
    manual_proposals = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("experiment-question:maintenance.structural_hotspot_requires_change",),
            statuses=("execution:manual",),
            limit=10,
        ),
    )
    assert [item["record_type"] for item in manual_proposals["matches"]] == ["experiment_proposal"]
    proposal = manual_proposals["matches"][0]
    assert proposal["facts"]["evaluation_id"] == facts["evaluation_id"]
    assert proposal["facts"]["missing_requirement_ids"]
    assert proposal["facts"]["executable"] is False
    assert proposal["facts"]["selected_action"]["kind"] == "experiment"
    assert proposal["facts"]["alternative_actions"]
    assert proposal["facts"]["template_limitations"] == [
        "static_characterization_does_not_select_a_refactor",
        "clusters_and_consumers_do_not_prove_product_intent",
    ]
    assert proposal["facts"]["manual_reason"] == ("registered_template_has_no_allowlisted_runner")

    summary_result = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("summary",),
            limit=10,
        ),
    )
    assert summary_result["counts"]["matched"] == 1
    summary = summary_result["matches"][0]
    assert summary["record_type"] == "experiment_plan_summary"
    summary_facts = summary["facts"]
    assert summary_facts["planning_coverage"] == "partial"
    assert summary_facts["execution_readiness"] == "partially_executable"
    assert summary_facts["manual_count"] == (
        summary_facts["planned_count"] - summary_facts["executable_count"]
    )
    assert summary_facts["limitations"]
    assert summary["dimensions"]["statuses"] == [
        "execution:partially_executable",
        "experiment:partial",
        "planning:partial",
    ]


def test_review_query_rejects_forged_v20_evidence_linkage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = review_code_state(state_directory, limit=1).as_payload()
    epistemics = cast("dict[str, object]", payload["epistemics"])
    evaluations = cast("list[dict[str, object]]", epistemics["evaluations"])
    evidence = cast("list[dict[str, object]]", evaluations[0]["evidence"])
    evidence[0]["source_record_id"] = "forged"

    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_rejects_forged_v20_question_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))
    epistemics = cast("dict[str, object]", payload["epistemics"])
    specs = cast("list[dict[str, object]]", epistemics["specs"])
    actions = cast("list[dict[str, object]]", specs[0]["next_actions"])
    actions[0]["description"] = "Delete the production symbol now."

    with pytest.raises(ValueError, match="v20 integrated projection is malformed"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


@pytest.mark.parametrize(
    ("projection", "field"),
    (
        ("interface_surface", "total_modules"),
        ("supply_chain", "analysis_run_id"),
        ("state_interactions", "literal_sql_sites"),
        ("analyzer_calibration", "labels_total"),
    ),
)
def test_review_query_rejects_tampered_v20_integrated_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    projection: str,
    field: str,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))
    receipt = cast("dict[str, object]", payload[projection])
    current = receipt[field]
    assert isinstance(current, int) and not isinstance(current, bool)
    receipt[field] = current + 1

    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_rejects_a_tampered_v20_retention_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))
    retention = cast("dict[str, object]", payload["retention_analysis"])
    retention["analysis_id"] = "code-retention-analysis-v1:forged"

    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_rejects_a_tampered_v20_experiment_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))
    plan = cast("dict[str, object]", payload["experiment_plan"])
    plan["executable_count"] = 999

    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


@pytest.mark.parametrize(
    "schema",
    (
        "neocortex.code-review/v16",
        "neocortex.code-review/v17",
        "neocortex.code-review/v18",
        "neocortex.code-review/v19",
        "neocortex.code-review/v20",
    ),
)
def test_review_query_preserves_abstained_v16_v20_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: str,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from _04_Nucleo_Operativo.code_review_epistemics import CodeReviewEvidenceResolutionError
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    monkeypatch.setattr(
        review_module,
        "resolve_code_review_questions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CodeReviewEvidenceResolutionError("source record changed")
        ),
    )
    payload = review_code_state(state_directory).as_payload()
    assert payload["status"] == "abstained"
    payload["schema"] = schema

    result = query_code_analysis(payload, CodeAnalysisQuery(surface="review"))

    assert result["status"] == "abstained"
    assert result["matches"] == []


def test_review_query_preserves_ready_v20_fail_closed_validation_order_and_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))

    oversized = deepcopy(payload)
    oversized["findings"] = [{}] * (CODE_ANALYSIS_QUERY_MAX_SOURCE_SEQUENCE_ITEMS + 1)
    oversized["structural_analysis"] = None
    with pytest.raises(ValueError, match="v20 integrated projection is malformed") as bounded:
        query_code_analysis(oversized, CodeAnalysisQuery(surface="review"))
    assert bounded.value.__cause__ is not None
    assert str(bounded.value.__cause__) == "query source sequence exceeds its item bound"

    missing_integrated = deepcopy(payload)
    missing_integrated["state_topology"] = None
    snapshot = cast("dict[str, object]", missing_integrated["snapshot"])
    snapshot["analysis_run_id"] = False
    with pytest.raises(ValueError, match="v20 integrated projection is malformed") as staged:
        query_code_analysis(missing_integrated, CodeAnalysisQuery(surface="review"))
    assert staged.value.__cause__ is not None
    assert str(staged.value.__cause__) == (
        "ready code-review/v20 payload lacks integrated evidence"
    )


def test_review_query_projects_a_valid_v20_receipt_and_closes_the_experiment_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _closed_v20_experiment_payload(tmp_path, monkeypatch)

    closed_question = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("capability.route_reaches_user_visible_outcome",),
            statuses=("decision:human_review_required",),
        ),
    )
    assert closed_question["counts"]["matched"] == 1
    question = closed_question["matches"][0]
    assert question["record_type"] == "analysis_question"
    assert question["facts"]["decision_readiness"] == "human_review_required"
    assert question["facts"].get("decision") is None
    assert question["facts"]["next_action_ids"] == []
    assert question["facts"]["next_actions"] == []
    assert question["facts"]["proposal_executable"] is False

    experiment_records = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", categories=("experiment_plan",), limit=20),
    )
    assert [item["record_type"] for item in experiment_records["matches"]] == [
        "experiment_plan_summary",
        "experiment_receipt",
    ]
    summary, receipt = experiment_records["matches"]
    assert summary["facts"]["status"] == "not_required"
    assert summary["facts"]["planning_coverage"] == "not_required"
    assert summary["facts"]["execution_readiness"] == "not_required"
    assert summary["facts"]["experiment_required_count"] == 0
    assert summary["facts"]["planned_count"] == 0
    assert summary["facts"]["executable_count"] == 0
    assert receipt["dimensions"]["statuses"] == [
        "database:unchanged",
        "provider:completed",
        "receipt:passed",
    ]
    assert receipt["facts"]["question_id"] == ("capability.route_reaches_user_visible_outcome")
    assert receipt["facts"]["status"] == "passed"
    assert receipt["facts"]["code_database_unchanged"] is True
    assert receipt["facts"]["gate_outcomes_count"] > 0
    assert receipt["facts"]["gate_outcomes_truncated"] is False
    assert receipt["facts"]["gate_states"]
    assert receipt["facts"]["limitations"]
    assert receipt["facts"]["mutation_authority"] is False
    assert all(
        item["record_type"] != "experiment_proposal" for item in experiment_records["matches"]
    )

    technical_records = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", categories=("technical_verification",), limit=20),
    )
    technical_by_type = {
        item["record_type"]: item for item in technical_records["matches"]
    }
    assert set(technical_by_type) == {
        "technical_verification_summary",
        "technical_verification_gap",
    }
    technical_summary = technical_by_type["technical_verification_summary"]
    technical_gap = technical_by_type["technical_verification_gap"]
    assert technical_summary["facts"]["status"] == "partial"
    assert technical_summary["facts"]["reviewed_count"] == 0
    assert technical_summary["facts"]["unresolved_count"] == 1
    assert technical_gap["facts"]["reason"] == (
        "technical_policy_scope_or_question_contract_changed"
    )


def test_review_query_rejects_a_tampered_v20_receipt_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _closed_v20_experiment_payload(tmp_path, monkeypatch)
    tampered = deepcopy(payload)
    receipts = cast("list[dict[str, object]]", tampered["experiment_receipts"])
    receipts[0]["payload_xxh3_128"] = "forged"

    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(tampered, CodeAnalysisQuery(surface="review"))

    future_owner = deepcopy(payload)
    future_receipts = cast("list[dict[str, object]]", future_owner["experiment_receipts"])
    snapshot = cast("dict[str, object]", future_owner["snapshot"])
    analysis_run_id = snapshot["analysis_run_id"]
    assert isinstance(analysis_run_id, int) and not isinstance(analysis_run_id, bool)
    from dataclasses import replace

    from _04_Nucleo_Operativo.code_experiment_store import (
        parse_resolved_code_experiment_receipt_payload,
    )

    future_receipts[0] = replace(
        parse_resolved_code_experiment_receipt_payload(future_receipts[0]),
        analysis_run_id=analysis_run_id + 1,
    ).as_payload()
    with pytest.raises(ValueError, match="receipt snapshot is inconsistent"):
        query_code_analysis(future_owner, CodeAnalysisQuery(surface="review"))

    missing_sequence = deepcopy(payload)
    missing_sequence.pop("experiment_receipts")
    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(missing_sequence, CodeAnalysisQuery(surface="review"))

    forged_technical = deepcopy(payload)
    technical = cast("dict[str, object]", forged_technical["technical_verification"])
    technical["reviewed_count"] = 1
    with pytest.raises(ValueError, match="code-review/v20"):
        query_code_analysis(forged_technical, CodeAnalysisQuery(surface="review"))


@pytest.mark.parametrize(
    (
        "status",
        "required",
        "planned",
        "executable",
        "gaps",
        "planning_coverage",
        "execution_readiness",
    ),
    (
        ("ready", 2, 2, 2, 0, "complete", "all_executable"),
        ("ready", 2, 2, 0, 0, "complete", "manual_only"),
        ("partial", 3, 2, 0, 1, "partial", "manual_with_registry_gaps"),
        ("partial", 3, 1, 1, 2, "partial", "partially_executable"),
        ("partial", 2, 0, 0, 2, "partial", "registry_gap_only"),
        ("not_required", 0, 0, 0, 0, "not_required", "not_required"),
        ("abstained", 0, 0, 0, 0, "abstained", "abstained"),
    ),
)
def test_experiment_plan_summary_separates_coverage_from_execution(
    status: str,
    required: int,
    planned: int,
    executable: int,
    gaps: int,
    planning_coverage: str,
    execution_readiness: str,
) -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    records: list[dict[str, object]] = []
    query_module._append_experiment_plan_summary(
        records,
        {
            "plan_id": f"plan-{status}-{required}-{executable}",
            "status": status,
            "reason": "bounded_fixture" if status in {"not_required", "abstained"} else None,
            "policy_id": "fixture-policy",
            "registry_fingerprint": "fixture-registry",
            "source_evaluation_count": required,
            "experiment_required_count": required,
            "planned_count": planned,
            "executable_count": executable,
            "registry_gap_count": gaps,
            "proposals": [],
            "limitations": ["fixture_does_not_assert_product_truth"],
            "authority": "advisory",
            "mutation_authority": False,
        },
    )

    assert len(records) == 1
    record = records[0]
    assert record["record_type"] == "experiment_plan_summary"
    facts = cast("dict[str, object]", record["facts"])
    assert facts["planning_coverage"] == planning_coverage
    assert facts["execution_readiness"] == execution_readiness
    assert facts["manual_count"] == planned - executable
    dimensions = cast("dict[str, list[str]]", record["dimensions"])
    assert f"planning:{planning_coverage}" in dimensions["statuses"]
    assert f"execution:{execution_readiness}" in dimensions["statuses"]


def test_experiment_plan_summary_rejects_counts_that_invent_readiness() -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    with pytest.raises(ValueError, match="cannot derive query readiness"):
        query_module._append_experiment_plan_summary(
            [],
            {
                "status": "ready",
                "experiment_required_count": 1,
                "planned_count": 0,
                "executable_count": 1,
                "registry_gap_count": 0,
                "limitations": [],
            },
        )


def test_status_query_supports_exact_or_descendant_module_matching() -> None:
    result = query_code_analysis(
        _surface("status"),
        CodeAnalysisQuery(surface="status", modules=("pkg.worker",), limit=100),
    )

    modules = {module for match in result["matches"] for module in match["dimensions"]["modules"]}
    assert "pkg.worker" in modules
    assert "pkg.worker.child" in modules
    assert all(module == "pkg.worker" or module.startswith("pkg.worker.") for module in modules)
    assert result["source"] == {
        "kind": "code-status",
        "schema": "neocortex.code-status/schema-v4",
        "digest": "fixture-processing-v1",
    }


def test_diff_query_exposes_provider_category_status_and_delta_filters() -> None:
    provider = query_code_analysis(
        _surface("diff"),
        CodeAnalysisQuery(
            surface="diff",
            providers=("cosmic-ray-focal-mutation",),
            statuses=("comparable",),
            deltas=("added",),
        ),
    )
    category = query_code_analysis(
        _surface("diff"),
        CodeAnalysisQuery(
            surface="diff",
            categories=("dependency_hygiene",),
            deltas=("increased",),
        ),
    )

    assert provider["counts"]["matched"] == 1
    assert provider["matches"][0]["record_type"] == "provider_delta"
    assert category["counts"]["matched"] == 1
    assert category["matches"][0]["record_type"] == "supply_chain_category_delta"


def test_diff_query_exposes_typed_relocation_with_exact_positions() -> None:
    payload = {
        "kind": "code-publication-diff",
        "schema": "neocortex.code-publication-diff/v10",
        "status": "ready",
        "providers": [
            {
                "provider_id": "mypy-trusted-project",
                "status": "ready",
                "common": 3,
                "added": 0,
                "resolved": 0,
                "relocated": 1,
                "gate": "passed",
                "relocation_examples": [
                    {
                        "baseline_finding_id": "finding-before",
                        "current_finding_id": "finding-after",
                        "path": "pkg/checks.py",
                        "category": "typing",
                        "code": "return-value",
                        "severity": "error",
                        "message": "Expected str",
                        "baseline_start_line": 10,
                        "baseline_start_column": 4,
                        "baseline_end_line": 10,
                        "baseline_end_column": 12,
                        "current_start_line": 12,
                        "current_start_column": 4,
                        "current_end_line": 12,
                        "current_end_column": 12,
                    }
                ],
            }
        ],
    }

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="diff",
            providers=("mypy-trusted-project",),
            categories=("typing",),
            modules=("pkg.checks",),
            deltas=("relocated",),
        ),
    )

    assert result["counts"]["matched"] == 1
    match = result["matches"][0]
    assert match["record_type"] == "provider_finding_relocation"
    facts = match["facts"]
    assert isinstance(facts, dict)
    assert facts["message"] == "Expected str"
    assert facts["baseline_start_line"] == 10
    assert facts["current_start_line"] == 12


def test_limit_is_hard_and_reports_honest_truncation() -> None:
    result = query_code_analysis(
        _surface("status"),
        CodeAnalysisQuery(surface="status", limit=1),
    )

    counts = result["counts"]
    matches = result["matches"]
    assert isinstance(counts, dict)
    assert isinstance(matches, list)
    available = counts["available"]
    assert isinstance(available, int)
    assert available > 1
    assert counts["matched"] == available
    assert counts["returned"] == len(matches) == 1
    assert counts["truncated"] is True


def test_normalization_is_idempotent_and_filtering_is_monotonic() -> None:
    normalized = CodeAnalysisQuery(
        surface=cast("object", " REVIEW "),  # type: ignore[arg-type]
        providers=("MYPY-TRUSTED-PROJECT", "mypy-trusted-project"),
        categories=(" Finding ", "finding"),
    )
    again = CodeAnalysisQuery(
        surface=normalized.surface,
        providers=normalized.providers,
        categories=normalized.categories,
    )
    assert normalized == again
    assert normalized.providers == ("mypy-trusted-project",)
    assert normalized.categories == ("finding",)

    payload = _surface("status")
    unfiltered = query_code_analysis(payload, CodeAnalysisQuery(surface="status", limit=500))
    randomizer = random.Random(20260803)
    choices = (
        ("providers", "cosmic-ray-focal-mutation"),
        ("providers", "complexipy-cognitive"),
        ("categories", "engineering"),
        ("categories", "dependency_hygiene"),
        ("modules", "pkg.worker"),
        ("statuses", "ready"),
    )
    for _ in range(40):
        name, value = randomizer.choice(choices)
        arguments: dict[str, object] = {name: (value,)}
        query = CodeAnalysisQuery(surface="status", limit=500, **arguments)  # type: ignore[arg-type]
        first = query_code_analysis(payload, query)
        second = query_code_analysis(payload, query)
        assert first == second
        first_counts = first["counts"]
        unfiltered_counts = unfiltered["counts"]
        assert isinstance(first_counts, dict)
        assert isinstance(unfiltered_counts, dict)
        first_matched = first_counts["matched"]
        unfiltered_matched = unfiltered_counts["matched"]
        assert isinstance(first_matched, int)
        assert isinstance(unfiltered_matched, int)
        assert first_matched <= unfiltered_matched


@pytest.mark.parametrize(
    "constructor",
    (
        lambda: CodeAnalysisQuery(surface=cast("object", "unknown")),
        lambda: CodeAnalysisQuery(surface="status", providers=(" ",)),
        lambda: CodeAnalysisQuery(surface="status", providers=cast("object", ["ruff"])),
        lambda: CodeAnalysisQuery(surface="status", limit=0),
        lambda: CodeAnalysisQuery(surface="status", limit=501),
        lambda: CodeAnalysisQuery(surface="status", limit=cast("object", True)),
    ),
)
def test_malformed_queries_fail_closed(constructor: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        cast("object", constructor)()  # type: ignore[operator]


def test_filter_quantity_and_utf8_byte_bounds_fail_closed_before_echo() -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    with pytest.raises(ValueError, match="exceed 32 values"):
        CodeAnalysisQuery(
            surface="status",
            categories=tuple(f"category-{index}" for index in range(33)),
        )
    with pytest.raises(ValueError, match="512 UTF-8 bytes"):
        CodeAnalysisQuery(surface="status", categories=("é" * 257,))
    with pytest.raises(ValueError, match="64 total values"):
        CodeAnalysisQuery(
            surface="status",
            providers=tuple(f"provider-{index}" for index in range(32)),
            categories=tuple(f"category-{index}" for index in range(32)),
            modules=("one-more",),
        )
    oversized_total = tuple(("x" * 510) + f"{index:02}" for index in range(17))
    with pytest.raises(ValueError, match="8192 total UTF-8 bytes"):
        CodeAnalysisQuery(surface="status", categories=oversized_total)

    exact_total = tuple(("x" * 510) + f"{index:02}" for index in range(16))
    accepted = CodeAnalysisQuery(surface="status", categories=exact_total)
    assert sum(len(item.encode("utf-8")) for item in accepted.categories) == (
        query_module.CODE_ANALYSIS_QUERY_MAX_FILTER_BYTES_TOTAL
    )


def test_query_output_applies_a_public_json_byte_bound_with_honest_counts() -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    repeated = "x" * 512
    modules = [
        {
            "module_id": f"pkg.module_{index:04}",
            "path_namespace_id": repeated,
            "owner_id": repeated,
            "fan_in": repeated,
            "fan_out": repeated,
            "blast_radius": repeated,
            "dependency_reach": repeated,
            "cross_path_namespace_fan_in": repeated,
            "cross_path_namespace_fan_out": repeated,
            "cross_owner_fan_in": repeated,
            "cross_owner_fan_out": repeated,
            "directed_degree_centrality": repeated,
            "cognitive_complexity_max": repeated,
            "cognitive_complexity_total": repeated,
        }
        for index in range(500)
    ]
    payload = {
        "kind": "code-status",
        "schema": "neocortex.code-status/v1",
        "exists": True,
        "latest_run": {"analysis_run_id": 1, "status": "completed"},
        "self_analysis": {
            "manifest_status": "valid",
            "freshness": {"current": True},
        },
        "external_evidence_suite": {"status": "ready", "providers": []},
        "architecture": {
            "status": "ready",
            "gate": "observed",
            "analysis_run_id": 1,
            "modules": modules,
        },
    }

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="status", categories=("architecture",), limit=500),
    )
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True).encode("utf-8") + b"\n"

    assert len(encoded) <= query_module.CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES
    assert result["output_bound"] == {
        "max_public_json_bytes": query_module.CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES,
        "byte_truncated": True,
    }
    assert result["counts"]["available"] == 500
    assert result["counts"]["matched"] == 500
    assert 0 < result["counts"]["returned"] < 500
    assert result["counts"]["truncated"] is True
    assert "query_output_byte_bound_applied" in result["limitations"]


def test_wrong_surface_and_arbitrary_nested_fields_are_not_projected() -> None:
    payload = _surface("review")
    payload["private_extension"] = {
        "secret": "must-not-leak",
        "aggregate_score": 999,
        "defect_probability": 1.0,
    }
    payload["experiment_plan"] = {
        "plan_id": "forged-legacy-extension",
        "status": "ready",
        "experiment_required_count": 1,
        "planned_count": 1,
        "executable_count": 1,
        "registry_gap_count": 0,
        "proposals": [
            {
                "proposal_id": "must-not-be-projected",
                "runner_kind": "pytest_nodeids",
            }
        ],
    }
    result = query_code_analysis(payload, CodeAnalysisQuery(surface="review", limit=500))
    serialized = json.dumps(result, sort_keys=True)

    assert "must-not-leak" not in serialized
    assert "forged-legacy-extension" not in serialized
    assert "must-not-be-projected" not in serialized
    assert all(item["record_type"] != "experiment_plan_summary" for item in result["matches"])
    assert "999" not in serialized
    assert result["aggregate_score"] is None
    assert result["defect_probability"] is None

    wrong = deepcopy(payload)
    wrong["kind"] = "code-status"
    with pytest.raises(ValueError, match="requires kind"):
        query_code_analysis(wrong, CodeAnalysisQuery(surface="review"))


def test_missing_status_state_abstains_without_creating_evidence() -> None:
    result = query_code_analysis(
        {
            "kind": "code-status",
            "schema_version": 4,
            "exists": False,
            "limitations": ["code_state_missing"],
        },
        CodeAnalysisQuery(surface="status"),
    )

    assert result["status"] == "abstained"
    assert result["matches"] == []
    assert "code_state_missing" in result["limitations"]


def test_status_query_marks_stale_publication_and_keeps_bounded_architecture_summary() -> None:
    payload = {
        "kind": "code-status",
        "schema": "neocortex.code-status/v1",
        "exists": True,
        "latest_run": {"analysis_run_id": 77, "status": "completed"},
        "self_analysis": {
            "manifest_status": "valid",
            "freshness": {"current": False},
        },
        "external_evidence_suite": {"status": "abstained", "providers": []},
        "architecture": {
            "status": "ready",
            "gate": "passed",
            "analysis_run_id": 77,
            "counts": {"modules": 359, "symbols": 7_397, "imports": 1_581},
        },
    }

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="status", categories=("architecture",)),
    )

    assert result["status"] == "abstained"
    assert result["counts"] == {
        "available": 1,
        "matched": 1,
        "returned": 1,
        "truncated": False,
    }
    assert result["matches"][0]["record_type"] == "architecture_summary"
    assert result["matches"][0]["facts"]["modules"] == 359
    assert "self_analysis_freshness_not_current" in result["limitations"]
    assert "external_evidence_suite_status_abstained" in result["limitations"]


def test_diff_extractor_signature_order_and_exact_fixture_are_frozen() -> None:
    from hashlib import sha256
    from inspect import signature

    import _04_Nucleo_Operativo.code_analysis_query as query_module

    assert str(signature(query_module._extract_diff)) == (
        "(payload: 'Mapping[str, object]') -> 'list[dict[str, object]]'"
    )
    payload = _surface("diff")
    before = deepcopy(payload)

    records = query_module._extract_diff(payload)

    assert payload == before
    assert records == query_module._extract_diff(deepcopy(payload))
    assert [(record["record_type"], record["id"], record["source_path"]) for record in records] == [
        (
            "provider_delta",
            "providers[0]:cosmic-ray-focal-mutation",
            "providers[0]",
        ),
        (
            "architecture_module_delta",
            "architecture.modules[0]:pkg.worker",
            "architecture.modules[0]",
        ),
        (
            "hotspot_delta",
            "hotspots.added_examples[0]:pkg/new_module.py::new_module.hotspot",
            "hotspots.added_examples[0]",
        ),
        (
            "hotspot_delta",
            "hotspots.removed_examples[0]:pkg/worker.py::worker.old",
            "hotspots.removed_examples[0]",
        ),
        (
            "supply_chain_category_delta",
            "supply_chain.categories[0]:dependency_hygiene",
            "supply_chain.categories[0]",
        ),
        (
            "supply_chain_provider_delta",
            "supply_chain.providers[0]:deptry-project-dependencies",
            "supply_chain.providers[0]",
        ),
        (
            "coverage_delta",
            "test_coverage:pytest-coverage-trusted-deep",
            "test_coverage",
        ),
        (
            "engineering_dimension_delta",
            "engineering_analytics.dimensions[0]:mutation",
            "engineering_analytics.dimensions[0]",
        ),
    ]
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert sha256(encoded).hexdigest() == (
        "d2288e5e152abbeaaac93be78699c52cca07a6a229f22b80593f65ecf055f4e6"
    )


def test_diff_extractor_ignores_unsupported_and_malformed_sections() -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    assert query_module._extract_diff({}) == []
    records = query_module._extract_diff(
        {
            "providers": "not-a-sequence",
            "architecture": ["not-a-mapping"],
            "hotspots": {"added_examples": [None, 3, {"private": "ignored"}]},
            "supply_chain": {"categories": "not-a-sequence"},
            "test_coverage": "not-a-mapping",
            "engineering_analytics": {"dimensions": [None, "invalid"]},
            "private_extension": {"secret": "must-not-leak"},
        }
    )
    assert [record["id"] for record in records] == ["hotspots.added_examples[2]:2"]
    assert records[0]["facts"] == {}
    assert "must-not-leak" not in json.dumps(records, sort_keys=True)
    assert "ignored" not in json.dumps(records, sort_keys=True)
