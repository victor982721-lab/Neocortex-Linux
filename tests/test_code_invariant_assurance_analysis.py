from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.code.code_invariant_assurance_analysis import (
    analyze_code_invariant_assurance,
    invariant_assurance_questions,
    parse_code_invariant_assurance_payload,
)
from neocortex.code.code_invariant_contracts import (
    CALIBRATION_SCENARIO_IDS,
    EXPERIMENT_SCENARIO_IDS,
    INVARIANT_SPECS,
    INVARIANT_RUNTIME_SCENARIOS,
    INVARIANT_SCENARIO_IDS,
    RUNTIME_SCENARIOS,
    invariant_registry_fingerprint,
    invariant_registry_payload,
    runtime_scenario,
    runtime_scenario_registry_fingerprint,
)
from neocortex.code.external_deep_coverage import PYTEST_COVERAGE_PROVIDER_ID
from neocortex.code.external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderRelation,
    external_relation_identity,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _declared_test_targets(path: Path) -> set[tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[tuple[str, ...]] = set()
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.add((item.name,))
        elif isinstance(item, ast.ClassDef):
            targets.update(
                (item.name, member.name)
                for member in item.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return targets


def _relation(nodeid: str, outcome: str, index: int) -> ExternalProviderRelation:
    run_key = "coverage-run:fixture-scope"
    return ExternalProviderRelation(
        external_relation_identity(
            PYTEST_COVERAGE_PROVIDER_ID,
            relation_kind="declared_test_outcome",
            source_kind="contract",
            source_key=f"pytest-nodeid:{nodeid}",
            target_kind="run",
            target_key=run_key,
        ),
        "declared_test_outcome",
        "contract",
        f"pytest-nodeid:{nodeid}",
        "run",
        run_key,
        confidence=1.0,
        metadata={
            "nodeid": nodeid,
            "outcome": outcome,
            "claim_scope": "exact_selected_test_execution_outcome",
            "assertion_or_invariant_proof": False,
            "measurement_scope_signature": "fixture-scope",
            "ordinal": index,
        },
    )


def _provider(outcomes: dict[str, str]) -> ExternalProviderEvidence:
    relations = tuple(
        _relation(nodeid, outcome, index)
        for index, (nodeid, outcome) in enumerate(sorted(outcomes.items()), start=1)
    )
    return ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID,
        17,
        17,
        "ready",
        None,
        (),
        (),
        relations,
    )


def test_registry_is_canonical_versioned_and_scenarios_have_explicit_roles() -> None:
    assert tuple(item.invariant_id for item in INVARIANT_SPECS) == tuple(
        sorted(item.invariant_id for item in INVARIANT_SPECS)
    )
    assert tuple(item.scenario_id for item in RUNTIME_SCENARIOS) == tuple(
        sorted(item.scenario_id for item in RUNTIME_SCENARIOS)
    )
    assert {
        scenario for invariant in INVARIANT_SPECS for scenario in invariant.scenario_ids
    } == set(INVARIANT_SCENARIO_IDS)
    assert set(INVARIANT_SCENARIO_IDS) & set(EXPERIMENT_SCENARIO_IDS) == {
        "semantic.staging_process_death_resume"
    }
    assert (
        set(INVARIANT_SCENARIO_IDS) | set(EXPERIMENT_SCENARIO_IDS) | set(CALIBRATION_SCENARIO_IDS)
    ) == {item.scenario_id for item in RUNTIME_SCENARIOS}
    assert invariant_registry_fingerprint().startswith("code-invariant-registry-v3:xxh3_128:")
    assert runtime_scenario_registry_fingerprint().startswith(
        "code-runtime-scenario-registry-v11:xxh3_128:"
    )
    invariant_payload = invariant_registry_payload()
    assert "runtime_scenario_registry_fingerprint" not in invariant_payload
    assert {item["scenario_id"] for item in invariant_payload["scenarios"]} == set(
        INVARIANT_SCENARIO_IDS
    )
    all_nodeids = tuple(nodeid for item in RUNTIME_SCENARIOS for nodeid in item.test_nodeids)
    assert len(all_nodeids) == len(set(all_nodeids))
    assert runtime_scenario("semantic.staging_process_death_resume").scenario_kind == (
        "process_death"
    )
    public_cli = runtime_scenario("interfaces.public_cli_and_static_surface")
    assert public_cli.version == "v4"
    assert public_cli.scenario_id in EXPERIMENT_SCENARIO_IDS
    assert public_cli.scenario_id not in CALIBRATION_SCENARIO_IDS
    assert len(public_cli.test_nodeids) == 26
    assert tuple(item.gate_id for item in public_cli.gate_specs) == (
        "declared_entrypoint_and_effective_help_contract_are_observed",
        "dynamic_hidden_and_static_surfaces_remain_explicitly_non_equivalent",
        "focal_question_and_storage_reads_are_bounded_and_immutable",
        "invalid_abbreviated_and_incomplete_commands_fail_closed_without_state",
        "special_human_canonical_and_flat_dispatch_precedence_is_exact",
    )
    knowledge_health = runtime_scenario("knowledge.asset_health_causal_acceptance")
    assert knowledge_health.version == "v1"
    assert knowledge_health.scenario_id in EXPERIMENT_SCENARIO_IDS
    assert knowledge_health.scenario_id not in CALIBRATION_SCENARIO_IDS
    assert len(knowledge_health.test_nodeids) == 12
    assert tuple(item.gate_id for item in knowledge_health.gate_specs) == (
        "aligned_four_stage_causal_trace_is_healthy",
        "mismatch_absence_future_corruption_and_unpublished_fail_closed",
        "public_read_is_read_only_and_resource_identity_is_strict",
        "snapshot_change_abstains_and_search_health_identity_is_stable",
    )
    pdf_health = runtime_scenario("knowledge.pdf_asset_health_causal_acceptance")
    assert pdf_health.version == "v1"
    assert pdf_health.scenario_id in EXPERIMENT_SCENARIO_IDS
    assert pdf_health.scenario_id not in CALIBRATION_SCENARIO_IDS
    assert len(pdf_health.test_nodeids) == 12
    assert tuple(item.gate_id for item in pdf_health.gate_specs) == (
        "page_staging_fts_and_catalog_mismatch_fail_closed",
        "recovery_is_version_and_message_independent",
        "typed_pdf_states_preserve_partial_and_protected_semantics",
        "wal_snapshot_and_owner_ambiguity_remain_read_only",
    )
    with pytest.raises(ValueError, match="unknown runtime scenario"):
        runtime_scenario("delete.production.now")


def test_every_runtime_scenario_nodeid_names_a_declared_test_function() -> None:
    targets_by_path: dict[str, set[tuple[str, ...]]] = {}
    missing: list[str] = []
    for scenario in RUNTIME_SCENARIOS:
        for nodeid in scenario.test_nodeids:
            relative_path, *target = nodeid.split("::")
            target[-1] = target[-1].split("[", 1)[0]
            if relative_path not in targets_by_path:
                targets_by_path[relative_path] = _declared_test_targets(
                    _PROJECT_ROOT / relative_path
                )
            targets = targets_by_path[relative_path]
            if tuple(target) not in targets:
                missing.append(nodeid)

    assert missing == []


def test_exact_passing_scenario_receipts_are_observed_but_never_become_a_decision() -> None:
    provider = _provider(
        {nodeid: "passed" for item in INVARIANT_RUNTIME_SCENARIOS for nodeid in item.test_nodeids}
    )

    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: provider},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.status == "ready"
    assert result.resolved_scenarios == result.passed_scenarios == len(INVARIANT_RUNTIME_SCENARIOS)
    assert result.counterevidence_scenarios == 0
    assert all(item.status == "all_declared_scenarios_passed" for item in result.observations)
    assert all(item.observation_status == "confirmed" for item in result.question_evaluations)
    assert all(
        item.decision_readiness == "experiment_required" for item in result.question_evaluations
    )
    assert all(item.decision is None for item in result.question_evaluations)
    assert result.authority == "advisory"
    assert result.mutation_authority is False


def test_failed_scenario_is_preserved_as_counterevidence_not_change_authority() -> None:
    outcomes = {
        nodeid: "passed" for item in INVARIANT_RUNTIME_SCENARIOS for nodeid in item.test_nodeids
    }
    failed_nodeid = INVARIANT_RUNTIME_SCENARIOS[0].test_nodeids[0]
    outcomes[failed_nodeid] = "failed"

    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: _provider(outcomes)},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.counterevidence_scenarios == 1
    failed = next(
        item for item in result.observations if any(s.status == "failed" for s in item.scenarios)
    )
    assert failed.status == "counterevidence_observed"
    evaluation = next(
        item
        for item in result.question_evaluations
        if item.subject.subject_key == f"invariant:{failed.invariant_id}"
    )
    assert evaluation.counterevidence_status == "evaluated"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None


def test_partial_selection_remains_partial_and_unselected_is_not_a_failure() -> None:
    selected = INVARIANT_RUNTIME_SCENARIOS[:2]
    result = analyze_code_invariant_assurance(
        {
            PYTEST_COVERAGE_PROVIDER_ID: _provider(
                {nodeid: "passed" for item in selected for nodeid in item.test_nodeids}
            )
        },
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )

    assert result.status == "partial"
    assert result.resolved_scenarios == 2
    assert result.counterevidence_scenarios == 0
    assert sum(item.missing for item in result.observations) == 2
    assert any(item.status == "scenario_evidence_incomplete" for item in result.observations)


def test_missing_or_abstained_provider_fails_closed_without_nominal_evidence() -> None:
    missing = analyze_code_invariant_assurance(
        {}, snapshot_id="snapshot-fixture", snapshot_freshness="publication_only"
    )
    abstained = analyze_code_invariant_assurance(
        {
            PYTEST_COVERAGE_PROVIDER_ID: ExternalProviderEvidence(
                PYTEST_COVERAGE_PROVIDER_ID,
                9,
                None,
                "abstained",
                "provider_failed",
            )
        },
        snapshot_id="snapshot-fixture",
        snapshot_freshness="publication_only",
    )

    assert missing.status == abstained.status == "abstained"
    assert missing.observations == abstained.observations == ()
    assert missing.question_evaluations == abstained.question_evaluations == ()
    specs, evaluations = invariant_assurance_questions(missing, rank_offset=11)
    assert len(specs) == 1
    assert len(evaluations) == len(INVARIANT_SPECS)
    assert tuple(item.rank for item in evaluations) == tuple(range(12, 12 + len(INVARIANT_SPECS)))
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.question_readiness == "ready" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None for item in evaluations)


def test_forged_test_outcome_relation_and_authority_are_rejected() -> None:
    scenario = INVARIANT_RUNTIME_SCENARIOS[0]
    valid = _relation(scenario.test_nodeids[0], "passed", 1)
    forged = replace(
        valid,
        metadata={**valid.metadata, "assertion_or_invariant_proof": True},
    )
    provider = ExternalProviderEvidence(
        PYTEST_COVERAGE_PROVIDER_ID, 1, 1, "ready", None, (), (), (forged,)
    )

    with pytest.raises(ValueError, match="incompatible"):
        analyze_code_invariant_assurance(
            {PYTEST_COVERAGE_PROVIDER_ID: provider},
            snapshot_id="snapshot-fixture",
            snapshot_freshness="current",
        )


def test_wire_round_trip_and_digest_bound_counts_reject_forgery() -> None:
    provider = _provider(
        {nodeid: "passed" for item in INVARIANT_RUNTIME_SCENARIOS for nodeid in item.test_nodeids}
    )
    result = analyze_code_invariant_assurance(
        {PYTEST_COVERAGE_PROVIDER_ID: provider},
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
    )
    payload = json.loads(json.dumps(result.as_payload()))

    assert parse_code_invariant_assurance_payload(payload) == result

    payload["passed_scenarios"] = 0
    with pytest.raises(ValueError, match="pass count"):
        parse_code_invariant_assurance_payload(payload)
