"""Synthetic regressions for primary-intent document classification."""

from __future__ import annotations

import pytest

from neocortex.documents.document_taxonomy import DocumentSignals, classify_document

TEST_CAPABILITIES = ("documents",)


def test_daily_dusmal_report_beats_incidental_catalog_and_equipment_words() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            "/tmp/12 DUSMAL-SEM17-G general.xlsx",
            "complete",
            title="Catálogo de equipo y reporte diario",
            leading_text=(
                "CATALOGO DE EQUIPO\n"
                "REPORTE DIARIO DE ACTIVIDADES\n"
                "FECHA: 2026-04-26\nPERSONAL: cuadrilla A\n"
                "ACTIVIDADES: inspección de transformadores\n"
                "HORAS: 8\nRECURSOS: equipo de prueba\n"
                "OBSERVACIONES: sin novedades."
            ),
        )
    )

    assert classification.primary_kind == "reporte_actividades"
    assert classification.primary_kind != "catalogo_equipo"
    assert classification.confidence >= 0.82
    assert any("estructura=reporte_actividades_diario" in item for item in classification.evidence)


def test_daily_work_report_listing_equipment_is_not_an_equipment_catalog() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            "/tmp/daily-work-report.xlsx",
            "complete",
            title="Daily work report",
            leading_text=(
                "DAILY WORK REPORT\nDATE: 2026-04-26\nCREW: A\n"
                "ACTIVITIES: inspection\nEQUIPMENT: transformer tester\n"
                "HOURS: 8\nLOCATION: site A"
            ),
        )
    )

    assert classification.primary_kind == "reporte_actividades"
    assert classification.primary_kind != "catalogo_equipo"


def test_cv_course_mentions_do_not_make_a_training_course() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            "/tmp/curriculum-vitae.xlsx",
            "complete",
            title="Curriculum Vitae",
            leading_text=(
                "CURRICULUM VITAE\nEXPERIENCIA PROFESIONAL\n"
                "Cursos de capacitación: seguridad y Excel\n"
                "FORMACIÓN ACADÉMICA\nHABILIDADES"
            ),
        )
    )

    assert classification.primary_kind == "expediente_personal"
    assert classification.primary_kind != "curso_capacitacion"


@pytest.mark.parametrize(
    ("title", "body", "expected"),
    (
        (
            "Catálogo de equipo",
            "CATALOGO DE EQUIPO\nModelo: X\nMarca: Y\nEspecificaciones técnicas.",
            "catalogo_equipo",
        ),
        (
            "Curso de capacitación",
            "CURSO DE CAPACITACIÓN\nObjetivos\nMódulo 1\nEvaluación del participante.",
            "curso_capacitacion",
        ),
    ),
)
def test_true_catalog_and_training_intents_remain_stable(
    title: str,
    body: str,
    expected: str,
) -> None:
    classification = classify_document(
        DocumentSignals("xlsx", "/tmp/synthetic.xlsx", "complete", title=title, leading_text=body)
    )
    assert classification.primary_kind == expected


def test_vague_image_and_heading_only_partial_source_stay_uncertain() -> None:
    vague = classify_document(
        DocumentSignals("image", "/tmp/Montageprogram.png", "complete", title="Montageprogram")
    )
    partial = classify_document(
        DocumentSignals(
            "xlsx",
            "/tmp/reporte.xlsx",
            "partial",
            title="Reporte diario de actividades",
            leading_text="REPORTE DIARIO DE ACTIVIDADES",
        )
    )

    assert vague.primary_kind == "otro"
    assert vague.uncertainty == "alta"
    assert partial.confidence < 0.72
    assert partial.uncertainty == "alta"
