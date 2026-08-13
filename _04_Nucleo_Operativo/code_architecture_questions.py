"""Evidence-bound questions over the published static architecture projection.

The existing architecture analysis already owns the Ruff/Grimp differential
graph and the versioned import contracts.  This adapter makes those facts part
of the general question registry without relabelling package namespaces as
logical ownership.  Logical ownership remains an explicit missing contract
until NeoCortex declares and resolves one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields
from typing import Any, Literal, cast

from .code_analysis_epistemics import (
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
    validate_analysis_question_evaluation,
)
from .code_architecture_analysis import (
    CODE_ARCHITECTURE_REQUIRED_PROVIDERS,
    CODE_ARCHITECTURE_SCHEMA,
    ArchitectureContract,
    ArchitectureCycle,
    ArchitectureGateEvaluation,
    ArchitectureImportEdge,
    ArchitectureModule,
    ArchitectureProviderStatus,
    ArchitectureSummary,
    ArchitectureSymbolComplexity,
    CodeArchitectureAnalysis,
)

ARCHITECTURE_STATIC_GRAPH_QUESTION = AnalysisQuestionSpec(
    question_id="architecture.static_import_graph_is_comparably_observed",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "comparable_static_import_graph_projection",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "logical_owner_mapping_resolved",
            "decision",
            "supporting",
            ("contract", "internal_relation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "dynamic_dependency_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "representative_dependency_acceptance_experiment",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "the_comparable_static_graph_represents_the_relevant_dependency_surface",
        "dynamic_imports_runtime_dispatch_or_unmapped_owners_leave_material_edges_unobserved",
    ),
    counterevidence_rules=(
        "static_import_consensus_does_not_prove_runtime_dependency_reachability",
        "a_first_package_segment_is_a_path_namespace_not_logical_ownership",
        "provider_absence_or_disagreement_is_preserved_as_missing_evidence",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "declare_and_resolve_logical_owner_mapping",
            "characterization",
            "Declare a versioned logical-owner mapping and resolve every mapped module.",
        ),
        AnalysisNextActionSpec(
            "trace_representative_dynamic_dependency_paths",
            "counterevidence_search",
            "Trace representative plugin, dispatch, and lazy-import paths at runtime.",
        ),
        AnalysisNextActionSpec(
            "run_architecture_boundary_acceptance_scenario",
            "experiment",
            "Run a bounded scenario that exercises a declared dependency boundary.",
        ),
    ),
)

ARCHITECTURE_CONTRACT_QUESTION = AnalysisQuestionSpec(
    question_id="architecture.declared_import_contracts_are_evaluated",
    version="v1",
    subject_kinds=("contract",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "versioned_import_contract_evaluations",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "observed_violation_impact_characterized",
            "decision",
            "supporting",
            ("internal_relation", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "contract_exception_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("contract", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "boundary_acceptance_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "declared_import_contracts_match_the_intended_architecture",
        "a_contract_or_its_allowlist_is_incomplete_stale_or_semantically_misaligned",
    ),
    counterevidence_rules=(
        "a_passing_import_contract_does_not_observe_dynamic_dispatch",
        "a_failed_contract_is_an_observation_not_an_automatic_refactor_plan",
        "an_allowlisted_edge_can_still_be_semantically_inappropriate",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "characterize_contract_violation_impact",
            "characterization",
            "Resolve the affected capability, state, and runtime paths for each violation.",
        ),
        AnalysisNextActionSpec(
            "inspect_contract_exception_counterevidence",
            "counterevidence_search",
            "Inspect explicit exception, compatibility, and composition-root evidence.",
        ),
        AnalysisNextActionSpec(
            "exercise_declared_architecture_boundary",
            "experiment",
            "Execute a bounded acceptance scenario across the declared boundary.",
        ),
    ),
)

ARCHITECTURE_LOGICAL_OWNER_QUESTION = AnalysisQuestionSpec(
    question_id="architecture.logical_owner_mapping_is_explicitly_declared",
    version="v1",
    subject_kinds=("project",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "versioned_logical_owner_registry",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "module_to_logical_owner_projection",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "unmapped_and_overlapping_owner_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("contract", "internal_relation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "representative_owner_boundary_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "package_namespaces_coincide_with_intended_logical_ownership",
        "logical_ownership_crosscuts_packages_and_requires_an_explicit_mapping",
    ),
    counterevidence_rules=(
        "path_prefixes_never_establish_logical_or_state_ownership_by_themselves",
        "one_module_can_coordinate_multiple_owners_without_owning_their_state",
        "unmapped_modules_are_unknown_not_implicitly_framework_owned",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "declare_versioned_logical_owner_registry",
            "characterization",
            "Declare owners, exact module selectors, boundary policy, and contract fingerprint.",
        ),
        AnalysisNextActionSpec(
            "inspect_unmapped_and_overlapping_modules",
            "counterevidence_search",
            "Resolve unmapped and multiply matched modules without name-based inference.",
        ),
        AnalysisNextActionSpec(
            "exercise_representative_logical_owner_boundary",
            "experiment",
            "Trace one representative workflow across each declared logical-owner boundary.",
        ),
    ),
)

_LIMITATIONS = (
    "static_imports_do_not_observe_dynamic_dispatch_or_runtime_reachability",
    "package_path_namespace_is_not_logical_repository_or_state_ownership",
    "architecture_contract_status_does_not_authorize_a_source_change",
    "logical_owner_registry_is_not_declared_in_v1",
    "human_decision_not_owned_by_code_analysis",
)


def _subject(
    analysis: CodeArchitectureAnalysis,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    kind: Literal["run", "contract", "project"],
) -> AnalysisSubjectRef:
    analysis_key = analysis_identity(
        "code-architecture-question-subject-v1", analysis.digest_payload()
    )
    labels = {
        "run": "Static architecture provider projection",
        "contract": "Declared import architecture contracts",
        "project": "NeoCortex logical ownership model",
    }
    return AnalysisSubjectRef(
        subject_kind=kind,
        subject_key=f"architecture:{kind}:{analysis_key}",
        display_name=labels[kind],
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=CODE_ARCHITECTURE_SCHEMA,
    )


def _graph_evidence(
    analysis: CodeArchitectureAnalysis,
    subject: AnalysisSubjectRef,
) -> AnalysisEvidenceRef:
    if analysis.summary is None:
        raise ValueError("ready architecture graph requires a summary")
    projection = {
        "summary": asdict(analysis.summary),
        "gates": tuple(asdict(item) for item in analysis.gates),
        "providers": tuple(
            {
                "provider_id": item.provider_id,
                "status": item.status,
                "tool_name": item.tool_name,
                "tool_version": item.tool_version,
                "provider_schema": item.provider_schema,
                "comparability_signature": item.comparability_signature,
                "metrics": item.metrics,
                "relations": item.relations,
            }
            for item in analysis.providers
        ),
        "imports": tuple(asdict(item) for item in analysis.imports),
        "cycles": tuple(asdict(item) for item in analysis.cycles),
    }
    digest = analysis_identity("code-architecture-static-graph-evidence-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-architecture-static-graph-ref-v1",
            {"subject": subject.subject_key, "projection": digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_relation",
        source_owner_id="code",
        producer_id="code-architecture-analysis",
        producer_version=CODE_ARCHITECTURE_SCHEMA,
        source_schema=CODE_ARCHITECTURE_SCHEMA,
        source_record_kind="comparable_static_import_graph",
        source_record_id=str(analysis.analysis_run_id),
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=(
            AnalysisFact("modules", analysis.summary.modules, "count"),
            AnalysisFact("import_edges", analysis.summary.import_edges, "count"),
            AnalysisFact("consensus_edges", analysis.summary.consensus_edges, "count"),
            AnalysisFact(
                "graph_disagreements",
                analysis.summary.graph_disagreements,
                "count",
            ),
            AnalysisFact("cyclic_sccs", analysis.summary.cyclic_sccs, "count"),
            AnalysisFact(
                "grimp_counts_consistent",
                analysis.summary.grimp_counts_consistent,
            ),
            AnalysisFact(
                "gate_statuses_json",
                json.dumps(
                    {item.gate: item.status for item in analysis.gates},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-architecture-projection-resolver",
        resolver_version="v1",
        limitations=(
            "static_graph_only",
            "provider_consensus_does_not_establish_runtime_reachability",
        ),
    )


def _contract_evidence(
    analysis: CodeArchitectureAnalysis,
    subject: AnalysisSubjectRef,
) -> AnalysisEvidenceRef:
    projection = tuple(asdict(item) for item in analysis.contracts)
    digest = analysis_identity("code-architecture-contract-evidence-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-architecture-contract-ref-v1",
            {"subject": subject.subject_key, "projection": digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id="grimp-architecture-contract-evaluator",
        producer_version=CODE_ARCHITECTURE_SCHEMA,
        source_schema=CODE_ARCHITECTURE_SCHEMA,
        source_record_kind="versioned_import_contract_evaluations",
        source_record_id=str(analysis.analysis_run_id),
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=(
            AnalysisFact("contracts", len(analysis.contracts), "count"),
            AnalysisFact(
                "passed_contracts",
                sum(item.status == "passed" for item in analysis.contracts),
                "count",
            ),
            AnalysisFact(
                "failed_contracts",
                sum(item.status == "failed" for item in analysis.contracts),
                "count",
            ),
            AnalysisFact(
                "contract_violations",
                sum(item.violations for item in analysis.contracts),
                "count",
            ),
            AnalysisFact(
                "contract_ids_json",
                json.dumps(
                    tuple(item.contract_id for item in analysis.contracts),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-architecture-contract-projection-resolver",
        resolver_version="v1",
        limitations=(
            "declared_import_contracts_only",
            "contract_result_does_not_authorize_mutation",
        ),
    )


def _evaluation(
    spec: AnalysisQuestionSpec,
    subject: AnalysisSubjectRef,
    *,
    analysis_id: str,
    rank: int,
    evidence: tuple[AnalysisEvidenceRef, ...],
    requirements: tuple[AnalysisRequirementEvaluation, ...],
    reason: str | None = None,
) -> AnalysisQuestionEvaluation:
    question_ready = all(
        item.status == "satisfied"
        for requirement, item in zip(spec.requirements, requirements, strict=True)
        if requirement.stage == "question"
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-architecture-question-evaluation-v1",
            {
                "analysis_id": analysis_id,
                "question": spec.question_id,
                "subject": subject.subject_key,
                "evidence": tuple(item.evidence_id for item in evidence),
                "requirements": tuple(asdict(item) for item in requirements),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed" if question_ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready" if question_ready else "abstained",
        decision_readiness="experiment_required" if question_ready else "abstained",
        decision=None,
        decision_reason=(
            "decision_evidence_incomplete" if question_ready else "question_evidence_incomplete"
        ),
        counterevidence_status="not_evaluated",
        next_action_ids=(
            tuple(item.action_id for item in spec.next_actions) if question_ready else ()
        ),
        limitations=(*_LIMITATIONS, *((reason,) if reason else ())),
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return evaluation


def architecture_questions(
    analysis: CodeArchitectureAnalysis,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Project architecture, contract, and explicit ownership-gap questions."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("architecture question rank offset must be non-negative")
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("architecture question snapshot freshness is invalid")
    specs = (
        ARCHITECTURE_STATIC_GRAPH_QUESTION,
        ARCHITECTURE_CONTRACT_QUESTION,
        ARCHITECTURE_LOGICAL_OWNER_QUESTION,
    )
    graph_subject = _subject(
        analysis,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        kind="run",
    )
    contract_subject = _subject(
        analysis,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        kind="contract",
    )
    owner_subject = _subject(
        analysis,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        kind="project",
    )
    ready = analysis.status == "ready" and analysis.summary is not None
    reason = analysis.reason or "architecture_projection_unavailable"

    graph_evidence = (_graph_evidence(analysis, graph_subject),) if ready else ()
    graph_requirements = (
        AnalysisRequirementEvaluation(
            "comparable_static_import_graph_projection",
            "satisfied" if graph_evidence else "missing",
            tuple(item.evidence_id for item in graph_evidence),
            "comparable_ruff_grimp_graph_resolved" if graph_evidence else reason,
        ),
        AnalysisRequirementEvaluation(
            "logical_owner_mapping_resolved",
            "missing",
            (),
            "logical_owner_registry_not_declared",
        ),
        AnalysisRequirementEvaluation(
            "dynamic_dependency_counterevidence_evaluated",
            "not_evaluated",
            (),
            "dynamic_dependency_evidence_not_recorded",
        ),
        AnalysisRequirementEvaluation(
            "representative_dependency_acceptance_experiment",
            "missing",
            (),
            "architecture_boundary_acceptance_experiment_not_recorded",
        ),
    )

    contracts_ready = (
        ready
        and bool(analysis.contracts)
        and all(
            item.evaluated and item.status in {"passed", "failed"} for item in analysis.contracts
        )
    )
    contract_evidence = (_contract_evidence(analysis, contract_subject),) if contracts_ready else ()
    contract_reason = (
        "versioned_import_contracts_resolved"
        if contract_evidence
        else (
            "no_versioned_architecture_contracts_recorded"
            if ready and not analysis.contracts
            else "architecture_contract_evidence_incomplete"
            if ready
            else reason
        )
    )
    contract_requirements = (
        AnalysisRequirementEvaluation(
            "versioned_import_contract_evaluations",
            "satisfied" if contract_evidence else "missing",
            tuple(item.evidence_id for item in contract_evidence),
            contract_reason,
        ),
        AnalysisRequirementEvaluation(
            "observed_violation_impact_characterized",
            "missing",
            (),
            "runtime_and_capability_impact_not_characterized",
        ),
        AnalysisRequirementEvaluation(
            "contract_exception_counterevidence_evaluated",
            "not_evaluated",
            (),
            "contract_exception_counterevidence_not_evaluated",
        ),
        AnalysisRequirementEvaluation(
            "boundary_acceptance_experiment_result",
            "missing",
            (),
            "boundary_acceptance_experiment_not_recorded",
        ),
    )
    owner_requirements = (
        AnalysisRequirementEvaluation(
            "versioned_logical_owner_registry",
            "missing",
            (),
            "logical_owner_registry_not_declared",
        ),
        AnalysisRequirementEvaluation(
            "module_to_logical_owner_projection",
            "missing",
            (),
            "module_to_logical_owner_projection_not_resolved",
        ),
        AnalysisRequirementEvaluation(
            "unmapped_and_overlapping_owner_counterevidence_evaluated",
            "not_evaluated",
            (),
            "owner_coverage_counterevidence_not_evaluated",
        ),
        AnalysisRequirementEvaluation(
            "representative_owner_boundary_experiment_result",
            "missing",
            (),
            "logical_owner_boundary_experiment_not_recorded",
        ),
    )
    evaluations = (
        _evaluation(
            specs[0],
            graph_subject,
            analysis_id=analysis_identity("architecture-analysis-v1", analysis.digest_payload()),
            rank=rank_offset + 1,
            evidence=graph_evidence,
            requirements=graph_requirements,
            reason=None if ready else reason,
        ),
        _evaluation(
            specs[1],
            contract_subject,
            analysis_id=analysis_identity("architecture-analysis-v1", analysis.digest_payload()),
            rank=rank_offset + 2,
            evidence=contract_evidence,
            requirements=contract_requirements,
            reason=None if contracts_ready else contract_reason,
        ),
        _evaluation(
            specs[2],
            owner_subject,
            analysis_id=analysis_identity("architecture-analysis-v1", analysis.digest_payload()),
            rank=rank_offset + 3,
            evidence=(),
            requirements=owner_requirements,
            reason="logical_owner_registry_not_declared",
        ),
    )
    return specs, evaluations


def _wire_sequence(label: str, value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    return value


def _wire_tuple(label: str, value: object) -> tuple[str, ...]:
    result = tuple(_wire_sequence(label, value))
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{label} must contain non-empty strings")
    return cast(tuple[str, ...], result)


def _wire_model(
    label: str,
    value: object,
    model: type[Any],
    *,
    tuple_fields: tuple[str, ...] = (),
) -> Any:
    expected = {field.name for field in fields(model)}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    values = dict(value)
    for key in tuple_fields:
        values[key] = _wire_tuple(f"{label} {key}", values[key])
    return model(**cast(Any, values))


def parse_code_architecture_question_payload(
    payload: Mapping[str, object],
) -> CodeArchitectureAnalysis:
    """Strictly reconstruct the architecture fields used by v15 questions."""

    expected = {field.name for field in fields(CodeArchitectureAnalysis)} | {"kind", "schema"}
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected
        or payload.get("kind") != "code-architecture-analysis"
        or payload.get("schema") != CODE_ARCHITECTURE_SCHEMA
    ):
        raise ValueError("architecture question payload envelope is invalid")
    values = {key: value for key, value in payload.items() if key not in {"kind", "schema"}}
    nested: tuple[tuple[str, type[Any], tuple[str, ...]], ...] = (
        ("gates", ArchitectureGateEvaluation, ()),
        ("providers", ArchitectureProviderStatus, ()),
        (
            "modules",
            ArchitectureModule,
            (
                "cycle_ids",
                "contract_ids",
                "dependency_path_namespace_ids",
                "consumer_path_namespace_ids",
            ),
        ),
        ("symbols", ArchitectureSymbolComplexity, ()),
        ("imports", ArchitectureImportEdge, ()),
        ("cycles", ArchitectureCycle, ("modules",)),
    )
    for key, model, tuple_fields in nested:
        values[key] = tuple(
            _wire_model(f"architecture {key}", item, model, tuple_fields=tuple_fields)
            for item in _wire_sequence(f"architecture {key}", values[key])
        )
    raw_contracts = _wire_sequence("architecture contracts", values["contracts"])
    contracts: list[ArchitectureContract] = []
    for raw in raw_contracts:
        expected_contract = {field.name for field in fields(ArchitectureContract)}
        if not isinstance(raw, Mapping) or set(raw) != expected_contract:
            raise ValueError("architecture contract fields are invalid")
        contract_values = dict(raw)
        for key in ("importer_modules", "imported_modules"):
            contract_values[key] = _wire_tuple(f"architecture contract {key}", contract_values[key])
        contract_values["import_chains"] = tuple(
            _wire_tuple("architecture import chain", item)
            for item in _wire_sequence(
                "architecture contract import chains", contract_values["import_chains"]
            )
        )
        contracts.append(ArchitectureContract(**cast(Any, contract_values)))
    values["contracts"] = tuple(contracts)
    values["limitations"] = _wire_tuple("architecture limitations", values["limitations"])
    raw_summary = values["summary"]
    values["summary"] = (
        None
        if raw_summary is None
        else _wire_model("architecture summary", raw_summary, ArchitectureSummary)
    )
    result = CodeArchitectureAnalysis(**cast(Any, values))
    if result.status not in {"ready", "abstained"} or result.gate not in {
        "observed",
        "abstained",
    }:
        raise ValueError("architecture question payload status is invalid")
    if result.status == "ready":
        if (
            result.reason is not None
            or result.gate != "observed"
            or result.summary is None
            or isinstance(result.analysis_run_id, bool)
            or not isinstance(result.analysis_run_id, int)
            or result.analysis_run_id < 1
        ):
            raise ValueError("ready architecture question payload is incomplete")
        if tuple(item.provider_id for item in result.providers) != tuple(
            sorted(CODE_ARCHITECTURE_REQUIRED_PROVIDERS)
        ) or any(item.status != "ready" for item in result.providers):
            raise ValueError("ready architecture providers are incomplete")
        if (
            result.summary.modules != len(result.modules)
            or result.summary.import_edges != len(result.imports)
            or result.summary.cyclic_sccs != len(result.cycles)
            or result.summary.consensus_edges + result.summary.graph_disagreements
            != result.summary.import_edges
        ):
            raise ValueError("architecture summary disagrees with its projection")
    elif result.reason is None or result.gate != "abstained":
        raise ValueError("abstained architecture question payload lacks a reason")
    return result


__all__ = [
    "ARCHITECTURE_CONTRACT_QUESTION",
    "ARCHITECTURE_LOGICAL_OWNER_QUESTION",
    "ARCHITECTURE_STATIC_GRAPH_QUESTION",
    "architecture_questions",
    "parse_code_architecture_question_payload",
]
