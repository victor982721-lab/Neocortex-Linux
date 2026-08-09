"""Deterministic semantic filename suggestions from bounded catalog signals."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable


# region [01] Public naming contract

NAMING_VERSION = "technical-document-naming-v9"
MAX_SUGGESTED_STEM_CHARS = 180


@dataclass(frozen=True, slots=True)
class NamingSuggestion:
    stem: str
    evidence: tuple[str, ...]


_KIND_LABELS = {
    "catalogo_equipo": "Catalogo de equipo",
    "accion_correctiva_preventiva": "Accion correctiva o preventiva",
    "audio_transcrito": "Audio transcrito",
    "certificado_calibracion": "Certificado de calibracion",
    "certificado_calidad": "Certificado de calidad",
    "constancia_capacitacion": "Constancia de capacitacion",
    "control_metrologico": "Control metrologico de equipos",
    "comprobante_viaje": "Comprobante de viaje",
    "correspondencia": "Correspondencia",
    "credencial_visitante": "Credencial para visitantes",
    "curso_capacitacion": "Curso de capacitacion",
    "documento_empresa": "Documento tecnico",
    "descripcion_tecnica_sistema": "Descripcion tecnica del sistema",
    "dossier_calidad": "Dossier de calidad",
    "especificacion_tecnica": "Especificacion tecnica",
    "ficha_tecnica": "Ficha tecnica",
    "hoja_datos_seguridad": "Hoja de datos de seguridad",
    "formato_empresa": "Formato",
    "formato_inspeccion": "Formato de inspeccion",
    "informe_analisis": "Informe de analisis",
    "informe_auditoria": "Informe de auditoria",
    "informe_inspeccion": "Informe de inspeccion",
    "informe_tecnico": "Informe tecnico",
    "instructivo_trabajo": "Instructivo de trabajo",
    "instruccion_cuenta_bancaria": "Instruccion de cuenta bancaria",
    "lista_materiales": "Lista de materiales",
    "lista_verificacion": "Lista de verificacion",
    "manual_equipo": "Manual de equipo",
    "manual_sistema_gestion": "Manual del sistema de gestion",
    "memoria_calculo": "Memoria de calculo",
    "normativa": "Normativa",
    "orden_trabajo": "Orden de trabajo",
    "plano_diagrama": "Plano o diagrama",
    "plan_tecnico": "Plan tecnico",
    "procedimiento": "Procedimiento",
    "programa_cronograma": "Programa o cronograma",
    "programa_gestion_ambiental": "Programa de gestion ambiental",
    "programa_seguridad_salud": "Programa de seguridad y salud",
    "hoja_asignacion_proyecto": "Hoja de asignacion de proyecto",
    "protocolo_pruebas": "Protocolo de pruebas",
    "referencia_tecnica": "Referencia tecnica",
    "registro_bitacora": "Bitacora o registro",
    "registro_fotografico": "Registro fotografico",
    "registro_asistencia": "Registro de asistencia",
    "registro_mediciones": "Registro de mediciones",
    "reporte_actividades": "Reporte de actividades",
    "reporte_anomalias": "Reporte de anomalias",
    "reporte_fat_sat": "Informe FAT SAT",
    "reporte_laboratorio": "Informe de laboratorio",
    "reporte_no_conformidad": "Reporte de no conformidad",
    "reporte_inventario_archivo": "Reporte de inventario de archivo",
    "reporte_resultados_pruebas": "Reporte de resultados de pruebas",
    "registro_auditores": "Registro de auditores internos",
    "registro_entrega_epp": "Registro de entrega de EPP",
    "registro_incidencias": "Registro de incidencias",
    "viaticos_gastos": "Viaticos y gastos",
}


def suggest_document_stem(
    *,
    path: str,
    title: str,
    leading_text: str,
    primary_kind: str,
    standard_identifiers: Iterable[str] = (),
    organization: str | None = None,
    topic: str | None = None,
) -> NamingSuggestion:
    """Build one bounded human-readable stem without touching the source file."""

    identifiers = tuple(
        token for value in standard_identifiers if (token := _clean_standard_identifier(value))
    )
    specialized = _specialized_suggestion(
        path=path,
        title=title,
        leading_text=leading_text,
        primary_kind=primary_kind,
        identifiers=identifiers,
        organization=organization,
        topic=topic,
    )
    if specialized is not None:
        return specialized
    return _generic_suggestion(
        path=path,
        title=title,
        leading_text=leading_text,
        primary_kind=primary_kind,
        organization=organization,
    )


def _specialized_suggestion(
    *,
    path: str,
    title: str,
    leading_text: str,
    primary_kind: str,
    identifiers: tuple[str, ...],
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    direct = _direct_kind_suggestion(
        path=path,
        title=title,
        leading_text=leading_text,
        primary_kind=primary_kind,
        identifiers=identifiers,
        organization=organization,
        topic=topic,
    )
    if direct is not None:
        return direct
    return _evidence_dependent_suggestion(
        leading_text=leading_text,
        primary_kind=primary_kind,
        organization=organization,
        topic=topic,
    )


def _direct_kind_suggestion(
    *,
    path: str,
    title: str,
    leading_text: str,
    primary_kind: str,
    identifiers: tuple[str, ...],
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    if primary_kind == "normativa":
        return _normative_suggestion(
            path=path,
            title=title,
            leading_text=leading_text,
            identifiers=identifiers,
        )
    if primary_kind == "audio_transcrito":
        return _audio_transcript_suggestion(path)
    if primary_kind == "comprobante_viaje":
        return _travel_receipt_suggestion(
            leading_text,
            organization=organization,
        )
    if primary_kind in _CONTROLLED_RECORD_KINDS:
        return _controlled_record_suggestion(
            leading_text,
            primary_kind=primary_kind,
            organization=organization,
            topic=topic,
        )
    return None


def _evidence_dependent_suggestion(
    *,
    leading_text: str,
    primary_kind: str,
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    if primary_kind == "registro_mediciones":
        measurement = _measurement_record_suggestion(
            leading_text,
            organization=organization,
        )
        if measurement is not None:
            return measurement
    if primary_kind in {"registro_fotografico", "reporte_actividades"}:
        report = _report_suggestion(
            leading_text,
            primary_kind=primary_kind,
            organization=organization,
            topic=topic,
        )
        if report is not None:
            return report
    if primary_kind == "certificado_calibracion":
        calibration = _calibration_suggestion(
            leading_text,
            organization=organization,
        )
        if calibration is not None:
            return calibration
    if primary_kind == "correspondencia":
        correspondence = _correspondence_suggestion(
            leading_text,
            organization=organization,
        )
        if correspondence is not None:
            return correspondence
    if primary_kind == "reporte_laboratorio":
        laboratory_report = _laboratory_report_suggestion(
            leading_text,
            organization=organization,
        )
        if laboratory_report is not None:
            return laboratory_report
    return None


def _report_suggestion(
    leading_text: str,
    *,
    primary_kind: str,
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    report = _structured_report_suggestion(
        leading_text,
        primary_kind=primary_kind,
        organization=organization,
        topic=topic,
    )
    if report is not None or primary_kind != "reporte_actividades":
        return report
    return _daily_field_report_suggestion(
        leading_text,
        organization=organization,
        topic=topic,
    )


def _generic_suggestion(
    *,
    path: str,
    title: str,
    leading_text: str,
    primary_kind: str,
    organization: str | None,
) -> NamingSuggestion:
    original_stem = _portable_path_stem(path)
    descriptive = _meaningful_title(title, original_stem)
    evidence: list[str] = []
    if descriptive:
        evidence.append("metadata:title")
    else:
        descriptive = _meaningful_original_stem(original_stem)
        if descriptive:
            evidence.append("path:original_stem")
    if not descriptive:
        descriptive = _leading_heading(leading_text)
        if descriptive:
            evidence.append("text:leading_heading")

    pieces: list[str] = []
    if descriptive:
        pieces.append(descriptive)
    if not pieces:
        pieces.append(_KIND_LABELS.get(primary_kind, primary_kind.replace("_", " ")))
        evidence.append("classification:document_kind")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


# endregion [01]


# region [02] Normative and audio names

_RECOVERED_OR_HASH_NAME = re.compile(
    r"(?i)^(?:(?:19|20)\d{2}\s*[-—]\s*)?"
    r"(?:documento|archivo)\s+(?:personal\s+)?(?:protegido\s+)?recuperado\b|"
    r"^documen~[0-9a-f]+$|"
    r"^[0-9a-f]{24,}(?:\.repaired)?$"
)
_GENERATED_SUFFIX = re.compile(
    r"(?i)(?:__(?:pdf|docx|xlsx|pptx|odt)_[0-9a-f_]+|\s+-\s+[0-9a-f]{8})$"
)
_NORMATIVE_BOILERPLATE = re.compile(
    r"(?i)^(?:norma\s+internacional|international\s+standard|"
    r"norma\s+mexicana|norma\s+oficial\s+mexicana|"
    r"comisi[oó]n\s+federal\s+de\s+electricidad|"
    r"documento\s+(?:personal\s+)?recuperado|"
    r"derechos\s+reservados|la\s+propuesta\s+de\s+revisi[oó]n|"
    r"acordaron\s+una\s+pr[oó]rroga|doc\b|"
    r"designation|reference\s+number|impresi[oó]n\s+de\s+fax|"
    r"p[aá]gina|page|esquema\s+de\s+norma|uso\s+corporativo|"
    r"all\s+rights\s+reserved|microsoft\s+word|copyright|©|"
    r"this\s+is\s+a\s+preview|d1\b|wnd\b|\d{1,4}\b|\d{8}\b)"
)
_KNOWN_STANDARD_TITLES = {
    "ASTM D1169": "Specific Resistance of Electrical Insulating Liquids",
    "ASTM D1816": "Dielectric Breakdown Voltage of Insulating Liquids Using VDE Electrodes",
    "ASTM D3612": "Analysis of Gases Dissolved in Electrical Insulating Oil by Gas Chromatography",
    "ASTM D877": "Dielectric Breakdown Voltage of Insulating Liquids Using Disk Electrodes",
    "CFE 00J00-52": "Red de puesta a tierra para estructuras de líneas de transmisión aéreas de 69 kV a 400 kV",
    "CFE D3100-19": "Aceite aislante",
    "CFE D8500-02": "Recubrimientos anticorrosivos",
    "CFE DCDSET01": "Diseño de subestaciones eléctricas de transmisión",
    "CFE DCCAMBT": "Construcción de instalaciones aéreas en media y baja tensión",
    "CFE DCCSSUBT": "Construcción de sistemas subterráneos",
    "CFE G0100-04": "Interconexión a la red eléctrica de baja tensión de sistemas fotovoltaicos hasta 30 kW",
    "CFE K0000-06": "Transformadores de potencia de 10 MVA y mayores",
    "CFE K0000-13": "Transformadores y autotransformadores de potencia para subestaciones de distribución",
    "CFE K0000-15": "TRANSFORMADORES",
    "CFE K0000-17": "Transformadores tipo seco para excitación de generadores eléctricos",
    "CFE K0000-23": "Monitoreo en línea de gases disueltos y agua en líquido aislante de transformadores",
    "CFE U0000-24": "Sistema de control, automatización y adquisición de datos (SCAAD)",
    "CFE VE100-13": "Transformadores de potencia",
    "CFE V8000-52": "Banco de capacitores de 13.8 kV a 34.5 kV",
    "CFE V8000-53": "Banco de capacitores de 69 kV a 161 kV",
    "CFE VY200-40": "Subestaciones blindadas en gas SF6 de 72.5 kV a 420 kV",
    "IEC 60076-1": "POWER TRANSFORMERS PART 1 GENERAL",
    "IEC 60076-3": "Power transformers - Part 3: Insulation levels, dielectric tests and external clearances in air",
    "IEC 60270": "High-voltage test techniques - Partial discharge measurements",
    "IEC 61869-2": "Instrument transformers - Part 2: Additional requirements for current transformers",
    "IEEE 43-2000": "IEEE Recommended Practice for Testing Insulation Resistance of Rotating Machinery",
    "IEEE 62-1995": "IEEE Guide for Diagnostic Field Testing of Electric Power Apparatus",
    "IEEE 62.2-2004": "IEEE Guide for Diagnostic Field Testing of Electric Power Apparatus - Electrical Machinery",
    "IEEE 80-2000": "IEEE Guide for Safety in AC Substation Grounding",
    "IEEE 81-1983": "IEEE Guide for Measuring Earth Resistivity, Ground Impedance, and Earth Surface Potentials",
    "IEEE 95-1977": "IEEE Guide for Insulation Maintenance of Large AC Rotating Machinery",
    "IEEE 95-2002": "IEEE Recommended Practice for Insulation Testing of AC Electric Machinery with High Direct Voltage",
    "IEEE 286-2000": "IEEE Recommended Practice for Measurement of Power Factor Tip-Up of Electric Machinery Stator Coil Insulation",
    "IEEE 400-2001": "IEEE Guide for Field Testing and Evaluation of Shielded Power Cable Systems",
    "IEEE 400.2-2013": "IEEE Guide for Field Testing of Shielded Power Cable Systems Using Very Low Frequency",
    "IEEE 400.4-2015": "IEEE Guide for Field Testing of Shielded Power Cable Systems Using Damped Alternating Current",
    "IEEE 433-2009": "IEEE Recommended Practice for Insulation Testing of AC Electric Machinery with Very Low Frequency",
    "IEEE 450-2010": "IEEE Recommended Practice for Maintenance, Testing, and Replacement of Vented Lead-Acid Batteries",
    "IEEE 1106-2005": "IEEE Recommended Practice for Vented Nickel-Cadmium Batteries for Stationary Applications",
    "IEEE 1188-2005": "IEEE Recommended Practice for Maintenance, Testing, and Replacement of Valve-Regulated Lead-Acid Batteries",
    "IEEE 2760-2020": "IEEE Guide for Wind Power Plant Grounding System Design for Personnel Safety",
    "IEEE 2778-2020": "IEEE Guide for Solar Power Plant Grounding for Personnel Protection",
    "IEEE C37.09-1999": "IEEE Standard Test Procedure for AC High-Voltage Circuit Breakers",
    "IEEE C57.12.00-2015": "IEEE Standard for General Requirements for Liquid-Immersed Distribution, Power, and Regulating Transformers",
    "IEEE C57.12.90-2015": "IEEE Standard Test Code for Liquid-Immersed Distribution, Power, and Regulating Transformers",
    "IEEE C57.12.200-2022": "IEEE Guide for Dielectric Frequency Response Measurement of Bushings",
    "IEEE C57.13-2016": "IEEE Standard Requirements for Instrument Transformers",
    "IEEE C57.98-1993": "IEEE Guide for Transformer Impulse Tests",
    "IEEE C57.106-2002": "IEEE Guide for Acceptance and Maintenance of Insulating Mineral Oil in Electrical Equipment",
    "IEEE C57.149-2012": "IEEE Guide for Frequency Response Analysis of Oil-Immersed Transformers",
    "IEEE C57.152-2013": "IEEE Guide for Diagnostic Field Testing of Fluid-Filled Power Transformers, Regulators, and Reactors",
    "IEEE C57.154-2012": "IEEE Standard for Liquid-Immersed Transformers Designed for High-Temperature Operation",
    "IEEE C57.161-2018": "IEEE Guide for Dielectric Frequency Response Test",
    "NETA MTS": "Standard for Maintenance Testing Specifications for Electrical Power Equipment and Systems",
    "ISO 9001:2015": "Quality management systems - Requirements",
    "ISO 18434-1:2008": "Condition monitoring and diagnostics of machines - Thermography - Part 1: General procedures",
    "NMX-CC-9001-IMNC-2015": "Sistemas de gestión de la calidad - Requisitos",
    "NMX-CH-6789-IMNC-2006": "Herramientas manuales de medición de par torsional - Requisitos y métodos de ensayo",
    "NMX-J-123-ANCE-2008": "Aceites minerales aislantes para transformadores - Especificaciones, muestreo y métodos de prueba",
    "NMX-J-123-ANCE-2019": "Aceites minerales aislantes para transformadores - Especificaciones, muestreo y métodos de prueba",
    "NMX-J-142-1-ANCE-2017": "Cables de energía con pantalla metálica - Especificaciones y métodos de prueba",
    "NMX-J-284-ANCE-2012": "Transformadores y autotransformadores de potencia - Especificaciones",
    "NMX-J-308-ANCE-2004": "Transformadores - Guía para el manejo, almacenamiento, control y tratamiento de aceites aislantes",
    "NMX-SAA-14001-IMNC-2004": "Sistemas de gestión ambiental - Requisitos con orientación para su uso",
    "NMX-SAA-14001-IMNC-2015": "Sistemas de gestión ambiental - Requisitos con orientación para su uso",
    "NMX-SAST-45001-IMNC-2018": "Sistemas de gestión de la seguridad y salud en el trabajo - Requisitos con orientación para su uso",
    "NOM-001-SEDE-2012": "Instalaciones eléctricas (utilización)",
    "NOM-002-SEDE-ENER-2014": "Requisitos de seguridad y eficiencia energética para transformadores de distribución",
    "NOM-022-STPS-2015": "Electricidad estática en los centros de trabajo - Condiciones de seguridad",
    "NOM-133-SEMARNAT-2015": "Protección ambiental - Bifenilos policlorados - Especificaciones de manejo",
}


def _normative_suggestion(
    *,
    path: str,
    title: str,
    leading_text: str,
    identifiers: tuple[str, ...],
) -> NamingSuggestion:
    """Name a standard from its cover identifier and a concise subject."""

    primary = identifiers[0] if identifiers else _KIND_LABELS["normativa"]
    evidence = [
        "classification:standard_identifier" if identifiers else "classification:document_kind"
    ]
    canonical_title = _canonical_standard_title(primary)
    candidates = (
        (_clean_token(title), "metadata:title"),
        (_meaningful_original_stem(_portable_path_stem(path)), "path:original_stem"),
        (_normative_leading_candidate(leading_text), "text:leading_heading"),
    )
    description = canonical_title
    if description is not None:
        evidence.append("registry:standard_title")
    else:
        for candidate, source in candidates:
            description = _normative_description(candidate, identifiers)
            if description is not None:
                evidence.append(source)
                break
    pieces = [primary]
    if description and not _contains_token(pieces, description):
        pieces.append(description)
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _canonical_standard_title(identifier: str) -> str | None:
    """Return a stable domain title without inferring it from cited boilerplate."""

    key = re.sub(r"\s+", " ", identifier.upper()).strip()
    key = re.sub(r"^(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+", "IEEE ", key)
    key = re.sub(r"^ANSI\s*/?\s*NETA\s+", "NETA ", key)
    if title := _KNOWN_STANDARD_TITLES.get(key):
        return title
    neta = re.fullmatch(r"NETA\s+(MTS)(?:-\d{4})?", key)
    if neta is not None:
        return _KNOWN_STANDARD_TITLES.get(f"NETA {neta.group(1)}")
    iec = re.fullmatch(r"(IEC\s+\d+(?:-\d+)*)(?:[: ]\d{4})?", key)
    if iec is not None:
        return _KNOWN_STANDARD_TITLES.get(iec.group(1))
    return None


def _normative_description(
    value: str | None,
    identifiers: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    candidate = value
    for identifier in identifiers:
        expression = _identifier_match_expression(identifier)
        candidate = re.sub(expression, " ", candidate, flags=re.IGNORECASE)
    for identifier in identifiers:
        base_expression = _identifier_base_match_expression(identifier)
        if base_expression is not None:
            candidate = re.sub(
                base_expression,
                " ",
                candidate,
                flags=re.IGNORECASE,
            )
    candidate = re.split(
        r"(?i)\b(?:NORMATIVE\s+REFERENCES|REFERENCES|REFERENCIAS)\b",
        candidate,
        maxsplit=1,
    )[0]
    candidate = re.sub(
        r"\b(?=[A-Za-z0-9]{16,}\b)(?=[A-Za-z0-9]*[A-Za-z])"
        r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b",
        " ",
        candidate,
    )
    candidate = re.sub(r"(?i)^\s*(?:19|20)\d{2}\s*[-—:]\s*", "", candidate)
    candidate = re.sub(r"(?i)\(\s*(?:REVISION\s+OF)?\s*\)", " ", candidate)
    candidate = re.sub(
        r"(?i)^\s*(?:A\s+)?THIS\s+IS\s+A\s+PREVIEW"
        r"(?:\s*[-—]\s*CLICK\s+HERE\s+TO\s+BUY\s+THE\s+FULL\s+PUBLICATION)?[.\s]*",
        "",
        candidate,
    )
    candidate = re.sub(
        r"(?i)^\s*(?:COPIA\s+CONTROLADA[\s.:-]*)+",
        "",
        candidate,
    )
    candidate = re.sub(
        r"(?i)^\s*CANCELA\s+Y\s+REEMPLAZA\s+A\s+LA\s+"
        r"NORMA\s+MEXICANA\s*",
        "",
        candidate,
    )
    candidate = re.sub(
        r"(?i)^\s*(?:(?:NORMA\s+INTERNACIONAL|NORME\s+INTERNATIONALE|"
        r"INTERNATIONAL\s+STANDARD|NORMA\s+MEXICANA|"
        r"NORMA\s+OFICIAL\s+MEXICANA|NORMA|PROY|M[EÉ]XICO|IMNC|ANCE|"
        r"IEEE\s+STANDARDS?\s+ASSOCIATION|CEI|IEC)\s*[-—:]?\s*)+",
        "",
        candidate,
    )
    candidate = re.sub(
        r"(?i)^\s*(?:\d{1,4}\s+)?(?:"
        r"ESPECIFICACI[OÓ]N(?:ES)?(?:\s+T[EÉ]CNICAS?)?(?:\s+CFE)?|"
        r"DESIGNATION)\s*[-—:]?\s*(?:PARA\s+LA\s+)?",
        "",
        candidate,
    )
    candidate = re.sub(
        r"(?i)^\s*(?:CFE(?:\s+COMISI[OÓ]N\s+FEDERAL\s+DE\s+ELECTRICIDAD)?|"
        r"ANDRITZ|SERINTRA|LAPEM)\s*[-—:]\s*",
        "",
        candidate,
    )
    candidate = candidate.lstrip(" .,_-—;:–")  # noqa: RUF001 - accepted typography
    candidate = re.split(r"\.\s+(?=[A-ZÁÉÍÓÚÑ])", candidate, maxsplit=1)[0]
    candidate = re.sub(r"\s+", " ", candidate).strip(" .,_-—;“”'\"")
    if not candidate or _NORMATIVE_BOILERPLATE.match(candidate):
        return None
    readable = sum(
        character.isalnum() or character.isspace() or character in "-–—,().áéíóúÁÉÍÓÚñÑ"  # noqa: RUF001 - accepted typography
        for character in candidate
    ) / len(candidate)
    words = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]{3,}", candidate)
    if readable < 0.78 or not words:
        return None
    if len(candidate.split()) > 18:
        candidate = " ".join(candidate.split()[:18])
    return _clean_token(candidate)


def _normative_leading_candidate(value: str) -> str | None:
    """Retain enough one-line cover text to reach the actual standard title."""

    normalized = unicodedata.normalize("NFKC", value[:1_200]).replace("�", " ")
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._-—")
    return normalized[:600].rstrip(" .") or None


def _identifier_match_expression(identifier: str) -> str:
    """Match normalized identifiers against trademark and common OCR variants."""

    iec = re.fullmatch(
        r"(IEC(?:\s+(?:TR|TS|PAS))?\s+.+?):((?:19|20)\d{2})",
        identifier,
        re.IGNORECASE,
    )
    if iec is not None:
        base = _flexible_identifier_literal(iec.group(1))
        year = re.escape(iec.group(2))
        return (
            base
            + r"(?:\s+EDITION\s+\d+(?:\.\d+)?\s+"
            + year
            + r"(?:-\d{2})?|[\s_./:-]+"
            + year
            + r")"
        )
    ieee = re.fullmatch(
        r"(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+(.+)",
        identifier,
        re.IGNORECASE,
    )
    if ieee is not None:
        dated = re.fullmatch(r"(.+?)([-:]\d{4})", ieee.group(1))
        designation_text = ieee.group(1) if dated is None else dated.group(1)
        edition_text = None if dated is None else dated.group(2)
        edition = "" if edition_text is None else re.escape(edition_text)
        return (
            r"(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+"
            + _flexible_identifier_literal(designation_text)
            + r"(?:\s*TM)?"
            + (rf"\s*{edition}" if edition else "")
        )
    astm = re.fullmatch(r"ASTM\s+([A-Z])\s*(\d{1,5})(?:[-:]\d{2,4})?", identifier)
    if astm is not None:
        return (
            rf"(?:ASTM\s*)?{astm.group(1)}\s*{astm.group(2)}"
            r"(?:\s*[-–:]\s*\d{2,4})?"  # noqa: RUF001 - accepted identifier punctuation
        )
    expression = _flexible_identifier_literal(identifier)
    if identifier.upper().startswith("CFE "):
        expression = expression.replace("0", "[0O]")
    return expression


def _identifier_base_match_expression(identifier: str) -> str | None:
    """Match an undated IEEE basename only after dated references are removed."""

    ieee = re.fullmatch(
        r"(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+(.+)",
        identifier,
        re.IGNORECASE,
    )
    if ieee is not None:
        dated = re.fullmatch(r"(.+?)([-:]\d{4})", ieee.group(1))
        if dated is not None:
            return r"(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+" + _flexible_identifier_literal(
                dated.group(1)
            )
    dated = re.fullmatch(r"(.+?)[-:]((?:19|20)\d{2})", identifier)
    return None if dated is None else _flexible_identifier_literal(dated.group(1))


def _flexible_identifier_literal(value: str) -> str:
    """Allow harmless punctuation and spacing differences in a document code."""

    tokens = re.findall(r"[A-Za-z]+|\d+", value)
    if not tokens:
        return re.escape(value)
    return r"[\s_./:-]*".join(re.escape(token) for token in tokens)


def _audio_transcript_suggestion(path: str) -> NamingSuggestion:
    managed = "consulta_tecnica_organizada" in path.replace(" ", "_").casefold()
    original = None if managed else _meaningful_original_stem(_portable_path_stem(path))
    if original is not None and len(original.split()) <= 12:
        return NamingSuggestion(
            _join_stem((_KIND_LABELS["audio_transcrito"], original)),
            ("classification:document_kind", "path:original_stem"),
        )
    return NamingSuggestion(
        _KIND_LABELS["audio_transcrito"],
        ("classification:document_kind",),
    )


def _portable_path_stem(path: str) -> str:
    """Read persisted Windows paths without changing native POSIX filenames."""

    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", path):
        return PureWindowsPath(path).stem
    return Path(path).stem


def _measurement_record_suggestion(
    text: str,
    *,
    organization: str | None,
) -> NamingSuggestion | None:
    """Name a field record from the measured quantity rather than a temp name."""

    collapsed = re.sub(r"\s+", " ", text[:2_000]).strip()
    match = re.search(
        r"(?i)\b(MEDICI[OÓ]N\s+DE\s+LA\s+RESISTENCIA\s+"
        r"(?:DE|EN)\s+(?:LA\s+)?(?:RED|SISTEMA)\s+DE\s+TIERRAS?"
        r"(?:\s+DE\s+UNA\s+SUBESTACI[OÓ]N\s+EL[EÉ]CTRICA\s+EN\s+MEDIA\s+TENSI[OÓ]N)?)\b",
        collapsed,
    )
    if match is None:
        return None
    pieces = [match.group(1).title()]
    evidence = ["text:measurement_heading"]
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


# endregion [02]


# region [03] Controlled records

_CONTROLLED_RECORD_KINDS = frozenset(
    {
        "accion_correctiva_preventiva",
        "credencial_visitante",
        "hoja_asignacion_proyecto",
        "manual_sistema_gestion",
        "programa_gestion_ambiental",
        "programa_seguridad_salud",
        "registro_auditores",
        "registro_entrega_epp",
        "registro_incidencias",
    }
)
_CONTROLLED_DOCUMENT_CODE = re.compile(
    r"\b(?:F[A-Z]{2,10}\d{2,4}[-.]\d{1,3}|"
    r"[A-Z]{2,8}(?:-[A-Z0-9]{1,8}){2,})\b",
    re.IGNORECASE,
)
_MONTH_YEAR = re.compile(
    r"\b(?:ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|"
    r"SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)\s+\d{4}\b",
    re.IGNORECASE,
)


def _controlled_record_suggestion(
    text: str,
    *,
    primary_kind: str,
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion:
    """Name controlled forms from type, control code and revision date."""

    bounded = text[:4_000]
    pieces = [_KIND_LABELS[primary_kind]]
    evidence = ["classification:document_kind"]
    if match := _CONTROLLED_DOCUMENT_CODE.search(bounded):
        pieces.append(match.group(0).upper())
        evidence.append("text:controlled_document_code")
    if match := _MONTH_YEAR.search(bounded):
        pieces.append(match.group(0).title())
        evidence.append("text:revision_month")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    if topic and not _contains_token(pieces, topic):
        pieces.append(topic.replace("_", " "))
        evidence.append("classification:topic")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _correspondence_suggestion(
    text: str,
    *,
    organization: str | None,
) -> NamingSuggestion | None:
    """Prefer an exported message subject over sender and mailbox boilerplate."""

    collapsed = re.sub(r"\s+", " ", text[:4_000]).strip()
    match = re.search(
        r"(?i)\bASUNTO\s*:\s*(.{3,120}?)(?=\s+(?:DATOS\s+ADJUNTOS|"
        r"IMPORTANCIA|DE\s*:|ENVIADO\s+EL\s*:|PARA\s*:|CC\s*:)|$)",
        collapsed,
    )
    if match is None:
        return None
    subject = _clean_token(match.group(1))
    if subject is None:
        return None
    pieces = [_KIND_LABELS["correspondencia"], subject]
    evidence = ["classification:document_kind", "text:email_subject"]
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _travel_receipt_suggestion(
    text: str,
    *,
    organization: str | None,
) -> NamingSuggestion:
    """Name a travel receipt from its service date and total when available."""

    bounded = re.sub(r"\s+", " ", text[:4_000]).strip()
    pieces = [_KIND_LABELS["comprobante_viaje"]]
    evidence = ["classification:document_kind"]
    date = re.search(
        r"(?i)\b(\d{1,2})\s+de\s+"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
        r"octubre|noviembre|diciembre)\s+de\s+(\d{4})\b",
        bounded,
    )
    if date is not None:
        month = (
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ).index(date.group(2).casefold()) + 1
        pieces.append(f"{int(date.group(3)):04d}-{month:02d}-{int(date.group(1)):02d}")
        evidence.append("text:service_date")
    total = re.search(
        r"(?i)\bTOTAL\s+([0-9][0-9., ]{0,18})\s*(MXN|USD)\b",
        bounded,
    )
    if total is not None:
        pieces.append(f"{total.group(1).strip()} {total.group(2).upper()}")
        evidence.append("text:receipt_total")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


# endregion [03]


# region [04] Calibration-certificate identity extraction

_CALIBRATION_TERMINATORS = (
    "DESCRIPTION",
    "MARCA",
    "BRAND",
    "MODELO",
    "MODEL",
    "SERIE",
    "SERIAL",
    "FECHA DE CALIBRACIÓN",
    "FECHA DE CALIBRACION",
    "CALIBRATION DATE",
    "FECHA DE EMISIÓN",
    "FECHA DE EMISION",
    "EMISSION DATE",
    "FECHA DE RECEPCIÓN",
    "FECHA DE RECEPCION",
    "RECEPTION DATE",
    "TEMPERATURA AMBIENTE",
    "ENVIRONMENT TEMPERATURE",
    "FECHA",
    "DATE",
    "ID EQUIPO",
    "EQUIPMENT ID",
    "CONDICIONES",
    "CALIBRATION CONDITIONS",
)


def _calibration_suggestion(
    text: str,
    *,
    organization: str | None,
) -> NamingSuggestion | None:
    description = _field_value(text, ("DESCRIPCIÓN", "DESCRIPCION"), _CALIBRATION_TERMINATORS)
    if description is None:
        description = _field_value(text, ("DESCRIPTION",), _CALIBRATION_TERMINATORS)
    description = _canonical_instrument_description(description)
    brand = _field_value(text, ("MARCA", "BRAND"), _CALIBRATION_TERMINATORS)
    brand = _canonical_brand(brand)
    model = _field_value(text, ("MODELO", "MODEL"), _CALIBRATION_TERMINATORS)
    serial = _field_value(
        text,
        ("NO. DE SERIE", "NÚMERO DE SERIE", "NUMERO DE SERIE", "SERIE", "SERIAL"),
        _CALIBRATION_TERMINATORS,
    )
    model = _equipment_identifier(model)
    serial = _equipment_identifier(serial)
    report_id = _certificate_identifier(text)
    calibration_date = _field_value(
        text,
        ("FECHA DE CALIBRACIÓN", "FECHA DE CALIBRACION", "CALIBRATION DATE"),
        (
            "FECHA",
            "DATE",
            "MODELO",
            "MODEL",
            "SERIE",
            "SERIAL",
            "VIGENCIA",
            "DATOS DEL PATRÓN DE REFERENCIA",
            "DATOS DEL PATRON DE REFERENCIA",
            "REFERENCE PATTERN DATA",
            "DATOS DE CALIBRACIÓN",
            "DATOS DE CALIBRACION",
            "DATOS",
            "DATA",
        ),
    )
    extracted = tuple(
        value for value in (description, brand, model, serial, report_id, calibration_date) if value
    )
    if not extracted:
        return None
    pieces = ["Certificado de calibracion", *extracted]
    evidence = tuple(
        label
        for label, value in (
            ("text:instrument_description", description),
            ("text:brand", brand),
            ("text:model", model),
            ("text:serial", serial),
            ("text:certificate_identifier", report_id),
            ("text:calibration_date", calibration_date),
        )
        if value
    )
    return NamingSuggestion(_join_stem(pieces), evidence)


def _field_value(
    text: str,
    labels: tuple[str, ...],
    terminators: tuple[str, ...],
) -> str | None:
    label_expression = "|".join(re.escape(label) for label in labels)
    terminator_expression = "|".join(re.escape(label) for label in terminators)
    match = re.search(
        rf"(?is)(?<!\w)(?:{label_expression})\s*[:#-]\s*"
        rf"(.{{2,100}}?)(?=\s+(?:{terminator_expression})(?:\s*[:#-]|\s)|\r?\n|$)",
        text,
    )
    return None if match is None else _clean_field_token(match.group(1))


def _certificate_identifier(text: str) -> str | None:
    labelled = re.search(
        r"(?i)\b(?:INFORME|CERTIFICATE)\s*(?:NO\.?|N[ÚU]MERO|NUMBER)?\s*[:#]"
        r"\s*([A-Z0-9][A-Z0-9./-]{4,40})\b",
        text,
    )
    if labelled is not None:
        candidate = _clean_field_token(labelled.group(1))
        if candidate is not None:
            return candidate
    laboratory = re.search(
        r"(?i)\b(?:CLAM|SEPRI|LAB)[-_][A-Z0-9]*\d[A-Z0-9-]{3,30}\b",
        text,
    )
    return None if laboratory is None else _clean_field_token(laboratory.group(0))


_INSTRUMENT_DESCRIPTIONS = (
    (
        re.compile(r"\bamperimetro\b.{0,24}\b(?:gancho|pinza)\b"),
        "Amperimetro de gancho",
    ),
    (
        re.compile(r"\b(?:respuesta a la frecuencia|frequency response)\b"),
        "Analizador de respuesta en frecuencia",
    ),
    (
        re.compile(r"\b(?:factor de potencia|power factor)\b.{0,40}\btangente\b"),
        "Medidor de factor de potencia y tangente delta",
    ),
    (
        re.compile(r"\b(?:equipo multifuncional|multifunction test)\b"),
        "Equipo multifuncional de pruebas",
    ),
    (
        re.compile(r"\b(?:probador de tc|transformadores? de corriente)\b"),
        "Probador de transformadores de corriente",
    ),
    (
        re.compile(r"\b(?:ducter|medidor de baja resistencia)\b"),
        "Medidor de baja resistencia (ducter)",
    ),
    (re.compile(r"\bmicrohmetro\b"), "Microhmetro"),
    (
        re.compile(r"\b(?:probador|medidor) de rigidez dielectrica\b"),
        "Medidor de rigidez dielectrica",
    ),
    (re.compile(r"\bmultimetro\b"), "Multimetro"),
    (
        re.compile(r"(?:\bttr\w*|\brelacion de transformacion\b)"),
        "Medidor de relacion de transformacion TTR",
    ),
)


def _canonical_instrument_description(value: str | None) -> str | None:
    if value is None:
        return None
    key = _search_key(value)
    if "resistencia de aislamiento" in key:
        voltage = re.search(r"\b(5|10)\s*k\s*v\b", key)
        suffix = "" if voltage is None else f" de {voltage.group(1)} kV"
        return f"Medidor de resistencia de aislamiento{suffix}"
    for pattern, canonical in _INSTRUMENT_DESCRIPTIONS:
        if pattern.search(key):
            return canonical
    candidate = re.split(
        r"(?i)\b(?:instrument data|fecha de calibraci[oó]n|calibration date)\b",
        value,
        maxsplit=1,
    )[0]
    return _clean_field_token(candidate)


_KNOWN_BRANDS = (
    "AEMC",
    "DOBLE",
    "FLUKE",
    "HIOKI",
    "HIPOTRONICS",
    "MEGGER",
    "METREL",
    "OMICRON",
    "WAVETEK",
)


def _canonical_brand(value: str | None) -> str | None:
    if value is None:
        return None
    key = _search_key(value)
    for brand in _KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(brand.casefold())}\b", key):
            return brand
    return _clean_field_token(value)


# endregion [04]


# region [05] Structured reports

_REPORT_FIELD_TERMINATORS = (
    "CONTRATO",
    "OBRA",
    "CLIENTE",
    "FECHA DEL INFORME",
    "REALIZADO POR",
    "TIPO DE INFORME",
    "INFORME NO",
    "FOTO NO",
    "FOTOGRAFÍA NO",
    "FOTOGRAFIA NO",
    "CALLE",
    "PÁGINA",
    "PAGINA",
    "ÍNDICE",
    "INDICE",
)


def _structured_report_suggestion(
    text: str,
    *,
    primary_kind: str,
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    report_type = _front_matter_field(text, ("TIPO DE INFORME",))
    if report_type is None:
        return None
    project = _front_matter_field(text, ("PROYECTO",))
    report_number = _front_matter_field(text, ("INFORME NO.", "INFORME NO"))
    date = _front_matter_field(text, ("FECHA DEL INFORME",))
    pieces = [_KIND_LABELS[primary_kind]]
    evidence = ["text:report_type"]
    if project:
        pieces.append(project)
        evidence.append("text:project")
    if report_number:
        pieces.append(f"Informe {report_number}")
        evidence.append("text:report_number")
    if normalized_date := _numeric_date(date):
        pieces.append(normalized_date)
        evidence.append("text:report_date")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    if topic and not _contains_token(pieces, topic):
        pieces.append(topic.replace("_", " "))
        evidence.append("classification:topic")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _laboratory_report_suggestion(
    text: str,
    *,
    organization: str | None,
) -> NamingSuggestion | None:
    """Name a laboratory result from its sample, asset, site and analysis date."""

    bounded = re.sub(r"\s+", " ", text[:4_000]).strip()
    if (
        re.search(
            r"(?i)\b(?:INFORME\s+DE\s+(?:ENSAYOS?|PRUEBAS?)|"
            r"LABORATORY\s+(?:TEST\s+)?REPORT)\b",
            bounded,
        )
        is None
    ):
        return None
    pieces = [_KIND_LABELS["reporte_laboratorio"]]
    evidence = ["classification:document_kind"]
    analysis = re.search(
        r"(?i)\bNO\.?\s+DE\s+AN[AÁ]LISIS\s*:\s*([A-Z0-9][A-Z0-9/.-]{2,30})",
        bounded,
    )
    if analysis is not None:
        pieces.append(re.sub(r"[/\\]+", "-", analysis.group(1)).upper())
        evidence.append("text:analysis_number")
    equipment = re.search(
        r"(?i)\bEQUIPO\s*:\s*(.{2,50}?)(?=\s+(?:BANCO|MARCA|"
        r"NO\.?\s+DE\s+SERIE|FECHA)\s*:)",
        bounded,
    )
    bank = re.search(
        r"(?i)\bBANCO\s*:\s*(.{2,50}?)(?=\s+(?:MARCA|"
        r"NO\.?\s+DE\s+SERIE|FECHA)\s*:)",
        bounded,
    )
    asset_parts = tuple(
        value
        for match in (equipment, bank)
        if match is not None and (value := _clean_field_token(match.group(1)))
    )
    if asset_parts:
        pieces.append(" ".join(asset_parts))
        evidence.append("text:equipment")
    site = re.search(
        r"(?i)\bSITIO\s*:\s*(.{2,60}?)(?=\s+(?:EQUIPO|BANCO|MARCA|"
        r"NO\.?\s+DE\s+SERIE|FECHA)\s*:)",
        bounded,
    )
    if site is not None and (site_name := _clean_field_token(site.group(1))):
        pieces.append(site_name)
        evidence.append("text:site")
    analysis_date = re.search(
        r"(?i)\bFECHA\s+DE\s+AN[AÁ]LISIS\s*:\s*"
        r"(\d{1,2}[ ./-]+\d{1,2}[ ./-]+\d{4})",
        bounded,
    )
    if analysis_date is not None and (date := _numeric_date(analysis_date.group(1))):
        pieces.append(date)
        evidence.append("text:analysis_date")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    if len(pieces) == 1:
        return None
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _daily_field_report_suggestion(
    text: str,
    *,
    organization: str | None,
    topic: str | None,
) -> NamingSuggestion | None:
    """Name a controlled daily field report from code, project and date."""

    bounded = re.sub(r"\s+", " ", text[:4_000]).strip()
    if re.search(r"(?i)\bREPORTE\s+DIARIO\s+DE\s+CAMPO\b", bounded) is None:
        return None
    pieces = [_KIND_LABELS["reporte_actividades"]]
    evidence = ["classification:document_kind"]
    if code := _CONTROLLED_DOCUMENT_CODE.search(bounded):
        pieces.append(code.group(0).upper())
        evidence.append("text:controlled_document_code")
    project = re.search(
        r"(?i)\bPROYECTO\s*:?[ ]*(.{2,60}?)(?=\s+(?:CONTRATO|ID|PARTIDA|CLIENTE)\b)",
        bounded,
    )
    if project is not None and (project_name := _clean_field_token(project.group(1))):
        pieces.append(project_name)
        evidence.append("text:project")
    date = re.search(r"\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b", bounded)
    if date is not None:
        day, month, year = (int(value) for value in date.groups())
        if year < 100:
            year += 2_000
        if 1 <= day <= 31 and 1 <= month <= 12:
            pieces.append(f"{year:04d}-{month:02d}-{day:02d}")
            evidence.append("text:report_date")
    if organization and not _contains_token(pieces, organization):
        pieces.append(organization)
        evidence.append("classification:organization")
    if topic and not _contains_token(pieces, topic):
        pieces.append(topic.replace("_", " "))
        evidence.append("classification:topic")
    return NamingSuggestion(_join_stem(pieces), tuple(evidence))


def _front_matter_field(text: str, labels: tuple[str, ...]) -> str | None:
    collapsed = re.sub(r"\s+", " ", text[:4_000]).strip()
    label_expression = "|".join(re.escape(label) for label in labels)
    terminators = "|".join(re.escape(value) for value in _REPORT_FIELD_TERMINATORS)
    match = re.search(
        rf"(?i)\b(?:{label_expression})\s*:?\s*"
        rf"(.{{1,140}}?)(?=\s+(?:{terminators})\b|$)",
        collapsed,
    )
    return None if match is None else _clean_field_token(match.group(1))


def _numeric_date(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.search(r"\b(\d{1,2})[ ./-]+(\d{1,2})[ ./-]+(\d{4})\b", value)
    if match is None:
        return _clean_field_token(value)
    day, month, year = (int(part) for part in match.groups())
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# endregion [05]


# region [06] Generic headings and Windows-safe normalization

_GENERIC_TITLES = re.compile(
    r"(?i)^(?:documento|document|microsoft\s+word|hoja\d*|sheet\d*|"
    r"presentaci[oó]n\d*|presentation\d*|sin\s+t[ií]tulo|untitled|x[_-]?[0-9a-f]*)$"
)
_HEADING_REJECT = re.compile(
    r"(?i)^(?:p[aá]gina|page|fecha|date|c[oó]digo|code|revisi[oó]n|revision|"
    r"elabor[oó]|revis[oó]|aprob[oó]|formato)\s*[:#-]"
)
_PAGE_COUNTER = re.compile(r"(?i)^(?:p[aá]gina|page)\s+\d+\s+(?:de|of)\s+\d+$")


def _meaningful_title(title: str, original_stem: str) -> str | None:
    candidate = _clean_token(title)
    if candidate is None or _GENERIC_TITLES.fullmatch(candidate):
        return None
    if _comparison_key(candidate) == _comparison_key(original_stem):
        return None
    return candidate


def _meaningful_original_stem(original_stem: str) -> str | None:
    candidate = _clean_token(original_stem)
    if candidate is None or _RECOVERED_OR_HASH_NAME.match(candidate):
        return None
    candidate = _GENERATED_SUFFIX.sub("", candidate).strip(" ._-")
    candidate = re.sub(r"(?i)^[0-9a-f]{10,}\s*[-—]\s*", "", candidate)
    if not candidate or _GENERIC_TITLES.fullmatch(candidate):
        return None
    return candidate[:120].rstrip(" .")


def _leading_heading(text: str) -> str | None:
    for raw_line in text.splitlines()[:24]:
        candidate = _clean_token(raw_line)
        if candidate is None or len(candidate) < 8 or len(candidate) > 150:
            continue
        if (
            _HEADING_REJECT.match(candidate)
            or _PAGE_COUNTER.fullmatch(candidate)
            or _GENERIC_TITLES.fullmatch(candidate)
        ):
            continue
        if len(candidate.split()) < 2:
            continue
        if len(candidate.split()) > 18 or re.match(
            r"(?i)^(?:la\s+presente|este\s+documento|esta\s+(?:norma|encuesta))\b",
            candidate,
        ):
            continue
        return candidate
    return None


def _clean_token(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value).replace("�", " ")
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._-—")
    if not normalized or len(normalized) < 2:
        return None
    return normalized[:120].rstrip(" .")


def _clean_standard_identifier(value: str) -> str | None:
    """Preserve semantic edition separators until final filename sanitization."""

    normalized = unicodedata.normalize("NFKC", value).replace("�", " ")
    normalized = re.sub(r'[<>"\\|?*\x00-\x1f]', " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._-—")
    return normalized[:120].rstrip(" .") or None


_FIELD_PLACEHOLDER = re.compile(
    r"(?i)^(?:description|descripcion|brand|marca|model|modelo|serial|serie|"
    r"certificate|informe|calibration|calibrationdate|fechadecalibracion)$"
)
_FIELD_TRAILING_LABEL = re.compile(
    r"(?i)\b(?:fecha\s+de\s+(?:calibraci[oó]n|emisi[oó]n|recepci[oó]n)|"
    r"calibration\s+date|emission\s+date|reception\s+date|temperatura\s+ambiente|"
    r"environment\s+temperature|description|marca|brand|modelo|model|serie|serial)\b"
)


def _clean_field_token(value: str) -> str | None:
    candidate = _clean_token(value)
    if candidate is None:
        return None
    candidate = _FIELD_TRAILING_LABEL.split(candidate, maxsplit=1)[0].strip(" ._-")
    if not candidate or _FIELD_PLACEHOLDER.fullmatch(_comparison_key(candidate)):
        return None
    return candidate[:64].rstrip(" .")


def _equipment_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    known_ocr_repairs = {
        "mit1025": "MIT1025",
        "mt1025": "MIT1025",
    }
    if repaired := known_ocr_repairs.get(_comparison_key(value)):
        return repaired
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,39}", value)
    return value if match is None else match.group(0)


def _join_stem(values: Iterable[str]) -> str:
    cleaned = tuple(value for raw in values if (value := _clean_token(raw)))
    stem = " - ".join(dict.fromkeys(cleaned)) or "Documento tecnico"
    return stem[:MAX_SUGGESTED_STEM_CHARS].rstrip(" ._-")


def _comparison_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", folded.casefold())


def _search_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


def _contains_token(values: Iterable[str], candidate: str) -> bool:
    key = _comparison_key(candidate)
    return any(key and key in _comparison_key(value) for value in values)


# endregion [06]
