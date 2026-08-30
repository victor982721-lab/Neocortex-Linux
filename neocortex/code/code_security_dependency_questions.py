"""Evidence-bound security and dependency questions over the supply-chain view.

The existing supply-chain projection remains the source of truth.  This module
only translates its exact provider and gate receipt into generic questions; it
does not reinterpret a passing tool run as proof that the repository is safe.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from dataclasses import asdict
from typing import Literal

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
from .code_supply_chain_analysis import (
    CODE_SUPPLY_CHAIN_SCHEMA,
    CodeSupplyChainAnalysis,
)

SECURITY_EVIDENCE_QUESTION = AnalysisQuestionSpec(
    question_id="security.static_invariants_and_vulnerability_evidence_is_resolved",
    version="v1",
    subject_kinds=("project",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "security_provider_coverage_projection",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "semgrep_invariant_provider_and_gate_evaluated",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "current_vulnerability_provider_and_gates_evaluated",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "security_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "security_verification_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "recorded_security_providers_cover_the_declared_invariant_and_vulnerability_scope",
        "security_provider_coverage_is_missing_stale_or_contains_advisory_failures",
    ),
    counterevidence_rules=(
        "zero_findings_only_applies_to_the_recorded_complete_provider_input",
        "a_current_vulnerability_database_does_not_cover_unknown_or_uninstalled_dependencies",
        "static_invariant_rules_do_not_establish_runtime_security",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "run_missing_or_stale_security_providers",
            "characterization",
            "Run Semgrep invariants and pip-audit under their complete comparable contracts.",
        ),
        AnalysisNextActionSpec(
            "inspect_failed_security_gates_and_counterexamples",
            "counterevidence_search",
            "Inspect exact failed gates, provider inputs, and bounded finding identities.",
        ),
        AnalysisNextActionSpec(
            "execute_bounded_security_boundary_scenarios",
            "experiment",
            "Execute targeted hostile-input scenarios for the affected trust boundaries.",
        ),
    ),
)

DEPENDENCY_EVIDENCE_QUESTION = AnalysisQuestionSpec(
    question_id="dependency.declaration_installation_and_license_evidence_is_resolved",
    version="v1",
    subject_kinds=("dependency",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "dependency_provider_coverage_projection",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "dependency_declaration_provider_and_gate_evaluated",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "installed_package_and_license_gates_evaluated",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "dependency_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "release_artifact_dependency_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "declared_dependencies_installed_artifacts_and_license_inventory_are_consistent",
        "dependency_or_artifact_evidence_is_missing_inconsistent_or_not_release_equivalent",
    ),
    counterevidence_rules=(
        "the_development_environment_is_not_automatically_the_published_artifact",
        "license_metadata_presence_does_not_establish_legal_compatibility",
        "import_manifest_agreement_does_not_prove_runtime_reachability",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "run_missing_dependency_and_inventory_providers",
            "characterization",
            "Run Deptry and installed-package inventory with complete comparable inputs.",
        ),
        AnalysisNextActionSpec(
            "inspect_dependency_gate_counterexamples",
            "counterevidence_search",
            "Inspect exact declaration, RECORD, version, and license metadata discrepancies.",
        ),
        AnalysisNextActionSpec(
            "compare_built_artifact_with_lock_and_installed_inventory",
            "experiment",
            "Build an isolated artifact and compare its contents and dependency closure.",
        ),
    ),
)

_LIMITATIONS = (
    "tool_reported_evidence_is_not_a_confirmed_defect_or_safety_proof",
    "provider_absence_never_passes_a_gate",
    "zero_findings_is_bounded_to_recorded_provider_inputs",
    "human_decision_not_owned_by_code_analysis",
)


def _domain_provider_ids(domain: Literal["security", "dependency"]) -> tuple[str, str]:
    return (
        ("semgrep-neocortex-invariants", "pip-audit-known-vulnerabilities")
        if domain == "security"
        else ("deptry-project-dependencies", "installed-package-inventory")
    )


def _semantic_projection_digest(
    analysis: CodeSupplyChainAnalysis,
    *,
    domain: Literal["security", "dependency"],
) -> str:
    """Identify decision-relevant supply evidence without capture-local run data."""

    provider_ids = _domain_provider_ids(domain)
    providers = {item.provider_id: item for item in analysis.providers}
    provider_projection: list[object] = []
    for provider_id in provider_ids:
        provider = providers.get(provider_id)
        if provider is None:
            provider_projection.append({"provider_id": provider_id, "status": "absent"})
            continue
        provider_projection.append(
            {
                "provider_id": provider.provider_id,
                "status": provider.status,
                "reason": provider.reason,
                "profile": provider.profile,
                "tool_name": provider.tool_name,
                "tool_version": provider.tool_version,
                "provider_schema": provider.provider_schema,
                "comparability_signature": provider.comparability_signature,
                "findings": provider.findings,
                "metrics": provider.metrics,
                "relations": provider.relations,
                "freshness": provider.freshness,
                "limitations": tuple(sorted(provider.limitations)),
                "authority": provider.authority,
                "mutation_authority": provider.mutation_authority,
            }
        )
    gates = tuple(
        (
            item.gate,
            item.provider_id,
            item.status,
            item.reason,
            item.evidence_count,
        )
        for item in sorted(analysis.gates, key=lambda item: (item.provider_id, item.gate))
        if item.provider_id in provider_ids
    )
    return analysis_identity(
        f"code-supply-{domain}-decision-projection-v1",
        {
            "status": analysis.status,
            "reason": analysis.reason,
            "providers": tuple(provider_projection),
            "gates": gates,
            "observations_truncated": analysis.counts.observations_truncated,
            "authority": analysis.authority,
            "mutation_authority": analysis.mutation_authority,
        },
    )


def _subject(
    analysis: CodeSupplyChainAnalysis,
    *,
    kind: Literal["project", "dependency"],
    domain: Literal["security", "dependency"],
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisSubjectRef:
    return AnalysisSubjectRef(
        subject_kind=kind,
        subject_key=(
            "project:neocortex-security-evidence"
            if kind == "project"
            else "dependency:neocortex-environment"
        ),
        display_name=(
            "NeoCortex security provider evidence"
            if kind == "project"
            else "NeoCortex dependency and installed-artifact evidence"
        ),
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=_semantic_projection_digest(analysis, domain=domain),
    )


def _coverage_evidence(
    analysis: CodeSupplyChainAnalysis,
    subject: AnalysisSubjectRef,
    *,
    domain: Literal["security", "dependency"],
) -> AnalysisEvidenceRef:
    provider_ids = _domain_provider_ids(domain)
    providers = {item.provider_id: item for item in analysis.providers}
    gates = tuple(item for item in analysis.gates if item.provider_id in provider_ids)
    projection = {
        "analysis_status": analysis.status,
        "analysis_reason": analysis.reason,
        "analysis_run_id": analysis.analysis_run_id,
        "supply_digest": asdict(analysis.digest),
        "providers": tuple(
            (
                provider_id,
                providers[provider_id].status if provider_id in providers else "absent",
            )
            for provider_id in provider_ids
        ),
        "gates": tuple((item.gate, item.status, item.evidence_count) for item in gates),
    }
    projection_digest = analysis_identity(
        f"code-{domain}-provider-coverage-projection-v1", projection
    )
    semantic_digest = _semantic_projection_digest(analysis, domain=domain)
    facts = (
        AnalysisFact("supply_chain_status", analysis.status),
        AnalysisFact("supply_chain_reason", analysis.reason),
        AnalysisFact("required_provider_count", len(provider_ids), "count"),
        AnalysisFact(
            "ready_provider_count",
            sum(
                provider_id in providers and providers[provider_id].status == "ready"
                for provider_id in provider_ids
            ),
            "count",
        ),
        AnalysisFact(
            "evaluated_gate_count",
            sum(item.status in {"passed", "failed"} for item in gates),
            "count",
        ),
        AnalysisFact("failed_gate_count", sum(item.status == "failed" for item in gates), "count"),
        AnalysisFact("observation_projection_truncated", analysis.counts.observations_truncated),
        AnalysisFact("supply_chain_semantic_digest", semantic_digest),
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            f"code-{domain}-provider-coverage-evidence-v1",
            {"subject": subject.subject_key, "projection": projection_digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_fact",
        source_owner_id="code",
        producer_id="code-supply-chain-analysis",
        producer_version="v1",
        source_schema=CODE_SUPPLY_CHAIN_SCHEMA,
        source_record_kind=f"{domain}_provider_coverage_projection",
        source_record_id=str(analysis.analysis_run_id or "unresolved"),
        source_projection_digest=projection_digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-supply-chain-question-resolver",
        resolver_version="v1",
        limitations=("coverage_fact_does_not_establish_gate_success",),
    )


def _provider_gate_evidence(
    analysis: CodeSupplyChainAnalysis,
    subject: AnalysisSubjectRef,
    *,
    requirement_id: str,
    provider_id: str,
    gate_ids: tuple[str, ...],
) -> AnalysisEvidenceRef | None:
    provider = next((item for item in analysis.providers if item.provider_id == provider_id), None)
    gates = tuple(item for item in analysis.gates if item.gate in gate_ids)
    if (
        provider is None
        or provider.status != "ready"
        or len(gates) != len(gate_ids)
        or any(item.status not in {"passed", "failed"} for item in gates)
    ):
        return None
    projection = {
        "provider": asdict(provider),
        "gates": tuple(asdict(item) for item in gates),
        "supply_digest": asdict(analysis.digest),
    }
    projection_digest = analysis_identity("code-supply-provider-gate-projection-v1", projection)
    facts = (
        AnalysisFact("provider_id", provider.provider_id),
        AnalysisFact("provider_status", provider.status),
        AnalysisFact("provider_tool", provider.tool_name),
        AnalysisFact("provider_tool_version", provider.tool_version),
        AnalysisFact("provider_freshness", provider.freshness),
        AnalysisFact("provider_findings", provider.findings, "count"),
        AnalysisFact("provider_metrics", provider.metrics, "count"),
        AnalysisFact("provider_relations", provider.relations, "count"),
        AnalysisFact("evaluated_gate_count", len(gates), "count"),
        AnalysisFact("failed_gate_count", sum(item.status == "failed" for item in gates), "count"),
        AnalysisFact("gate_statuses", ",".join(f"{item.gate}:{item.status}" for item in gates)),
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-supply-provider-gate-evidence-v1",
            {
                "subject": subject.subject_key,
                "requirement": requirement_id,
                "projection": projection_digest,
            },
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_fact",
        source_owner_id="code",
        producer_id=provider.provider_id,
        producer_version=provider.provider_schema or "unknown",
        source_schema=CODE_SUPPLY_CHAIN_SCHEMA,
        source_record_kind="provider_and_gate_projection",
        source_record_id=f"{provider.provider_id}:{provider.tool_run_id or 'unresolved'}",
        source_projection_digest=projection_digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-supply-chain-question-resolver",
        resolver_version="v1",
        limitations=("gate_status_is_advisory_tool_evidence",),
    )


def _requirement(
    requirement_id: str,
    evidence: AnalysisEvidenceRef | None,
    *,
    missing_reason: str,
) -> AnalysisRequirementEvaluation:
    if evidence is None:
        return AnalysisRequirementEvaluation(requirement_id, "missing", (), missing_reason)
    return AnalysisRequirementEvaluation(
        requirement_id,
        "satisfied",
        (evidence.evidence_id,),
        "linked_complete_provider_and_gate_projection",
    )


def _evaluation(
    analysis: CodeSupplyChainAnalysis,
    spec: AnalysisQuestionSpec,
    *,
    rank: int,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    domain: Literal["security", "dependency"],
) -> AnalysisQuestionEvaluation:
    subject = _subject(
        analysis,
        kind="project" if domain == "security" else "dependency",
        domain=domain,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
    )
    coverage = _coverage_evidence(analysis, subject, domain=domain)
    if domain == "security":
        first_id = "semgrep_invariant_provider_and_gate_evaluated"
        first = _provider_gate_evidence(
            analysis,
            subject,
            requirement_id=first_id,
            provider_id="semgrep-neocortex-invariants",
            gate_ids=("semgrep_invariants",),
        )
        second_id = "current_vulnerability_provider_and_gates_evaluated"
        second = _provider_gate_evidence(
            analysis,
            subject,
            requirement_id=second_id,
            provider_id="pip-audit-known-vulnerabilities",
            gate_ids=("vulnerability_snapshot_current", "no_known_vulnerabilities"),
        )
        counter_id = "security_counterevidence_evaluated"
        experiment_id = "security_verification_experiment_result"
    else:
        first_id = "dependency_declaration_provider_and_gate_evaluated"
        first = _provider_gate_evidence(
            analysis,
            subject,
            requirement_id=first_id,
            provider_id="deptry-project-dependencies",
            gate_ids=("dependency_declaration_integrity",),
        )
        second_id = "installed_package_and_license_gates_evaluated"
        second = _provider_gate_evidence(
            analysis,
            subject,
            requirement_id=second_id,
            provider_id="installed-package-inventory",
            gate_ids=("installed_package_integrity", "license_inventory_available"),
        )
        counter_id = "dependency_counterevidence_evaluated"
        experiment_id = "release_artifact_dependency_experiment_result"
    evidence = (coverage, *(item for item in (first, second) if item is not None))
    requirements = (
        AnalysisRequirementEvaluation(
            spec.requirements[0].requirement_id,
            "satisfied",
            (coverage.evidence_id,),
            "linked_supply_chain_provider_coverage_projection",
        ),
        _requirement(
            first_id,
            first,
            missing_reason="required_provider_or_gate_is_not_completely_evaluated",
        ),
        _requirement(
            second_id,
            second,
            missing_reason="required_provider_or_gate_is_not_completely_evaluated",
        ),
        AnalysisRequirementEvaluation(
            counter_id,
            "not_evaluated",
            (),
            "independent_counterevidence_not_linked",
        ),
        AnalysisRequirementEvaluation(
            experiment_id,
            "missing",
            (),
            "verification_experiment_result_not_linked",
        ),
    )
    question_evidence_complete = all(
        requirement.status == "satisfied"
        for requirement_spec, requirement in zip(spec.requirements, requirements, strict=True)
        if requirement_spec.stage == "question"
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            f"code-{domain}-evidence-question-v1",
            {
                "question": analysis_question_spec_fingerprint(spec),
                "snapshot": snapshot_id,
                "supply_digest": analysis.digest.xxh3_128,
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed" if question_evidence_complete else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready" if question_evidence_complete else "abstained",
        decision_readiness="experiment_required" if question_evidence_complete else "abstained",
        decision=None,
        decision_reason=(
            "decision_evidence_incomplete"
            if question_evidence_complete
            else "question_evidence_incomplete"
        ),
        counterevidence_status="not_evaluated",
        next_action_ids=(
            tuple(item.action_id for item in spec.next_actions)
            if question_evidence_complete
            else ()
        ),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return evaluation


def security_dependency_questions(
    analysis: CodeSupplyChainAnalysis,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Return two deterministic questions even when providers are explicitly absent."""

    if not snapshot_id or not isinstance(snapshot_id, str):
        raise ValueError("security/dependency questions require a snapshot identity")
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("security/dependency snapshot freshness is invalid")
    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("security/dependency rank offset must be non-negative")
    specs = (SECURITY_EVIDENCE_QUESTION, DEPENDENCY_EVIDENCE_QUESTION)
    evaluations = (
        _evaluation(
            analysis,
            SECURITY_EVIDENCE_QUESTION,
            rank=rank_offset + 1,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            domain="security",
        ),
        _evaluation(
            analysis,
            DEPENDENCY_EVIDENCE_QUESTION,
            rank=rank_offset + 2,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            domain="dependency",
        ),
    )
    return specs, evaluations


__all__ = [
    "DEPENDENCY_EVIDENCE_QUESTION",
    "SECURITY_EVIDENCE_QUESTION",
    "security_dependency_questions",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_security_dependency_questions")
