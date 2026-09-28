"""Minimized real-shape XLSX/PDF regressions from the private C03 audit."""

from __future__ import annotations

import json

from neocortex.documents.document_taxonomy import DocumentSignals, classify_document


TEST_CAPABILITIES = ("documents",)


def _xlsx_cells(*cells: tuple[str, str, str]) -> str:
    return "\n".join(
        "XLSX_CELL "
        + json.dumps(
            {
                "workbook": "fixture.xlsx",
                "sheet": sheet,
                "a1": address,
                "type": "shared_string",
                "value": value,
                "formula": None,
                "cached_value": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for sheet, address, value in cells
    )


def test_real_shape_daily_report_cells_beat_catalog_vocabulary() -> None:
    text = _xlsx_cells(
        ("Catálogo", "B2", "FRENTE"),
        ("Catálogo", "B4", "HORAS HOMBRE"),
        ("Catálogo", "L4", "Semana"),
        ("Catálogo", "B5", "REPORTE DE ACTIVIDADES SISTEMAS GENERALES"),
        ("Catálogo", "D5", "Lunes"),
        ("Catálogo", "F5", "Martes"),
        ("Catálogo", "H5", "Miércoles"),
        ("Catálogo", "J5", "Jueves"),
        ("Catálogo", "L5", "Viernes"),
        ("Catálogo", "N5", "Sábado"),
        ("Catálogo", "P5", "Domingo"),
        ("Catálogo", "R5", "Total"),
        ("Catálogo", "B7", "Código"),
        ("Catálogo", "C7", "Actividad"),
        ("Catálogo", "B8", "ACT-01"),
    )
    result = classify_document(
        DocumentSignals("xlsx", "/tmp/daily.xlsx", "complete", leading_text=text)
    )
    assert result.primary_kind == "reporte_actividades"
    assert result.confidence == 0.96
    assert result.uncertainty == "baja"
    assert any("xlsx_positional" in item for item in result.evidence)


def test_real_shape_cv_sections_beat_incidental_training_course() -> None:
    text = _xlsx_cells(
        ("HOJA 1", "C1", "C U R R I C U L U M V I T A E"),
        ("HOJA 1", "B3", "DATOS PERSONALES"),
        ("HOJA 1", "B6", "NOMBRE:"),
        ("HOJA 1", "B9", "DOMICILIO:"),
        ("HOJA 1", "B14", "CURP:"),
        ("HOJA 1", "D22", "DATOS FAMILIARES"),
        ("HOJA 1", "D36", "ESCOLARIDAD"),
        ("HOJA 2", "B5", "EXPERIENCIA EN GENERAL"),
        ("HOJA 3", "B3", "ALGUNAS DE LAS ACTIVIDADES LABORALES"),
        ("HOJA 4", "B5", "Curso de capacitación en seguridad"),
    )
    result = classify_document(
        DocumentSignals("xlsx", "/tmp/curriculum.xlsx", "complete", leading_text=text)
    )
    assert result.primary_kind == "expediente_personal"
    assert result.confidence == 0.96
    assert result.uncertainty == "baja"
    assert any("curriculum_vitae" in item for item in result.evidence)


def test_real_shape_hours_report_is_personnel_time_not_generic_reference() -> None:
    text = _xlsx_cells(
        ("Reporte de horas", "A1", "Reporte de horas"),
        ("Reporte de horas", "B5", "Puesto"),
        ("Reporte de horas", "C5", "Personal"),
        ("Reporte de horas", "F5", "Costo hrs diurno"),
        ("Reporte de horas", "G5", "Periodo semanal"),
        ("Reporte de horas", "J5", "Lunes"),
        ("Reporte de horas", "K5", "Martes"),
        ("Reporte de horas", "L5", "Miércoles"),
        ("Reporte de horas", "M5", "Jueves"),
        ("Reporte de horas", "N5", "Viernes"),
        ("Reporte de horas", "R5", "Costo del servicio"),
        ("Reporte de horas", "U5", "Viáticos"),
        ("Reporte de horas", "X5", "Observaciones"),
    )
    result = classify_document(
        DocumentSignals("xlsx", "/tmp/reporte-horas.xlsx", "complete", leading_text=text)
    )
    assert result.primary_kind == "registro_tiempo_personal"
    assert result.confidence == 0.96
    assert result.uncertainty == "baja"


def test_heading_only_service_certificate_stays_high_uncertainty() -> None:
    result = classify_document(
        DocumentSignals(
            "pdf",
            "/tmp/service-work-report.pdf",
            "done",
            page_count=1,
            leading_text=(
                "Service Work Report\n"
                "Certificado de calibración para pruebas de análisis del gas SF6\n"
                "TEST-001.\n© Fabricante Demo 2025 228"
            ),
        )
    )
    assert result.primary_kind == "informe_tecnico"
    assert result.confidence == 0.60
    assert result.uncertainty == "alta"
    assert any("heading_only_insufficient_content" in item for item in result.evidence)
