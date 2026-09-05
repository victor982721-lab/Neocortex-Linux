"""Behavioral contracts for path, OCR, and visual semantic fusion."""

from __future__ import annotations


import inspect

import pytest

from neocortex.capabilities.formats.image.document import DocumentTextEvidence
from neocortex.capabilities.formats.image.models import (
    IndustrialContext,
    SemanticLabel,
    VisualSemanticEvidence,
)
from neocortex.capabilities.formats.image.semantics import classify_industrial_context


TEST_CAPABILITIES = ('image',)


PATH_CONTEXT = "transformador mantenimiento sala de control epp"
GENERIC_CONTEXT = "contenido generico"
DOCUMENT_EVIDENCE = DocumentTextEvidence(
    attempted=True,
    available=True,
    industrial_entities=("transformador",),
    industrial_activities=("mantenimiento",),
    industrial_operational_contexts=("sala_control",),
    industrial_safety_conditions=("epp",),
    provenance="fixture-ocr-v1",
)
VISUAL_LABEL = SemanticLabel(
    label="transformador",
    score=0.47,
    evidence=("visual:fixture",),
    provenance="visual-features-multilabel-v1",
)
VISUAL_EVIDENCE = VisualSemanticEvidence(
    entities=(VISUAL_LABEL,),
    activities=(),
    operational_contexts=(),
    safety_conditions=(),
    calibrated=False,
    uncertainty="visual_fixture_uncertainty",
    provenance=("visual-features-multilabel-v1",),
)


def test_classify_industrial_context_signature_and_abstention_contract() -> None:
    assert str(inspect.signature(classify_industrial_context)) == (
        "(context: 'str', document_text: 'DocumentTextEvidence | None' = None, "
        "visual: 'VisualSemanticEvidence | None' = None) -> 'IndustrialContext'"
    )
    unavailable_ocr = DocumentTextEvidence(
        attempted=True,
        available=False,
        industrial_entities=("transformador",),
        provenance="unavailable-fixture",
    )
    empty_visual = VisualSemanticEvidence(
        entities=(),
        activities=(),
        operational_contexts=(),
        safety_conditions=(),
        calibrated=False,
        uncertainty="must_not_surface_without_labels",
        provenance=("visual-features-multilabel-v1",),
    )

    assert classify_industrial_context(
        GENERIC_CONTEXT,
        unavailable_ocr,
        empty_visual,
    ) == IndustrialContext(
        entities=(),
        activities=(),
        operational_contexts=(),
        safety_conditions=(),
        uncertainty="sin_evidencia_semantica_suficiente",
        provenance=(),
    )


@pytest.mark.parametrize(
    (
        "context",
        "document_text",
        "visual",
        "expected_uncertainty",
        "expected_entity",
        "expected_provenance",
    ),
    (
        (
            PATH_CONTEXT,
            None,
            None,
            "evidencia_semantica_limitada_a_nombre_y_ruta",
            SemanticLabel(
                "transformador",
                0.56,
                ("nombre/ruta:transformador",),
                "path-keywords-v1",
            ),
            ("path-keywords-v1",),
        ),
        (
            GENERIC_CONTEXT,
            DOCUMENT_EVIDENCE,
            None,
            "evidencia_semantica_limitada_a_ocr",
            SemanticLabel(
                "transformador",
                0.64,
                ("ocr-keywords:transformador",),
                "ocr-keywords-v1",
            ),
            ("ocr-keywords-v1",),
        ),
        (
            GENERIC_CONTEXT,
            None,
            VISUAL_EVIDENCE,
            "visual_fixture_uncertainty",
            VISUAL_LABEL,
            ("visual-features-multilabel-v1",),
        ),
        (
            PATH_CONTEXT,
            DOCUMENT_EVIDENCE,
            None,
            "evidencia_semantica_indirecta_de_ruta_y_ocr",
            SemanticLabel(
                "transformador",
                0.72,
                ("nombre/ruta:transformador", "ocr-keywords:transformador"),
                "path-keywords-v1+ocr-keywords-v1",
            ),
            ("path-keywords-v1+ocr-keywords-v1",),
        ),
        (
            PATH_CONTEXT,
            None,
            VISUAL_EVIDENCE,
            "evidencia_multifuente_con_componente_visual_no_calibrado",
            SemanticLabel(
                "transformador",
                0.64,
                ("nombre/ruta:transformador", "visual:fixture"),
                "path-keywords-v1+visual-features-multilabel-v1",
            ),
            (
                "path-keywords-v1",
                "path-keywords-v1+visual-features-multilabel-v1",
            ),
        ),
        (
            GENERIC_CONTEXT,
            DOCUMENT_EVIDENCE,
            VISUAL_EVIDENCE,
            "evidencia_multifuente_con_componente_visual_no_calibrado",
            SemanticLabel(
                "transformador",
                0.72,
                ("ocr-keywords:transformador", "visual:fixture"),
                "ocr-keywords-v1+visual-features-multilabel-v1",
            ),
            (
                "ocr-keywords-v1",
                "ocr-keywords-v1+visual-features-multilabel-v1",
            ),
        ),
        (
            PATH_CONTEXT,
            DOCUMENT_EVIDENCE,
            VISUAL_EVIDENCE,
            "evidencia_multifuente_con_componente_visual_no_calibrado",
            SemanticLabel(
                "transformador",
                0.8,
                (
                    "nombre/ruta:transformador",
                    "ocr-keywords:transformador",
                    "visual:fixture",
                ),
                "path-keywords-v1+ocr-keywords-v1+visual-features-multilabel-v1",
            ),
            (
                "path-keywords-v1+ocr-keywords-v1",
                "path-keywords-v1+ocr-keywords-v1+visual-features-multilabel-v1",
            ),
        ),
    ),
)
def test_classify_industrial_context_preserves_source_fusion(
    context: str,
    document_text: DocumentTextEvidence | None,
    visual: VisualSemanticEvidence | None,
    expected_uncertainty: str,
    expected_entity: SemanticLabel,
    expected_provenance: tuple[str, ...],
) -> None:
    result = classify_industrial_context(context, document_text, visual)

    assert result.entities == (expected_entity,)
    assert result.uncertainty == expected_uncertainty
    assert result.provenance == expected_provenance


def test_classify_industrial_context_preserves_all_labeled_domains() -> None:
    result = classify_industrial_context(PATH_CONTEXT, DOCUMENT_EVIDENCE)

    assert result == IndustrialContext(
        entities=(
            SemanticLabel(
                "transformador",
                0.72,
                ("nombre/ruta:transformador", "ocr-keywords:transformador"),
                "path-keywords-v1+ocr-keywords-v1",
            ),
        ),
        activities=(
            SemanticLabel(
                "mantenimiento",
                0.72,
                ("nombre/ruta:mantenimiento", "ocr-keywords:mantenimiento"),
                "path-keywords-v1+ocr-keywords-v1",
            ),
        ),
        operational_contexts=(
            SemanticLabel(
                "sala_control",
                0.72,
                ("nombre/ruta:sala de control", "ocr-keywords:sala_control"),
                "path-keywords-v1+ocr-keywords-v1",
            ),
        ),
        safety_conditions=(
            SemanticLabel(
                "epp",
                0.72,
                ("nombre/ruta:epp", "ocr-keywords:epp"),
                "path-keywords-v1+ocr-keywords-v1",
            ),
        ),
        uncertainty="evidencia_semantica_indirecta_de_ruta_y_ocr",
        provenance=("path-keywords-v1+ocr-keywords-v1",),
    )
