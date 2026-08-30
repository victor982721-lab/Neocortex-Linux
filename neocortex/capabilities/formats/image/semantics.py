"""Path-derived semantic evidence for image classification.

This module never claims visual recognition. It preserves the provenance and
uncertainty of labels inferred only from file and directory names.
"""

from __future__ import annotations
import unicodedata
from pathlib import Path
from typing import Iterable

from .document import DocumentTextEvidence
from .models import (
    IndustrialContext,
    SemanticLabel,
    VisualSemanticEvidence,
)
from .policy import (
    COMPATIBLE_FEATURE_PREFIXES,
    FEATURE_VERSION,
    GENERATED_DIRS,
    INDUSTRIAL_ACTIVITY_HINTS,
    INDUSTRIAL_ENTITY_HINTS,
    LEGACY_FEATURE_SIGNATURES,
    OPERATIONAL_CONTEXT_HINTS,
    SAFETY_CONDITION_HINTS,
    TOKEN_RE,
)


# region [01] Normalized path evidence


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(TOKEN_RE.findall(value))


def textual_context(path: Path, root: Path) -> str:
    """Omit generated folders so reclassification does not bias itself."""

    try:
        parts = path.relative_to(root).with_suffix("").parts
    except ValueError:
        parts = path.with_suffix("").parts
    generated = {part.casefold() for part in GENERATED_DIRS}
    useful = [part for part in parts if part.casefold() not in generated]
    return normalize_text(" ".join(useful))


def phrase_matches(text: str, phrases: Iterable[str]) -> list[str]:
    padded = f" {text} "
    matches = []
    for phrase in phrases:
        normalized = normalize_text(phrase)
        if normalized and f" {normalized} " in padded:
            matches.append(phrase)
    return matches


def cached_features_are_compatible(processing_signature: str | None) -> bool:
    """Allow decision-only upgrades to reuse bounded v3 pixel features."""

    if processing_signature in LEGACY_FEATURE_SIGNATURES:
        return True
    return bool(
        processing_signature
        and processing_signature.startswith(
            (
                *COMPATIBLE_FEATURE_PREFIXES,
                f"image-route-v4|{FEATURE_VERSION}|",
                f"psig-v1|image|{FEATURE_VERSION}|",
            )
        )
    )


# endregion [01]


# region [02] Industrial vocabulary classification


def _semantic_labels(
    context: str,
    hints: dict[str, tuple[str, ...]],
) -> tuple[SemanticLabel, ...]:
    labels: list[SemanticLabel] = []
    for label, phrases in hints.items():
        matches = phrase_matches(context, phrases)
        if not matches:
            continue
        score = min(0.78, 0.56 + 0.07 * (len(matches) - 1))
        labels.append(
            SemanticLabel(
                label=label,
                score=round(score, 3),
                evidence=tuple(f"nombre/ruta:{value}" for value in matches[:3]),
                provenance="path-keywords-v1",
            )
        )
    return tuple(sorted(labels, key=lambda item: (-item.score, item.label)))


def _ocr_semantic_labels(labels: tuple[str, ...]) -> tuple[SemanticLabel, ...]:
    return tuple(
        SemanticLabel(
            label=label,
            score=0.64,
            evidence=(f"ocr-keywords:{label}",),
            provenance="ocr-keywords-v1",
        )
        for label in sorted(set(labels))
    )


def _merge_semantic_labels(
    *sources: tuple[SemanticLabel, ...],
) -> tuple[SemanticLabel, ...]:
    merged: dict[str, SemanticLabel] = {}
    for source in sources:
        for item in source:
            prior = merged.get(item.label)
            if prior is None:
                merged[item.label] = item
                continue
            provenances = tuple(
                dict.fromkeys(
                    (*prior.provenance.split("+"), *item.provenance.split("+"))
                )
            )
            merged[item.label] = SemanticLabel(
                label=item.label,
                score=round(min(0.86, max(prior.score, item.score) + 0.08), 3),
                evidence=tuple(dict.fromkeys((*prior.evidence, *item.evidence))),
                provenance="+".join(provenances),
            )
    return tuple(sorted(merged.values(), key=lambda value: (-value.score, value.label)))


def _document_semantic_sources(
    document_text: DocumentTextEvidence | None,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    if document_text is None or not document_text.available:
        return (), (), (), ()
    return (
        document_text.industrial_entities,
        document_text.industrial_activities,
        document_text.industrial_operational_contexts,
        document_text.industrial_safety_conditions,
    )


def _visual_semantic_sources(
    visual: VisualSemanticEvidence | None,
) -> tuple[
    tuple[SemanticLabel, ...],
    tuple[SemanticLabel, ...],
    tuple[SemanticLabel, ...],
    tuple[SemanticLabel, ...],
]:
    if visual is None:
        return (), (), (), ()
    return (
        visual.entities,
        visual.activities,
        visual.operational_contexts,
        visual.safety_conditions,
    )


def _classify_semantic_family(
    context: str,
    hints: dict[str, tuple[str, ...]],
    document_labels: tuple[str, ...],
    visual_labels: tuple[SemanticLabel, ...],
) -> tuple[SemanticLabel, ...]:
    return _merge_semantic_labels(
        _semantic_labels(context, hints),
        _ocr_semantic_labels(document_labels),
        visual_labels,
    )


def _semantic_provenances(
    groups: tuple[tuple[SemanticLabel, ...], ...],
) -> set[str]:
    return {item.provenance for group in groups for item in group}


def _industrial_context_uncertainty(
    provenances: set[str],
    visual: VisualSemanticEvidence | None,
) -> str:
    has_path = any("path-keywords-v1" in value for value in provenances)
    has_ocr = any("ocr-keywords-v1" in value for value in provenances)
    has_visual = any("visual-" in value for value in provenances)
    if has_visual and (has_path or has_ocr):
        return "evidencia_multifuente_con_componente_visual_no_calibrado"
    if has_path and has_ocr:
        return "evidencia_semantica_indirecta_de_ruta_y_ocr"
    if has_visual:
        return (
            visual.uncertainty
            if visual is not None
            else "evidencia_visual_no_calibrada"
        )
    if has_ocr:
        return "evidencia_semantica_limitada_a_ocr"
    if has_path:
        return "evidencia_semantica_limitada_a_nombre_y_ruta"
    return "sin_evidencia_semantica_suficiente"


def classify_industrial_context(
    context: str,
    document_text: DocumentTextEvidence | None = None,
    visual: VisualSemanticEvidence | None = None,
) -> IndustrialContext:
    """Fuse path, OCR and visual labels while retaining their provenance."""

    (
        document_entities,
        document_activities,
        document_operational,
        document_safety,
    ) = _document_semantic_sources(document_text)
    (
        visual_entities,
        visual_activities,
        visual_operational,
        visual_safety,
    ) = _visual_semantic_sources(visual)
    entities = _classify_semantic_family(
        context,
        INDUSTRIAL_ENTITY_HINTS,
        document_entities,
        visual_entities,
    )
    activities = _classify_semantic_family(
        context,
        INDUSTRIAL_ACTIVITY_HINTS,
        document_activities,
        visual_activities,
    )
    operational = _classify_semantic_family(
        context,
        OPERATIONAL_CONTEXT_HINTS,
        document_operational,
        visual_operational,
    )
    safety = _classify_semantic_family(
        context,
        SAFETY_CONDITION_HINTS,
        document_safety,
        visual_safety,
    )
    provenances = _semantic_provenances(
        (entities, activities, operational, safety)
    )
    return IndustrialContext(
        entities=entities,
        activities=activities,
        operational_contexts=operational,
        safety_conditions=safety,
        uncertainty=_industrial_context_uncertainty(provenances, visual),
        provenance=tuple(sorted(provenances)),
    )


# endregion [02]
