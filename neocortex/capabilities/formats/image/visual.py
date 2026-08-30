"""Pluggable visual multi-label evidence with conservative built-in cues."""

from __future__ import annotations
from pathlib import Path
from typing import Protocol

from .models import Features, SemanticLabel, VisualSemanticEvidence


# region [01] Classifier contract


class VisualSemanticClassifier(Protocol):
    """Return bounded visual evidence without requiring a specific model stack."""

    @property
    def signature(self) -> str: ...

    def classify(
        self,
        path: Path,
        features: Features,
    ) -> VisualSemanticEvidence: ...


# endregion [01]


# region [02] Dependency-free conservative classifier


class FeatureVisualClassifier:
    """Infer only broad visual candidates supported by geometric pixel cues."""

    signature = "visual-features-multilabel-v1"

    def classify(
        self,
        path: Path,
        features: Features,
    ) -> VisualSemanticEvidence:
        del path
        rectilinear = (
            features.edge_fraction >= 0.13
            and features.long_horizontal_lines >= 0.18
            and features.long_vertical_lines >= 0.12
        )
        equipment_surface = (
            rectilinear
            and features.neutral_fraction >= 0.64
            and features.colorfulness <= 42.0
            and features.quantized_colors >= 12
        )
        if not equipment_surface:
            return VisualSemanticEvidence(
                (),
                (),
                (),
                (),
                calibrated=False,
                uncertainty="sin_evidencia_visual_industrial_suficiente",
                provenance=(self.signature,),
            )

        evidence = (
            f"visual:edge_fraction={features.edge_fraction:.3f}",
            f"visual:horizontal_lines={features.long_horizontal_lines:.3f}",
            f"visual:vertical_lines={features.long_vertical_lines:.3f}",
            f"visual:neutral_fraction={features.neutral_fraction:.3f}",
        )
        entities = (
            SemanticLabel(
                label="equipo_electrico_panelizado_candidato",
                score=0.47,
                evidence=evidence,
                provenance=self.signature,
            ),
        )
        operational = (
            SemanticLabel(
                label="instalacion_electrica_interior_candidata",
                score=0.43,
                evidence=evidence[:3],
                provenance=self.signature,
            ),
        )
        safety: tuple[SemanticLabel, ...] = ()
        if features.brightness_mean <= 0.22:
            safety = (
                SemanticLabel(
                    label="visibilidad_reducida_candidata",
                    score=0.41,
                    evidence=(
                        f"visual:brightness_mean={features.brightness_mean:.3f}",
                        *evidence[:2],
                    ),
                    provenance=self.signature,
                ),
            )
        return VisualSemanticEvidence(
            entities=entities,
            activities=(),
            operational_contexts=operational,
            safety_conditions=safety,
            calibrated=False,
            uncertainty="evidencia_visual_heuristica_no_calibrada",
            provenance=(self.signature,),
        )


DEFAULT_VISUAL_CLASSIFIER = FeatureVisualClassifier()


# endregion [02]
