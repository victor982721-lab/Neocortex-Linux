from __future__ import annotations

import json
from dataclasses import replace

import pytest

from _04_Nucleo_Operativo.code_architecture_analysis import (
    ArchitectureContract,
    ArchitectureGateEvaluation,
    ArchitectureImportEdge,
    ArchitectureModule,
    ArchitectureProviderStatus,
    ArchitectureSummary,
    CodeArchitectureAnalysis,
)
from _04_Nucleo_Operativo.code_architecture_questions import (
    architecture_questions,
    parse_code_architecture_question_payload,
)


def _provider(provider_id: str) -> ArchitectureProviderStatus:
    return ArchitectureProviderStatus(
        provider_id=provider_id,
        status="ready",
        reason=None,
        tool_name=provider_id.partition("-")[0],
        tool_version="fixture-v1",
        provider_schema=f"neocortex.{provider_id}/v1",
        comparability_signature=f"{provider_id}-fixture",
        provider_gate="passed",
        execution="full",
        tool_run_id=1,
        source_tool_run_id=1,
        metrics=1,
        relations=1,
    )


def _module(module_id: str, namespace: str) -> ArchitectureModule:
    return ArchitectureModule(
        module_id=module_id,
        fan_in=0,
        fan_out=0,
        cognitive_complexity_total=1.0,
        cognitive_complexity_max=1.0,
        cognitive_symbol_count=1,
        cycle_ids=(),
        contract_ids=("fixture-boundary-v1",),
        grimp_fan_in=0,
        grimp_fan_out=0,
        grimp_scc_size=1,
        grimp_cycle_membership=False,
        path_namespace_id=namespace,
    )


def _ready_architecture() -> CodeArchitectureAnalysis:
    return CodeArchitectureAnalysis(
        database="fixture.sqlite3",
        analysis_run_id=7,
        status="ready",
        reason=None,
        gate="observed",
        gates=(
            ArchitectureGateEvaluation("import_graph_consensus", "passed", None),
            ArchitectureGateEvaluation(
                "architecture_contracts",
                "failed",
                "architecture_contract_violation_observed",
            ),
            ArchitectureGateEvaluation(
                "module_complexity_displacement",
                "not_evaluated",
                "baseline_not_supplied",
            ),
        ),
        providers=(
            _provider("complexipy-cognitive"),
            _provider("grimp-architecture"),
            _provider("ruff-analyze-imports"),
        ),
        summary=ArchitectureSummary(
            modules=2,
            import_edges=1,
            consensus_edges=1,
            graph_disagreements=0,
            cyclic_sccs=0,
            grimp_reported_internal_modules=2,
            grimp_reported_import_edges=1,
            grimp_reported_cyclic_sccs=0,
            grimp_counts_consistent=True,
        ),
        modules=(
            _module("pkg.source", "pkg"),
            _module("other.target", "other"),
        ),
        symbols=(),
        imports=(
            ArchitectureImportEdge(
                source_module="pkg.source",
                target_module="other.target",
                comparison="both",
                ruff_observed=True,
                grimp_observed=True,
                confirmed=True,
                confidence=1.0,
            ),
        ),
        cycles=(),
        contracts=(
            ArchitectureContract(
                contract_id="fixture-boundary-v1",
                status="failed",
                evaluated=True,
                violations=1,
                importer_modules=("pkg.source",),
                imported_modules=("other.target",),
                import_chains=(("pkg.source", "other.target"),),
                contract_schema="neocortex.code-architecture-contracts/v1",
            ),
        ),
        limitations=("fixture",),
    )


def test_ready_architecture_exposes_graph_contract_and_owner_gap_without_a_decision() -> None:
    specs, evaluations = architecture_questions(
        _ready_architecture(),
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
        rank_offset=4,
    )

    assert tuple(item.question_id for item in specs) == (
        "architecture.static_import_graph_is_comparably_observed",
        "architecture.declared_import_contracts_are_evaluated",
        "architecture.logical_owner_mapping_is_explicitly_declared",
    )
    assert tuple(item.rank for item in evaluations) == (5, 6, 7)
    assert tuple(item.observation_status for item in evaluations) == (
        "confirmed",
        "confirmed",
        "confirmed",
    )
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision is None for item in evaluations)
    assert all(item.authority == "advisory" for item in evaluations)
    assert all(item.mutation_authority is False for item in evaluations)
    contract_facts = {fact.name: fact.value for fact in evaluations[1].evidence[0].facts}
    assert contract_facts["failed_contracts"] == 1
    assert contract_facts["contract_violations"] == 1


def test_path_namespace_rename_does_not_change_explicit_owner_projection_or_authority() -> None:
    first = _ready_architecture()
    renamed = replace(
        first,
        modules=tuple(replace(item, path_namespace_id="renamed") for item in first.modules),
    )

    _first_specs, first_evaluations = architecture_questions(
        first,
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
        rank_offset=0,
    )
    _renamed_specs, renamed_evaluations = architecture_questions(
        renamed,
        snapshot_id="snapshot-fixture",
        snapshot_freshness="current",
        rank_offset=0,
    )

    assert first_evaluations[2].observation_status == "confirmed"
    assert renamed_evaluations[2].observation_status == "confirmed"
    assert first_evaluations[2].decision is renamed_evaluations[2].decision is None
    assert tuple(
        (fact.name, fact.value)
        for evidence in first_evaluations[2].evidence
        for fact in evidence.facts
        if fact.name in {"mapped_modules", "unmapped_modules", "overlapping_modules"}
    ) == tuple(
        (fact.name, fact.value)
        for evidence in renamed_evaluations[2].evidence
        for fact in evidence.facts
        if fact.name in {"mapped_modules", "unmapped_modules", "overlapping_modules"}
    )


def test_unavailable_architecture_abstains_every_question_without_partial_evidence() -> None:
    unavailable = replace(
        _ready_architecture(),
        analysis_run_id=None,
        status="abstained",
        reason="required_architecture_provider_not_ready",
        gate="abstained",
        summary=None,
        modules=(),
        imports=(),
        contracts=(),
    )

    _specs, evaluations = architecture_questions(
        unavailable,
        snapshot_id="snapshot-fixture",
        snapshot_freshness="publication_only",
        rank_offset=0,
    )

    assert len(evaluations) == 3
    assert all(item.observation_status == "abstained" for item in evaluations)
    assert all(item.question_readiness == "abstained" for item in evaluations)
    assert all(item.decision_readiness == "abstained" for item in evaluations)
    assert all(item.evidence == () for item in evaluations)
    assert all(item.decision is None for item in evaluations)


def test_architecture_question_wire_parser_rejects_summary_and_provider_forgery() -> None:
    architecture = _ready_architecture()
    payload = json.loads(json.dumps(architecture.as_payload()))

    assert parse_code_architecture_question_payload(payload) == architecture

    forged_summary = json.loads(json.dumps(payload))
    forged_summary["summary"]["modules"] = 999
    with pytest.raises(ValueError, match="summary"):
        parse_code_architecture_question_payload(forged_summary)

    forged_provider = json.loads(json.dumps(payload))
    forged_provider["providers"][0]["provider_id"] = "name-proxy-owner"
    with pytest.raises(ValueError, match="providers"):
        parse_code_architecture_question_payload(forged_provider)
