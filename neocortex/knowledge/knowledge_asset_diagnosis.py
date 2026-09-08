"""Pure federation of published owner evidence into scoped asset findings."""

from __future__ import annotations

from pathlib import PurePath

from .knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty as Certainty,
    AssetDiagnosticEvidenceRef,
    AssetDiagnosticFinding as Finding,
    AssetDiagnosticObservation,
    AssetDiagnosticObservationKind as ObservationKind,
    AssetDiagnosticRecommendation as Recommendation,
    AssetProblemScope as Scope,
    KnowledgeAssetDiagnosis,
)
from .knowledge_asset_health_contracts import (
    KnowledgeAssetHealthFact,
    KnowledgeAssetHealthReport,
    KnowledgeAssetHealthStage as Stage,
)


def _evidence(
    report: KnowledgeAssetHealthReport, fact: KnowledgeAssetHealthFact
) -> tuple[AssetDiagnosticEvidenceRef, ...]:
    return (
        AssetDiagnosticEvidenceRef(
            owner=fact.owner,
            record_id=fact.record_id,
            resource_id=report.resource_id,
            snapshot_id=report.fact_snapshot_id or "unavailable",
            projection_digest=fact.projection_digest,
            publication_id=fact.publication_id,
        ),
    )


def _source_findings(
    report: KnowledgeAssetHealthReport,
    source: KnowledgeAssetHealthFact | None,
) -> tuple[tuple[Finding, ...], tuple[Recommendation, ...]]:
    if source is None:
        return (
            Finding(
                Scope.FILE,
                "physical_integrity_not_assessed",
                Certainty.UNKNOWN,
                missing_checks=("file_integrity_inspection",),
            ),
            Finding(
                Scope.PROCESSING,
                "published_processing_evidence_missing",
                Certainty.UNKNOWN,
                missing_checks=("published_source_result",),
            ),
        ), ()
    refs = _evidence(report, source)
    if source.status == "protected":
        return (
            Finding(
                Scope.FILE,
                "protected_not_inspected",
                Certainty.UNKNOWN,
                refs,
                ("authorized_content_inspection", "file_integrity_inspection"),
            ),
            Finding(Scope.PROCESSING, "processing_blocked_by_protection", Certainty.OBSERVED, refs),
        ), (
            Recommendation(
                Scope.PROCESSING,
                "inspect_with_authorized_access",
                refs,
                ("authorized_content_inspection",),
                ("authorized_access", "bounded_read_only_inspection"),
            ),
        )
    processing_code = {
        "complete": "published_processing_complete",
        "done": "published_processing_complete",
        "partial": "published_processing_partial",
        "degraded": "published_processing_partial",
        "processing": "processing_incomplete",
        "error": "published_processing_failed",
        "failed": "published_processing_failed",
    }.get(source.status, "processing_status_not_interpreted")
    findings = (
        Finding(
            Scope.FILE,
            "physical_integrity_not_assessed",
            Certainty.UNKNOWN,
            refs,
            ("file_integrity_inspection",),
        ),
        Finding(Scope.PROCESSING, processing_code, Certainty.OBSERVED, refs),
    )
    recommendations: tuple[Recommendation, ...] = ()
    if source.status in {"error", "failed", "partial", "degraded"}:
        recommendations = (
            Recommendation(
                Scope.PROCESSING,
                "inspect_processing_evidence",
                refs,
                ("failure_cause", "retry_safety"),
                ("bounded_read_only_inspection", "source_snapshot_revalidation"),
            ),
        )
    return findings, recommendations


def _observation_projection(
    observation: AssetDiagnosticObservation,
) -> tuple[tuple[Finding, ...], tuple[Recommendation, ...]]:
    refs = observation.evidence_refs
    if observation.kind is ObservationKind.DUPLICATE_CONTENT:
        preference_recorded = observation.selection_basis in {
            "explicit_user_decision",
            "preferred_location",
        }
        missing_set = set(observation.missing_checks) | {"retention_and_dependency_review"}
        if not preference_recorded:
            missing_set.add("contextual_keeper_selection")
        missing = tuple(sorted(missing_set))
        return (
            Finding(Scope.FILE, "duplicate_content_proved", Certainty.OBSERVED, refs),
            Finding(
                Scope.POLICY,
                "keeper_preference_recorded"
                if preference_recorded
                else "preferred_keeper_location_unresolved",
                Certainty.OBSERVED if preference_recorded else Certainty.UNKNOWN,
                refs,
                missing,
            ),
        ), (
            Recommendation(
                Scope.POLICY,
                "review_disposal_preconditions"
                if preference_recorded
                else "review_context_before_selecting_keeper",
                refs,
                missing,
                ("explicit_mutation_authorization", "physical_revalidation", "reviewed_keeper"),
            ),
        )
    if observation.kind is ObservationKind.LOGICAL_FORMAT:
        missing = tuple(
            sorted(
                set(observation.missing_checks)
                | {
                    "file_integrity_inspection",
                    "opening_verification",
                }
            )
        )
        return (
            Finding(Scope.FILE, observation.code, Certainty.INFERRED, refs, missing),
            Finding(
                Scope.POLICY,
                "format_identification_not_disposal_evidence",
                Certainty.OBSERVED,
                refs,
            ),
        ), (
            Recommendation(
                Scope.FILE,
                "review_format_identification",
                refs,
                missing,
                ("explicit_metadata_or_rename_authorization", "physical_revalidation"),
            ),
        )
    return (
        Finding(
            Scope.DOCUMENT_CONDITION,
            "condition_described_in_document",
            Certainty.OBSERVED,
            refs,
            observation.missing_checks,
        ),
    ), ()


def build_knowledge_asset_diagnosis(
    report: KnowledgeAssetHealthReport,
    *,
    observations: tuple[AssetDiagnosticObservation, ...] | None = None,
) -> KnowledgeAssetDiagnosis:
    """Explain evidence without probing files, running tools, or granting effects.

    Health v1 deliberately describes alignment. Search eligibility is *not* an
    observed index posting, and successful parsing is not an integrity proof.
    Additional owner observations must be captured under the caller's fence.
    """

    if not isinstance(report, KnowledgeAssetHealthReport):
        raise TypeError("report must be a KnowledgeAssetHealthReport")
    selected = report.diagnostic_observations if observations is None else observations
    if not isinstance(selected, tuple) or any(
        not isinstance(item, AssetDiagnosticObservation)
        or item.resource_id != report.resource_id
        or any(ref.snapshot_id != report.knowledge_snapshot_id for ref in item.evidence_refs)
        for item in selected
    ):
        raise ValueError("observations must be a typed tuple bound to the report resource")
    if report.snapshot_consistency != "stable":
        # A changed snapshot cannot support fresh assertions, even if its last
        # sampled records look plausible. The original report retains those refs.
        return KnowledgeAssetDiagnosis(
            report.resource_id,
            tuple(
                Finding(
                    scope,
                    "snapshot_not_stable",
                    Certainty.UNKNOWN,
                    missing_checks=("stable_owner_snapshot",),
                )
                for scope in Scope
            ),
            (),
        )
    facts = {fact.stage: fact for fact in report.facts}
    findings_tuple, recommendations_tuple = _source_findings(report, facts.get(Stage.SOURCE_OWNER))
    findings = list(findings_tuple)
    recommendations = list(recommendations_tuple)
    if report.diagnostic_gaps:
        findings.append(
            Finding(
                Scope.FILE,
                "supplemental_owner_evidence_incomplete",
                Certainty.UNKNOWN,
                missing_checks=report.diagnostic_gaps,
            )
        )
    search = facts.get(Stage.KNOWLEDGE_SEARCH)
    if search is None:
        findings.append(
            Finding(
                Scope.INDEX,
                "index_presence_not_verified",
                Certainty.UNKNOWN,
                missing_checks=("published_index_posting",),
            )
        )
    else:
        refs = _evidence(report, search)
        code = (
            "search_eligible_not_index_verified"
            if search.status == "eligible"
            else (
                "search_projection_excluded"
                if search.status == "excluded"
                else "search_status_observed"
            )
        )
        findings.append(
            Finding(Scope.INDEX, code, Certainty.OBSERVED, refs, ("published_index_posting",))
        )
    inventory = facts.get(Stage.INVENTORY)
    inventory_refs = () if inventory is None else _evidence(report, inventory)
    path = (
        ""
        if inventory is None
        else next((item.value for item in inventory.values if item.name == "path"), "")
    )
    findings.append(
        Finding(
            Scope.POLICY,
            "dispensability_not_demonstrated",
            Certainty.UNKNOWN,
            inventory_refs,
            ("retention_and_dependency_review",),
        )
    )
    if PurePath(path).suffix.casefold() == ".tmp" and inventory_refs:
        findings.append(
            Finding(Scope.FILE, "temporary_suffix_observed", Certainty.OBSERVED, inventory_refs)
        )
        recommendations.append(
            Recommendation(
                Scope.POLICY,
                "inspect_usage_and_retention",
                inventory_refs,
                ("independent_disposability", "retention_and_dependency_review"),
                ("bounded_read_only_inspection", "source_snapshot_revalidation"),
            )
        )
    if not any(item.kind is ObservationKind.DOCUMENT_CONDITION for item in selected):
        findings.append(
            Finding(
                Scope.DOCUMENT_CONDITION,
                "document_condition_not_assessed",
                Certainty.UNKNOWN,
                missing_checks=("document_claim_evidence",),
            )
        )
    for observation in selected:
        observed_findings, observed_recommendations = _observation_projection(observation)
        findings.extend(observed_findings)
        recommendations.extend(observed_recommendations)
    return KnowledgeAssetDiagnosis(
        report.resource_id,
        tuple(findings),
        tuple(recommendations),
        selected,
    )


__all__ = ["build_knowledge_asset_diagnosis"]
