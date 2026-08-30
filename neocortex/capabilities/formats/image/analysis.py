"""Composition boundary for the modular image-analysis pipeline.

Models, feature extraction, semantic evidence and decision policy remain
independent while this module assembles the public classification operation.
"""

from __future__ import annotations
from dataclasses import replace
from pathlib import Path

from .adult import (
    DEFAULT_ADULT_CLASSIFIER,
    AdultContentClassifier,
)

from .decision import (
    add,
    attribute_confidence,
    classify as _classify,
    classify_photo_attributes,
    requires_document_verification,
)
from .document import (
    DocumentVerifierRuntime,
    verify_document_text,
)
from .features import (
    ImageMemoryGate,
    ImageResourceLimits,
    entropy,
    estimated_image_memory_bytes,
    exif_number,
    extract_features,
    is_skin_tone,
    projection_features,
)
from .models import (
    AdultContentEvidence,
    AdultDetection,
    Decision,
    DocumentCandidate,
    Features,
    IndustrialContext,
    PhotoAttributes,
    SemanticLabel,
    VisualSemanticEvidence,
)
from .visual import (
    DEFAULT_VISUAL_CLASSIFIER,
    FeatureVisualClassifier,
    VisualSemanticClassifier,
)
from .policy import (
    ANALYSIS_VERSION,
    CATEGORY_DIRS,
    DECISION_VERSION,
    FEATURE_VERSION,
    GENERATED_DIRS,
    IMAGE_SUFFIXES,
    MIB,
    NAME_HINT_POINTS,
    NAME_HINTS,
    PROFILE_EXCLUDED_DIRS,
    SAMPLE_SIDE,
)
from .semantics import (
    cached_features_are_compatible,
    classify_industrial_context,
    normalize_text,
    phrase_matches,
    textual_context,
)


# region [01] Stable public entry point


def classify(
    path: Path,
    root: Path,
    memory_gate: ImageMemoryGate | None = None,
    *,
    features: Features | None = None,
    document_verifier: DocumentVerifierRuntime | None = None,
    visual_classifier: VisualSemanticClassifier = DEFAULT_VISUAL_CLASSIFIER,
    adult_classifier: AdultContentClassifier = DEFAULT_ADULT_CLASSIFIER,
    analyze_adult: bool = True,
) -> Decision:
    """Classify through modular components while preserving patchable seams."""

    decision = _classify(
        path,
        root,
        memory_gate,
        features=features,
        document_verifier=document_verifier,
        feature_extractor=extract_features,
        verifier=verify_document_text,
        visual_classifier=visual_classifier,
    )
    if not analyze_adult:
        return decision
    adult_content = adult_classifier.classify(
        path,
        decision.category,
        decision.features,
        decision.document_candidate,
    )
    return replace(decision, adult_content=adult_content)


# endregion [01]


__all__ = [
    "ANALYSIS_VERSION",
    "CATEGORY_DIRS",
    "DECISION_VERSION",
    "DEFAULT_VISUAL_CLASSIFIER",
    "FEATURE_VERSION",
    "GENERATED_DIRS",
    "IMAGE_SUFFIXES",
    "MIB",
    "NAME_HINTS",
    "NAME_HINT_POINTS",
    "PROFILE_EXCLUDED_DIRS",
    "SAMPLE_SIDE",
    "AdultContentClassifier",
    "AdultContentEvidence",
    "AdultDetection",
    "Decision",
    "DocumentCandidate",
    "FeatureVisualClassifier",
    "Features",
    "ImageMemoryGate",
    "ImageResourceLimits",
    "IndustrialContext",
    "PhotoAttributes",
    "SemanticLabel",
    "VisualSemanticClassifier",
    "VisualSemanticEvidence",
    "add",
    "attribute_confidence",
    "cached_features_are_compatible",
    "classify",
    "classify_industrial_context",
    "classify_photo_attributes",
    "entropy",
    "estimated_image_memory_bytes",
    "exif_number",
    "extract_features",
    "is_skin_tone",
    "normalize_text",
    "phrase_matches",
    "projection_features",
    "requires_document_verification",
    "textual_context",
    "verify_document_text",
]
