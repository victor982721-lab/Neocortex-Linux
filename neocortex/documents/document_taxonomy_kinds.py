"""Ordered document-kind specialists for explainable classification."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_kinds.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import re
from typing import Iterable, Mapping

from .document_signals import compiled_regex, fold_signal
from .document_taxonomy_entities import _clean_identifier, _pattern_evidence
from .document_taxonomy_models import ScoredLabel, StandardReference
from .document_taxonomy_references import (
    _EXPLICIT_NON_NORMATIVE_FORM_PATTERN,
    _front_before_reference_section,
    _reference_position,
)
from .document_taxonomy_vocabulary import _KIND_PATTERNS
# endregion [01]

# region [02] Implementación


_HIGH_SPECIFICITY_KINDS = frozenset(
    {
        "certificado_calibracion",
        "certificado_calidad",
        "constancia_capacitacion",
        "accion_correctiva_preventiva",
        "credencial_visitante",
        "comprobante_viaje",
        "descripcion_tecnica_sistema",
        "dossier_calidad",
        "especificacion_tecnica",
        "etiqueta_muestra_laboratorio",
        "formato_inspeccion",
        "hoja_datos_seguridad",
        "informe_analisis",
        "informe_auditoria",
        "informe_inspeccion",
        "instructivo_trabajo",
        "instruccion_cuenta_bancaria",
        "registro_tiempo_personal",
        "lista_empaque_embarque",
        "lista_verificacion",
        "manual_equipo",
        "manual_sistema_gestion",
        "memoria_calculo",
        "orden_trabajo",
        "plan_tecnico",
        "protocolo_pruebas",
        "registro_fotografico",
        "reporte_actividades",
        "reporte_anomalias",
        "reporte_fat_sat",
        "reporte_entrega_embarque",
        "reporte_laboratorio",
        "reporte_no_conformidad",
        "reporte_resultados_pruebas",
        "reporte_inventario_archivo",
        "hoja_asignacion_proyecto",
        "registro_auditores",
        "registro_entrega_epp",
        "registro_incidencias",
    }
)


_STRUCTURALLY_EXPLICIT_KINDS = frozenset(
    {
        "control_metrologico",
        "formato_inspeccion",
        "lista_verificacion",
        "registro_asistencia",
        "registro_mediciones",
        "viaticos_gastos",
    }
)


_CORROBORATED_TECHNICAL_KINDS = frozenset(
    {
        "catalogo_equipo",
        "curso_capacitacion",
        "ficha_tecnica",
        "formato_empresa",
        "informe_tecnico",
        "lista_materiales",
        "plano_diagrama",
        "procedimiento",
        "programa_cronograma",
        "referencia_tecnica",
        "registro_bitacora",
        "registro_asistencia",
        "viaticos_gastos",
    }
)


_SPECIFIC_KIND_PRECEDENCE = {
    "hoja_datos_seguridad": frozenset(
        {"ficha_tecnica", "formato_empresa", "normativa"}
    ),
    "lista_empaque_embarque": frozenset(
        {"factura_comprobante", "lista_materiales", "referencia_tecnica"}
    ),
    "reporte_entrega_embarque": frozenset(
        {"lista_empaque_embarque", "lista_materiales", "referencia_tecnica"}
    ),
    "etiqueta_muestra_laboratorio": frozenset(
        {"formato_empresa", "reporte_laboratorio"}
    ),
    "constancia_capacitacion": frozenset({"curso_capacitacion", "formato_empresa"}),
    "control_metrologico": frozenset(
        {"certificado_calibracion", "programa_cronograma", "registro_bitacora"}
    ),
    "registro_mediciones": frozenset(
        {"formato_empresa", "plano_diagrama", "protocolo_pruebas"}
    ),
    "instructivo_trabajo": frozenset({"formato_empresa", "procedimiento"}),
    "formato_inspeccion": frozenset(
        {"contrato_legal", "formato_empresa", "informe_inspeccion", "plano_diagrama"}
    ),
    "lista_verificacion": frozenset({"formato_empresa", "normativa"}),
    "informe_auditoria": frozenset(
        {"informe_inspeccion", "licitacion", "normativa", "procedimiento"}
    ),
    "dossier_calidad": frozenset(
        {"informe_auditoria", "informe_inspeccion", "reporte_anomalias"}
    ),
    "manual_sistema_gestion": frozenset(
        {"formato_empresa", "normativa", "procedimiento", "referencia_tecnica"}
    ),
    "descripcion_tecnica_sistema": frozenset(
        {"documento_empresa", "especificacion_tecnica", "formato_empresa"}
    ),
    "reporte_anomalias": frozenset(
        {"informe_inspeccion", "informe_tecnico", "registro_bitacora"}
    ),
    "reporte_actividades": frozenset(
        {
            "contrato_legal",
            "licitacion",
            "procedimiento",
            "registro_bitacora",
            "registro_fotografico",
        }
    ),
    "informe_analisis": frozenset({"informe_tecnico", "referencia_tecnica"}),
    "reporte_laboratorio": frozenset({"informe_analisis", "informe_tecnico"}),
    "plan_tecnico": frozenset({"normativa", "procedimiento", "programa_cronograma"}),
    "registro_asistencia": frozenset({"formato_empresa", "referencia_tecnica"}),
    "viaticos_gastos": frozenset(
        {"compra_requisicion", "factura_comprobante", "referencia_tecnica"}
    ),
    "protocolo_pruebas": frozenset({"procedimiento", "formato_empresa"}),
    "reporte_no_conformidad": frozenset({"informe_tecnico", "formato_empresa"}),
    "accion_correctiva_preventiva": frozenset(
        {"formato_empresa", "reporte_no_conformidad"}
    ),
    "reporte_resultados_pruebas": frozenset(
        {"informe_tecnico", "protocolo_pruebas", "registro_mediciones"}
    ),
    "reporte_fat_sat": frozenset(
        {"formato_empresa", "informe_tecnico", "plano_diagrama", "protocolo_pruebas"}
    ),
    "minuta_acta": frozenset(
        {
            "procedimiento",
            "programa_cronograma",
            "protocolo_pruebas",
            "reporte_actividades",
        }
    ),
    "registro_auditores": frozenset({"formato_empresa", "registro_asistencia"}),
    "registro_entrega_epp": frozenset({"formato_empresa", "lista_materiales"}),
    "registro_incidencias": frozenset({"formato_empresa", "registro_bitacora"}),
    "credencial_visitante": frozenset({"formato_empresa", "documento_empresa"}),
    "programa_seguridad_salud": frozenset({"plan_tecnico", "programa_cronograma"}),
    "programa_gestion_ambiental": frozenset({"plan_tecnico", "programa_cronograma"}),
    "hoja_asignacion_proyecto": frozenset(
        {"contrato_legal", "cotizacion_propuesta", "formato_empresa"}
    ),
    "instruccion_cuenta_bancaria": frozenset({"correspondencia", "formato_empresa"}),
    "registro_tiempo_personal": frozenset(
        {"registro_asistencia", "reporte_actividades", "programa_cronograma"}
    ),
    "comprobante_viaje": frozenset({"factura_comprobante", "viaticos_gastos"}),
    "reporte_inventario_archivo": frozenset({"informe_tecnico", "reporte_actividades"}),
}


def _calibrated_kind_confidence(primary: ScoredLabel) -> float:
    """Promote explicit document-form evidence without inflating broad admin terms."""

    if primary.label in _STRUCTURALLY_EXPLICIT_KINDS:
        return 0.76
    direct_scope = any(
        item.startswith(("path:", "title:", "opening:")) for item in primary.evidence
    )
    distinct_rules = {item.split("=", 1)[-1].casefold() for item in primary.evidence}
    corroborated = len(distinct_rules) >= 2
    if primary.label in _HIGH_SPECIFICITY_KINDS and (direct_scope or corroborated):
        return 0.76
    if (
        primary.label in _CORROBORATED_TECHNICAL_KINDS
        and primary.score >= 0.62
        and (direct_scope or corroborated)
    ):
        return 0.74
    return primary.score


def _single_kind_specialists(
    scopes: Mapping[str, str],
    *,
    page_count: int | None,
) -> tuple[ScoredLabel, ...]:
    """Evaluate independent one-result specialists in their stable order."""

    candidates = (
        _calibration_certificate_evidence(scopes, page_count=page_count),
        _controlled_procedure_evidence(scopes),
        _cfe_technical_manual_chapter_evidence(scopes),
        _test_requirement_checklist_evidence(scopes),
        _field_measurement_record_evidence(scopes),
        _metrology_control_record_evidence(scopes),
        _packing_list_evidence(scopes),
        _delivery_report_evidence(scopes),
        _laboratory_sample_label_evidence(scopes),
        _test_result_export_evidence(scopes),
        _personnel_time_report_evidence(scopes),
        _daily_resource_schedule_evidence(scopes),
        _laboratory_report_evidence(scopes),
    )
    return tuple(candidate for candidate in candidates if candidate is not None)


def _adjust_and_consolidate_kind_evidence(
    labels: list[ScoredLabel],
    organizations: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, ...]:
    """Apply stable specificity precedence and keep the best score per label."""

    specific_form_kinds = {
        label.label
        for label in labels
        if label.label
        in {
            "certificado_calibracion",
            "certificado_calidad",
            "constancia_capacitacion",
            "protocolo_pruebas",
            "registro_bitacora",
            "registro_mediciones",
            "formato_inspeccion",
            "lista_verificacion",
            "accion_correctiva_preventiva",
            "credencial_visitante",
            "registro_auditores",
            "registro_entrega_epp",
            "registro_incidencias",
        }
    }
    adjusted: list[ScoredLabel] = []
    present_kinds = {label.label for label in labels}
    for label in labels:
        score = label.score
        if label.label == "formato_empresa" and specific_form_kinds:
            score = max(0.0, score - 0.04)
        elif label.label == "formato_empresa" and organizations:
            score = min(0.95, score + 0.14)
        lower_priority = _SPECIFIC_KIND_PRECEDENCE.get(label.label, frozenset())
        precedence_matches = sorted(lower_priority.intersection(present_kinds))
        evidence = label.evidence
        if precedence_matches:
            score = min(0.96, score + 0.12)
            evidence = (
                *evidence,
                "precedencia_especifica:" + ",".join(precedence_matches),
            )
        adjusted.append(ScoredLabel(label.label, round(score, 6), evidence))
    if not adjusted and organizations and organizations[0].score >= 0.74:
        organization = organizations[0]
        adjusted.append(
            ScoredLabel(
                "documento_empresa",
                round(max(0.72, organization.score - 0.05), 6),
                organization.evidence,
            )
        )
    best: dict[str, ScoredLabel] = {}
    for label in adjusted:
        prior = best.get(label.label)
        if prior is None or label.score > prior.score:
            best[label.label] = label
    return tuple(sorted(best.values(), key=lambda item: (-item.score, item.label)))


def _kind_evidence(
    scopes: Mapping[str, str],
    standards: tuple[StandardReference, ...],
    organizations: tuple[ScoredLabel, ...],
    *,
    primary_authority: str | None,
    page_count: int | None,
    managed_path: bool,
    managed_normative_path: bool,
) -> tuple[ScoredLabel, ...]:
    labels = list(_pattern_evidence(scopes, _KIND_PATTERNS, base_score=0.46))
    labels.extend(_single_kind_specialists(scopes, page_count=page_count))
    labels.extend(_project_technical_document_evidence(scopes))
    som_procedure = _som_3531_procedure_evidence(scopes, standards)
    if som_procedure is not None:
        labels.append(som_procedure)
    labels.extend(_commercial_document_evidence(scopes))
    labels.extend(_administrative_document_evidence(scopes))
    labels.extend(_correspondence_document_evidence(scopes))
    labels.extend(_structured_document_type_evidence(scopes))
    standards_study = _standards_study_evidence(scopes)
    if standards_study is not None:
        labels.append(standards_study)
    if not _strong_non_normative_document(
        labels,
        primary_authority=primary_authority,
    ):
        normative = _normative_document_evidence(
            scopes,
            standards,
            primary_authority=primary_authority,
            managed_path=managed_path,
            managed_normative_path=managed_normative_path,
        )
        if normative is not None:
            labels.append(normative)
    return _adjust_and_consolidate_kind_evidence(labels, organizations)


_NORMATIVE_SUPPRESSING_KINDS = frozenset(
    {
        "certificado_calibracion",
        "certificado_calidad",
        "cotizacion_propuesta",
        "dossier_calidad",
        "especificacion_tecnica",
        "hoja_datos_seguridad",
        "informe_analisis",
        "informe_inspeccion",
        "manual_equipo",
        "minuta_acta",
        "reporte_fat_sat",
        "reporte_laboratorio",
        "reporte_no_conformidad",
        "reporte_resultados_pruebas",
        "registro_mediciones",
    }
)


def _strong_non_normative_document(
    labels: Iterable[ScoredLabel],
    *,
    primary_authority: str | None,
) -> bool:
    """Treat standards as references when a complete operational form is present."""

    for label in labels:
        if label.label in _NORMATIVE_SUPPRESSING_KINDS and label.score >= 0.88:
            return True
        if label.label == "procedimiento" and any(
            "estructura_procedimiento_controlado" in item
            or (
                primary_authority == "CFE"
                and item.startswith("opening:encabezado=PROCEDIMIENTO")
            )
            for item in label.evidence
        ):
            return True
        if label.label == "lista_verificacion" and any(
            "estructura_lista_pruebas_cfe" in item for item in label.evidence
        ):
            return True
        if (
            label.label == "manual_equipo"
            and label.score >= 0.64
            and any(
                item.startswith(("path:regla=", "title:regla=", "opening:regla="))
                for item in label.evidence
            )
        ):
            return True
        if label.label == "curso_capacitacion" and label.score >= 0.76:
            front_markers = {
                item
                for item in label.evidence
                if item.startswith(("path:regla=", "title:regla=", "opening:regla="))
            }
            if len(front_markers) >= 2:
                return True
        if (
            label.label == "protocolo_pruebas"
            and label.score >= 0.72
            and any(
                marker in item
                for item in label.evidence
                for marker in (
                    "regla=TEST RESULTS",
                    "regla=RESULTADOS DE PRUEBAS",
                    "regla=REGISTRO DE PRUEBAS",
                )
            )
        ):
            return True
    return False


_CALIBRATION_EVIDENCE_GROUPS: Mapping[str, tuple[str, ...]] = {
    "heading": (
        r"\bCERTIFICADO\s+DE\s+CALIBRACION\b",
        r"\bINFORME\s+DE\s+CALIBRACION\b",
        r"\bCALIBRATION\s+CERTIFICATE\b",
        r"\bCERTIFICATE\s+OF\s+CALIBRATION\b",
    ),
    "subject": (
        r"\bINSTRUMENTO\s*:",
        r"\bINSTRUMENT\s*:",
        r"\bINSTRUMENTO\s+EN\s+CALIBRACION\b",
        r"\bDATOS\s+DEL\s+INSTRUMENTO\s+EN\s+CALIBRACION\b",
        r"\bINSTRUMENT(?:\s+DATA)?\s+UNDER\s+CALIBRATION\b",
        r"\b(?:EQUIPO|INSTRUMENTO)\s+CALIBRADO\b",
        r"\bUNIT\s+UNDER\s+TEST\b",
    ),
    "traceability": (
        r"\bTRAZABILIDAD\b",
        r"\bTRACEABILITY\b",
        r"\bPATRON(?:ES)?\s+DE\s+REFERENCIA\b",
        r"\bREFERENCE\s+(?:PATTERN|STANDARD)\b",
    ),
    "identity": (
        r"\bORDEN\s+DE\s+RECEPCION\b",
        r"\bRECEPTION\s+ORDER\b",
        r"\bFECHA\s+DE\s+CALIBRACION\b",
        r"\bCALIBRATION\s+DATE\b",
        r"\bCERTIFICATE\s+(?:NO|NUMBER)\b",
    ),
    "results": (
        r"\bDATOS\s+DE\s+CALIBRACION\b",
        r"\bCALIBRATION\s+(?:DATA|RESULTS?)\b",
        r"\bINCERTIDUMBRE\s+EXPANDIDA\b",
        r"\bEXPANDED\s+UNCERTAINTY\b",
    ),
}


_FIELD_MEASUREMENT_HEADINGS = (
    r"\bMEDICION\s+DE\s+LA\s+RESISTENCIA\s+EN\s+SISTEMA\s+DE\s+TIERRAS\b",
    r"\bMEDICION\s+DE\s+LA\s+RESISTENCIA\s+(?:DE|EN)\s+(?:LA\s+)?RED\s+DE\s+TIERRA\b",
    r"\bFIELD\s+MEASUREMENT\s+RECORD\b",
)


_FIELD_MEASUREMENT_FIELDS = (
    r"\bPROYECTO\s*:",
    r"\bOBRA\s*:",
    r"\bFECHA\s*:",
    r"\bELEMENTO\s*:",
    r"\bAREA\s*:",
    r"\bPLANO\s+DE\s+REFERENCIA\s*:",
    r"\bCLIENTE\s*:",
    r"\bEQUIPO\s*:",
    r"\bLOCALIZACION\s*:",
    r"\bEQUIPO\s+DE\s+PRUEBA\b",
    r"\b(?:NO\.?\s+DE\s+)?SERIE\s*:",
    r"\bOBSERVACIONES\s*:",
)


_METROLOGY_CONTROL_FIELDS = (
    r"\bNOMBRE\s+DEL\s+EQUIPO\b",
    r"\bMARCA\b",
    r"\bMODELO\b",
    r"\b(?:NO\.?\s+DE\s+)?SERIE\b",
    r"\bFECHA\s+DE\s+PROXIMA\s+CALIBRACION\b",
)


_PACKING_LIST_FIELDS = (
    r"\bSHIPMENT\s+(?:NO|NUMBER|TYPE)\b",
    r"\bPACKAGE\s+NO\b",
    r"\bTYPE\s+OF\s+PACKING\b",
    r"\bGROSS\s+WEIGHT\b",
    r"\bNET\s+WEIGHT\b",
    r"\bPLACE\s+OF\s+DELIVERY\b",
    r"\bSTORAGE\s+INSTRUCTIONS\b",
    r"\bDESIGNATION\s+OF\s+CONTENTS\b",
)


def _field_measurement_record_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Recognize a populated field sheet, not prose describing how to measure."""

    opening = scopes.get("opening", "")
    heading = next(
        (
            pattern
            for pattern in _FIELD_MEASUREMENT_HEADINGS
            if compiled_regex(pattern).search(opening)
        ),
        None,
    )
    if heading is None:
        return None
    matched_fields = tuple(
        pattern
        for pattern in _FIELD_MEASUREMENT_FIELDS
        if compiled_regex(pattern).search(opening)
    )
    if len(matched_fields) < 3:
        return None
    evidence = (
        "opening:estructura=registro_medicion_campo",
        f"opening:campos_estructurados={len(matched_fields)}",
    )
    return ScoredLabel("registro_mediciones", 0.98, evidence)


def _metrology_control_record_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Recognize the populated equipment-control table over its form title."""

    opening = scopes.get("opening", "")
    if not compiled_regex(
        r"\bCONTROL\s+DE\s+EQUIPOS\s+DE\s+INSPECCION,?\s+MEDICION\s+Y\s+PRUEBAS\b",
    ).search(opening):
        return None
    matched_fields = sum(
        1
        for pattern in _METROLOGY_CONTROL_FIELDS
        if compiled_regex(pattern).search(opening)
    )
    if matched_fields < 3:
        return None
    return ScoredLabel(
        "control_metrologico",
        0.97,
        (
            "opening:estructura=control_metrologico_tabular",
            f"opening:campos_estructurados={matched_fields}",
        ),
    )


def _packing_list_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Recognize a logistics packing list instead of treating Invoice No as an invoice."""

    path = scopes.get("path", "")
    front = f"{path} {scopes.get('opening', '')[:2_000]}"
    heading = compiled_regex(r"\b(?:PACKING\s+LIST|LISTA\s+DE\s+EMPAQUE)\b").search(
        front
    )
    if heading is None:
        return None
    matched_fields = sum(
        1 for pattern in _PACKING_LIST_FIELDS if compiled_regex(pattern).search(front)
    )
    if matched_fields < 3:
        path_heading = compiled_regex(
            r"\b(?:PACKING\s+LIST|LISTA\s+DE\s+EMPAQUE)\b"
        ).search(path)
        hcn_package = compiled_regex(r"\bMALPASO[-\s]*HCN[-\s/]?\d{1,3}\b").search(path)
        if path_heading is not None and hcn_package is not None:
            return ScoredLabel(
                "lista_empaque_embarque",
                0.94,
                (
                    f"path:encabezado={_clean_identifier(path_heading.group(0))}",
                    f"path:paquete={_clean_identifier(hcn_package.group(0))}",
                    "texto:estructura_logistica_degradada",
                ),
            )
        return None
    return ScoredLabel(
        "lista_empaque_embarque",
        0.98,
        (
            f"opening:encabezado={_clean_identifier(heading.group(0))}",
            f"opening:campos_logisticos={matched_fields}",
        ),
    )


def _delivery_report_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Recognize receipt and site-delivery reports for shipped project packages."""

    opening = scopes.get("opening", "")[:2_000]
    heading = compiled_regex(r"\bDELIVERY\s+REPORT\b").search(opening)
    if heading is None:
        return None
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bMATERIALS\s+RECEIVED\b",
            r"\bSHIPMENT\s+NO\b",
            r"\bPACKAGE\s+NO\b",
            r"\bDELIVERY\s+DATE\b",
            r"\bSTORAGE\s+AREA\b",
            r"\bSTORAGE\s+POSITION\b",
            r"\bINSPECTION\s*:",
        )
    )
    if fields < 4:
        return None
    return ScoredLabel(
        "reporte_entrega_embarque",
        0.99,
        (
            f"opening:encabezado={_clean_identifier(heading.group(0))}",
            f"opening:campos_entrega={fields}",
        ),
    )


def _laboratory_sample_label_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Identify oil-analysis container labels without treating them as reports."""

    front = f"{scopes.get('path', '')} {scopes.get('opening', '')[:1_200]}"
    container = compiled_regex(
        r"\b(?:CEE\s*/\s*CROMATOGRAFIA\s+JERINGA"
        r"(?:\s+DE\s+VIDRIO)?\s+50\s*ML|"
        r"CLA\s*/\s*F\.?\s*Q\.?\s*E\.?\s+FRASCO"
        r"(?:\s+(?:DE\s+VIDRIO|AMBAR))?\s+1\s*L|"
        r"EGA\s*/\s*PCB'?S?\s+FRASCO"
        r"(?:\s+(?:DE\s+VIDRIO|AMBAR))?\s+25\s*ML)\b"
    ).search(front)
    if container is None:
        return None
    fields = sum(
        compiled_regex(pattern).search(front) is not None
        for pattern in (
            r"\bC\.?\s*H\.?\s+MALPASO\b",
            r"\bS\.?\s*E\.?\s*:\s*C\.?\s*H\.?\s+MALPASO\b",
            r"\bTR[-\s]?\d{1,2}\b",
            r"\bU\d+[-\s]*FASE\s+[ABC]\b",
            r"\bEQUIPO\s*:\s*U\d+[-\s]*FASE\s+[ABC]\b",
            r"\bNS\s+\d{6,}(?:-\d+)?\b",
            r"\bMARCA\s*/\s*SERIE\s*:\s*HYOSUNG\s+\d{6,}(?:-\d+)?\b",
            r"\b\d{2}/\d{2}/\d{2}\s+MVA\b",
            r"\bFECHA\s+_+",
        )
    )
    if fields < 3:
        return None
    return ScoredLabel(
        "etiqueta_muestra_laboratorio",
        0.99,
        (
            f"opening:tipo_etiqueta={_clean_identifier(container.group(0))}",
            f"opening:campos_muestra={fields}",
        ),
    )


def _test_result_export_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Recognize structured instrument exports with measured CT test curves."""

    opening = scopes.get("opening", "")[:3_000]
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bFILENAME\b",
            r"\bDATE\b",
            r"\bTIME\b",
            r"\bCOMPANY\b",
            r"\bCIRCUIT\b",
            r"\bPASS\s*/\s*FAIL\b",
            r"\bTEST\s*#\b",
            r"\bTEST\s+NOTES\b",
            r"\bDATA\s+POINTS\b",
            r"\bWINDING\s+RES\b",
            r"\bIEEE\s+30\s+(?:VKP|IKP)\b",
        )
    )
    if fields < 6:
        return None
    return ScoredLabel(
        "reporte_resultados_pruebas",
        0.99,
        (
            "opening:estructura=exportacion_instrumento_pruebas",
            f"opening:campos_resultados={fields}",
        ),
    )


def _personnel_time_report_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Recognize weekly additional-time records without treating them as payroll."""

    opening = scopes.get("opening", "")[:2_500]
    heading = compiled_regex(r"\bREPORTE\s+DE\s+TIEMPO\s+ADICIONAL\b").search(opening)
    if heading is None:
        return None
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bFRENTE\s*:",
            r"\bSEMANA\s*:",
            r"\bNUMERO\s+DE\s+HORAS\s+LABORADAS\s+ADICIONALES\b",
            r"\bNOMBRE\s+DEL\s+TRABAJADOR\b",
            r"\bPUESTO\b",
            r"\bTOTAL\b",
            r"\b(?:LUN|LUNES)\b.{0,120}\b(?:DOM|DOMINGO)\b",
        )
    )
    if fields < 4:
        return None
    return ScoredLabel(
        "registro_tiempo_personal",
        0.98,
        (
            f"opening:encabezado={_clean_identifier(heading.group(0))}",
            f"opening:campos_tiempo={fields}",
        ),
    )


def _daily_resource_schedule_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Recognize a daily personnel, vehicle and tooling resource matrix."""

    opening = scopes.get("opening", "")[:2_500]
    heading = compiled_regex(r"\bRECURSOS\s+UTILIZADOS\s+POR\s+DIA\b").search(opening)
    if heading is None:
        return None
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bPERSONAL\b",
            r"\bVEHICULOS\b",
            r"\bOTROS\s+RECURSOS\b",
            r"\bLOTE\s+DE\s+HERRAMIENTA\b",
            r"\bTIEMPO\s+LABORADO\b",
            r"\bLUNES\b.{0,160}\bDOMINGO\b",
            r"\bC\.?\s*H\.?\s+MALPASO\b",
        )
    )
    if fields < 5:
        return None
    return ScoredLabel(
        "programa_cronograma",
        0.97,
        (
            f"opening:encabezado={_clean_identifier(heading.group(0))}",
            f"opening:campos_recursos={fields}",
        ),
    )


def _laboratory_report_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Separate a laboratory result from the standards used by the method."""

    opening = scopes.get("opening", "")[:2_400]
    heading = compiled_regex(
        r"\b(?:INFORME\s+DE\s+(?:ENSAYOS?|PRUEBAS?|RESULTADOS)|"
        r"LABORATORY\s+(?:TEST\s+)?REPORT)\b"
    ).search(opening)
    if heading is None:
        return None
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\b(?:CODIGO\s+)?INFORME\s*:",
            r"\bCONTROL\s+INTERNO\s*:",
            r"\b(?:EQUIPO\s+ANALIZADO|MUESTRA\s+DE)\b",
            r"\b(?:PRUEBA|ENSAYO)\s+NO\s*:",
            r"\b(?:METODO\s+(?:EMPLEADO|DE\s+PRUEBA)|TEST\s+METHOD)\b",
            r"\b(?:RESULTADO|RESULTS?)\b",
            r"\b(?:LABORATORIO|LABORATORY)\b",
            r"\bFECHA\s+DE\s+(?:PRUEBA|ENSAYO|ANALISIS)\b",
        )
    )
    if fields < 3:
        return None
    return ScoredLabel(
        "reporte_laboratorio",
        0.99,
        (
            f"opening:encabezado={_clean_identifier(heading.group(0))}",
            f"opening:campos_laboratorio={fields}",
        ),
    )


def _project_technical_document_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    """Recognize project offers and completed CFE annexes as project records."""

    opening = scopes.get("opening", "")[:4_000]
    if not opening:
        return ()
    labels: list[ScoredLabel] = []
    offer_heading = compiled_regex(
        r"\b(?:BASES\s+DE\s+LA\s+PROPUESTA|PROPUESTA\s+NO|"
        r"PROPOSAL\s+NO|OFERTA\s+REF\s+NO|TECHNICAL\s+PROPOSAL)\b"
    ).search(opening)
    offer_fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\b(?:ALCANCE\s+Y\s+DESCRIPCION\s+DE\s+SUMINISTROS|SCOPE\s+OF\s+SUPPLY)\b",
            r"\b(?:CLIENTE|CUSTOMER)\s*:",
            r"\b(?:INQUIRY|CONCURSO)\s+(?:NO|ABIERTO)\b",
            r"\bCUESTIONARIOS?\s+Y\s+CUMPLIMIENTOS?\b",
            r"\bSERVICIO\s+DE\s+MODERNIZACION\b",
            r"\b(?:ANDRITZ|SIEMENS\s+ENERGY)\b",
        )
    )
    is_technical_offer = offer_heading is not None and offer_fields >= 2
    if is_technical_offer:
        assert offer_heading is not None
        labels.append(
            ScoredLabel(
                "cotizacion_propuesta",
                0.99,
                (
                    f"opening:encabezado={_clean_identifier(offer_heading.group(0))}",
                    f"opening:campos_oferta_tecnica={offer_fields}",
                ),
            )
        )

    annex_heading = compiled_regex(
        r"\b(?:CUESTIONARIO\s+TECNICO|CARACTERISTICAS\s+PARTICULARES|"
        r"DESCRIPCION\s+TECNICA)\b"
    ).search(opening)
    specification = compiled_regex(
        r"\b(?:CORRESPONDIENTE\s+A\s+LA\s+)?ESPECIFICACION\s+CFE\b"
    ).search(opening)
    project_context = compiled_regex(
        r"\b(?:CENTRAL\s+HIDROELECTRICA|MALPASO|ANDRITZ|LOTE\s+NO|"
        r"REPOSICION\s+NO|PROPUESTA\s+NO|PROPOSAL\s+NO)\b"
    ).search(opening)
    if (
        annex_heading is not None
        and specification is not None
        and project_context is not None
        and annex_heading.start() < specification.start()
        and not is_technical_offer
    ):
        labels.append(
            ScoredLabel(
                "especificacion_tecnica",
                0.98,
                (
                    f"opening:anexo_proyecto={_clean_identifier(annex_heading.group(0))}",
                    f"opening:referencia_base={_clean_identifier(specification.group(0))}",
                ),
            )
        )
    response_heading = compiled_regex(
        r"\bAPENDICE\s+[A-Z0-9]+\s+INFORMACION\s+TECNICA\s+REQUERIDA\b"
    ).search(opening)
    response_fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bPROPONENTE\s*:",
            r"\bLICITACION\s+NO\.?\s*:",
            r"\bREQUISICION\s+NO\.?\s*:",
            r"\bSOLICITADO\b",
            r"\bOFERTADO\b",
        )
    )
    andritz_response = compiled_regex(
        r"\bCONFIDENTIAL\s+DOCUMENT\b.{0,160}\bANDRITZ\b"
    ).search(opening)
    if (
        response_heading is not None
        and response_fields >= 3
        and andritz_response is not None
    ):
        labels.append(
            ScoredLabel(
                "especificacion_tecnica",
                0.99,
                (
                    "opening:anexo_tecnico_respondido=ANDRITZ",
                    f"opening:campos_respuesta_tecnica={response_fields}",
                ),
            )
        )
    return tuple(labels)


def _som_3531_procedure_evidence(
    scopes: Mapping[str, str],
    standards: tuple[StandardReference, ...],
) -> ScoredLabel | None:
    """Recover the CFE SOM-3531 manual when its scanned cover is absent."""

    som_reference = next(
        (
            reference.identifier
            for reference in standards
            if reference.authority == "CFE"
            and reference.identifier.startswith("SOM-3531")
        ),
        None,
    )
    if som_reference is None:
        return None
    path_title = f"{scopes.get('path', '')} {scopes.get('title', '')}"
    if compiled_regex(
        r"\b(?:REPORTE|INFORME|FORMATO|REGISTRO|EXTRACTO|RESULTADOS?)\b"
    ).search(path_title):
        return None
    opening = scopes.get("opening", "")
    structure_patterns = (
        r"\bCOMISION\s+FEDERAL\s+DE\s+ELECTRICIDAD\b",
        r"\bINDICE\b",
        r"\bCAPITULO\s+1\b",
        r"\bCAPITULO\s+2\b",
        r"\bPRUEBAS?\s+DE\s+CAMPO\b",
        r"\bEQUIPO\s+PRIMARIO\b",
    )
    matched = tuple(
        pattern
        for pattern in structure_patterns
        if compiled_regex(pattern).search(opening) is not None
    )
    if len(matched) < 4:
        return None
    return ScoredLabel(
        "procedimiento",
        0.96,
        (
            f"referencia_cfe:{som_reference}",
            f"estructura_manual_procedimientos:senales={len(matched)}",
        ),
    )


def _commercial_document_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    """Require transaction headings plus fields, not incidental commercial terms."""

    opening = scopes.get("opening", "")
    text = scopes.get("text", "")
    front = f"{opening} {text}"[:16_000]
    labels: list[ScoredLabel] = []
    quote_heading = compiled_regex(
        r"\b(?:SOLICITUD\s+DE\s+COTIZACIONES|COTIZACION|"
        r"OFERTA\s+(?:COMERCIAL|ECONOMICA|TECNICA(?:[-\s]+ECONOMICA)?)|"
        r"PROPUESTA\s+(?:COMERCIAL|ECONOMICA|TECNICA(?:[-\s]+ECONOMICA)?))\b",
    ).search(front)
    quote_fields = sum(
        bool(compiled_regex(pattern).search(front))
        for pattern in (
            r"\b(?:P\.?|PRECIO)\s+UNITARIO\b",
            r"\bSUBTOTAL\b",
            r"\bIMPORTE\b",
            r"\bTOTAL(?:\s+CON\s+IVA)?\b",
            r"\bVIGENCIA\s+DE\s+(?:LA\s+)?COTIZACION\b",
            r"\bTERMINOS\s+DE\s+VENTA\b",
            r"\b(?:FORMA|CONDICIONES)\s+DE\s+PAGO\b",
            r"\bPLAZO\s+DE\s+ENTREGA\b",
        )
    )
    if quote_heading is not None and quote_fields >= 2:
        labels.append(
            ScoredLabel(
                "cotizacion_propuesta",
                0.97,
                (
                    f"opening:encabezado={_clean_identifier(quote_heading.group(0))}",
                    f"opening:campos_comerciales={quote_fields}",
                ),
            )
        )
    purchase_heading = compiled_regex(r"\bORDEN\s+DE\s+COMPRA\b").search(front[:350])
    purchase_fields = sum(
        bool(compiled_regex(pattern).search(front))
        for pattern in (
            r"\bPROVEEDOR\b",
            r"\b(?:NUMERO\s+DE\s+PEDIDO|ORDEN\s+DE\s+COMPRA\s+NO)\b",
            r"\bFECHA\s+DE\s+ENTREGA\b",
            r"\bPRECIO\s+UNITARIO\b",
            r"\bCONDICIONES\s+DE\s+PAGO\b",
        )
    )
    if purchase_heading is not None and purchase_fields >= 2:
        labels.append(
            ScoredLabel(
                "compra_requisicion",
                0.97,
                (
                    "opening:encabezado=ORDEN DE COMPRA",
                    f"opening:campos_comerciales={purchase_fields}",
                ),
            )
        )
    return tuple(labels)


def _administrative_document_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    """Recognize administrative records only from their own filename or heading."""

    path = scopes.get("path", "")
    title = scopes.get("title", "")
    opening = scopes.get("opening", "")
    filename = path.rsplit("\\", 1)[-1]
    for scope, value in (
        ("path", filename),
        ("title", title),
        ("opening", opening[:120]),
    ):
        match = compiled_regex(r"\s*VIATICOS\b").match(value)
        if match is not None:
            return (
                ScoredLabel(
                    "viaticos_gastos",
                    0.90,
                    (f"{scope}:encabezado=VIATICOS",),
                ),
            )
    return ()


def _correspondence_document_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    """Recognize exported email headers over commercial terms in the message."""

    opening = scopes.get("opening", "")[:1_600]
    fields = tuple(
        label
        for label, pattern in (
            ("de", r"\bDE\s*:"),
            ("enviado", r"\bENVIADO\s+EL\s*:"),
            ("para", r"\bPARA\s*:"),
            ("cc", r"\bCC\s*:"),
            ("asunto", r"\bASUNTO\s*:"),
        )
        if compiled_regex(pattern).search(opening)
    )
    if len(fields) < 3:
        return ()
    return (
        ScoredLabel(
            "correspondencia",
            0.99,
            (
                "opening:estructura=correo_exportado",
                "opening:campos_correo=" + ",".join(fields),
            ),
        ),
    )


_STRUCTURED_OPENING_RULES: tuple[tuple[str, str, int], ...] = (
    (
        "procedimiento",
        r"\bPROCEDIMIENTO(?!\s+DE\s+CALIBRACION\s+Y\s+METODO)"
        r"(?:\s+[A-Z0-9-]{3,20})?\b",
        600,
    ),
    (
        "instructivo_trabajo",
        r"\b(?:INSTRUCTIVO\s+(?:DE\s+)?TRABAJO|MANUAL\s+DE\s+INSTRUCTIVOS|"
        r"NOMBRE\s+DEL\s+INSTRUCTIVO)\b",
        1_600,
    ),
    (
        "manual_sistema_gestion",
        r"\bMANUAL\s+(?:DEL?\s+)?(?:SISTEMA\s+DE\s+GESTION|CALIDAD|"
        r"SEGURIDAD|AMBIENTAL)\b",
        1_600,
    ),
    ("informe_auditoria", r"\b(?:INFORME|REPORTE)\s+DE\s+AUDITORIA\b", 1_600),
    ("dossier_calidad", r"\bDOSSIER\s+DE\s+CALIDAD\b", 1_600),
    (
        "reporte_anomalias",
        r"\b(?:INFORME|REPORTE|LEVANTAMIENTO)\s+DE\s+ANOMALIAS\b",
        1_600,
    ),
    (
        "reporte_actividades",
        r"\b(?:(?:INFORME|REPORTE)\s+(?:DIARIO\s+)?DE\s+ACTIVIDADES|"
        r"REPORTE\s+DIARIO\s+DE\s+CAMPO)\b",
        1_600,
    ),
    (
        "descripcion_tecnica_sistema",
        r"\bDESCRIPCION\s+(?:GENERAL|FUNCIONAL)\s+(?:DEL?\s+)?SISTEMA\b",
        1_600,
    ),
    (
        "formato_inspeccion",
        r"\b(?:HOJA|FORMATO)\s+DE\s+INSPECCION\b",
        1_600,
    ),
    ("lista_verificacion", r"\bLISTA\s+DE\s+VERIFICACION\b", 1_600),
    (
        "plan_tecnico",
        r"\bPLAN\s+DE\s+(?:ATENCION\s+Y\s+RESPUESTA\s+A\s+)?EMERGENCIAS\b",
        1_600,
    ),
    (
        "reporte_laboratorio",
        r"\b(?:INFORME|REPORTE)\s+(?:DE\s+)?(?:LABORATORIO|ANALISIS\s+DE\s+ACEITE)\b",
        1_600,
    ),
    (
        "informe_analisis",
        r"\b(?:INFORME|REPORTE)\s+(?:DE\s+)?(?:ANALISIS|TRAZABILIDAD\s+NORMATIVA)\b",
        1_600,
    ),
    (
        "reporte_no_conformidad",
        r"\b(?:INFORME|REPORTE)\s+DE\s+NO\s+CONFORMIDAD\b",
        1_600,
    ),
    (
        "certificado_calidad",
        r"\b(?:CERTIFICATE\s+OF\s+(?:COMPLIANCE|CONFORMITY|QUALITY)|"
        r"CONSTANCIA\s+DE\s+ACEPTACION\s+DE\s+PROTOTIPO)\b",
        1_000,
    ),
    (
        "accion_correctiva_preventiva",
        r"\bACCION\s*:\s*(?:CORRECTIVA|PREVENTIVA)\b",
        1_600,
    ),
    (
        "reporte_resultados_pruebas",
        r"\b(?:REPORTE\s+DE\s+RESULTADOS\s+(?:DE|DEL)|RESULTADOS\s+DE\s+LA\s+PRUEBA)\b",
        1_600,
    ),
    (
        "reporte_inventario_archivo",
        r"\bREPORTE\s+DE\s+ARCHIVO\s*:.{0,180}\bRUTA\s+RELATIVA\s*:",
        800,
    ),
    (
        "hoja_asignacion_proyecto",
        r"\bHOJA\s+DE\s+ASIGNACION\s+DE\s+PROYECTO\b",
        1_600,
    ),
    (
        "instruccion_cuenta_bancaria",
        r"\b(?:CARTA\s+INSTRUCCION\s+PARA\s+REGISTRO\s+DE\s+CUENTA\s+BANCARIA|"
        r"FORMATO\s+DE\s+SOLICITUD\s+DE\s+PAGO\s+MEDIANTE\s+"
        r"TRANSFERENCIA\s+ELECTRONICA\s+BANCARIA)\b",
        1_600,
    ),
    (
        "comprobante_viaje",
        r"\bGRACIAS\s+POR\s+ELEGIR\s+UBER\b",
        1_600,
    ),
    (
        "programa_cronograma",
        r"\bPLAN\s+DE\s+ACTIVIDADES\b",
        1_600,
    ),
    ("registro_auditores", r"\bLISTA\s+DE\s+AUDITORES\s+INTERNOS\b", 1_600),
    ("registro_entrega_epp", r"\bENTREGA\s+DE\s+EPP\b", 1_600),
    ("registro_incidencias", r"\bINCIDENCIAS\s+Y\s+NOVEDADES\b", 1_600),
    ("credencial_visitante", r"\bCREDENCIAL\s+PARA\s+VISITANTES\b", 1_600),
    (
        "programa_seguridad_salud",
        r"\bCOMISION\s+DE\s+SEGURIDAD\s+E\s+HIGIENE\s+EN\s+OBRA\b",
        1_600,
    ),
    (
        "programa_gestion_ambiental",
        r"\bACTIVIDAD,?\s+ASPECTO\s+AMBIENTAL\b.{0,100}\bOBJETIVO\b.{0,60}\bMETA\b",
        1_600,
    ),
)


def _structured_document_type_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    """Prefer explicit front-matter document types over incidental body labels."""

    opening = scopes.get("opening", "")
    if not opening:
        return ()
    labels: dict[str, ScoredLabel] = {}
    form_context = (
        f"{scopes.get('path', '')} {scopes.get('title', '')} {opening[:4_000]}"
    )
    for label, pattern in (
        (
            "informe_analisis",
            r"\b(?:INFORME|REPORTE)\s+(?:DE\s+)?TRAZABILIDAD\s+NORMATIVA\b",
        ),
        (
            "reporte_no_conformidad",
            r"\b(?:INFORME|REPORTE)\s+DE\s+[^A-Z0-9]{0,6}NO\s+CONFORMIDAD\b",
        ),
        (
            "reporte_fat_sat",
            r"\b(?:(?:REPORTE|INFORME)\s+(?:DE\s+PRUEBAS?\s+)?(?:FAT|SAT)|"
            r"SITE\s+TEST\s+(?:AND|&)\s+COMMISSIONING\s+REPORT)\b",
        ),
        (
            "certificado_calidad",
            r"\b(?:CERTIFICADO\s+DE\s+(?:CALIDAD|CONFORMIDAD|INSPECCION)|"
            r"CERTIFICATE\s+OF\s+(?:QUALITY|CONFORMITY|INSPECTION)|"
            r"INSPECTION\s+CERTIFICATE|ABNAHMEPR(?:U|II)FZEUGNIS)\b",
        ),
        (
            "hoja_datos_seguridad",
            r"\b(?:HOJA\s+(?:DE\s+DATOS\s+)?DE\s+SEGURIDAD|"
            r"(?:MATERIAL\s+)?SAFETY\s+DATA\s+SHEET)\b",
        ),
    ):
        match = compiled_regex(pattern).search(form_context)
        if match is not None:
            labels[label] = ScoredLabel(
                label,
                0.98,
                (f"documento:encabezado={_clean_identifier(match.group(0))}",),
            )
    minutes = compiled_regex(
        r"^.{0,120}\b(?:MINUTES\s+OF\s+MEETING|MINUTA\s+DE\s+REUNION)\b"
    ).search(opening)
    if minutes is not None:
        labels["minuta_acta"] = ScoredLabel(
            "minuta_acta",
            0.98,
            (f"opening:encabezado={_clean_identifier(minutes.group(0))}",),
        )
    report_type = compiled_regex(
        r"\bTIPO\s+DE\s+INFORME\s*:?\s*"
        r"(.{3,140}?)(?=\s+(?:INFORME\s+NO|CALLE|PAGINA|INDICE)\b|$)",
    ).search(opening)
    if report_type is not None:
        value = _clean_identifier(report_type.group(1))
        if "FOTOGRAFICO" in value and "ACTIVIDADES" in value:
            label = "reporte_actividades"
        elif "FOTOGRAFICO" in value:
            label = "registro_fotografico"
        elif "ACTIVIDADES" in value:
            label = "reporte_actividades"
        elif "ANOMALIA" in value:
            label = "reporte_anomalias"
        elif "INSPECCION" in value:
            label = "informe_inspeccion"
        else:
            label = ""
        if label:
            labels[label] = ScoredLabel(
                label,
                0.98,
                (f"opening:tipo_de_informe={value}",),
            )
    for label, pattern, max_chars in _STRUCTURED_OPENING_RULES:
        match = compiled_regex(pattern).search(opening[:max_chars])
        if match is None or label in labels:
            continue
        labels[label] = ScoredLabel(
            label,
            0.95,
            (f"opening:encabezado={_clean_identifier(match.group(0))}",),
        )
    return tuple(labels.values())


_STRONG_OPERATIONAL_FORM_PATTERN = (
    r"\b(?:CONTRATO|COTIZACION|OFERTA\s+(?:COMERCIAL|TECNICA|REF)|"
    r"PROPUESTA\s+(?:COMERCIAL|TECNICA)|PROPOSAL\s+NO|PROCEDIMIENTO\s+PARA|"
    r"INFORME\s+EGA|(?:INFORME|REPORTE)\s+DE\s+(?:ACTIVIDADES|INSPECCION|"
    r"ENSAYO|PRUEBAS|RESULTADOS|[^A-Z0-9]{0,6}NO\s+CONFORMIDAD)|"
    r"(?:INFORME|REPORTE)\s+(?:DE\s+)?TRAZABILIDAD\s+NORMATIVA|"
    r"CERTIFICATE\s+OF\s+(?:COMPLIANCE|CONFORMITY|QUALITY)|"
    r"CONSTANCIA\s+DE\s+ACEPTACION\s+DE\s+PROTOTIPO|"
    r"PACKING\s+LIST|LISTA\s+DE\s+EMPAQUE)\b"
)


def _standards_study_evidence(scopes: Mapping[str, str]) -> ScoredLabel | None:
    """Separate a study or summary about a standard from the standard itself."""

    front = " ".join(
        (
            scopes.get("path", ""),
            scopes.get("title", ""),
            scopes.get("opening", "")[:1_200],
        )
    )
    match = compiled_regex(
        r"\b(?:ESTUDIO|RESUMEN|ANALISIS|COMENTARIOS?|INTERPRETACION)\s+"
        r"(?:DE|SOBRE)\s+(?:LA\s+)?NORMAS?\b|"
        r"\b(?:STANDARD|NORM)\s+(?:STUDY|SUMMARY|REVIEW)\b"
    ).search(front)
    if match is None:
        return None
    return ScoredLabel(
        "informe_analisis",
        0.97,
        (f"estructura:estudio_norma={_clean_identifier(match.group(0))}",),
    )


_FORMAL_NORMATIVE_CUES = (
    r"\bINTERNATIONAL\s+STANDARD\b",
    r"\bNORMA\s+INTERNACIONAL\b",
    r"\bAN\s+AMERICAN\s+NATIONAL\s+STANDARD\b",
    r"\bNORMA\s+OFICIAL\s+MEXICANA\b",
    r"\bNORMA\s+MEXICANA\b",
    r"\bIEEE\s+STD\.?\s+[A-Z0-9]",
    r"\bIEEE\s+STANDARDS?\s+ASSOCIATION\b",
    r"\bIEEE\s+(?:STANDARD|GUIDE|RECOMMENDED\s+PRACTICE)\s+FOR\b",
    r"\bIEEE\s+STANDARDS?\s+BOARD\b",
    r"\bAUTHORIZED\s+LICENSED\s+USE\s+LIMITED\s+TO\b",
    r"\bCOPYRIGHT\b.{0,80}\bIEEE\b",
    r"(?:�|\bCOPYRIGHT\b)\s*(?:IEC|ISO)\b",
    r"\bANSI\s*/?\s*NETA\s+(?:STANDARD|MTS|ATS|ECS|ETT)\b",
    r"\bDESIGNATION\s*:?\s*(?:ASTM\s*)?[A-Z]\s*\d{1,5}\b",
    r"(?:�|\bDERECHOS\s+RESERVADOS\b).{0,80}\bIMNC\b",
    r"\bSTANDARD\s+TEST\s+METHOD\s+FOR\b",
    r"\bESPECIFICACION\s+CFE\b",
    r"\bTHIS\s+(?:INTERNATIONAL\s+)?STANDARD\b",
    r"\bDECLARATORIA\s+DE\s+VIGENCIA\b",
)


def _reject_cfe_procedure_as_normative(
    front_matter: str,
    standards: tuple[StandardReference, ...],
    primary_authority: str | None,
) -> bool:
    if primary_authority != "CFE":
        return False
    som_reference = any(
        reference.authority == "CFE" and reference.identifier.startswith("SOM-")
        for reference in standards
    )
    if som_reference and compiled_regex(
        r"\b(?:MANUAL\s+DE\s+)?PROCEDIMIENTOS?\b"
    ).search(front_matter):
        return True
    cfe_procedure = compiled_regex(
        r"\b(?:MANUAL\s+DE\s+PROCEDIMIENTOS|MANUAL\s+CFE|PROCEDIMIENTO\s+CFE)\b"
    ).search(front_matter)
    cfe_specification = compiled_regex(r"\bESPECIFICACION\s+CFE\b").search(front_matter)
    return cfe_procedure is not None and cfe_specification is None


def _formal_normative_cue(front_matter: str) -> str | None:
    match = next(
        (
            candidate
            for cue in _FORMAL_NORMATIVE_CUES
            if (candidate := compiled_regex(cue).search(front_matter)) is not None
        ),
        None,
    )
    return None if match is None else match.group(0)


def _direct_normative_identifiers(
    front_matter: str,
    standards: tuple[StandardReference, ...],
    primary_authority: str | None,
) -> tuple[tuple[str, ...], bool]:
    direct = tuple(
        reference.identifier
        for reference in standards
        if _reference_position(front_matter, reference) >= 0
    )
    preferred = tuple(
        reference.identifier
        for reference in standards
        if reference.authority == primary_authority
        and _reference_position(front_matter, reference) >= 0
    )
    if preferred:
        direct = tuple(dict.fromkeys((*preferred, *direct)))
    starts_with_identifier = any(
        0
        <= _reference_position(
            front_matter,
            next(
                reference
                for reference in standards
                if reference.identifier == identifier
            ),
        )
        <= 40
        for identifier in direct
    )
    return direct, starts_with_identifier


def _operational_form_blocks_normative(
    scopes: Mapping[str, str],
    opening: str,
    *,
    starts_with_identifier: bool,
) -> bool:
    title = scopes.get("title", "")
    path_title_form = compiled_regex(_EXPLICIT_NON_NORMATIVE_FORM_PATTERN).search(
        f"{title} {scopes.get('path', '')}"
    )
    opening_form = compiled_regex(_EXPLICIT_NON_NORMATIVE_FORM_PATTERN).search(
        opening[:600]
    )
    strong_operational_form = compiled_regex(_STRONG_OPERATIONAL_FORM_PATTERN).search(
        f"{title} {scopes.get('path', '')} {opening[:1_000]}"
    )
    return (
        strong_operational_form is not None
        or path_title_form is not None
        or (opening_form is not None and not starts_with_identifier)
    )


def _direct_normative_evidence(
    identifiers: tuple[str, ...],
    formal_cue: str | None,
    *,
    starts_with_identifier: bool,
) -> ScoredLabel | None:
    if not identifiers or (formal_cue is None and not starts_with_identifier):
        return None
    evidence = [f"opening:identificador_normativo={identifiers[0]}"]
    if formal_cue is not None:
        evidence.append(f"opening:estructura_normativa={_clean_identifier(formal_cue)}")
    return ScoredLabel("normativa", 0.96, tuple(evidence))


def _path_normative_evidence(
    scopes: Mapping[str, str],
    standards: tuple[StandardReference, ...],
    *,
    primary_authority: str | None,
    managed_path: bool,
    managed_normative_path: bool,
) -> ScoredLabel | None:
    if managed_path and not managed_normative_path:
        return None
    filename = scopes.get("path", "").rsplit("\\", 1)[-1]
    for reference in standards:
        identifier = fold_signal(reference.identifier)
        position = filename.find(identifier)
        if (not managed_path and 0 <= position <= 12) or (
            managed_normative_path and position >= 0
        ):
            return ScoredLabel(
                "normativa",
                0.90,
                (f"path:identificador_normativo={reference.identifier}",),
            )
    if not managed_normative_path or not standards:
        return None
    reference = next(
        (item for item in standards if item.authority == primary_authority),
        standards[0],
    )
    return ScoredLabel(
        "normativa",
        0.88,
        (f"path:categoria_normativa_previa={reference.identifier}",),
    )


def _normative_document_evidence(
    scopes: Mapping[str, str],
    standards: tuple[StandardReference, ...],
    *,
    primary_authority: str | None,
    managed_path: bool,
    managed_normative_path: bool,
) -> ScoredLabel | None:
    """Separate a standard itself from a procedure merely citing standards."""

    opening = _front_before_reference_section(scopes.get("opening", ""))
    front_matter = f"{scopes.get('title', '')} {opening[:1_800]}".strip()
    if _reject_cfe_procedure_as_normative(
        front_matter,
        standards,
        primary_authority,
    ):
        return None
    formal_cue = _formal_normative_cue(front_matter)
    identifiers, starts_with_identifier = _direct_normative_identifiers(
        front_matter,
        standards,
        primary_authority,
    )
    if _operational_form_blocks_normative(
        scopes,
        opening,
        starts_with_identifier=starts_with_identifier,
    ):
        return None
    direct = _direct_normative_evidence(
        identifiers,
        formal_cue,
        starts_with_identifier=starts_with_identifier,
    )
    if direct is not None:
        return direct
    return _path_normative_evidence(
        scopes,
        standards,
        primary_authority=primary_authority,
        managed_path=managed_path,
        managed_normative_path=managed_normative_path,
    )


def _controlled_procedure_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Recognize a controlled procedure whose references include standards."""

    opening = scopes.get("opening", "")[:4_000]
    if compiled_regex(r"\bCONTROL\s+DE\s+REVISIONES\b").search(opening) is None:
        return None
    signals = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bOBJETIVO\b",
            r"\bALCANCE\b",
            r"\bRESPONSABILIDADES\b",
            r"\bDEFINICIONES\b",
            r"\bDOCUMENTOS\s+DE\s+REFERENCIA\b",
            r"\bEQUIPO\s+DE\s+PROTECCION\s+PERSONAL\b",
            r"\bACCIONES\b",
        )
    )
    if signals < 4:
        return None
    return ScoredLabel(
        "procedimiento",
        0.99,
        (f"opening:estructura_procedimiento_controlado={signals}",),
    )


def _cfe_technical_manual_chapter_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Separate a CFE technical-manual chapter from the specifications it cites."""

    opening = scopes.get("opening", "")[:2_000]
    markers = tuple(
        pattern
        for pattern in (
            r"\bCOMISION\s+FEDERAL\s+DE\s+ELECTRICIDAD\b",
            r"\bCOORDINACION\s+DE\s+DISTRIBUCION\b",
            r"\bCAPITULO\s+\d{1,3}\b",
            r"\bTEORIA\s+GENERAL\b",
            r"\bREQUERIMIENTOS\s+PARA\s+EL\s+MONTAJE\s+Y\s+MANTENIMIENTO\b",
        )
        if compiled_regex(pattern).search(opening) is not None
    )
    if len(markers) < 4:
        return None
    return ScoredLabel(
        "manual_equipo",
        0.97,
        (f"opening:estructura_capitulo_manual_cfe={len(markers)}",),
    )


def _test_requirement_checklist_evidence(
    scopes: Mapping[str, str],
) -> ScoredLabel | None:
    """Identify a filled CFE test-requirement matrix, not the cited specification."""

    opening = scopes.get("opening", "")[:1_600]
    heading = compiled_regex(
        r"\bPRUEBAS\s+QUE\s+SOLICITA\s+CFE\s+EN\s+ESPECIFICACION\b"
    ).search(opening)
    fields = sum(
        compiled_regex(pattern).search(opening) is not None
        for pattern in (
            r"\bREALIZADA\b",
            r"\bSI\s+NO\b",
            r"\bSE\s+ENCUENTRA\s+EN\s+REGISTRO\s+DE\s+PRUEBAS\b",
            r"\bRESISTENCIA\s+DE\s+AISLAMIENTO\b",
        )
    )
    if heading is None or fields < 2:
        return None
    return ScoredLabel(
        "lista_verificacion",
        0.98,
        (f"opening:estructura_lista_pruebas_cfe={fields}",),
    )


def _calibration_certificate_evidence(
    scopes: Mapping[str, str],
    *,
    page_count: int | None,
) -> ScoredLabel | None:
    """Require a dedicated calibration-report structure, not a passing mention."""

    matches: dict[str, tuple[str, str]] = {}
    for group, patterns in _CALIBRATION_EVIDENCE_GROUPS.items():
        for scope, text in scopes.items():
            match = next(
                (
                    found
                    for pattern in patterns
                    if (found := compiled_regex(pattern, re.IGNORECASE).search(text))
                    is not None
                ),
                None,
            )
            if match is not None:
                matches[group] = (scope, _clean_identifier(match.group(0)))
                break
    if "heading" not in matches:
        return None
    support = {"traceability", "identity", "results"}.intersection(matches)
    if len(support) < 2:
        return None
    heading_scope = matches["heading"][0]
    if "subject" not in matches and heading_scope not in {"path", "title"}:
        return None
    if (
        page_count is not None
        and page_count > 20
        and heading_scope
        not in {
            "path",
            "title",
        }
    ):
        return None
    evidence = tuple(
        f"{scope}:calibracion_{group}={value}"
        for group, (scope, value) in matches.items()
    )
    score = 0.97 if heading_scope in {"path", "title"} else 0.88
    return ScoredLabel("certificado_calibracion", score, evidence)


# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_kinds")
