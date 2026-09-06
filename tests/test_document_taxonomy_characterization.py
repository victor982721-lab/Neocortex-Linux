"""Preserved v14 behavioral payload plus current additive role evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import pytest
import xxhash

import neocortex.documents.document_taxonomy as taxonomy_module
from neocortex.documents.document_taxonomy import (
    CLASSIFIER_VERSION,
    DocumentSignals,
    ScoredLabel,
    classify_document,
)


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
        "2c70d654629fdc4c02d04b254a059609",
    ),
    "normative_iec_formal": (
        "normativa",
        0.96,
        "baja",
        "18dbe95042b740a4f25eed0015f277a3",
    ),
    "normative_nom_formal": (
        "normativa",
        0.96,
        "baja",
        "f111e8b38f00b63acbe5483724c2728c",
    ),
    "normative_nmx_formal": (
        "normativa",
        0.96,
        "baja",
        "0e676cc9eee0c91516907d61e0ae037e",
    ),
    "normative_astm_formal": (
        "normativa",
        0.96,
        "baja",
        "8b0ff3fec1476ff1d5a25e9f5dca7348",
    ),
    "normative_cfe_specification": (
        "normativa",
        0.96,
        "baja",
        "2c4e0032d9c645156ea92abb55f7f47e",
    ),
    "normative_neta_formal": (
        "normativa",
        0.96,
        "baja",
        "10b35feaf8f8a3aec7210ace4c5bfc0d",
    ),
    "normative_iso_formal": (
        "normativa",
        0.96,
        "baja",
        "260e5a1597e20b35c629697c8448d1a6",
    ),
    "reference_som_procedure": (
        "procedimiento",
        0.81,
        "media",
        "9c25cc595cd17acf0bca7d14e39171d7",
    ),
    "reference_inspection_report": (
        "informe_inspeccion",
        0.76,
        "media",
        "0e9e18d26043c7a09e303e5c9ccae304",
    ),
    "reference_invoice": (
        "normativa",
        0.96,
        "baja",
        "19194d9cc0a19b6b8dbf63062cfd6c69",
    ),
    "reference_calibration_certificate": (
        "otro",
        0.35,
        "alta",
        "ef73fa646d298ea5a4ac912ddf4348b3",
    ),
    "reference_equipment_manual": (
        "manual_equipo",
        0.76,
        "media",
        "ab819e6fe3840a2b6adbd78fd11c4f87",
    ),
    "reference_meeting_minutes": (
        "minuta_acta",
        0.69,
        "media",
        "d6998d5759cd09d0e0d76a38ce39b7d0",
    ),
    "reference_laboratory_report": (
        "reporte_resultados_pruebas",
        0.76,
        "media",
        "6896c4c53e61ba752cc5d892fc22aa4b",
    ),
    "reference_measurement_register": (
        "protocolo_pruebas",
        0.76,
        "media",
        "ad7fecc333f4870cac0b469564085838",
    ),
    "reference_technical_offer": (
        "otro",
        0.35,
        "alta",
        "2440d61f922c02981fa253c0e7ca493d",
    ),
    "reference_nonconformance": (
        "reporte_no_conformidad",
        0.98,
        "baja",
        "9afafd1326ba2a22771b3ce0d1d1d155",
    ),
    "boundary_empty": (
        "otro",
        0.35,
        "alta",
        "2055ee2a4c801d99dd65740dbb24f1be",
    ),
    "boundary_partial_generic": (
        "otro",
        0.27,
        "alta",
        "04eab9b734cd55a0624cf7f6a6c62013",
    ),
    "boundary_managed_normative_without_evidence": (
        "otro",
        0.35,
        "alta",
        "351b10a5c65af1efa161887f9dd19112",
    ),
    "boundary_normative_words_without_identifier": (
        "informe_analisis",
        0.97,
        "baja",
        "02e0f1d1d6c1de699deae05826dc767c",
    ),
    "boundary_technical_topic_only": (
        "referencia_tecnica",
        0.74,
        "media",
        "de4a50bc7150c9073273cfab1d4b180b",
    ),
    "boundary_audio_year_token": (
        "audio_transcrito",
        0.62,
        "alta",
        "6964ed154f13a327cdce8768011d161c",
    ),
    "cross_andritz_form": (
        "lista_verificacion",
        0.95,
        "baja",
        "5bbed903a9aff4b849bfce5229c8de2f",
    ),
    "cross_omicron_manual": (
        "manual_equipo",
        0.8,
        "media",
        "0d36eee4d5a1a6846c7fb84d1a971edb",
    ),
    "cross_packing_list": (
        "lista_empaque_embarque",
        0.98,
        "baja",
        "5639a60f00192192fa0a3d2594aac311",
    ),
    "cross_audio_meeting": (
        "reunion_grabada",
        0.97,
        "baja",
        "df2731ad446cce636fa293adcc226fc3",
    ),
    "cross_field_service_report": (
        "informe_tecnico",
        0.77,
        "media",
        "5226a74b42d0f5e6c8774b6fc9289aa6",
    ),
    "cross_technical_email": (
        "otro",
        0.35,
        "alta",
        "1eafb3d8d8fda3cde67b5714cf6f08eb",
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
    return xxhash.xxh3_128_hexdigest(payload)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_classifier_v14_representative_payload_is_characterized(
    case: _TaxonomyCase,
) -> None:
    classification = classify_document(case.signals)

    assert CLASSIFIER_VERSION == "technical-document-classifier-v16"
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
        "classifier-v16", "classifier-v14"
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
