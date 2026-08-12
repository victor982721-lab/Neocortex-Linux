"""Deterministic digest construction for the Code review envelope."""

from __future__ import annotations

from dataclasses import asdict

from .code_architecture_analysis import CodeArchitectureAnalysis
from .code_coverage_analysis import CodeCoverageAnalysis
from .code_engineering_analytics import CodeEngineeringAnalytics
from .code_external_evidence import (
    ExternalEvidenceStatus,
    external_status_digest_payload,
)
from .code_unused_analysis import CodeUnusedAnalysis
from .code_supply_chain_analysis import CodeSupplyChainAnalysis
from .code_review_models import (
    CODE_REVIEW_SCHEMA,
    CodeReviewCoverage,
    CodeReviewDigest,
    CodeReviewFinding,
    CodeReviewRecommendation,
    CodeReviewResult,
    CodeReviewSnapshot,
    CodeReviewWorkPackage,
    RecommendationStatus,
)
from .external_evidence_models import ExternalEvidenceSuiteStatus
from .semantic_models import canonical_json, fingerprint_text


def build_code_review_digest(
    snapshot: CodeReviewSnapshot,
    coverage: CodeReviewCoverage,
    findings: tuple[CodeReviewFinding, ...],
    *,
    ranking: str,
    actionability_version: str,
    recommendation_status: RecommendationStatus,
    recommendation_reason: str | None,
    recommendations: tuple[CodeReviewRecommendation, ...],
    planning_version: str,
    work_package_status: RecommendationStatus,
    work_package_reason: str | None,
    work_packages: tuple[CodeReviewWorkPackage, ...],
    external_evidence: ExternalEvidenceStatus,
    external_evidence_suite: ExternalEvidenceSuiteStatus,
    architecture: CodeArchitectureAnalysis,
    test_coverage: CodeCoverageAnalysis,
    engineering_analytics: CodeEngineeringAnalytics,
    unused_analysis: CodeUnusedAnalysis,
    supply_chain: CodeSupplyChainAnalysis,
    limitations: tuple[str, ...],
) -> CodeReviewDigest:
    """Hash every decision-bearing field while excluding local database paths."""

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
            "unused_analysis": unused_analysis.digest_payload(),
            "supply_chain": {
                "schema": supply_chain.as_payload()["schema"],
                "status": supply_chain.status,
                "reason": supply_chain.reason,
                "digest": asdict(supply_chain.digest),
            },
            "limitations": list(limitations),
        }
    )
    fingerprint = fingerprint_text(payload)
    return CodeReviewDigest(
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
        fingerprint.byte_count,
    )


def rebuild_code_review_result_digest(result: CodeReviewResult) -> CodeReviewDigest:
    """Recompute a ready envelope digest from every evidence-bearing projection."""

    required = (
        result.snapshot,
        result.coverage,
        result.external_evidence,
        result.external_evidence_suite,
        result.architecture,
        result.test_coverage,
        result.engineering_analytics,
        result.unused_analysis,
        result.supply_chain,
    )
    if any(item is None for item in required):
        raise ValueError("ready code-review result lacks evidence required by its digest")
    assert result.snapshot is not None
    assert result.coverage is not None
    assert result.external_evidence is not None
    assert result.external_evidence_suite is not None
    assert result.architecture is not None
    assert result.test_coverage is not None
    assert result.engineering_analytics is not None
    assert result.unused_analysis is not None
    assert result.supply_chain is not None
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
        unused_analysis=result.unused_analysis,
        supply_chain=result.supply_chain,
        limitations=result.limitations,
    )


__all__ = ["build_code_review_digest", "rebuild_code_review_result_digest"]
