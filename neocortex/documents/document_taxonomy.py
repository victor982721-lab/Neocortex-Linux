"""Versioned, explainable taxonomy for technical document classification.

This public facade keeps the stable taxonomy contract while focused modules own
value models, built-in vocabulary, bounded overlays, and evidence specialists.
"""

from __future__ import annotations
from dataclasses import dataclass

from .document_naming import NAMING_VERSION, suggest_document_stem
from .document_signals import (
    classification_path_signal,
    fold_signal,
    is_framework_managed_path,
)
from .document_taxonomy_entities import (
    _audio_kind_adjustment,
    _client_evidence,
    _document_subtype_evidence,
    _document_topic_adjustment,
    _managed_top_level_is,
    _organization_evidence,
    _pattern_evidence,
    _project_evidence,
    _specific_equipment_evidence,
    _technical_reference_evidence,
    _workstream_evidence,
)
from .document_taxonomy_kinds import (
    _calibrated_kind_confidence,
    _kind_evidence,
)
from .document_taxonomy_models import (
    AuthoritySpec,
    ClientSpec,
    DocumentClassification,
    DocumentSignals,
    EntityRoleEvidence,
    OrganizationSpec,
    ProjectSpec,
    ScoredLabel,
    StandardReference,
    TechnicalTaxonomy,
)
from .document_taxonomy_overlay import (
    MAX_TAXONOMY_BYTES,
    MAX_TAXONOMY_PATTERN_CHARS,
    MAX_TAXONOMY_PATTERNS,
    MAX_TAXONOMY_SEQUENCE_ITEMS,
    MAX_TAXONOMY_TABLES_PER_SECTION,
    MAX_TAXONOMY_TEXT_CHARS,
    load_taxonomy,
)
from .document_taxonomy_references import (
    _authority_evidence,
    _document_authority_adjustment,
    _naming_reference_rank,
)
from .document_taxonomy_roles import (
    RoleAssessment,
    document_role_assessment,
    entity_role_evidence,
)
from .document_taxonomy_vocabulary import (
    BUILTIN_TAXONOMY_VERSION,
    _ACTIVITY_PATTERNS,
    _TOPIC_PATTERNS,
    builtin_taxonomy,
    semantic_label_inventory,
)


__all__ = (  # noqa: RUF022
    "AuthoritySpec",
    "BUILTIN_TAXONOMY_VERSION",
    "CLASSIFIER_VERSION",
    "ClientSpec",
    "DocumentClassification",
    "DocumentSignals",
    "EntityRoleEvidence",
    "MAX_TAXONOMY_BYTES",
    "MAX_TAXONOMY_PATTERNS",
    "MAX_TAXONOMY_PATTERN_CHARS",
    "MAX_TAXONOMY_SEQUENCE_ITEMS",
    "MAX_TAXONOMY_TABLES_PER_SECTION",
    "MAX_TAXONOMY_TEXT_CHARS",
    "OrganizationSpec",
    "ProjectSpec",
    "ScoredLabel",
    "StandardReference",
    "TechnicalTaxonomy",
    "builtin_taxonomy",
    "classify_document",
    "document_classifier_signature",
    "load_taxonomy",
    "semantic_label_inventory",
)


# region [01] Stable public classification contract

CLASSIFIER_VERSION = "technical-document-classifier-v15"


def document_classifier_signature(taxonomy: TechnicalTaxonomy) -> str:
    """Version classification and semantic naming as one durable cache unit."""

    return f"{CLASSIFIER_VERSION}|{taxonomy.signature}|{NAMING_VERSION}"


# endregion [01]


# region [02] Classification orchestration


@dataclass(frozen=True, slots=True)
class _ClassificationContext:
    managed_path: bool
    managed_normative_path: bool
    scopes: dict[str, str]
    folded: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ClassificationEvidence:
    standards: tuple[StandardReference, ...]
    authorities: tuple[ScoredLabel, ...]
    organizations: tuple[ScoredLabel, ...]
    clients: tuple[ScoredLabel, ...]
    projects: tuple[ScoredLabel, ...]
    workstreams: tuple[ScoredLabel, ...]
    topics: tuple[ScoredLabel, ...]
    kinds: tuple[ScoredLabel, ...]
    document_subtypes: tuple[ScoredLabel, ...]
    equipment: tuple[ScoredLabel, ...]
    activities: tuple[ScoredLabel, ...]
    role_assessment: RoleAssessment


@dataclass(frozen=True, slots=True)
class _ClassificationDecision:
    primary: ScoredLabel
    confidence: float
    uncertainty: str


def _classification_context(signals: DocumentSignals) -> _ClassificationContext:
    managed_path = is_framework_managed_path(signals.path)
    scopes = {
        "path": classification_path_signal(signals.path),
        "title": signals.title,
        "author": signals.author,
        "metadata": signals.metadata,
        "opening": signals.leading_text[:4_000],
        "text": signals.leading_text[4_000:],
    }
    return _ClassificationContext(
        managed_path=managed_path,
        managed_normative_path=_managed_top_level_is(signals.path, "NORMATIVA"),
        scopes=scopes,
        folded={name: fold_signal(value) for name, value in scopes.items() if value},
    )


def _collect_document_evidence(
    signals: DocumentSignals,
    taxonomy: TechnicalTaxonomy,
    context: _ClassificationContext,
) -> _ClassificationEvidence:
    standards, authorities = _authority_evidence(
        context.folded,
        taxonomy.authorities,
        raw_scopes=context.scopes,
        managed_path=context.managed_path,
    )
    authorities = _document_authority_adjustment(
        context.folded,
        standards,
        authorities,
    )
    organizations = _organization_evidence(context.folded, taxonomy.organizations)
    projects = _project_evidence(context.folded, taxonomy.projects)
    clients = _client_evidence(
        context.folded,
        taxonomy.clients,
        taxonomy.projects,
        projects,
    )
    workstreams = _workstream_evidence(context.folded)
    topics = _pattern_evidence(context.folded, _TOPIC_PATTERNS, base_score=0.38)
    equipment = _specific_equipment_evidence(context.folded)
    activities = _pattern_evidence(context.folded, _ACTIVITY_PATTERNS, base_score=0.40)
    kinds = _kind_evidence(
        context.folded,
        standards,
        organizations,
        primary_authority=authorities[0].label if authorities else None,
        page_count=signals.page_count,
        managed_path=context.managed_path,
        managed_normative_path=context.managed_normative_path,
    )
    if not kinds and signals.source_kind != "audio":
        kinds = _technical_reference_evidence(topics)
    if signals.source_kind == "audio":
        kinds = _audio_kind_adjustment(
            kinds,
            context.folded,
            managed_path=context.managed_path,
        )
    role_assessment = document_role_assessment(signals, context.folded, kinds)
    kinds = role_assessment.kinds
    primary = kinds[0] if kinds else ScoredLabel("otro", 0.35, ("sin_regla_fuerte",))
    document_subtypes = _document_subtype_evidence(
        context.folded,
        primary_kind=primary.label,
        primary_authority=authorities[0].label if authorities else None,
        activities=activities,
    )
    return _ClassificationEvidence(
        standards=standards,
        authorities=authorities,
        organizations=organizations,
        clients=clients,
        projects=projects,
        workstreams=workstreams,
        topics=topics,
        kinds=kinds,
        document_subtypes=document_subtypes,
        equipment=equipment,
        activities=activities,
        role_assessment=role_assessment,
    )


def _kind_ambiguity(
    kinds: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, bool, bool]:
    primary = kinds[0] if kinds else ScoredLabel("otro", 0.35, ("sin_regla_fuerte",))
    score_margin = round(kinds[0].score - kinds[1].score, 6) if len(kinds) > 1 else 1.0
    formal_normative_primary = primary.label == "normativa" and any(
        item.startswith("opening:estructura_normativa=") for item in primary.evidence
    )
    ambiguous = len(kinds) > 1 and score_margin < 0.10 and not formal_normative_primary
    normative_ambiguity = (
        len(kinds) > 1
        and score_margin < 0.12
        and "normativa" in {kinds[0].label, kinds[1].label}
        and not formal_normative_primary
    )
    return primary, ambiguous, normative_ambiguity


def _classification_confidence(
    signals: DocumentSignals,
    evidence: _ClassificationEvidence,
    primary: ScoredLabel,
    *,
    ambiguous: bool,
    normative_ambiguity: bool,
) -> float:
    confidence = primary.score
    if primary.label == "normativa" and evidence.standards:
        confidence = max(confidence, 0.92)
    if primary.label in {"formato_empresa", "manual_equipo"} and evidence.organizations:
        confidence = min(0.97, confidence + 0.07)
    if signals.source_status != "partial" and not ambiguous:
        confidence = max(confidence, _calibrated_kind_confidence(primary))
    if signals.source_status == "partial":
        confidence = max(0.0, confidence - 0.08)
    if ambiguous:
        confidence = max(0.0, confidence - 0.08)
    if normative_ambiguity:
        confidence = min(confidence, 0.67)
    return round(min(1.0, confidence), 6)


def _classification_uncertainty(
    confidence: float,
    *,
    ambiguous: bool,
    normative_ambiguity: bool,
) -> str:
    if normative_ambiguity:
        return "alta"
    if confidence >= 0.82 and not ambiguous:
        return "baja"
    if confidence >= 0.68:
        return "media"
    return "alta"


def _classification_decision(
    signals: DocumentSignals,
    evidence: _ClassificationEvidence,
) -> _ClassificationDecision:
    primary, ambiguous, normative_ambiguity = _kind_ambiguity(evidence.kinds)
    confidence = _classification_confidence(
        signals,
        evidence,
        primary,
        ambiguous=ambiguous,
        normative_ambiguity=normative_ambiguity,
    )
    return _ClassificationDecision(
        primary,
        confidence,
        _classification_uncertainty(
            confidence,
            ambiguous=ambiguous,
            normative_ambiguity=normative_ambiguity,
        ),
    )


def _combined_evidence(
    evidence: _ClassificationEvidence,
    topics: tuple[ScoredLabel, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item
            for label in (
                *evidence.kinds[:3],
                *evidence.document_subtypes[:2],
                *evidence.authorities[:3],
                *evidence.organizations[:3],
                *evidence.clients[:2],
                *evidence.projects[:2],
                *evidence.workstreams[:2],
                *evidence.equipment[:3],
                *evidence.activities[:3],
                *topics[:3],
            )
            for item in label.evidence[:4]
        )
    )


def _materialize_document_classification(
    signals: DocumentSignals,
    taxonomy: TechnicalTaxonomy,
    evidence: _ClassificationEvidence,
    decision: _ClassificationDecision,
    *,
    managed_path: bool,
) -> DocumentClassification:
    topics = _document_topic_adjustment(evidence.topics, decision.primary.label)
    primary_authority = evidence.authorities[0].label if evidence.authorities else None
    naming_references = tuple(
        sorted(
            evidence.standards,
            key=lambda reference: _naming_reference_rank(
                reference,
                signals=signals,
                primary_authority=primary_authority,
                managed_path=managed_path,
            ),
        )
    )
    if decision.primary.label.startswith(("informe_", "reporte_")) or decision.primary.label == "registro_log":
        # Referenced standards stay searchable as references, not issuer/type
        # evidence or a prefix that impersonates the cited source document.
        naming_references = ()
    entity_roles = entity_role_evidence(
        signals, taxonomy, decision.primary, evidence.authorities,
        evidence.organizations, evidence.standards,
    )
    issuers = tuple(item for item in entity_roles if item.role == "issuer")
    role_assessment = evidence.role_assessment
    naming_organization = evidence.organizations[0].label if evidence.organizations else None
    if decision.primary.label == "registro_log" or "cited_standard_not_document_role" in role_assessment.contradictions:
        naming_organization = issuers[0].entity if issuers else None
    naming = suggest_document_stem(
        path=signals.path,
        title=signals.title,
        leading_text=signals.leading_text,
        primary_kind=decision.primary.label,
        standard_identifiers=(reference.identifier for reference in naming_references),
        organization=naming_organization,
        topic=topics[0].label if topics else None,
    )
    return DocumentClassification(
        classifier_signature=document_classifier_signature(taxonomy),
        primary_kind=decision.primary.label,
        kind_candidates=evidence.kinds,
        authorities=evidence.authorities,
        standard_references=evidence.standards,
        organizations=evidence.organizations,
        clients=evidence.clients,
        projects=evidence.projects,
        workstreams=evidence.workstreams,
        topics=topics,
        document_subtypes=evidence.document_subtypes,
        equipment=evidence.equipment,
        activities=evidence.activities,
        confidence=decision.confidence,
        uncertainty=decision.uncertainty,
        evidence=_combined_evidence(evidence, topics)[:24],
        suggested_stem=naming.stem,
        naming_signature=NAMING_VERSION,
        naming_evidence=naming.evidence,
        document_role=role_assessment.outside_role or decision.primary.label,
        role_evidence=role_assessment.outside_evidence or decision.primary.evidence,
        entity_roles=entity_roles,
        issuer_status=(
            "declared" if any(item.evidence_kind == "declaration" for item in issuers)
            else "inferred" if issuers else "unknown"
        ),
        taxonomy_status=(
            "outside_taxonomy" if role_assessment.outside_role is not None
            else "insufficient_identification" if decision.primary.label == "otro"
            else "in_taxonomy"
        ),
        contradictions=role_assessment.contradictions,
        unknowns=("issuer_identity_unverified",) if issuers else ("issuer_unverified",),
    )


def classify_document(
    signals: DocumentSignals,
    taxonomy: TechnicalTaxonomy | None = None,
) -> DocumentClassification:
    """Classify one bounded signal set and retain every supporting rule."""

    active = taxonomy or builtin_taxonomy()
    context = _classification_context(signals)
    evidence = _collect_document_evidence(signals, active, context)
    decision = _classification_decision(signals, evidence)
    return _materialize_document_classification(
        signals,
        active,
        evidence,
        decision,
        managed_path=context.managed_path,
    )


# endregion [02]
