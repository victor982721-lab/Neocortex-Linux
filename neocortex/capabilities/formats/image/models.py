"""Stable data contracts shared by image analysis components."""

from __future__ import annotations
from dataclasses import dataclass

from .document import DocumentTextEvidence


# region [01] Extracted evidence


@dataclass(frozen=True)
class Features:
    width: int
    height: int
    file_size: int
    format: str
    frames: int
    has_transparency: bool
    alpha_fraction: float
    has_camera_exif: bool
    white_fraction: float
    light_fraction: float
    dark_fraction: float
    neutral_fraction: float
    colorfulness: float
    brightness_mean: float
    brightness_std: float
    entropy: float
    edge_strength: float
    edge_fraction: float
    quantized_colors: int
    border_white_fraction: float
    long_horizontal_lines: float
    long_vertical_lines: float
    text_band_fraction: float
    top_blue_fraction: float
    green_fraction: float
    warm_fraction: float
    skin_fraction: float
    central_skin_fraction: float
    flash_fired: bool
    iso: float | None
    exposure_time: float | None
    focal_length_35mm: float | None
    decode_quality: str = "strict"
    decode_provenance: str = "pillow-strict-v1"


@dataclass(frozen=True)
class PhotoAttributes:
    orientation: str
    lighting: str
    lighting_confidence: float
    scene: str
    scene_confidence: float
    panoramic: bool
    flash: bool
    exposure: str
    selfie_candidate: bool
    reasons: tuple[str, ...]


# endregion [01]


# region [02] Semantic evidence and final decision


@dataclass(frozen=True)
class SemanticLabel:
    label: str
    score: float
    evidence: tuple[str, ...]
    provenance: str


@dataclass(frozen=True)
class IndustrialContext:
    entities: tuple[SemanticLabel, ...]
    activities: tuple[SemanticLabel, ...]
    operational_contexts: tuple[SemanticLabel, ...]
    safety_conditions: tuple[SemanticLabel, ...]
    uncertainty: str
    provenance: tuple[str, ...]

    @property
    def has_evidence(self) -> bool:
        return bool(
            self.entities
            or self.activities
            or self.operational_contexts
            or self.safety_conditions
        )


@dataclass(frozen=True)
class VisualSemanticEvidence:
    entities: tuple[SemanticLabel, ...]
    activities: tuple[SemanticLabel, ...]
    operational_contexts: tuple[SemanticLabel, ...]
    safety_conditions: tuple[SemanticLabel, ...]
    calibrated: bool
    uncertainty: str
    provenance: tuple[str, ...]

    @property
    def has_evidence(self) -> bool:
        return bool(
            self.entities
            or self.activities
            or self.operational_contexts
            or self.safety_conditions
        )


@dataclass(frozen=True)
class DocumentCandidate:
    is_candidate: bool
    heuristic_score: float
    uncertainty: str
    kinds: tuple[str, ...]
    evidence: tuple[str, ...]
    provenance: tuple[str, ...]
    score_semantics: str = "uncalibrated_heuristic"
    severity: str = "informational"
    suggested_action: str = "extract_or_associate"
    logical_document_status: str = "unverified"
    human_review_required: bool = False


@dataclass(frozen=True)
class Decision:
    category: str
    confidence: float
    confidence_kind: str
    winner_score: float
    reasons: tuple[str, ...]
    runner_up: str | None
    runner_up_score: float
    score_margin: float
    features: Features
    photo_attributes: PhotoAttributes | None
    industrial_context: IndustrialContext
    visual_semantics: VisualSemanticEvidence
    document_candidate: DocumentCandidate
    document_text: DocumentTextEvidence | None = None


# endregion [02]
