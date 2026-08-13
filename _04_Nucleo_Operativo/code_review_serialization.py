"""Lower-level wire identity and digest construction for Code review."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_questions_payload,
)
from .code_external_evidence import external_status_digest_payload
from .semantic_models import canonical_json, fingerprint_text

CODE_REVIEW_SCHEMA = "neocortex.code-review/v15"
CODE_REVIEW_COMPATIBLE_SCHEMAS: tuple[str, ...] = ()
RecommendationStatus = Literal["ready", "abstained", "not_evaluated"]


@dataclass(frozen=True, slots=True)
class CodeReviewDigest:
    """Collision-guarded deterministic identity of review evidence."""

    xxh3_128: str
    xxh3_64_guard: str
    byte_count: int


def build_code_review_digest(
    snapshot: Any,
    coverage: Any,
    findings: tuple[Any, ...],
    *,
    ranking: str,
    actionability_version: str,
    recommendation_status: RecommendationStatus,
    recommendation_reason: str | None,
    recommendations: tuple[Any, ...],
    planning_version: str,
    work_package_status: RecommendationStatus,
    work_package_reason: str | None,
    work_packages: tuple[Any, ...],
    external_evidence: Any,
    external_evidence_suite: Any,
    architecture: Any,
    test_coverage: Any,
    engineering_analytics: Any,
    structural_analysis: Any,
    state_projection: Any,
    state_topology: Any,
    change_evolution: Any,
    assurance: Any,
    capability_reachability: Any,
    analyzer_effectiveness: Any,
    interface_surface: Any,
    unused_analysis: Any,
    supply_chain: Any,
    question_specs: tuple[AnalysisQuestionSpec, ...],
    question_evaluations: tuple[AnalysisQuestionEvaluation, ...],
    limitations: tuple[str, ...],
) -> CodeReviewDigest:
    """Hash every evidence-bearing field while excluding local database paths."""

    payload = canonical_json(
        {
            "schema": CODE_REVIEW_SCHEMA,
            "ranking": ranking,
            "actionability_version": actionability_version,
            "recommendation_status": recommendation_status,
            "recommendation_reason": recommendation_reason,
            "planning_version": planning_version,
            "work_package_status": work_package_status,
            "work_package_reason": work_package_reason,
            "snapshot": {
                "processing_signature": snapshot.processing_signature,
                "freshness": snapshot.freshness,
                "current": snapshot.current,
                "journal_status": snapshot.journal_status,
            },
            "coverage": asdict(coverage),
            "findings": [asdict(finding) for finding in findings],
            "recommendations": [asdict(recommendation) for recommendation in recommendations],
            "work_packages": [asdict(package) for package in work_packages],
            "external_evidence": external_status_digest_payload(external_evidence),
            "external_evidence_suite": external_evidence_suite.as_payload(),
            "architecture": architecture.digest_payload(),
            "test_coverage": test_coverage.digest_payload(),
            "engineering_analytics": {
                "status": engineering_analytics.status,
                "reason": engineering_analytics.reason,
                "digest": engineering_analytics.digest,
                "authority": engineering_analytics.authority,
                "mutation_authority": engineering_analytics.mutation_authority,
                "aggregate_score": engineering_analytics.aggregate_score,
                "defect_probability": engineering_analytics.defect_probability,
            },
            "structural_analysis": structural_analysis.as_payload(),
            "state_projection": state_projection.as_payload(),
            "state_topology": state_topology.as_payload(),
            "change_evolution": change_evolution.as_payload(),
            "assurance": assurance.as_payload(),
            "capability_reachability": capability_reachability.as_payload(),
            "analyzer_effectiveness": analyzer_effectiveness.as_payload(),
            "interface_surface": interface_surface.as_payload(),
            "unused_analysis": unused_analysis.digest_payload(),
            "supply_chain": {
                "schema": supply_chain.as_payload()["schema"],
                "status": supply_chain.status,
                "reason": supply_chain.reason,
                "digest": asdict(supply_chain.digest),
            },
            "epistemics": analysis_questions_payload(
                question_specs,
                question_evaluations,
            ),
            "limitations": list(limitations),
        }
    )
    fingerprint = fingerprint_text(payload)
    return CodeReviewDigest(
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
        fingerprint.byte_count,
    )


def rebuild_code_review_result_digest(result: Any) -> CodeReviewDigest:
    """Recompute a ready envelope digest from every evidence-bearing projection."""

    required = (
        result.snapshot,
        result.coverage,
        result.external_evidence,
        result.external_evidence_suite,
        result.architecture,
        result.test_coverage,
        result.engineering_analytics,
        result.structural_analysis,
        result.state_projection,
        result.state_topology,
        result.change_evolution,
        result.assurance,
        result.capability_reachability,
        result.analyzer_effectiveness,
        result.interface_surface,
        result.unused_analysis,
        result.supply_chain,
    )
    if any(item is None for item in required):
        raise ValueError("ready code-review result lacks evidence required by its digest")
    return build_code_review_digest(
        result.snapshot,
        result.coverage,
        result.findings,
        ranking=result.ranking,
        actionability_version=result.actionability_version,
        recommendation_status=result.recommendation_status,
        recommendation_reason=result.recommendation_reason,
        recommendations=result.recommendations,
        planning_version=result.planning_version,
        work_package_status=result.work_package_status,
        work_package_reason=result.work_package_reason,
        work_packages=result.work_packages,
        external_evidence=result.external_evidence,
        external_evidence_suite=result.external_evidence_suite,
        architecture=result.architecture,
        test_coverage=result.test_coverage,
        engineering_analytics=result.engineering_analytics,
        structural_analysis=result.structural_analysis,
        state_projection=result.state_projection,
        state_topology=result.state_topology,
        change_evolution=result.change_evolution,
        assurance=result.assurance,
        capability_reachability=result.capability_reachability,
        analyzer_effectiveness=result.analyzer_effectiveness,
        interface_surface=result.interface_surface,
        unused_analysis=result.unused_analysis,
        supply_chain=result.supply_chain,
        question_specs=result.question_specs,
        question_evaluations=result.question_evaluations,
        limitations=result.limitations,
    )


__all__ = [
    "CODE_REVIEW_COMPATIBLE_SCHEMAS",
    "CODE_REVIEW_SCHEMA",
    "CodeReviewDigest",
    "build_code_review_digest",
    "rebuild_code_review_result_digest",
]
