"""Preserved v14 behavioral payload plus current additive role evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import pytest
from neocortex.foundation.hash_compat import sha256

import neocortex.documents.document_taxonomy as taxonomy_module
from neocortex.documents.document_taxonomy import (
    CLASSIFIER_VERSION,
    DocumentSignals,
    ScoredLabel,
    classify_document,
)

TEST_CAPABILITIES = ("documents",)


@dataclass(frozen=True, slots=True)
class _TaxonomyCase:
    case_id: str
    signals: DocumentSignals
    expected_primary: str
    expected_confidence: float
    expected_uncertainty: str
    expected_fingerprint: str


_EXPECTED: dict[str, tuple[str, float, str, str]] = {
    "normative_ieee_formal": (
        "normativa",
        0.96,
        "baja",
        "91c163837a525bd75042732cb7c7dff5",
    ),
    "normative_iec_formal": (
        "normativa",
        0.96,
        "baja",
        "728341d2afcb8871a392b55c9c534ceb",
    ),
    "normative_nom_formal": (
        "normativa",
        0.96,
        "baja",
        "2a6b0e297366603c05e8a533aac15db7",
    ),
    "normative_nmx_formal": (
        "normativa",
        0.96,
        "baja",
        "75739e5adcc2a7d7358b56633f1e1c29",
    ),
    "normative_astm_formal": (
        "normativa",
        0.96,
        "baja",
        "5157440176e0127d3017a659854ccf46",
    ),
    "normative_cfe_specification": (
        "normativa",
        0.96,
        "baja",
        "e6972b7bcfd8faaac5292d346da52c4e",
    ),
    "normative_neta_formal": (
        "normativa",
        0.96,
        "baja",
        "59d51e5aa2b8c03c7cf06c47c5e9667c",
    ),
    "normative_iso_formal": (
        "normativa",
        0.96,
        "baja",
        "73b947685910de32751225a4226b7a3a",
    ),
    "reference_som_procedure": (
        "procedimiento",
        0.81,
        "media",
        "7f6069dfb7a175dbafb3733e13b55c5a",
    ),
    "reference_inspection_report": (
        "informe_inspeccion",
        0.76,
        "media",
        "78a34e68e3f32535e29669148ce9a0c1",
    ),
    "reference_invoice": (
        "normativa",
        0.96,
        "baja",
        "57b20055744b0387e904ce4ba4081f4b",
    ),
    "reference_calibration_certificate": (
        "otro",
        0.35,
        "alta",
        "930e4659339fc09448a40fd48cb4af95",
    ),
    "reference_equipment_manual": (
        "manual_equipo",
        0.76,
        "media",
        "9b5c61e8698163e2217b5fdef354d416",
    ),
    "reference_meeting_minutes": (
        "minuta_acta",
        0.69,
        "media",
        "ee9349a3ab353112d80901b09f98a055",
    ),
    "reference_laboratory_report": (
        "reporte_resultados_pruebas",
        0.76,
        "media",
        "20e9682b3c100ebb4e50bacad22c8597",
    ),
    "reference_measurement_register": (
        "protocolo_pruebas",
        0.76,
        "media",
        "db02d25bc72fc15044575e94ea5bdafd",
    ),
    "reference_technical_offer": (
        "otro",
        0.35,
        "alta",
        "e5f3035286c3b76ad42c96cbe18addde",
    ),
    "reference_nonconformance": (
        "reporte_no_conformidad",
        0.98,
        "baja",
        "ed35670ede2317b235881a207aefa3b5",
    ),
    "boundary_empty": (
        "otro",
        0.35,
        "alta",
        "126fb14bc24f9c4e45631ebabc426f5e",
    ),
    "boundary_partial_generic": (
        "otro",
        0.27,
        "alta",
        "e293ad2f7d1719f82574d10f35d0fcce",
    ),
    "boundary_managed_normative_without_evidence": (
        "otro",
        0.35,
        "alta",
        "6d92a8393fc3e5cc491d7f2790727eea",
    ),
    "boundary_normative_words_without_identifier": (
        "informe_analisis",
        0.97,
        "baja",
        "6c0046e4f3d25110bc68ebf8ad21d1da",
    ),
    "boundary_technical_topic_only": (
        "referencia_tecnica",
        0.74,
        "media",
        "4a89d6eb2bff460dbd76f49b287a3de7",
    ),
    "boundary_audio_year_token": (
        "audio_transcrito",
        0.62,
        "alta",
        "9e14a8f8c7b5eeabf568df650ff0bc1b",
    ),
    "cross_andritz_form": (
        "lista_verificacion",
        0.95,
        "baja",
        "54611049d1afafd9cf159b04b7d3810c",
    ),
    "cross_omicron_manual": (
        "manual_equipo",
        0.8,
        "media",
        "f38b739a5eb8c2ba5468d1177ad9de1d",
    ),
    "cross_packing_list": (
        "lista_empaque_embarque",
        0.98,
        "baja",
        "738bd61325a1008a68f349609448d316",
    ),
    "cross_audio_meeting": (
        "reunion_grabada",
        0.97,
        "baja",
        "60fd10a3808ace3fc6624e68c18f6958",
    ),
    "cross_field_service_report": (
        "informe_tecnico",
        0.77,
        "media",
        "f441f464fa027c5d15c17136acb585fb",
    ),
    "cross_technical_email": (
        "otro",
        0.35,
        "alta",
        "f117ab907f798afcc848ca61213dc1c9",
    ),
}


def _case(
    case_id: str,
    path: str,
    *,
    source_kind: str = "pdf",
    source_status: str = "done",
    title: str = "",
    author: str = "",
    metadata: str = "",
    text: str = "",
    page_count: int | None = None,
) -> _TaxonomyCase:
    expected_primary, expected_confidence, expected_uncertainty, fingerprint = (
        _EXPECTED[case_id]
    )
    return _TaxonomyCase(
        case_id,
        DocumentSignals(
            source_kind,
            path,
            source_status,
            title=title,
            author=author,
            metadata=metadata,
            leading_text=text,
            page_count=page_count,
        ),
        expected_primary,
        expected_confidence,
        expected_uncertainty,
        fingerprint,
    )


CASES = (
    _case(
        "normative_ieee_formal",
        r"C:\Corpus\IEEE Std C37.20.2-2015.pdf",
        title="IEEE Std C37.20.2-2015 Metal-Clad Switchgear",
        text="IEEE STANDARD FOR metal-clad switchgear and circuit breakers.",
    ),
    _case(
        "normative_iec_formal",
        r"C:\Corpus\IEC 62271-200.pdf",
        title="IEC 62271-200 High-voltage switchgear",
        text="INTERNATIONAL STANDARD IEC 62271-200. High-voltage switchgear.",
    ),
    _case(
        "normative_nom_formal",
        r"C:\Corpus\NOM-001-SEDE-2018.pdf",
        title="NOM-001-SEDE-2018 Instalaciones eléctricas",
        text="NORMA OFICIAL MEXICANA NOM-001-SEDE-2018 instalaciones eléctricas.",
    ),
    _case(
        "normative_nmx_formal",
        r"C:\Corpus\NMX-J-549-ANCE-2005.pdf",
        title="NMX-J-549-ANCE-2005 Sistema de protección contra tormentas",
        text="NORMA MEXICANA NMX-J-549-ANCE-2005. Declaratoria de vigencia.",
    ),
    _case(
        "normative_astm_formal",
        r"C:\Corpus\ASTM D877-20.pdf",
        title="ASTM D877-20 Dielectric Breakdown Voltage",
        text="STANDARD TEST METHOD FOR dielectric breakdown voltage ASTM D877-20.",
    ),
    _case(
        "normative_cfe_specification",
        r"C:\Corpus\CFE L0000-15.pdf",
        title="Especificación CFE L0000-15",
        text="ESPECIFICACION CFE L0000-15 para equipos de subestaciones eléctricas.",
    ),
    _case(
        "normative_neta_formal",
        r"C:\Corpus\ANSI NETA ATS-2021.pdf",
        title="ANSI NETA ATS-2021",
        text="ANSI/NETA STANDARD ATS-2021 acceptance testing specifications.",
    ),
    _case(
        "normative_iso_formal",
        r"C:\Corpus\ISO 9001 2015.pdf",
        title="ISO 9001:2015 Quality management systems",
        text="INTERNATIONAL STANDARD ISO 9001:2015. Requirements.",
    ),
    _case(
        "reference_som_procedure",
        r"C:\Corpus\Procedimiento SOM-3531.pdf",
        title="Manual de procedimientos SOM-3531",
        text="OBJETIVO ALCANCE RESPONSABILIDADES ACCIONES. Documentos de referencia.",
    ),
    _case(
        "reference_inspection_report",
        r"C:\Corpus\Informe inspeccion interruptor.pdf",
        title="Informe de inspección de interruptor",
        text="Resultados de inspección. Referencia IEEE Std C37.09-2018.",
    ),
    _case(
        "reference_invoice",
        r"C:\Corpus\Factura 1842.pdf",
        title="Factura electrónica 1842",
        text="CFDI subtotal IVA total. Servicio realizado conforme a ANSI NETA ATS-2021.",
    ),
    _case(
        "reference_calibration_certificate",
        r"C:\Corpus\Certificado calibracion.pdf",
        title="Certificado de calibración",
        text="Laboratorio acreditado. Trazabilidad conforme a ISO 17025. Resultado e incertidumbre.",
    ),
    _case(
        "reference_equipment_manual",
        r"C:\Corpus\Manual relevador.pdf",
        title="Manual de usuario del relevador de protección",
        text="Operating instructions. Device complies with IEC 60255-1.",
    ),
    _case(
        "reference_meeting_minutes",
        r"C:\Corpus\Minuta tecnica.docx",
        source_kind="docx",
        title="Minuta de reunión técnica",
        text="Orden del día, asistentes y acuerdos. Revisar cumplimiento NOM-001-SEDE-2018.",
    ),
    _case(
        "reference_laboratory_report",
        r"C:\Corpus\Reporte laboratorio aceite.pdf",
        title="Reporte de resultados de laboratorio",
        text="Resultados de rigidez dieléctrica obtenidos mediante ASTM D877-20.",
    ),
    _case(
        "reference_measurement_register",
        r"C:\Corpus\Registro mediciones.xlsx",
        source_kind="xlsx",
        title="Registro de mediciones eléctricas",
        text="Tabla de corriente, voltaje y resistencia. Referencia NOM-001-SEDE-2018.",
    ),
    _case(
        "reference_technical_offer",
        r"C:\Corpus\Oferta tecnica.pdf",
        title="Oferta técnica y económica",
        text="Alcance, precio y plazo. Equipos propuestos conforme a IEEE C37.20.2.",
    ),
    _case(
        "reference_nonconformance",
        r"C:\Corpus\RNC-014.pdf",
        title="Reporte de no conformidad RNC-014",
        text="Hallazgo, causa raíz y acción correctiva según ISO 9001:2015.",
    ),
    _case("boundary_empty", r"C:\Corpus\sin_datos.pdf"),
    _case(
        "boundary_partial_generic",
        r"C:\Corpus\extracto parcial.pdf",
        source_status="partial",
        text="Documento técnico sobre equipo eléctrico.",
    ),
    _case(
        "boundary_managed_normative_without_evidence",
        r"C:\Users\Victor\Documents\Normativa\archivo.pdf",
        title="Archivo pendiente de identificar",
    ),
    _case(
        "boundary_normative_words_without_identifier",
        r"C:\Corpus\comentario.pdf",
        title="Comentarios sobre norma mexicana",
        text="La norma mexicana se revisará durante la siguiente reunión.",
    ),
    _case(
        "boundary_technical_topic_only",
        r"C:\Corpus\notas interruptor.pdf",
        text="Interruptor de potencia, transformador de corriente y subestación.",
    ),
    _case(
        "boundary_audio_year_token",
        r"C:\Corpus\audio EN 2018.mp3",
        source_kind="audio",
        title="Reunión EN 2018",
        text="Grabación de seguimiento del proyecto y acuerdos de campo.",
    ),
    _case(
        "cross_andritz_form",
        r"C:\Corpus\Formato inspeccion ANDRITZ.docx",
        source_kind="docx",
        title="Formato de inspección ANDRITZ",
        author="ANDRITZ HYDRO",
        text="Lista de verificación de pruebas eléctricas.",
    ),
    _case(
        "cross_omicron_manual",
        r"C:\Corpus\OMICRON CMC 356.pdf",
        title="CMC 356 User Manual",
        author="OMICRON electronics",
        text="Test equipment for protection and control.",
    ),
    _case(
        "cross_packing_list",
        r"C:\Corpus\Packing List PL-0042.pdf",
        text="PACKING LIST Package No BX-14 Gross Weight 118 kg Net Weight 104 kg Dimensions 90 x 60 x 40 cm",
    ),
    _case(
        "cross_audio_meeting",
        r"C:\Corpus\Reunion seguimiento.mp3",
        source_kind="audio",
        title="Reunión de seguimiento",
        text="Grabación de la reunión de trabajo. Orden del día y acuerdos.",
    ),
    _case(
        "cross_field_service_report",
        r"C:\Corpus\Reporte servicio interruptor.pdf",
        title="Reporte de servicio en campo",
        text="Mantenimiento preventivo, pruebas eléctricas y resultados del interruptor.",
    ),
    _case(
        "cross_technical_email",
        r"C:\Corpus\Correo seguimiento.eml",
        source_kind="email",
        title="Seguimiento pruebas de protección",
        author="ingenieria@example.com",
        text="Buen día. Adjunto resultados y solicito confirmar la próxima intervención.",
    ),
)


def _payload_fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256.sha256_128_hexdigest(payload)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_classifier_v14_representative_payload_is_characterized(
    case: _TaxonomyCase,
) -> None:
    classification = classify_document(case.signals)

    assert CLASSIFIER_VERSION == "technical-document-classifier-v17"
    assert classification.primary_kind == case.expected_primary
    assert classification.confidence == case.expected_confidence
    assert classification.uncertainty == case.expected_uncertainty
    payload = asdict(classification)
    # Keep the 30 original fingerprints instead of silently rebaselining their
    # behavior when later versions add independent explanatory dimensions.
    for field in (
        "document_role", "role_evidence", "entity_roles", "issuer_status",
        "taxonomy_status", "contradictions", "unknowns", "confidence_kind",
    ):
        payload.pop(field)
    payload["classifier_signature"] = payload["classifier_signature"].replace(
        "classifier-v17", "classifier-v14"
    )
    assert _payload_fingerprint(payload) == case.expected_fingerprint
    assert classification.confidence_kind == "uncalibrated_heuristic"


def test_characterization_matrix_is_bounded_and_covers_uncertainty() -> None:
    assert len(CASES) == 30
    assert {case.expected_uncertainty for case in CASES} == {"baja", "media", "alta"}
    assert len({case.case_id for case in CASES}) == len(CASES)


@pytest.mark.parametrize(
    ("kinds", "expected_primary"),
    (
        (
            (
                ScoredLabel("informe_tecnico", 0.75, ("fixture:primary",)),
                ScoredLabel("manual_equipo", 0.66, ("fixture:secondary",)),
            ),
            "informe_tecnico",
        ),
        (
            (
                ScoredLabel("normativa", 0.76, ("fixture:normative",)),
                ScoredLabel("informe_tecnico", 0.65, ("fixture:secondary",)),
            ),
            "normativa",
        ),
    ),
)
def test_ambiguity_boundaries_abstain_with_high_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    kinds: tuple[ScoredLabel, ...],
    expected_primary: str,
) -> None:
    monkeypatch.setattr(
        taxonomy_module, "_kind_evidence", lambda *_args, **_kwargs: kinds
    )

    result = classify_document(
        DocumentSignals("pdf", r"C:\Corpus\ambiguous.pdf", "done")
    )

    assert result.primary_kind == expected_primary
    assert result.confidence == 0.67
    assert result.uncertainty == "alta"
