from __future__ import annotations

import json
import os
import sqlite3
import zlib
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.document_catalog_schema as catalog_schema_module
from _02_Deduplicacion import snapshot_path
from _03_Progreso import RecordingProgress
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from _04_Nucleo_Operativo.document_cache_sync import synchronize_moved_document
from _04_Nucleo_Operativo.document_catalog import (
    MAX_CLASSIFICATION_TEXT_CHARS,
    document_catalog_database,
    initialize_document_catalog,
    list_catalog_documents,
    update_document_catalog,
    update_document_catalog_source,
)
from _04_Nucleo_Operativo.document_naming import suggest_document_stem
from _04_Nucleo_Operativo.document_organization import (
    apply_all_document_organization,
    apply_document_organization,
    default_organization_root,
    list_organization_plans,
    plan_document_organization,
)
from _04_Nucleo_Operativo.document_taxonomy import (
    DocumentSignals,
    classify_document,
    load_taxonomy,
)
from _04_Nucleo_Operativo.docx_state import initialize_docx_state
from _04_Nucleo_Operativo.models import FrameworkConfig
from _04_Nucleo_Operativo.pdf_state import initialize_pdf_state
from _04_Nucleo_Operativo.protected_content import (
    ProtectedContentError,
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from _04_Nucleo_Operativo.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    ExactSearchQuery,
    SearchHit,
    SemanticItem,
    TextChunk,
    fingerprint_text,
)
from _04_Nucleo_Operativo.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    initialize_semantic_state,
    register_embedding_model,
    resolve_search_hits,
    search_exact_page,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from neocortex.platform_policy import LINUX_MUTATION_REASON
from tests.internal_paths_test_support import disjoint_internal_paths_policy

WINDOWS_MUTATION_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="identity-bound corpus mutation is available only on Windows",
)


def _normal_mutation_guard(root: Path) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        disjoint_internal_paths_policy(root),
    )


def _protected_mutation_guard(
    root: Path,
    protected_root: Path,
) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        disjoint_internal_paths_policy(root),
        ProtectedContentPolicy.capture(
            (
                ProtectedPathSpec(
                    "fixture_read_only",
                    "tree",
                    "analyze_read_only",
                    protected_root,
                ),
            )
        ),
    )


# region [01] Explainable sector taxonomy


def test_framework_uses_bounded_document_classification_prefix() -> None:
    assert FrameworkConfig().document_classification_max_chars == MAX_CLASSIFICATION_TEXT_CHARS


@pytest.mark.parametrize(
    ("signals", "kind", "authority", "organization"),
    (
        (
            DocumentSignals(
                "pdf",
                r"C:\Normativa\IEEE Std C37.20.2-2015.pdf",
                "done",
                title="IEEE Std C37.20.2-2015 Metal-Clad Switchgear",
                leading_text="This standard applies to circuit breakers.",
            ),
            "normativa",
            "IEEE",
            None,
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\CFE\Curso transformadores.pdf",
                "done",
                title="Curso CFE de mantenimiento de transformadores",
                leading_text=(
                    "Comisión Federal de Electricidad. Material didáctico de capacitación."
                ),
            ),
            "curso_capacitacion",
            "CFE",
            "CFE",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\ANDRITZ\Formato inspeccion.docx",
                "complete",
                title="Formato de inspección ANDRITZ",
                author="ANDRITZ HYDRO",
                leading_text="Lista de verificación de pruebas eléctricas.",
            ),
            "lista_verificacion",
            None,
            "ANDRITZ",
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\Manuales\OMICRON CMC 356.pdf",
                "done",
                title="CMC 356 User Manual",
                author="OMICRON electronics",
                leading_text="Test equipment for protection and control.",
            ),
            "manual_equipo",
            None,
            "OMICRON",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\SERINTRA\Memoria de cálculo.docx",
                "complete",
                title="Memoria de cálculo",
                author="SERINTRA",
                leading_text="Dimensionamiento de una instalación eléctrica.",
            ),
            "memoria_calculo",
            None,
            "SERINTRA",
        ),
    ),
)
def test_sector_taxonomy_classifies_with_provenance(
    signals: DocumentSignals,
    kind: str,
    authority: str | None,
    organization: str | None,
) -> None:
    classification = classify_document(signals)

    assert classification.primary_kind == kind
    assert classification.primary_authority == authority
    assert classification.primary_organization == organization
    assert classification.evidence
    assert classification.classifier_signature.startswith("technical-document-classifier-v14|")
    assert classification.classifier_signature.endswith("|technical-document-naming-v9")


def test_taxonomy_toml_adds_company_without_replacing_defaults(tmp_path: Path) -> None:
    taxonomy_path = tmp_path / "taxonomy.toml"
    taxonomy_path.write_text(
        """
[[organizations]]
name = "EMPRESA PRUEBA"
aliases = ["EPRUEBA"]

[[clients]]
name = "CLIENTE PRUEBA"
aliases = ["CPRUEBA"]

[[projects]]
name = "Proyecto Delta"
client = "CLIENTE PRUEBA"
aliases = ["PRJ-DELTA-77"]
""".strip(),
        encoding="utf-8",
    )

    taxonomy = load_taxonomy(taxonomy_path)
    custom = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Formatos\EPRUEBA.docx",
            "complete",
            title="Formato EPRUEBA",
        ),
        taxonomy,
    )
    builtin = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Formatos\SERINTRA.docx",
            "complete",
            title="Formato SERINTRA",
        ),
        taxonomy,
    )
    project = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Proyectos\PRJ-DELTA-77\Packing list.pdf",
            "done",
            leading_text="Project PRJ-DELTA-77. Packing list for CPRUEBA.",
        ),
        taxonomy,
    )

    assert custom.primary_organization == "EMPRESA PRUEBA"
    assert builtin.primary_organization == "SERINTRA"
    assert project.primary_client == "CLIENTE PRUEBA"
    assert project.primary_project == "Proyecto Delta"
    assert "custom-xxh3-64=" in custom.classifier_signature


@pytest.mark.parametrize(
    ("identifier", "authority"),
    (
        ("IEC 61850-9-2:2011", "IEC"),
        ("ISO/IEC 17025:2017", "ISO/IEC"),
        ("IEC/IEEE 61850-9-3:2016", "IEC/IEEE"),
        ("NMX-J-549-ANCE-2005", "NMX"),
        ("NOM-001-SEDE-2012", "NOM"),
        ("NRF-011-CFE-2004", "NRF"),
        ("CFE L0000-15", "CFE"),
        ("NETA ATS-2017", "NETA"),
        ("NERC PRC-005-6", "NERC"),
        ("ISA-18.2-2016", "ISA"),
        ("AWS D1.1-2020", "AWS"),
        ("SSPC-SP 10", "SSPC"),
    ),
)
def test_sector_standard_identifiers_are_normalized_once(
    identifier: str,
    authority: str,
) -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            rf"C:\Normativa\{identifier}.pdf",
            "done",
            title=identifier,
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == authority
    assert tuple(reference.identifier for reference in classification.standard_references) == (
        identifier.upper(),
    )


def test_cover_issuer_wins_over_cited_authorities_and_managed_folder() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Normativa\NMX\IEC 60076-1.pdf",
            "done",
            leading_text=(
                "INTERNATIONAL STANDARD IEC 60076-1:2011 Power transformers - "
                "Part 1: General. Normative references include "
                "NMX-J-169-ANCE-2015 and NMX-J-284-ANCE-2018."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "IEC"
    assert classification.primary_subtype == "norma"
    assert classification.primary_equipment == "transformadores_potencia"
    assert any(
        "autoridad_emisora=IEC" in evidence for evidence in classification.authorities[0].evidence
    )


def test_managed_normative_migration_keeps_standard_without_folder_echo() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Normativa\NMX"
            r"\Normas\Tema_general\2018 - NORMA INTERNACIONAL.pdf",
            "done",
            leading_text=(
                "Documento distribuido para consulta técnica interna sin "
                "encabezado legible. NMX-SAST-45001-IMNC-2018 Sistemas de "
                "gestión de la seguridad "
                "y salud en el trabajo. Incluye requisitos sobre capacitación, "
                "procedimientos, comunicación por correo electrónico y facturas."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "NMX"
    assert "path:categoria_normativa_previa=NMX-SAST-45001-IMNC-2018" in (
        classification.kind_candidates[0].evidence
    )


def test_electronic_invoice_citing_neta_is_not_a_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Correspondencia"
            r"\NETA MTS - RV Facturacion Electronica 2 GASOLINERA.xlsx",
            "complete",
            leading_text=(
                "CORREO ELECTRÓNICO. Factura electrónica de GASOLINERA "
                "ARCALMEX. Referencia de mensaje ANSI NETA MTS 7011. "
                "Adjunto factura y datos de pago."
            ),
        )
    )

    assert classification.primary_kind == "correspondencia"
    assert classification.primary_authority == "NETA"
    assert all(candidate.label != "normativa" for candidate in classification.kind_candidates)


def test_ieee_trademarked_current_edition_precedes_revision_reference() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Entrada\Documento recuperado.pdf",
            "done",
            leading_text=(
                "IEEE Std C57.13™-2016 (Revision of IEEE Std C57.13-2008), "
                "IEEE Standard Requirements for Instrument Transformers"
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "IEEE"
    assert classification.suggested_stem == (
        "IEEE STD C57.13-2016 - IEEE Standard Requirements for Instrument Transformers"
    )


def test_nmx_current_edition_precedes_cancelled_edition_on_cover() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Entrada\Documento recuperado.pdf",
            "done",
            leading_text=(
                "COPIA CONTROLADA. Cancela y reemplaza a la "
                "NMX-CC-9001-IMNC-2008 NORMA MEXICANA. "
                "Sistemas de gestión de la calidad - Requisitos. "
                "NMX-CC-9001-IMNC-2015 ISO 9001:2015."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "NMX"
    assert classification.suggested_stem == (
        "NMX-CC-9001-IMNC-2015 - Sistemas de gestión de la calidad - Requisitos"
    )


def test_cfe_identifier_repairs_common_letter_o_ocr() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Entrada\Documento recuperado.pdf",
            "done",
            leading_text="ESPECIFICACIÓN CFE KOOOO-15 TRANSFORMADORES",
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "CFE"
    assert tuple(reference.identifier for reference in classification.standard_references) == (
        "CFE K0000-15",
    )
    assert classification.suggested_stem == "CFE K0000-15 - TRANSFORMADORES"


def test_iec_cover_edition_beats_later_series_references() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Entrada\Documento recuperado.pdf",
            "done",
            leading_text=(
                "THIS IS A PREVIEW - CLICK HERE TO BUY THE FULL PUBLICATION. "
                "IEC 60076-1 EDITION 3.0 2011-04 INTERNATIONAL STANDARD "
                "POWER TRANSFORMERS PART 1 GENERAL. References IEC "
                "60050-421:1990 and IEC 60076-10:2001."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "IEC"
    assert "IEC 60076-1:2011" in {
        reference.identifier for reference in classification.standard_references
    }
    assert classification.suggested_stem == ("IEC 60076-1 2011 - POWER TRANSFORMERS PART 1 GENERAL")


def test_mexican_standard_identifier_does_not_consume_following_reference() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Normativa\NMX-J-169-ANCE-2015.pdf",
            "done",
            leading_text=(
                "NORMA MEXICANA NMX-J-169-ANCE-2015 Transformadores y "
                "autotransformadores de distribución y potencia - Métodos de "
                "prueba. Referencia IEC 60076-1:2011."
            ),
        )
    )

    assert classification.primary_authority == "NMX"
    assert classification.primary_subtype == "metodo_prueba"
    assert {reference.identifier for reference in classification.standard_references} == {
        "IEC 60076-1:2011",
        "NMX-J-169-ANCE-2015",
    }


def test_cfe_specification_retains_issuer_and_rich_operational_facets() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Normativa\NMX\K0000-06.pdf",
            "done",
            leading_text=(
                "TRANSFORMADORES Y AUTOTRANSFORMADORES DE POTENCIA DE 10 MVA Y "
                "MAYORES. ESPECIFICACIÓN CFE K0000-06 NOVIEMBRE 2022. "
                "NORMAS QUE APLICAN NMX-J-169-ANCE-2015, NMX-J-308/1-ANCE-2016 "
                "e IEC 60076-5:2006."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "CFE"
    assert classification.primary_subtype == "especificacion"
    assert classification.primary_equipment == "transformadores_potencia"
    assert tuple(reference.identifier for reference in classification.standard_references) == (
        "CFE K0000-06",
        "IEC 60076-5:2006",
        "NMX-J-169-ANCE-2015",
        "NMX-J-308/1-ANCE-2016",
    )


def test_cfe_manual_and_procedure_receive_precise_subtypes() -> None:
    manual = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Manuales\V5420-63.pdf",
            "done",
            leading_text=(
                "MANUAL DE MANTENIMIENTO A TRANSFORMADORES DE POTENCIA "
                "M-2000-DC03. MANUAL CFE V5420-63. Diagnóstico, pruebas fuera "
                "de línea y mantenimiento basado en condición."
            ),
        )
    )
    procedure = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Procedimientos\DCPPSSED.pdf",
            "done",
            leading_text=(
                "PUESTA A PUNTO Y PUESTA EN SERVICIO DE SUBESTACIONES DE "
                "DISTRIBUCIÓN. PROCEDIMIENTO CFE DCPPSSED. Pruebas de campo de "
                "transformadores de potencia, interruptores de potencia y "
                "apartarrayos."
            ),
        )
    )

    assert manual.primary_kind == "manual_equipo"
    assert manual.primary_subtype == "manual_mantenimiento"
    assert manual.primary_authority == "CFE"
    assert manual.primary_equipment == "transformadores_potencia"
    assert {item.label for item in manual.activities} >= {
        "diagnostico_condicion",
        "mantenimiento",
    }
    assert procedure.primary_kind == "procedimiento"
    assert procedure.primary_subtype == "procedimiento_puesta_servicio"
    assert procedure.primary_authority == "CFE"
    assert {item.label for item in procedure.equipment} >= {
        "apartarrayos",
        "interruptores_potencia",
        "transformadores_potencia",
    }
    assert {item.label for item in procedure.activities} >= {
        "pruebas_campo",
        "puesta_punto",
        "puesta_servicio",
    }


def test_som_3531_is_recognized_as_cfe_primary_equipment_test_procedure() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\CFE\SOM-3531.pdf",
            "done",
            leading_text=(
                "SOM-3531 MANUAL DE PROCEDIMIENTOS DE PRUEBAS DE EQUIPO "
                "PRIMARIO DE SUBESTACIONES DE DISTRIBUCIÓN. Comisión Federal "
                "de Electricidad. Procedimientos de pruebas de campo."
            ),
        )
    )

    assert classification.primary_kind == "procedimiento"
    assert classification.primary_subtype == "procedimiento_pruebas"
    assert classification.primary_authority == "CFE"
    assert classification.primary_organization == "CFE"
    assert classification.primary_equipment == "equipo_primario_subestacion"
    assert classification.primary_activity == "pruebas_campo"


def test_som_3531_with_spaces_is_normalized_without_losing_procedure_context() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\CFE\CFE SOM 3531.pdf",
            "done",
            leading_text=(
                "SOM 3531 PROCEDIMIENTO PARA RESISTENCIA DE AISLAMIENTO. "
                "Procedimiento de pruebas de campo para equipo primario de "
                "subestaciones de distribución. Comisión Federal de Electricidad."
            ),
        )
    )

    assert classification.primary_kind == "procedimiento"
    assert classification.primary_subtype == "procedimiento_pruebas"
    assert classification.primary_authority == "CFE"
    assert classification.primary_equipment == "equipo_primario_subestacion"
    assert classification.primary_activity == "pruebas_campo"
    assert [item.identifier for item in classification.standard_references] == ["SOM-3531"]


def test_scanned_som_3531_index_recovers_procedure_without_cover_title() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Consulta\SOM 3531 2007 Pruebas de campo a TRs.pdf",
            "done",
            leading_text=(
                "COMISIÓN FEDERAL DE ELECTRICIDAD COORDINACIÓN DE DISTRIBUCIÓN. "
                "ÍNDICE. CAPÍTULO 1 GENERALIDADES. Introducción, objetivo, "
                "políticas y generalidades del mantenimiento. CAPÍTULO 2 PRUEBAS. "
                "Pruebas de fábrica. Pruebas de campo. Recomendaciones generales "
                "para realizar pruebas eléctricas al equipo primario. Prueba de "
                "resistencia de aislamiento y prueba de factor de potencia."
            ),
        )
    )
    report = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Reportes\SOM_3531_resultados_de_prueba_extracto.pdf",
            "done",
            leading_text=(
                "REPORTE DE RESULTADOS DE PRUEBAS. Referencia SOM 3531. "
                "Comisión Federal de Electricidad. Equipo primario."
            ),
        )
    )

    assert classification.primary_kind == "procedimiento"
    assert classification.primary_subtype == "procedimiento_pruebas"
    assert classification.primary_authority == "CFE"
    assert classification.uncertainty == "baja"
    assert report.primary_kind != "procedimiento"


def test_astm_cover_designation_wins_over_cited_ieee_guide() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Consulta\ASTM D1816 - Dielectric Breakdown Voltage.pdf",
            "done",
            leading_text=(
                "Designation: D1816. Standard Test Method for Dielectric Breakdown "
                "Voltage of Insulating Liquids Using VDE Electrodes. This standard "
                "is issued under the fixed designation D1816. ASTM Standards. "
                "This method is used as recommended by IEEE C57.106."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "ASTM"
    assert classification.primary_subtype == "metodo_prueba"
    assert {item.identifier for item in classification.standard_references} == {
        "ASTM D1816",
        "IEEE C57.106",
    }


def test_reference_section_does_not_turn_serintra_record_into_ieee_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Normativa\IEEE"
            r"\IEEE C57.152.2013 - Procedimiento SERINTRA.pdf",
            "done",
            leading_text=(
                "Procedimiento para Montaje, Mantenimiento y Pruebas a "
                "Transformadores de Potencia. PRUEBA DE FACTOR DE POTENCIA. "
                "REFERENCIAS DOCUMENTALES 1.- SOM - 3531. 2.- IEEE GUIDE FOR "
                "DIAGNOSTIC FIELD TESTING IEEE C57.152.2013."
            ),
        )
    )

    assert classification.primary_kind == "procedimiento"
    assert classification.primary_subtype == "procedimiento_pruebas"
    assert {item.identifier for item in classification.standard_references} == {
        "IEEE C57.152.2013",
        "SOM-3531",
    }


def test_cfe_and_lapem_words_are_not_invented_as_standard_identifiers() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Contratos\CONTRATO MALPASO.pdf",
            "done",
            leading_text=(
                "CONTRATO DE SERVICIOS celebrado por la COMISIÓN FEDERAL DE "
                "ELECTRICIDAD. CFE DISTRIBUCIÓN y LAPEM actualizarán los anexos. "
                "Cláusulas y condiciones del servicio. Como referencia se aplicará "
                "la ESPECIFICACIÓN CFE K0000-06."
            ),
        )
    )

    identifiers = {item.identifier for item in classification.standard_references}
    assert classification.primary_kind == "contrato_legal"
    assert "CFE DISTRIBUCION" not in identifiers
    assert not any(identifier.startswith("LAPEM ") for identifier in identifiers)
    assert "CFE K0000-06" in identifiers


def test_laboratory_result_keeps_norms_as_references_and_routes_to_client() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Laboratorio\EGA262139.pdf",
            "done",
            leading_text=(
                "CÓDIGO Informe: EGA262139. Control Interno: AL20261064. "
                "CLIENTE: ANDRITZ. EQUIPO ANALIZADO: C.H. Malpaso Unidad 1, "
                "Transformador de Potencia TR-9. INFORME DE ENSAYO: "
                "CROMATOGRAFÍA DE GASES PARA DETECCIÓN DE BPC. Resultado "
                "obtenido. Método empleado: ASTM D4059-00 y "
                "NMX-J-123-ANCE-2019. De acuerdo con la Norma Oficial Mexicana "
                "NOM-133-SEMARNAT-2015."
            ),
        )
    )

    assert classification.primary_kind == "reporte_laboratorio"
    assert classification.primary_client == "ANDRITZ"
    assert classification.primary_project == "Malpaso"
    assert {item.identifier for item in classification.standard_references} >= {
        "ASTM D4059-00",
        "NMX-J-123-ANCE-2019",
        "NOM-133-SEMARNAT-2015",
    }
    assert classification.uncertainty == "baja"


def test_lapem_singular_test_report_is_not_misclassified_as_ieee_standard() -> None:
    text = (
        "Dirección Corporativa de Negocios Comerciales. Laboratorio de Pruebas "
        "de Equipos y Materiales LAPEM. OFICINA DE QUÍMICA ANALÍTICA. "
        "INFORME DE PRUEBA. No. de Análisis: 1927-G/23. "
        "Procedencia: C.H. MALPASO. Sitio: C.H. MALPASO. "
        "Equipo: TRANSFORMADOR Banco: T-5 Fase A Marca: Parson Peebles. "
        "Fecha de Análisis: 20/10/2023. ANÁLISIS CROMATOGRÁFICO DE GASES "
        "DISUELTOS EN ACEITE AISLANTE. MÉTODO DE PRUEBA: ASTM D3612-02 "
        "Método C. GUÍA DE REFERENCIA: IEEE Std C57.104-2019. "
        "CONCENTRACIÓN, RESULTADO E INTERPRETACIÓN DEL ANÁLISIS. "
        "FORMATO: 31230301."
    )
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Clientes\ANDRITZ\Malpaso\2025 - Documento recuperado.pdf",
            "done",
            leading_text=text,
        )
    )
    suggestion = suggest_document_stem(
        path=r"C:\Clientes\ANDRITZ\Malpaso\2025 - Documento recuperado.pdf",
        title="",
        leading_text=text,
        primary_kind=classification.primary_kind,
        organization="LAPEM",
    )

    assert classification.primary_kind == "reporte_laboratorio"
    assert classification.primary_client == "ANDRITZ"
    assert {item.identifier for item in classification.standard_references} >= {
        "ASTM D3612-02",
        "IEEE STD C57.104-2019",
    }
    assert suggestion.stem == (
        "Informe de laboratorio - 1927-G-23 - TRANSFORMADOR T-5 Fase A - "
        "C.H. MALPASO - 2023-10-20 - LAPEM"
    )


def test_formal_standard_cover_is_not_sent_to_review_for_internal_procedure_text() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Revision_pendiente\NMX-CC-9001-IMNC-2015.pdf",
            "done",
            leading_text=(
                "COPIA CONTROLADA. NORMA MEXICANA NMX-CC-9001-IMNC-2015. "
                "Sistemas de gestión de la calidad - Requisitos. "
                "Los procedimientos documentados requeridos por esta norma "
                "incluyen el procedimiento de control y el correo electrónico."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "NMX"
    assert classification.confidence >= 0.92
    assert classification.uncertainty == "baja"
    assert classification.suggested_stem.startswith("NMX-CC-9001-IMNC-2015 - Sistemas de gestión")


def test_normative_traceability_report_is_analysis_not_nmx_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Normativa\NMX\Guias"
            r"\Reporte trazabilidad normativa DGA Malpaso.docx",
            "complete",
            leading_text=(
                "Proyecto FIEL/10670-005/2021 Repotenciación y Modernización "
                "de la Central Hidroeléctrica Malpaso. REPORTE DE TRAZABILIDAD "
                "NORMATIVA. Nivel, documento, referencia observada, conexión "
                "normativa y aplicación para DGA. CFE K0000-23 cita "
                "NMX-J-308/2-ANCE-2015 e IEC 60599."
            ),
        )
    )

    assert classification.primary_kind == "informe_analisis"
    assert classification.primary_client == "ANDRITZ"
    assert classification.primary_project == "Malpaso"
    assert classification.primary_authority == "CFE"
    assert {item.identifier for item in classification.standard_references} >= {
        "CFE K0000-23",
        "IEC 60599",
        "NMX-J-308/2-ANCE-2015",
    }


def test_sat_compliance_certificate_is_not_iec_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\COC for SAT-Mexico_Andritz_MALPASO.pdf",
            "done",
            leading_text=(
                "HYOSUNG CHINA. Certificate of Compliance. Document No: "
                "NH-3/QC2-20240410-01. Manufacturer: Hyosung China. Client: "
                "Mexico Andritz Malpaso PJT. Transformer Type: 75 MVA 400 kV. "
                "Topic: Clarification on UNIT TR SAT. The absorption rate is "
                "not required by IEC 60076-1:2011 international standard. "
                "Comparison of FAT and SAT test data. Quality Assurance Dept."
            ),
        )
    )

    assert classification.primary_kind == "certificado_calidad"
    assert classification.primary_authority == "IEC"
    assert classification.primary_client == "ANDRITZ"
    assert classification.primary_project == "Malpaso"
    assert {item.identifier for item in classification.standard_references} == {"IEC 60076-1:2011"}


def test_lapem_prototype_acceptance_is_quality_certificate_not_cfe_norm() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\LAPEM\Constancia K3112-18-E-7790 CDJV-145.pdf",
            "done",
            leading_text=(
                "LABORATORIO DE PRUEBAS DE EQUIPOS Y MATERIALES. Número: "
                "K3112-18-E/7790. CONSTANCIA DE ACEPTACIÓN DE PROTOTIPO. "
                "Empresa Jacob and Jacob. Con base en los resultados "
                "satisfactorios de las pruebas prototipo estipuladas en la "
                "Especificación CFE V4200-25 Cuchillas Desconectadoras de "
                "15 kV a 145 kV. No. de Informe de Pruebas K3112-K-5972-18."
            ),
        )
    )

    assert classification.primary_kind == "certificado_calidad"
    assert classification.primary_organization == "LAPEM"
    assert classification.primary_authority == "CFE"
    assert {item.identifier for item in classification.standard_references} == {"CFE V4200-25"}
    assert classification.uncertainty == "baja"


def test_completed_cfe_questionnaire_is_project_specification_not_norm() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Malpaso\CUESTIONARIO TECNICO APENDICE B.docx",
            "complete",
            leading_text=(
                "CENTRAL HIDROELÉCTRICA MALPASO. CUESTIONARIO TÉCNICO APÉNDICE B "
                "ESPECIFICACIÓN CFE W4101-16 SISTEMAS DE EXCITACIÓN. Tabla B1 "
                "Datos generales, concepto, respuesta y folio. ANDRITZ S.A. DE "
                "C.V. Características y respuestas del sistema suministrado."
            ),
        )
    )

    assert classification.primary_kind == "especificacion_tecnica"
    assert classification.primary_authority == "CFE"
    assert classification.primary_client == "ANDRITZ"
    assert classification.primary_project == "Malpaso"
    assert {item.identifier for item in classification.standard_references} == {"CFE W4101-16"}


def test_andritz_technical_offer_is_proposal_not_cfe_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\Oferta transformador de potencia ANDRITZ.pdf",
            "done",
            leading_text=(
                "BASES DE LA PROPUESTA. ALCANCE Y DESCRIPCIÓN DE SUMINISTROS Y "
                "SERVICIOS. REPOTENCIACIÓN DE LA CENTRAL HIDROELÉCTRICA MALPASO. "
                "ANDRITZ. Cuestionarios y cumplimientos de la especificación "
                "CFE K0000-06 Transformadores de Potencia de 10 MVA y mayores."
            ),
        )
    )

    assert classification.primary_kind == "cotizacion_propuesta"
    assert classification.primary_authority == "CFE"
    assert classification.primary_client == "ANDRITZ"
    assert classification.primary_project == "Malpaso"


def test_nonconformity_report_is_not_norm_from_iso_reference() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Calidad\Reporte de no conformidad 3 Compras.pdf",
            "done",
            leading_text=(
                "SERINTRA. REPORTE DE NO CONFORMIDAD. Oficina Calidad, ambiente "
                "y seguridad. Documentación de referencia: ISO 9001:2015, 7.5.3 "
                "Control de la información documentada. Descripción del hallazgo, "
                "acción y seguimiento."
            ),
        )
    )

    assert classification.primary_kind == "reporte_no_conformidad"
    assert classification.primary_authority == "ISO"
    assert {item.identifier for item in classification.standard_references} == {"ISO 9001:2015"}


def test_malpaso_hcn_documents_receive_client_project_and_workstream() -> None:
    packing = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Logistica\MALPASO-HCN-04 Packing List Report.pdf",
            "done",
            leading_text=(
                "ANDRITZ (China) Ltd. Packing List. Place of Delivery. Project. "
                "Package No MALPASO-HCN-04-001. Shipment No MALPASO-HCN-04. "
                "Type of Packing Wooden Box. Gross Weight. Net Weight. Storage "
                "Instructions. Designation of Contents: Transformer main body."
            ),
        )
    )
    pressure = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Inspecciones\Pressure inspection report MALPASO-HCN-005.pdf",
            "done",
            leading_text=(
                "Project FIEL/10670-005/2021 Repowering and Modernization. "
                "Malpaso Hydroelectric Power Plant. INTERNAL PRESSURE INSPECTION "
                "REPORT. Shipment No MALPASO-HCN-05-001. Transformer Unit 2 "
                "Phase A. Pressure Reading 0.05 MPa."
            ),
        )
    )

    assert packing.primary_kind == "lista_empaque_embarque"
    assert packing.primary_client == "ANDRITZ"
    assert packing.primary_project == "Malpaso"
    assert packing.primary_workstream == "embarques_hcn"
    assert pressure.primary_kind == "informe_inspeccion"
    assert pressure.primary_client == "ANDRITZ"
    assert pressure.primary_project == "Malpaso"
    assert pressure.primary_workstream == "control_presion_unidades"
    assert pressure.primary_equipment == "transformadores_potencia"


def test_andritz_logistics_labels_and_test_exports_get_operational_kinds() -> None:
    delivery = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Logistica\Delivery Report Project LH-MALPASO.pdf",
            "done",
            leading_text=(
                "Project: LH-MALPASO. Delivery Report. Materials received. "
                "Shipment No MALPASO-HCN-02. Number of packages. Package No "
                "MALPASO-HCN-02-029. Delivery Date. Storage Area. Storage "
                "Position. Inspection: Pending."
            ),
        )
    )
    label = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Etiquetas\TR-08 CEE Cromatografia.docx",
            "complete",
            leading_text=(
                "CEE / CROMATOGRAFIA Jeringa 50 mL C.H. MALPASO TR-08 | "
                "U1-FASE A NS 10184386-08 | HYOSUNG 45/60/75 MVA | "
                "400/3/15 kV | 16,000 KG | CEE | Fecha __________"
            ),
        )
    )
    instrument_export = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\ANDRITZ\CH Malpaso\Pruebas a TCs\H1 C2 A.xlsx",
            "complete",
            leading_text=(
                "FILENAME H1 C2 2.test DATE 03/11/26 TIME 10:00 TESTS 8 "
                "COMPANY DUSGEM CIRCUIT FASE A H1 C2 STATION HIDROELECTRICA "
                "MALPASO PASS / FAIL Disabled TEST # 1 TEST NOTES TAP X1-X2 "
                "IEEE 30 Vkp Volts 25.600 IEEE 30 Ikp Amps 1.9966 WINDING RES "
                "58.78 milliohms DATA POINTS CUR(A) VTG(V) Z(OHM)."
            ),
        )
    )
    degraded_packing = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\PDF\Packing_list_Report_MALPASO-HCN-05-048-049.pdf",
            "done",
            leading_text="Texto rotado sin estructura OCR utilizable.",
        )
    )

    assert delivery.primary_kind == "reporte_entrega_embarque"
    assert delivery.primary_workstream == "embarques_hcn"
    assert delivery.primary_client == "ANDRITZ"
    assert label.primary_kind == "etiqueta_muestra_laboratorio"
    assert label.primary_workstream == "muestreo_aceite_transformadores"
    assert label.primary_client == "ANDRITZ"
    assert instrument_export.primary_kind == "reporte_resultados_pruebas"
    assert instrument_export.primary_project == "Malpaso"
    assert degraded_packing.primary_kind == "lista_empaque_embarque"
    assert degraded_packing.confidence >= 0.90


def test_andritz_sample_batches_time_and_resource_records_are_distinguished() -> None:
    sample_batch = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Etiquetas\Etiquetas CEE Cromatografia U1 TR-08-09-10.pdf",
            "done",
            leading_text=(
                "CEE / CROMATOGRAFIA Jeringa de vidrio 50 mL. S.E.: C.H. "
                "MALPASO. Equipo: U1-FASE A. Marca/Serie: HYOSUNG 10184386-08. "
                "45/60/75 MVA. Análisis CEE. Fecha de muestreo."
            ),
        )
    )
    time_report = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\ANDRITZ\CH Malpaso\Tiempo adicional.xlsx",
            "complete",
            leading_text=(
                "REPORTE DE TIEMPO ADICIONAL. Frente: CH MALPASO. Semana: "
                "16 al 22. Lun Mar Mie Jue Vie Sab Dom. Número de horas "
                "laboradas adicionales. Nombre del trabajador. Puesto. Total."
            ),
        )
    )
    resources = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\ANDRITZ\CH Malpaso\Recursos por dia.xlsx",
            "complete",
            leading_text=(
                "Personal. Vehículos. Otros recursos. Lunes Martes Miércoles "
                "Jueves Viernes Sábado Domingo. Lote de herramienta. Tiempo "
                "laborado. Recursos utilizados por día. C.H. Malpaso."
            ),
        )
    )
    payment = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\Solicitud transferencia bancaria.pdf",
            "done",
            leading_text=(
                "ANEXO 9.3 FORMATO DE SOLICITUD DE PAGO MEDIANTE TRANSFERENCIA "
                "ELECTRÓNICA BANCARIA. Administrador del contrato CFE C.H. "
                "Malpaso. Datos bancarios y cuenta para transferencia."
            ),
        )
    )

    assert sample_batch.primary_kind == "etiqueta_muestra_laboratorio"
    assert sample_batch.primary_workstream == "muestreo_aceite_transformadores"
    assert time_report.primary_kind == "registro_tiempo_personal"
    assert time_report.primary_client == "ANDRITZ"
    assert resources.primary_kind == "programa_cronograma"
    assert resources.primary_project == "Malpaso"
    assert payment.primary_kind == "instruccion_cuenta_bancaria"


def test_malpaso_place_name_alone_does_not_create_andritz_project() -> None:
    fuel_receipt = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Facturas\GASOLINA Y LUBRICANTES MALPASO - PEMEX.pdf",
            "done",
            leading_text=(
                "PEMEX. Gasolina y Lubricantes Malpaso. Comprobante fiscal. "
                "Subtotal, IVA, total y forma de pago."
            ),
        )
    )
    project_record = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Minutas\Minuta Proyecto Malpaso.docx",
            "complete",
            leading_text="Minuta de trabajo del Proyecto Malpaso para ANDRITZ.",
        )
    )

    assert fuel_receipt.primary_project is None
    assert fuel_receipt.primary_client is None
    assert project_record.primary_project == "Malpaso"
    assert project_record.primary_client == "ANDRITZ"


def test_andritz_issuer_is_separate_from_explicit_client_role() -> None:
    issuer = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Formatos\Formato ANDRITZ.docx",
            "complete",
            author="ANDRITZ HYDRO",
            leading_text="Formato de inspección emitido por ANDRITZ HYDRO.",
        )
    )
    client = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Proyectos\Minuta de arranque.docx",
            "complete",
            leading_text="Minuta de arranque. Cliente: ANDRITZ.",
        )
    )

    assert issuer.primary_organization == "ANDRITZ"
    assert issuer.primary_client is None
    assert client.primary_client == "ANDRITZ"


def test_normative_name_uses_primary_nmx_reference_before_iec_equivalent() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Consulta\Transformadores.docx",
            "complete",
            leading_text=(
                "NMX-J-284-ANCE-2012 Transformadores y autotransformadores de "
                "potencia - Especificaciones. Concordancia con normas "
                "internacionales: esta norma no coincide con IEC 60076-1."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "NMX"
    assert classification.suggested_stem.startswith("NMX-J-284-ANCE-2012")
    assert not classification.suggested_stem.startswith("IEC 60076-1")


def test_ambiguous_en_prose_is_not_a_standard_or_normative_audio() -> None:
    audio = classify_document(
        DocumentSignals(
            "audio",
            r"C:\Audio\En 2018 se fue Cristiano Ronaldo.mp4",
            "complete",
            leading_text="En 2018 se fue Cristiano Ronaldo y el equipo cambió.",
        )
    )
    quotation = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Cotizaciones\Estudio de iluminacion.pdf",
            "done",
            leading_text=(
                "Estudio de iluminación en 118 áreas de trabajo. Precio "
                "unitario y total. N° DE COTIZACION CTZ-153-RCC-OTR15-2022. "
                "Oferta comercial para cumplir NOM-025-STPS-2008. "
                "Vigencia de la cotización: 30 días."
            ),
        )
    )

    assert audio.primary_kind == "audio_transcrito"
    assert audio.primary_authority is None
    assert not any(item.authority == "EN" for item in audio.standard_references)
    assert quotation.primary_kind == "cotizacion_propuesta"
    assert not any(item.authority == "EN" for item in quotation.standard_references)


def test_short_explicit_en_industrial_standard_remains_recognized() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Normas\Cascos de seguridad.pdf",
            "done",
            leading_text=(
                "EUROPEAN STANDARD EN 397:2012. Industrial safety helmets - "
                "requirements and test methods."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "EN"
    assert any(
        item.authority == "EN" and item.identifier == "EN 397:2012"
        for item in classification.standard_references
    )


def test_ocr_number_with_leading_zero_is_not_an_en_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Recuperados\seguridad trabajo.pdf",
            "done",
            leading_text="eemnpn med 2099 as ea EN 084649 seguridad en el trabajo",
        )
    )

    assert not any(item.authority == "EN" for item in classification.standard_references)


def test_standard_study_is_analysis_not_the_standard_it_discusses() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Calidad\ISO 9001 2015 - Estudio de norma ISO 9001 2015.docx",
            "complete",
            leading_text=(
                "Estudio de norma ISO 9001:2015. Sistemas de gestión de la "
                "calidad - Requisitos. Esta norma mexicana emplea el enfoque "
                "a procesos y el ciclo PHVA."
            ),
        )
    )

    assert classification.primary_kind == "informe_analisis"
    assert classification.primary_authority == "ISO"
    assert classification.suggested_stem.startswith("ISO 9001 2015 - Estudio")


def test_grounding_measurement_form_keeps_nom_only_as_reference() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\Mediciones\LOCALIZACION.xlsx",
            "complete",
            leading_text=(
                "MEDICIÓN DE LA RESISTENCIA DE RED DE TIERRA DE UNA "
                "SUBESTACIÓN ELÉCTRICA EN MEDIA TENSIÓN. CLIENTE: EQUIPO: "
                "LOCALIZACIÓN: FECHA: EQUIPO DE PRUEBA (MARCA): NO. DE "
                "SERIE: OBSERVACIONES: NORMA OFICIAL MEXICANA "
                "NOM-022-STPS-2015."
            ),
        )
    )

    assert classification.primary_kind == "registro_mediciones"
    assert classification.primary_authority == "NOM"
    assert "resistencia" in classification.suggested_stem.casefold()


def test_managed_conversation_does_not_inherit_document_kind_from_old_name() -> None:
    classification = classify_document(
        DocumentSignals(
            "audio",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Audio_transcrito"
            r"\Procedimientos\General\la fuente decia que el procedimiento.ogg",
            "complete",
            leading_text=(
                "La fuente decía que el procedimiento nos iba a llevar "
                "alrededor de veinte días por los tres transformadores."
            ),
        )
    )

    assert classification.primary_kind == "audio_transcrito"
    assert classification.suggested_stem == "Audio transcrito"
    assert classification.uncertainty == "alta"


def test_formal_normative_words_without_identifier_do_not_create_a_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Capacitacion\Encuesta.docx",
            "complete",
            leading_text=(
                "La presente encuesta está diseñada para valorar el "
                "aprovechamiento del curso sobre la NORMA MEXICANA."
            ),
        )
    )

    assert classification.primary_kind != "normativa"
    assert not classification.standard_references


@pytest.mark.parametrize(
    ("path", "text", "kind"),
    (
        (
            r"C:\Metrologia\Informe CIMEV.pdf",
            "INFORME DE CALIBRACIÓN. Instrumento: Termohigrómetro. Fecha de "
            "calibración: 2024-08-28. Incertidumbre expandida. Resultado de "
            "calibración conforme a NMX-CH-140-IMNC-2002.",
            "certificado_calibracion",
        ),
        (
            r"C:\Seguridad\HDS pintura.pdf",
            "EDICION NOM-018-STPS-2015. HOJA DE SEGURIDAD. Identificación "
            "del producto, ingredientes peligrosos, primeros auxilios y "
            "medidas contra incendios.",
            "hoja_datos_seguridad",
        ),
        (
            r"C:\Calidad\Cuchilla terminada.pdf",
            "CERTIFICADO DE CALIDAD COMO PRODUCTO TERMINADO. Orden de compra "
            "JF16-05. Los equipos cumplen con la Especificación CFE V4200-25.",
            "certificado_calidad",
        ),
        (
            r"C:\CFE\Capitulo 18 SF6.pdf",
            "COMISION FEDERAL DE ELECTRICIDAD COORDINACION DE DISTRIBUCION. "
            "CAPITULO 18 SUBESTACIONES BLINDADAS AISLADAS CON GAS SF6. "
            "TEORIA GENERAL. Requerimientos para el montaje y mantenimiento. "
            "Equipos descritos en la especificacion CFE VY200-40.",
            "manual_equipo",
        ),
        (
            r"C:\Pruebas\Matriz CFE.xlsx",
            "Pruebas que solicita CFE en Especificación. Realizada Si No. "
            "Resistencia de aislamiento. Se encuentra en Registro de Pruebas. "
            "Según CFE D3100-19 e IEC 60137.",
            "lista_verificacion",
        ),
    ),
)
def test_operational_forms_keep_standards_as_references(
    path: str,
    text: str,
    kind: str,
) -> None:
    classification = classify_document(
        DocumentSignals(Path(path).suffix.lstrip("."), path, "complete", leading_text=text)
    )

    assert classification.primary_kind == kind
    assert classification.primary_kind != "normativa"
    assert classification.standard_references


def test_andritz_sat_minutes_lab_and_completed_annex_get_specific_kinds() -> None:
    sat = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\Reporte_SAT_U1-33.pdf",
            "done",
            leading_text=(
                "HYOSUNG ANDRITZ. Site Test & Commissioning Report. Main "
                "Transformer. Acceptance criteria and satisfactory results."
            ),
        )
    )
    minutes = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\Minuta EPS-AT.pdf",
            "done",
            leading_text=(
                "MINUTES OF MEETING & ACTION PLAN. Company Andritz Hydro. "
                "Date and location Malpaso. Statements, agreements and actions. "
                "Protocolos de pruebas pendientes."
            ),
        )
    )
    laboratory = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Malpaso\CLA233163.pdf",
            "done",
            leading_text=(
                "CODIGO Informe: CLA233163. Control Interno: AL20231839. "
                "CLIENTE: ANDRITZ. EQUIPO ANALIZADO: Transformador Malpaso. "
                "INFORME DE ENSAYOS: FISICOS, QUIMICOS Y ELECTRICOS. Metodo "
                "empleado ASTM D1816. Resultado y fecha de ensayo."
            ),
        )
    )
    annex = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Malpaso\Respuesta tecnica.docx",
            "complete",
            leading_text=(
                "Especificacion CFE G0000-48. APENDICE C INFORMACION TECNICA "
                "REQUERIDA. Proponente: ANDRITZ. Licitacion No.: 0008-2021. "
                "Requisicion No.: 75. Concepto Solicitado Ofertado. "
                "Confidential document. All rights reserved by ANDRITZ."
            ),
        )
    )

    assert sat.primary_kind == "reporte_fat_sat"
    assert minutes.primary_kind == "minuta_acta"
    assert laboratory.primary_kind == "reporte_laboratorio"
    assert annex.primary_kind == "especificacion_tecnica"


@pytest.mark.parametrize(
    ("text", "authority", "subtype", "equipment"),
    (
        (
            "ASTM D877/D877M-19 Standard Test Method for Dielectric Breakdown "
            "Voltage of Insulating Liquids Using Disk Electrodes",
            "ASTM",
            "metodo_prueba",
            "aceite_aislante",
        ),
        (
            "ANSI/NETA MTS-2023 Standard for Maintenance Testing Specifications "
            "for Electrical Power Equipment and Systems",
            "NETA",
            "especificacion",
            None,
        ),
        (
            "IEEE Std C57.152-2025 IEEE Guide for Diagnostic Field Testing of "
            "Liquid-Filled Power Transformers, Regulators, and Reactors",
            "IEEE",
            "guia",
            "transformadores_potencia",
        ),
    ),
)
def test_international_normative_forms_are_distinguished(
    text: str,
    authority: str,
    subtype: str,
    equipment: str | None,
) -> None:
    classification = classify_document(
        DocumentSignals("pdf", rf"C:\Normativa\{authority}.pdf", "done", leading_text=text)
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == authority
    assert classification.primary_subtype == subtype
    assert classification.primary_equipment == equipment


@pytest.mark.parametrize(
    ("title", "kind"),
    (
        ("Informe de inspección del transformador", "informe_inspeccion"),
        ("Registro fotográfico de mantenimiento", "registro_fotografico"),
        ("Reporte de no conformidad NCR", "reporte_no_conformidad"),
    ),
)
def test_specific_operational_document_kinds_are_safe_to_organize(
    title: str,
    kind: str,
) -> None:
    classification = classify_document(
        DocumentSignals("pdf", rf"C:\Consulta\{title}.pdf", "done", title=title)
    )

    assert classification.primary_kind == kind
    assert classification.confidence >= 0.72


def test_sector_topic_recovers_unknown_consultation_material() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\02_TECNICO_REFERENCIA\Diagnostico.pdf",
            "done",
            title="Diagnóstico avanzado de transformadores",
        )
    )

    assert classification.primary_kind == "referencia_tecnica"
    assert classification.topics[0].label == "transformadores"
    assert classification.confidence >= 0.72


def test_standard_references_do_not_replace_the_primary_document_type() -> None:
    procedure = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\SERINTRA\SPCOM-16.pdf",
            "done",
            leading_text=(
                "Procedimiento SPCOM-16 Selección, Evaluación y Desarrollo de "
                "Proveedores. Objetivo y alcance. Referencias: ISO 9000 e ISO "
                "9001:2015."
            ),
        )
    )
    audit = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Auditorias\FTCAS07.pdf",
            "done",
            leading_text=(
                "INFORME DE AUDITORÍA. Objetivo: verificar el sistema de gestión. "
                "Se revisaron las cláusulas de ISO 9001:2015."
            ),
        )
    )
    standard = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Consulta\IEEE Std C37.20.2-2015.pdf",
            "done",
            title="IEEE Std C37.20.2-2015",
            leading_text=("IEEE Std C37.20.2-2015. IEEE Standard for Metal-Clad Switchgear."),
        )
    )

    assert procedure.primary_kind == "procedimiento"
    assert audit.primary_kind == "informe_auditoria"
    assert standard.primary_kind == "normativa"
    assert procedure.standard_references
    assert audit.standard_references


def test_short_ansi_ieee_guide_identifier_is_a_formal_standard() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Consulta\ANSI IEEE Std 81-1983.pdf",
            "done",
            title=(
                "ANSI/IEEE Std 81-1983 An American National Standard IEEE Guide "
                "for Measuring Earth Resistivity"
            ),
            leading_text=(
                "ANSI/IEEE Std 81-1983. An American National Standard. IEEE Guide "
                "for Measuring Earth Resistivity. Approved by the IEEE Standards "
                "Board."
            ),
        )
    )

    assert classification.primary_kind == "normativa"
    assert classification.primary_authority == "IEEE"
    assert classification.confidence >= 0.92


def test_manual_de_instructivos_is_not_reduced_to_a_generic_procedure() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Laboratorio\IT-LAB-08 Tensión Aplicada.pdf",
            "done",
            leading_text=(
                "Manual de Instructivos. Nombre del Instructivo IT-LAB-08 Prueba "
                "de tensión aplicada. Propósito, alcance y responsabilidades. "
                "Desarrollo del instructivo. Referencia NMX-J-116-ANCE-2005."
            ),
        )
    )

    assert classification.primary_kind == "instructivo_trabajo"
    assert classification.standard_references


def test_managed_category_directories_do_not_reinforce_the_previous_kind() -> None:
    email = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Administrativo\General\Cotizaciones_y_propuestas\Correo de aceptacion.pdf",
            "done",
            leading_text=(
                "De: Samuel Rodriguez Enviado el: lunes 5 de diciembre Para: "
                "Mariel Castro CC: Francisco Inguanzo Asunto: Alcances GMD. "
                "Favor de coordinar el muestreo de la cotización para GMD."
            ),
        )
    )
    system_description = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Empresas\ANDRITZ\Formatos\THYNE500.pdf",
            "done",
            leading_text=(
                "Descripción General Sistema de Excitación THYNE500. Lazos de "
                "control y funciones definidas en formato de función discreta."
            ),
        )
    )
    empty_standard_folder = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Users\Victor\Consulta_Tecnica_Organizada\Normativa\IEEE\Documento.docx",
            "complete",
        )
    )

    assert email.primary_kind == "correspondencia"
    assert system_description.primary_kind == "descripcion_tecnica_sistema"
    assert empty_standard_folder.primary_kind == "otro"


@pytest.mark.parametrize(
    ("text", "kind"),
    (
        (
            "ANEXO 1 CARTA INSTRUCCIÓN PARA REGISTRO DE CUENTA BANCARIA "
            "(personas morales / pesos mexicanos)",
            "instruccion_cuenta_bancaria",
        ),
        (
            "N° de contrato / cotización. Alcance del proyecto. HOJA DE ASIGNACIÓN DE PROYECTO.",
            "hoja_asignacion_proyecto",
        ),
        (
            "Descripción General Sistema de Excitación THYNE500. Lazos de control.",
            "descripcion_tecnica_sistema",
        ),
        (
            "FORMATO AC-F-02-02. MANUAL DE CALIDAD. Sistema de calidad.",
            "manual_sistema_gestion",
        ),
        (
            "FO-CIE-SS-01-R00 REPORTE DIARIO DE CAMPO. Descripción de actividades.",
            "reporte_actividades",
        ),
        (
            "# Reporte de archivo: OpenXmlFiller.dll Ruta relativa: .Toolbox/bin "
            "Tipo: binary Timestamp (UTC): 2025-10-09",
            "reporte_inventario_archivo",
        ),
        (
            "Gracias por elegir Uber. UberX 6.34 kilómetros. Total 309.90 MXN.",
            "comprobante_viaje",
        ),
    ),
)
def test_second_pass_corpus_document_families(text: str, kind: str) -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Recuperados\x_456.docx",
            "complete",
            leading_text=text,
        )
    )

    assert classification.primary_kind == kind
    assert classification.confidence >= 0.82


def test_exact_tenth_score_margin_is_not_ambiguous_from_float_roundoff() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Calidad\manual.docx",
            "complete",
            leading_text=(
                "FORMATO AC-F-02-02. MANUAL DE CALIDAD. Plan de calidad. "
                + ("contenido del sistema de gestión. " * 180)
                + "Plan de calidad. procedimiento documentado."
            ),
        )
    )

    assert classification.primary_kind == "manual_sistema_gestion"
    assert classification.kind_candidates[0].score == 0.96
    assert classification.kind_candidates[1].score == 0.86
    assert classification.uncertainty == "baja"
    assert classification.confidence == 0.96


def test_second_pass_semantic_names_use_operational_identity() -> None:
    email = suggest_document_stem(
        path=r"C:\Correos\x.pdf",
        title="",
        leading_text=(
            "De: Compras Enviado el: jueves Para: Proveedor Asunto: OC 61201 AGA "
            "Datos adjuntos: OC 61201.pdf"
        ),
        primary_kind="correspondencia",
    )
    daily = suggest_document_stem(
        path=r"C:\Reportes\x.pdf",
        title="",
        leading_text=(
            "FO-CIE-SS-01-R00 REPORTE DIARIO DE CAMPO. FECHA: 23-06-25 "
            "PROYECTO Malpaso CONTRATO DUSGEM-SERINTRA-02/23."
        ),
        primary_kind="reporte_actividades",
        organization="SERINTRA",
        topic="pruebas_electricas",
    )
    manual = suggest_document_stem(
        path=r"C:\Calidad\x.docx",
        title="",
        leading_text="FORMATO AC-F-02-02. MANUAL DE CALIDAD. Octubre 2021.",
        primary_kind="manual_sistema_gestion",
        organization="SEMIC",
    )
    travel = suggest_document_stem(
        path=r"C:\Gastos\x.docx",
        title="",
        leading_text=("14 de septiembre de 2023 Gracias por elegir Uber. Total 309,90 MXN."),
        primary_kind="comprobante_viaje",
    )

    assert email.stem == "Correspondencia - OC 61201 AGA"
    assert daily.stem == (
        "Reporte de actividades - FO-CIE-SS-01-R00 - Malpaso - 2025-06-23 "
        "- SERINTRA - pruebas electricas"
    )
    assert manual.stem == ("Manual del sistema de gestion - AC-F-02-02 - Octubre 2021 - SEMIC")
    assert travel.stem == "Comprobante de viaje - 2023-09-14 - 309,90 MXN"


def test_incidental_format_and_quote_words_do_not_define_the_document() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Consulta\Laboratorios para analisis de aceite.docx",
            "complete",
            leading_text=(
                "Laboratorios para análisis de aceite dieléctrico. Opciones para "
                "DGA y PCB. Confirmar el formato de entrega y solicitar cotización."
            ),
        )
    )

    assert classification.primary_kind == "referencia_tecnica"
    assert classification.primary_kind != "formato_empresa"
    assert classification.primary_kind != "cotizacion_propuesta"


def test_report_front_matter_overrides_template_and_contract_fields() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Reportes\07.- Reporte de actividades 28-09-2023.pdf",
            "done",
            author="SERINTRA",
            leading_text=(
                "Procedimiento Información documentada. Revisión No. 06. "
                "Proyecto: MALPASO Contrato/Licitación No. DUSGEM-SERINTRA-02/23 "
                "Obra: C.H. MALPASO Cliente: DUSGEM Fecha Del Informe: 28/09/2023 "
                "Realizado Por: Técnico Tipo De Informe: REPORTE FOTOGRÁFICO DE "
                "ACTIVIDADES Informe No.: 07 Página: 1 de 4."
            ),
            page_count=4,
        )
    )

    assert classification.primary_kind == "reporte_actividades"
    assert classification.confidence >= 0.82
    assert "MALPASO" in classification.suggested_stem
    assert "2023-09-28" in classification.suggested_stem
    assert "Procedimiento Información" not in classification.suggested_stem


@pytest.mark.parametrize(
    ("heading", "kind"),
    (
        (
            "Instructivo de trabajo para mantenimiento de cuchillas",
            "instructivo_trabajo",
        ),
        ("Hoja de inspección para desmonte y despalme", "formato_inspeccion"),
        ("Lista de verificación de pruebas eléctricas", "lista_verificacion"),
        ("Reporte de anomalías de transformadores", "reporte_anomalias"),
        ("Plan de atención y respuesta a emergencias", "plan_tecnico"),
        ("Control de asistencia del personal", "registro_asistencia"),
        ("Solicitud de gastos y viáticos", "viaticos_gastos"),
        ("Informe de análisis de condición del transformador", "informe_analisis"),
    ),
)
def test_specific_document_families_are_kept_separate(
    heading: str,
    kind: str,
) -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            rf"C:\Documentos\{heading}.docx",
            "complete",
            leading_text=heading,
        )
    )

    assert classification.primary_kind == kind
    assert classification.confidence >= 0.72


def test_quote_expenses_do_not_turn_the_quote_into_a_travel_record() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Cotizaciones\COT-VFV-001-2026.pdf",
            "done",
            leading_text=(
                "OFERTA COMERCIAL COT-VFV-001/2026. PROPUESTA ECONÓMICA. "
                "Concepto: supervisión, incluyendo gastos de viaje, viáticos y "
                "hospedaje. P. UNITARIO $98,000.00 SUBTOTAL $98,000.00 "
                "IVA $15,680.00 TOTAL CON IVA $113,680.00."
            ),
        )
    )

    assert classification.primary_kind == "cotizacion_propuesta"
    assert classification.confidence >= 0.82


def test_purchase_order_fields_override_the_generic_form_label() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Compras\FCOM02-03.pdf",
            "done",
            leading_text=(
                "Formato FCOM02-03. ORDEN DE COMPRA NO. BS-S-1-17. "
                "Proveedor: Distribuidora Industrial. Condiciones de pago: "
                "crédito. Fecha de entrega: 06-dic-19. Precio unitario."
            ),
        )
    )

    assert classification.primary_kind == "compra_requisicion"
    assert classification.confidence >= 0.82


@pytest.mark.parametrize(
    ("text", "kind"),
    (
        ("DOSSIER DE CALIDAD. Reporte de anomalías e inspecciones.", "dossier_calidad"),
        (
            "Acción: Correctiva. No. Control: 12. Tipo de no conformidad: "
            "Calidad. Descripción del hallazgo. Identificación de causa raíz.",
            "accion_correctiva_preventiva",
        ),
        (
            "OMICRON QuickCMC. Resultados de la prueba. Parámetros, tensión y corriente.",
            "reporte_resultados_pruebas",
        ),
        (
            "Nombre del Auditor Área Puesto Estado de Competencia. "
            "Lista de Auditores Internos FCAS04-04.",
            "registro_auditores",
        ),
        (
            "Nombre del empleado. EPP entregado. Entrega de EPP FCAS10-01.",
            "registro_entrega_epp",
        ),
        (
            "Fecha Hora Empresa. Incidencias y novedades FSL02-03.",
            "registro_incidencias",
        ),
        (
            "Proyecto Obra Visitante. Credencial para visitantes FSL02-07.",
            "credencial_visitante",
        ),
        (
            "Actividad responsable. Integración de la comisión de seguridad e "
            "higiene en obra. Año y mes.",
            "programa_seguridad_salud",
        ),
        (
            "Actividad, aspecto ambiental Objetivo Meta Responsables del cumplimiento Año mes.",
            "programa_gestion_ambiental",
        ),
    ),
)
def test_corpus_observed_controlled_document_families(
    text: str,
    kind: str,
) -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Recuperados\x_123.docx",
            "complete",
            leading_text=text,
        )
    )

    assert classification.primary_kind == kind
    assert classification.confidence >= 0.82


def test_controlled_record_name_prefers_type_code_and_revision_over_blank_fields() -> None:
    suggestion = suggest_document_stem(
        path=r"C:\Recuperados\x_16b.docx",
        title="",
        leading_text=(
            "No. Nombre del Auditor Área Puesto Estado de Competencia. "
            "Lista de Auditores Internos FCAS04-04 Abril 2025 Revisión 02."
        ),
        primary_kind="registro_auditores",
        organization="SERINTRA",
    )

    assert suggestion.stem == ("Registro de auditores internos - FCAS04-04 - Abril 2025 - SERINTRA")
    assert "Nombre del Auditor" not in suggestion.stem


def test_calibration_certificate_requires_dedicated_structural_evidence() -> None:
    certificate = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Calibracion\1. 98190720 Amperimetro de gancho.pdf",
            "done",
            author="Grupo de Metrología CLAM",
            leading_text=(
                "INFORME DE CALIBRACIÓN CALIBRATION CERTIFICATE "
                "Orden de Recepción: 00441-5 "
                "DATOS DEL INSTRUMENTO EN CALIBRACIÓN "
                "Descripción: AMPERIMETRO DE GANCHO Marca: FLUKE "
                "Modelo: 337 Serie: 98190720 "
                "Fecha de calibración: 2025 marzo 24 "
                "DATOS DEL PATRÓN DE REFERENCIA TRAZABILIDAD CENAM "
                "DATOS DE CALIBRACIÓN INCERTIDUMBRE EXPANDIDA"
            ),
            page_count=3,
        )
    )
    passing_mention = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Mediciones\poste 15.pdf",
            "done",
            leading_text=(
                "EQUIPO DE MEDICIÓN Marca Megabras Serie 22D0701. "
                "Certificado de calibración: 542758. "
                "Medición de resistencia del poste."
            ),
            page_count=1,
        )
    )

    assert certificate.primary_kind == "certificado_calibracion"
    assert certificate.confidence >= 0.82
    assert certificate.primary_organization == "GRUPO DE METROLOGÍA CLAM"
    assert "amperimetro" in certificate.suggested_stem.casefold()
    assert "98190720" in certificate.suggested_stem
    assert passing_mention.primary_kind != "certificado_calibracion"


def test_calibration_filename_recovers_a_certificate_with_missing_first_page_text() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Calibracion\Certificado de calibracion - CLAM-01328-23.pdf",
            "done",
            leading_text=(
                "Procedimiento de calibración y método empleado: CLAM-PC-01. "
                "Fecha de calibración: 2023 abril 04. Condiciones de calibración. "
                "Trazabilidad a patrones nacionales CENAM. Incertidumbre expandida."
            ),
            page_count=3,
        )
    )

    assert classification.primary_kind == "certificado_calibracion"
    assert classification.confidence >= 0.82


@pytest.mark.parametrize(
    ("text", "expected", "rejected"),
    (
        (
            "Descripción: AMPERIMETRO DE GANCHO E? 2 Eschia de callblacion "
            "2023 Male 24 Marca: FLUKE Modelo: 337 Serie: 98190720",
            "Amperimetro de gancho",
            "Eschia",
        ),
        (
            "Descripción: TTR (3 FASES) INSTRUMENT DATA UNDER CALIBRATIO! "
            "Marca: MEGGER : 4 Modelo: TSX300 Serie: 2300446",
            "Medidor de relacion de transformacion TTR",
            "CALIBRATIO",
        ),
        (
            "Descripción: DUCTER (MEDIDOR DE BAJA RESISTENCIA Y ALTA "
            "ARSRATION Marca: MEGGER Modelo: DLRO Serie: 101030599",
            "Medidor de baja resistencia (ducter)",
            "ARSRATION",
        ),
        (
            "Descripción: MEDIDOR DE RESISTENCIA DE AISLAMIENTO DE 10 kV "
            "Marca: MEGGER : 4 Modelo: �M�T1025 Serie: 102487432",
            "MIT1025",
            "�",
        ),
    ),
)
def test_calibration_semantic_names_normalize_observed_ocr_noise(
    text: str,
    expected: str,
    rejected: str,
) -> None:
    suggestion = suggest_document_stem(
        path=r"C:\Calibracion\x_1234abcd.pdf",
        title="",
        leading_text=text,
        primary_kind="certificado_calibracion",
    )

    assert expected in suggestion.stem
    assert rejected not in suggestion.stem


def test_calibration_control_register_is_not_a_certificate() -> None:
    classification = classify_document(
        DocumentSignals(
            "xlsx",
            r"C:\Orden final\0~d8666aaf.xlsx",
            "complete",
            leading_text=(
                "BITÁCORA Y CONTROL DE EQUIPOS DE INSPECCIÓN, MEDICIÓN Y PRUEBAS. "
                "Fecha de calibración. No. de certificado de calibración."
            ),
        )
    )

    assert classification.primary_kind == "control_metrologico"
    assert classification.confidence >= 0.72


def test_field_measurement_record_is_not_a_drawing_or_certificate() -> None:
    classification = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Mediciones\Documen~037c61da.pdf",
            "done",
            leading_text=(
                "FSYM08-02\nPágina 1 de 1\n"
                "MEDICIÓN DE LA RESISTENCIA EN SISTEMA DE TIERRAS "
                "EN LÍNEAS DE TRANSMISIÓN. Proyecto: Mantenimiento de línea "
                "de 115 kV Vitro. Elemento: Sistema de tierra de poste 31. "
                "Plano de referencia: ST-2241."
            ),
            page_count=1,
        )
    )

    assert classification.primary_kind == "registro_mediciones"
    assert classification.confidence >= 0.82
    assert classification.topics[0].label == "puesta_tierra"
    assert "resistencia" in classification.suggested_stem.casefold()
    assert "página 1 de 1" not in classification.suggested_stem.casefold()


def test_measurement_instruction_is_not_a_completed_field_record() -> None:
    classification = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Procedimientos\x_032.docx",
            "complete",
            leading_text=(
                "MEDICIÓN DE LA RESISTENCIA A TIERRA DE LA RED DE PUESTA A "
                "TIERRA. La medición se deberá realizar aplicando el método "
                "de caída de tensión. El medidor deberá contar con certificado "
                "de calibración vigente. Registrar los valores obtenidos."
            ),
        )
    )

    assert classification.primary_kind != "registro_mediciones"
    assert classification.primary_kind != "certificado_calibracion"


def test_appendix_and_photographic_column_do_not_override_primary_document() -> None:
    service_report = classify_document(
        DocumentSignals(
            "pdf",
            r"C:\Reportes\Service Work Report.pdf",
            "done",
            title="Service Work Report",
            leading_text=(
                "Service Work Report for SF6 maintenance. "
                "INFORME DE CALIBRACIÓN. INSTRUMENTO EN CALIBRACIÓN. "
                "TRAZABILIDAD. ORDEN DE RECEPCIÓN. DATOS DE CALIBRACIÓN."
            ),
            page_count=228,
        )
    )
    supply_list = classify_document(
        DocumentSignals(
            "docx",
            r"C:\Listas\Accesorios.docx",
            "complete",
            leading_text=(
                "TABLA DE ACCESORIOS A SUMINISTRAR. Descripción, cantidad y evidencia fotográfica."
            ),
        )
    )

    assert service_report.primary_kind == "informe_tecnico"
    assert supply_list.primary_kind != "registro_fotografico"


@pytest.mark.parametrize(
    ("signals", "kind", "organization"),
    (
        (
            DocumentSignals(
                "pdf",
                r"C:\03_PLANOS_DIAGRAMAS\Unifilar CYMI.pdf",
                "done",
                title="Diagrama unifilar de la subestación",
            ),
            "plano_diagrama",
            "CYMI",
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\Aceite_DGA_pruebas\Saavi.pdf",
                "done",
                title="Reporte DGA Saavi Energía",
                leading_text="Análisis de aceite dieléctrico por cromatografía de gases.",
            ),
            "reporte_laboratorio",
            "SAAVI ENERGÍA",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\Reportes_FAT\ANDRITZ.docx",
                "complete",
                title="FAT report ANDRITZ",
                leading_text="Factory acceptance test for the generator equipment.",
            ),
            "reporte_fat_sat",
            "ANDRITZ",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\STPS_DC3\ARBEIT.docx",
                "complete",
                title="Constancia de habilidades DC-3 ARBEIT Ingeniería",
            ),
            "constancia_capacitacion",
            "ARBEIT INGENIERÍA",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\Compras\CHINT.docx",
                "complete",
                title="Orden de compra CHINT",
            ),
            "compra_requisicion",
            "CHINT",
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\PEMEX\Informe de inspeccion.pdf",
                "done",
                title="Informe de inspección PEMEX",
            ),
            "informe_inspeccion",
            "PEMEX",
        ),
    ),
)
def test_index_driven_document_kinds_and_companies_are_classified(
    signals: DocumentSignals,
    kind: str,
    organization: str,
) -> None:
    classification = classify_document(signals)

    assert classification.primary_kind == kind
    assert classification.primary_organization == organization
    assert classification.confidence >= 0.72


# endregion [01]


# region [02] Incremental PDF/DOCX catalog


def _seed_pdf(
    state: Path,
    source: Path,
    text: str,
    *,
    title: str = "IEEE Std C37.20.2-2015",
) -> None:
    initialize_pdf_state(state)
    snapshot = snapshot_path(source)
    connection = sqlite3.connect(state)
    try:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            normalized_text_xxh3_128,metadata_json,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{snapshot.volume_id}:{snapshot.file_id}",
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "pdf-test-v1",
                "done",
                "pdf-text-xxh3-test",
                json.dumps({"title": title}),
                1,
            ),
        )
        connection.execute(
            """INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars)
            VALUES(?,?,?,?,?)""",
            (
                f"{snapshot.volume_id}:{snapshot.file_id}",
                0,
                "native",
                zlib.compress(text.encode("utf-8")),
                len(text),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_docx(
    state: Path,
    source: Path,
    text: str,
    *,
    title: str,
    author: str,
) -> None:
    initialize_docx_state(state)
    snapshot = snapshot_path(source)
    connection = sqlite3.connect(state)
    try:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{snapshot.volume_id}:{snapshot.file_id}",
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "docx-test-v1",
                "complete",
                "valid",
                zlib.compress(text.encode("utf-8")),
                len(text),
                "docx-text-xxh3-test",
                1,
                1,
                title,
                author,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_protected_organization_root_fails_before_catalog_creation(
    tmp_path: Path,
) -> None:
    protected_root = tmp_path / "read-only"
    protected_root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    catalog_path = state_directory / "document_catalog.sqlite3"
    guard = _protected_mutation_guard(tmp_path, protected_root)

    with pytest.raises(ProtectedContentError, match="protected_content_root"):
        plan_document_organization(
            catalog_path,
            protected_root,
            mutation_guard=guard,
        )

    assert not catalog_path.exists()


def test_planner_blocks_only_protected_source_and_keeps_no_destination(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    protected_root = tmp_path / "read-only"
    protected_root.mkdir()
    protected_source = protected_root / "IEEE protected.pdf"
    safe_source = tmp_path / "IEEE safe.pdf"
    protected_source.write_bytes(b"protected-pdf")
    safe_source.write_bytes(b"safe-pdf")
    pdf_state = state_directory / "pdf.sqlite3"
    _seed_pdf(
        pdf_state,
        protected_source,
        "IEEE Std C37.20.2-2015 protected switchgear",
    )
    _seed_pdf(
        pdf_state,
        safe_source,
        "IEEE Std C37.20.2-2015 safe switchgear",
    )
    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    destination_root = tmp_path / "organized"
    guard = _protected_mutation_guard(tmp_path, protected_root)

    summary = plan_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=guard,
    )
    plans = list_organization_plans(catalog_path, limit=10)
    by_source = {Path(plan.source_path): plan for plan in plans}

    assert summary.considered == 2
    assert summary.blocked == 1
    assert summary.planned == 1
    protected_plan = by_source[protected_source]
    safe_plan = by_source[safe_source]
    assert protected_plan.status == "blocked"
    assert protected_plan.reason == "protected_content_root"
    assert protected_plan.destination_path is None
    assert safe_plan.status == "planned"
    assert safe_plan.destination_path is not None
    assert protected_source.exists()
    assert safe_source.exists()
    assert not destination_root.exists()


def test_catalog_reads_existing_caches_and_reuses_classification(
    tmp_path: Path,
) -> None:
    pdf_source = tmp_path / "IEEE C37.20.2.pdf"
    docx_source = tmp_path / "Formato ANDRITZ.docx"
    pdf_source.write_bytes(b"pdf fixture")
    docx_source.write_bytes(b"docx fixture")
    _seed_pdf(
        tmp_path / "pdf.sqlite3",
        pdf_source,
        "IEEE Std C37.20.2-2015 switchgear circuit breaker",
    )
    _seed_docx(
        tmp_path / "docx.sqlite3",
        docx_source,
        "Formato ANDRITZ lista de verificación de pruebas eléctricas",
        title="Formato ANDRITZ",
        author="ANDRITZ HYDRO",
    )

    first = update_document_catalog(tmp_path)
    second = update_document_catalog(tmp_path)

    assert sum(item.classified for item in first) == 2
    assert sum(item.cache_hits for item in second) == 2
    catalog_path = tmp_path / "document_catalog.sqlite3"
    with document_catalog_database(catalog_path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT primary_kind,primary_authority,primary_organization,
            classifier_signature FROM documents ORDER BY path"""
        ).fetchall()
        history = connection.execute("SELECT COUNT(*) FROM classification_history").fetchone()[0]
    assert history == 2
    assert {row["primary_kind"] for row in rows} == {
        "normativa",
        "lista_verificacion",
    }
    assert any(row["primary_authority"] == "IEEE" for row in rows)
    assert any(row["primary_organization"] == "ANDRITZ" for row in rows)
    ieee = list_catalog_documents(
        catalog_path,
        limit=10,
        authority="IEEE",
    )
    assert len(ieee) == 1
    assert ieee[0].standard_identifiers == ("IEEE STD C37.20.2-2015",)
    assert ieee[0].primary_subtype == "norma"
    assert "interruptores_potencia" in ieee[0].equipment


def test_catalog_emits_real_time_bounded_progress(tmp_path: Path) -> None:
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    docx_database = tmp_path / "docx.sqlite3"
    _seed_docx(
        docx_database,
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    progress = RecordingProgress()

    summary = update_document_catalog_source(
        tmp_path / "document_catalog.sqlite3",
        docx_database,
        "docx",
        progress=progress,
        progress_operation="docx",
        verify_source_paths=False,
    )

    events = [event for event in progress.events if event.phase == "catalog-docx"]
    assert summary.classified == 1
    assert events[0].completed == 0
    assert events[0].total == 1
    assert not events[0].finished
    assert events[-1].completed == 1
    assert events[-1].total == 1
    assert events[-1].finished
    assert {metric.name: metric.value for metric in events[-1].metrics}["classified"] == 1


def test_catalog_records_corrupt_cached_text_as_classification_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        tmp_path / "docx.sqlite3",
        source,
        "Formato SERINTRA",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    connection = sqlite3.connect(tmp_path / "docx.sqlite3")
    try:
        connection.execute("UPDATE documents SET text_zlib=x'00010203'")
        connection.commit()
    finally:
        connection.close()

    _pdf, docx, *_office = update_document_catalog(tmp_path)

    assert docx.errors == 1
    with document_catalog_database(tmp_path / "document_catalog.sqlite3", readonly=True) as catalog:
        row = catalog.execute("SELECT catalog_status,error_type FROM documents").fetchone()
    assert row["catalog_status"] == "error"
    assert row["error_type"] == "error"


def test_direct_catalog_marks_stale_source_cache_without_reclassifying(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        tmp_path / "docx.sqlite3",
        source,
        "Formato SERINTRA",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    update_document_catalog(tmp_path)
    source.unlink()

    _pdf, docx, *_office = update_document_catalog(tmp_path)

    assert docx.source_stale == 1
    assert docx.classified == 0
    assert docx.stale_marked == 1
    with document_catalog_database(tmp_path / "document_catalog.sqlite3", readonly=True) as catalog:
        active = catalog.execute("SELECT active FROM documents").fetchone()[0]
    assert active == 0


def test_catalog_schema_migration_preserves_classification_history(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "document_catalog.sqlite3"
    connection = sqlite3.connect(catalog_path)
    try:
        catalog_schema_module._migrate_to_v1(connection)
        catalog_schema_module._set_schema_version(connection, 1)
        connection.execute(
            """INSERT INTO classification_history VALUES(
            'pdf','1:2','source-v1','text-v1','classifier-v1',
            'C:\\Normativa\\IEEE.pdf','{}',1)"""
        )
        connection.commit()
    finally:
        connection.close()

    initialize_document_catalog(catalog_path)

    with document_catalog_database(catalog_path, readonly=True) as migrated:
        version = migrated.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        history = migrated.execute("SELECT path FROM classification_history").fetchall()
        primary_key = tuple(
            row[1]
            for row in migrated.execute("PRAGMA table_info(classification_history)")
            if row[5]
        )
        organization_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(organization_plans)")
        }
        document_columns = {row[1] for row in migrated.execute("PRAGMA table_info(documents)")}
    assert version == "6"
    assert [row["path"] for row in history] == [r"C:\Normativa\IEEE.pdf"]
    assert primary_key[-1] == "path"
    assert {
        "move_completed_ns",
        "cache_sync_status",
        "cache_sync_json",
        "cache_sync_error",
    }.issubset(organization_columns)
    assert {
        "primary_subtype",
        "primary_client",
        "primary_project",
        "primary_workstream",
        "equipment_json",
        "activities_json",
        "clients_json",
        "projects_json",
        "workstreams_json",
    }.issubset(document_columns)


def test_readonly_catalog_preview_accepts_unmigrated_v3_schema(tmp_path: Path) -> None:
    catalog_path = tmp_path / "legacy_catalog.sqlite3"
    with sqlite3.connect(catalog_path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                active INTEGER NOT NULL,
                source_kind TEXT NOT NULL,
                path TEXT NOT NULL,
                primary_kind TEXT NOT NULL,
                primary_authority TEXT,
                primary_organization TEXT,
                standard_references_json TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                uncertainty TEXT NOT NULL,
                catalog_status TEXT NOT NULL
            );
            INSERT INTO documents VALUES(
                1,'pdf','C:\\Normativa\\IEC 60076-1.pdf','normativa','IEC',NULL,
                '[{"identifier":"IEC 60076-1:2011"}]',
                '[{"label":"transformadores"}]',0.96,'baja','classified'
            );
            """
        )

    documents = list_catalog_documents(catalog_path, limit=10)

    assert len(documents) == 1
    assert documents[0].primary_authority == "IEC"
    assert documents[0].primary_subtype is None
    assert documents[0].equipment == ()
    assert documents[0].activities == ()


def test_catalog_v2_migration_normalizes_hex_identity_fields(tmp_path: Path) -> None:
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    catalog_path = tmp_path / "document_catalog.sqlite3"
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
    volume_hex, file_hex = file_key.split(":", 1)
    connection = sqlite3.connect(catalog_path)
    try:
        catalog_schema_module._migrate_to_v1(connection)
        catalog_schema_module._migrate_to_v2(connection)
        catalog_schema_module._set_schema_version(connection, 2)
        connection.execute(
            """INSERT INTO documents(
            source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
            source_status,processing_signature,text_fingerprint,classifier_signature,
            primary_kind,primary_authority,primary_organization,confidence,uncertainty,
            standard_references_json,organizations_json,topics_json,
            classification_json,catalog_status,active,last_seen_catalog_run_id,updated_ns)
            VALUES('docx',?,?,?,?,?,?,?,'complete','source-v1','text-v1',
            'classifier-v1','otro',NULL,'SERINTRA',0.8,'media','[]','[]','[]','{}',
            'classified',1,1,1)""",
            (
                file_key,
                str(source),
                volume_hex,
                file_hex,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
            ),
        )
        connection.execute(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,evidence_json,
            planned_ns)
            VALUES(NULL,'docx',?,?,NULL,?,?,?,?,?,?,'classifier-v1','otro',0.8,
            'planned','fixture','{}',1)""",
            (
                file_key,
                str(source),
                str(tmp_path / "organizados"),
                volume_hex,
                file_hex,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    initialize_document_catalog(catalog_path)

    with document_catalog_database(catalog_path, readonly=True) as migrated:
        document_identity = migrated.execute("SELECT volume_id,file_id FROM documents").fetchone()
        plan_identity = migrated.execute(
            "SELECT volume_id,file_id FROM organization_plans"
        ).fetchone()
    expected = (str(snapshot.volume_id), str(snapshot.file_id))
    assert tuple(document_identity) == expected
    assert tuple(plan_identity) == expected


# endregion [02]


# region [03] Safe organization planning and application


def _seed_semantic_document(
    state: Path,
    *,
    source_kind: str,
    file_key: str,
    path: Path,
) -> tuple[str, str]:
    initialize_semantic_state(state)
    item_id = f"item:{source_kind}:{file_key}"
    chunk_id = f"chunk:{source_kind}:{file_key}:0"
    text = "contenido semántico de prueba"
    upsert_semantic_item(
        state,
        SemanticItem(
            item_id=item_id,
            source_kind=source_kind,
            source_identity=file_key,
            identity_version="organization-test-v1",
            fingerprint=fingerprint_text(f"descriptor:{source_kind}:{file_key}"),
            path=str(path),
        ),
        refresh_token="items-r1",
        updated_ns=10,
    )
    stage_text_chunks(
        state,
        (
            TextChunk(
                chunk_id=chunk_id,
                item_id=item_id,
                ordinal=0,
                section_kind="body",
                section_id="body",
                start_char=0,
                end_char=len(text),
                text=text,
                fingerprint=fingerprint_text(text),
                chunking_signature="organization-test-chunks-v1",
            ),
        ),
        refresh_token="chunks-r1",
        updated_ns=11,
    )
    finalize_text_chunk_refresh(
        state,
        item_id=item_id,
        chunking_signature="organization-test-chunks-v1",
        refresh_token="chunks-r1",
        updated_ns=12,
    )
    return item_id, chunk_id


def _publish_semantic_document_hit(state: Path, chunk_id: str) -> SearchHit:
    model = EmbeddingModelSpec(
        "organization-test-model-v1",
        "organization-test-space-v1",
        EmbeddingModality.TEXT,
        "fixture/organization-test-model",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    register_embedding_model(state, model, allow_test_provider=True)
    generation_id = start_embedding_generation(
        state,
        model_signature=model.model_signature,
        processing_signature="organization-test-generation-v1",
        started_ns=13,
    )
    enqueue_text_chunk_jobs(state, generation_id, (chunk_id,), now_ns=14)
    lease = claim_embedding_jobs(
        state,
        generation_id,
        worker_id="organization-test-worker",
        limit=1,
        lease_seconds=60,
        now_ns=15,
    )[0]
    complete_embedding_job(
        state,
        lease.job_id,
        worker_id="organization-test-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=16,
    )
    finalize_embedding_generation(state, generation_id, completed_ns=17)
    page = search_exact_page(
        state,
        ExactSearchQuery(
            model.model_signature,
            model.vector_space,
            model.dimensions,
            (1.0, 0.0, 0.0, 0.0),
            EmbeddingModality.TEXT,
            indexed_model_signatures=(model.model_signature,),
        ),
    )
    assert len(page.hits) == 1
    return page.hits[0]


def test_plan_organizes_standards_and_company_formats_without_moving(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    pdf_source = tmp_path / "IEEE C37.20.2.pdf"
    docx_source = tmp_path / "Formato SERINTRA.docx"
    reference_source = tmp_path / "Referencia transformadores.docx"
    pdf_source.write_bytes(b"pdf fixture")
    docx_source.write_bytes(b"docx fixture")
    reference_source.write_bytes(b"reference fixture")
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        pdf_source,
        "IEEE Std C37.20.2-2015 switchgear",
    )
    _seed_docx(
        state_directory / "docx.sqlite3",
        docx_source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    _seed_docx(
        state_directory / "docx.sqlite3",
        reference_source,
        "Referencia técnica sobre transformadores de potencia",
        title="Referencia técnica de transformadores",
        author="",
    )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"

    summary = plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    plans = list_organization_plans(
        state_directory / "document_catalog.sqlite3",
        limit=10,
        status="planned",
    )

    assert summary.planned == 3
    assert pdf_source.exists()
    assert docx_source.exists()
    assert not destination_root.exists()
    destinations = {Path(plan.destination_path) for plan in plans if plan.destination_path}
    normative_destinations = {
        destination
        for destination in destinations
        if destination.parent == destination_root / "Normativa" / "IEEE"
    }
    assert {destination.name for destination in normative_destinations} == {
        "IEEE STD C37.20.2-2015 - switchgear.pdf"
    }
    assert (
        destination_root
        / "Empresas"
        / "SERINTRA"
        / "Gestion_y_administracion"
        / "Formatos_y_registros"
        / docx_source.name
        in destinations
    )
    assert (
        destination_root
        / "Ingenieria_y_documentacion"
        / "Informes_y_referencias"
        / reference_source.name
        in destinations
    )


def test_plan_routes_malpaso_work_to_andritz_while_norms_stay_separate(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    pressure_source = tmp_path / "Pressure inspection MALPASO-HCN-05.pdf"
    packing_source = tmp_path / "MALPASO-HCN-04 Packing List Report.pdf"
    delivery_source = tmp_path / "Delivery Report Project LH-MALPASO.pdf"
    sample_label_source = tmp_path / "TR-08 CEE Cromatografia.pdf"
    standard_source = tmp_path / "ASTM D1816 ANDRITZ Malpaso.pdf"
    for source in (
        pressure_source,
        packing_source,
        delivery_source,
        sample_label_source,
        standard_source,
    ):
        source.write_bytes(b"pdf fixture")
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        pressure_source,
        "Project FIEL/10670-005/2021 Repowering and Modernization. Malpaso "
        "Hydroelectric Power Plant. INTERNAL PRESSURE INSPECTION REPORT. "
        "Shipment No MALPASO-HCN-05-001. Transformer Unit 2 Phase A. "
        "Pressure Reading 0.05 MPa.",
        title="Pressure inspection report MALPASO-HCN-05",
    )
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        packing_source,
        "ANDRITZ (China) Ltd. Packing List. Place of Delivery. Project. "
        "Package No MALPASO-HCN-04-001. Shipment No MALPASO-HCN-04. "
        "Type of Packing Wooden Box. Gross Weight. Net Weight. Storage "
        "Instructions. Designation of Contents: Transformer main body.",
        title="MALPASO-HCN-04 Packing List Report",
    )
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        delivery_source,
        "Project: LH-MALPASO. Delivery Report. Materials received. Shipment "
        "No MALPASO-HCN-02. Package No MALPASO-HCN-02-029. Delivery Date. "
        "Storage Area. Storage Position. Inspection: Pending.",
        title="Delivery Report Project LH-MALPASO",
    )
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        sample_label_source,
        "CEE / CROMATOGRAFIA Jeringa 50 mL C.H. MALPASO TR-08 | U1-FASE A "
        "NS 10184386-08 | HYOSUNG 45/60/75 MVA | 400/3/15 kV | CEE | "
        "Fecha __________",
        title="TR-08 CEE Cromatografia",
    )
    _seed_pdf(
        state_directory / "pdf.sqlite3",
        standard_source,
        "ANDRITZ project Malpaso. Designation: D1816. Standard Test Method "
        "for Dielectric Breakdown Voltage of Insulating Liquids Using VDE "
        "Electrodes. This standard is issued under the fixed designation "
        "D1816. ASTM Standards.",
        title="ASTM D1816 ANDRITZ Malpaso",
    )

    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    malpaso = list_catalog_documents(catalog_path, limit=10, project="Malpaso")
    pressure = list_catalog_documents(
        catalog_path,
        limit=10,
        client="ANDRITZ",
        workstream="control_presion_unidades",
    )
    packing = list_catalog_documents(
        catalog_path,
        limit=10,
        client="ANDRITZ",
        workstream="embarques_hcn",
    )
    samples = list_catalog_documents(
        catalog_path,
        limit=10,
        client="ANDRITZ",
        workstream="muestreo_aceite_transformadores",
    )
    destination_root = tmp_path / "organizados"
    summary = plan_document_organization(catalog_path, destination_root)
    plans = list_organization_plans(catalog_path, limit=10, status="planned")
    destinations = {
        Path(plan.source_path).name: Path(plan.destination_path)
        for plan in plans
        if plan.destination_path
    }

    assert len(malpaso) == 5
    assert len(pressure) == 1
    assert pressure[0].primary_project == "Malpaso"
    assert pressure[0].clients == ("ANDRITZ",)
    assert pressure[0].workstreams[0] == "control_presion_unidades"
    assert {document.primary_kind for document in packing} == {
        "lista_empaque_embarque",
        "reporte_entrega_embarque",
    }
    assert len(samples) == 1
    assert samples[0].primary_kind == "etiqueta_muestra_laboratorio"
    assert summary.planned == 5
    assert destinations[pressure_source.name].parent == (
        destination_root / "Clientes" / "ANDRITZ" / "Malpaso" / "Presion_de_unidades"
    )
    assert destinations[packing_source.name].parent == (
        destination_root / "Clientes" / "ANDRITZ" / "Malpaso" / "Embarques_HCN"
    )
    assert destinations[delivery_source.name].parent == (
        destination_root / "Clientes" / "ANDRITZ" / "Malpaso" / "Embarques_HCN"
    )
    assert destinations[sample_label_source.name].parent == (
        destination_root / "Clientes" / "ANDRITZ" / "Malpaso" / "Analisis_de_aceite"
    )
    standard_destination = destinations[standard_source.name]
    assert standard_destination.is_relative_to(destination_root / "Normativa" / "ASTM")
    assert not standard_destination.is_relative_to(destination_root / "Clientes")


def test_plan_uses_andritz_company_as_account_root_without_changing_client_role(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato de inspeccion ANDRITZ.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato de inspección emitido por ANDRITZ HYDRO. Equipo, fecha, "
        "resultado, observaciones y firma del inspector.",
        title="Formato de inspeccion",
        author="ANDRITZ HYDRO",
    )

    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    documents = list_catalog_documents(
        catalog_path,
        limit=10,
        organization="ANDRITZ",
    )
    destination_root = tmp_path / "organizados"
    summary = plan_document_organization(catalog_path, destination_root)
    plans = list_organization_plans(catalog_path, limit=10, status="planned")

    assert len(documents) == 1
    assert documents[0].primary_client is None
    assert summary.planned == 1
    destination = Path(plans[0].destination_path or "")
    relative = destination.relative_to(destination_root)
    assert relative.parts[:3] == ("Clientes", "ANDRITZ", "General")
    assert relative.parts.count("ANDRITZ") == 1


def test_plan_routes_observed_quality_safety_and_test_records(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixtures = (
        (
            tmp_path / "x_action.docx",
            "Acción: Correctiva. Tipo de no conformidad: Calidad. "
            "Descripción del hallazgo. Identificación de causa raíz.",
            ("Pruebas_y_calidad", "Calidad"),
        ),
        (
            tmp_path / "x_epp.docx",
            "Nombre del empleado. EPP entregado. Entrega de EPP FCAS10-01.",
            ("Seguridad_y_ambiente", "Seguridad"),
        ),
        (
            tmp_path / "x_test.docx",
            "OMICRON QuickCMC. Resultados de la prueba. Parámetros de tensión "
            "y corriente para transformador.",
            (
                "Empresas",
                "OMICRON",
                "Pruebas_y_calidad",
                "Pruebas_y_resultados",
            ),
        ),
    )
    for source, text, _expected_parent in fixtures:
        source.write_bytes(b"docx fixture")
        _seed_docx(
            state_directory / "docx.sqlite3",
            source,
            text,
            title="",
            author="",
        )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"

    summary = plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    plans = list_organization_plans(
        state_directory / "document_catalog.sqlite3",
        limit=10,
        status="planned",
    )

    assert summary.planned == len(fixtures)
    destination_parents = {Path(plan.destination_path or "").parent for plan in plans}
    for _source, _text, expected_parent in fixtures:
        assert destination_root.joinpath(*expected_parent) in destination_parents


def test_plan_routes_second_pass_families_and_reviews_sensitive_artifacts(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixtures = (
        (
            "manual.docx",
            "FORMATO AC-F-02-02. MANUAL DE CALIDAD. Sistema de calidad.",
        ),
        (
            "system.docx",
            "Descripción General Sistema de Excitación THYNE500. Lazos de control.",
        ),
        (
            "assignment.docx",
            "HOJA DE ASIGNACIÓN DE PROYECTO. Alcance y responsable.",
        ),
        (
            "travel.docx",
            "Gracias por elegir Uber. UberX 6.34 kilómetros. Total 309.90 MXN.",
        ),
        (
            "bank.docx",
            "CARTA INSTRUCCIÓN PARA REGISTRO DE CUENTA BANCARIA personas morales.",
        ),
        (
            "inventory.docx",
            "Reporte de archivo: tool.dll Ruta relativa: .Toolbox/bin Timestamp (UTC): 2025-10-09.",
        ),
    )
    for filename, text in fixtures:
        source = tmp_path / filename
        source.write_bytes(b"docx fixture")
        _seed_docx(
            state_directory / "docx.sqlite3",
            source,
            text,
            title="",
            author="",
        )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"

    summary = plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    planned = list_organization_plans(
        state_directory / "document_catalog.sqlite3",
        limit=20,
        status="planned",
    )
    review = list_organization_plans(
        state_directory / "document_catalog.sqlite3",
        limit=20,
        status="review",
    )

    assert summary.planned == 4
    assert summary.review_required == 2
    assert len(review) == 2
    parents = {Path(item.destination_path or "").parent for item in planned}
    assert destination_root / "Pruebas_y_calidad" / "Calidad" in parents
    assert destination_root / "Ingenieria_y_documentacion" / "Ingenieria_y_calculos" in parents
    assert destination_root / "Gestion_y_administracion" / "Proyecto_y_correspondencia" in parents
    assert destination_root / "Gestion_y_administracion" / "Administracion" in parents
    assert {item.reason for item in review} == {
        "financial_or_sensitive_document_requires_review",
        "generated_file_inventory_report_requires_review",
    }


def test_plan_semantically_renames_low_quality_calibration_certificate(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "20~a55e6fcf.docx"
    source.write_bytes(b"certificate fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        (
            "INFORME DE CALIBRACIÓN CALIBRATION CERTIFICATE "
            "Orden de Recepción: 01481-5 "
            "DATOS DEL INSTRUMENTO EN CALIBRACIÓN "
            "Descripción: MEDIDOR DE RELACION Marca: MEGGER "
            "Modelo: TTRU3 Serie: 1701155 "
            "Fecha de calibración: 2025-03-24 "
            "PATRÓN DE REFERENCIA TRAZABILIDAD CENAM "
            "DATOS DE CALIBRACIÓN INCERTIDUMBRE EXPANDIDA"
        ),
        title="",
        author="Grupo de Metrología CLAM",
    )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"

    summary = plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    plans = list_organization_plans(
        state_directory / "document_catalog.sqlite3",
        limit=10,
        status="planned",
    )

    assert summary.planned == 1
    assert source.exists()
    destination = Path(plans[0].destination_path or "")
    assert destination.parent == (
        destination_root
        / "Empresas"
        / "GRUPO DE METROLOGÍA CLAM"
        / "Pruebas_y_calidad"
        / "Laboratorio_y_metrologia"
    )
    assert destination.name.startswith("Certificado de calibracion - MEDIDOR DE RELACION - MEGGER")
    assert destination.suffix == ".docx"


@WINDOWS_MUTATION_ONLY
def test_plan_relocates_only_framework_managed_misclassification_to_review(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    destination_root = tmp_path / "organizados"
    plan_document_organization(catalog_path, destination_root)
    applied = apply_all_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
    )
    assert applied.applied == 1

    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            """UPDATE documents SET primary_kind='otro',confidence=0.35,
            uncertainty='alta',catalog_status='review',
            classification_json='{}' WHERE active=1"""
        )

    correction = plan_document_organization(catalog_path, destination_root)
    planned = list_organization_plans(catalog_path, limit=10, status="planned")

    assert correction.planned == 1
    assert len(planned) == 1
    destination = Path(planned[0].destination_path or "")
    assert destination.is_relative_to(destination_root / "Revision_pendiente")
    assert planned[0].reason.startswith("managed_reclassification:")


@WINDOWS_MUTATION_ONLY
def test_plan_disambiguates_same_named_documents_without_blocking(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    left_directory = tmp_path / "left"
    right_directory = tmp_path / "right"
    left_directory.mkdir()
    right_directory.mkdir()
    sources = (
        left_directory / "Formato SERINTRA.docx",
        right_directory / "Formato SERINTRA.docx",
    )
    for index, source in enumerate(sources):
        source.write_bytes(f"docx fixture {index}".encode())
        _seed_docx(
            state_directory / "docx.sqlite3",
            source,
            "Formato SERINTRA formulario de control",
            title="Formato SERINTRA",
            author="SERINTRA",
        )
    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    destination_root = tmp_path / "organizados"

    plan = plan_document_organization(catalog_path, destination_root)
    planned = list_organization_plans(catalog_path, limit=10, status="planned")

    assert plan.planned == 2
    assert plan.blocked == 0
    destinations = {item.destination_path for item in planned}
    assert len(destinations) == 2
    assert any("identity_disambiguation" in item.reason for item in planned)

    applied = apply_all_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
    )

    assert applied.applied == 2
    assert applied.blocked == 0
    assert all(not source.exists() for source in sources)
    assert all(Path(destination).is_file() for destination in destinations if destination)


@WINDOWS_MUTATION_ONLY
def test_apply_moves_only_snapshot_valid_plan_without_overwrite(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"
    plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )

    summary = apply_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )
    destination = (
        destination_root
        / "Empresas"
        / "SERINTRA"
        / "Gestion_y_administracion"
        / "Formatos_y_registros"
        / source.name
    )

    assert summary.applied == 1
    assert destination_root.is_dir()
    assert not source.exists()
    assert destination.read_bytes() == b"docx fixture"
    assert summary.cache_synced == 1
    assert summary.cache_pending == 0


@WINDOWS_MUTATION_ONLY
def test_apply_all_consumes_every_plan_in_bounded_batches(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    sources = tuple(tmp_path / f"Formato SERINTRA {index}.docx" for index in range(3))
    for source in sources:
        source.write_bytes(f"fixture {source.stem}".encode())
        _seed_docx(
            state_directory / "docx.sqlite3",
            source,
            "Formato SERINTRA formulario de control",
            title="Formato SERINTRA",
            author="SERINTRA",
        )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"
    plan_progress = RecordingProgress()
    plan = plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
        progress=plan_progress,
    )

    apply_progress = RecordingProgress()
    summary = apply_all_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        batch_size=1,
        progress=apply_progress,
    )

    assert plan.planned == 3
    assert summary.applied == 3
    assert summary.cache_synced == 3
    assert summary.batches == 3
    assert summary.remaining == 0
    assert plan_progress.events[0].completed == 0
    assert plan_progress.events[-1].completed == 3
    assert plan_progress.events[-1].finished
    assert apply_progress.events[0].completed == 0
    assert [event.completed for event in apply_progress.events if not event.finished][-3:] == [
        1,
        2,
        3,
    ]
    assert apply_progress.events[-1].completed == 3
    assert apply_progress.events[-1].finished
    for source in sources:
        destination = (
            destination_root
            / "Empresas"
            / "SERINTRA"
            / "Gestion_y_administracion"
            / "Formatos_y_registros"
            / source.name
        )
        assert not source.exists()
        assert destination.is_file()


@WINDOWS_MUTATION_ONLY
def test_apply_synchronizes_current_and_pending_path_caches(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    connection = sqlite3.connect(state_directory / "docx.sqlite3")
    try:
        connection.execute(
            """INSERT INTO docx_inventory(
            file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
            VALUES(?,?,?,?,?,?)""",
            (
                file_key,
                str(source),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO document_fts(file_key,path,title,author,body) VALUES(?,?,?,?,?)",
            (file_key, str(source), "Formato SERINTRA", "SERINTRA", "contenido"),
        )
        connection.commit()
    finally:
        connection.close()
    framework = sqlite3.connect(state_directory / "framework.sqlite3")
    try:
        framework.executescript(
            """
            CREATE TABLE initial_runs(run_id INTEGER PRIMARY KEY,status TEXT NOT NULL);
            CREATE TABLE route_candidates(
                run_id INTEGER NOT NULL,path TEXT NOT NULL,volume_id TEXT NOT NULL,
                file_id TEXT NOT NULL,PRIMARY KEY(run_id,path)
            );
            CREATE TABLE review_candidates(
                path TEXT NOT NULL,volume_id TEXT NOT NULL,file_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE file_actions(
                action_id INTEGER PRIMARY KEY,action_type TEXT NOT NULL,
                source_path TEXT NOT NULL,target_path TEXT,status TEXT NOT NULL
            );
            """
        )
        framework.execute("INSERT INTO initial_runs VALUES(1,'running')")
        framework.execute(
            "INSERT INTO route_candidates VALUES(?,?,?,?)",
            (1, str(source), str(snapshot.volume_id), str(snapshot.file_id)),
        )
        framework.execute(
            "INSERT INTO review_candidates VALUES(?,?,?,?)",
            (str(source), str(snapshot.volume_id), str(snapshot.file_id), "open"),
        )
        framework.execute(
            "INSERT INTO file_actions VALUES(1,'correct_extension',?,?,?)",
            (str(source), str(source.with_suffix(".pdf")), "planned"),
        )
        framework.execute(
            "INSERT INTO file_actions VALUES(2,'trash_duplicate',?,?,?)",
            (str(tmp_path / "other.docx"), str(source), "planned"),
        )
        framework.commit()
    finally:
        framework.close()
    identity = (
        snapshot.volume_id.to_bytes(16, "little"),
        snapshot.file_id.to_bytes(16, "little"),
    )
    dedup = sqlite3.connect(state_directory / "dedup.sqlite3")
    try:
        dedup.executescript(
            """
            CREATE TABLE files(
                path TEXT PRIMARY KEY COLLATE NOCASE,volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL
            );
            CREATE TABLE planned_duplicate_groups(
                group_id INTEGER PRIMARY KEY,keep_path TEXT NOT NULL
            );
            CREATE TABLE planned_duplicate_members(
                group_id INTEGER NOT NULL,role TEXT NOT NULL,path TEXT NOT NULL,
                volume_id BLOB NOT NULL,file_id BLOB NOT NULL
            );
            """
        )
        dedup.execute("INSERT INTO files VALUES(?,?,?)", (str(source), *identity))
        dedup.execute("INSERT INTO planned_duplicate_groups VALUES(1,?)", (str(source),))
        dedup.execute(
            "INSERT INTO planned_duplicate_members VALUES(1,'keep',?,?,?)",
            (str(source), *identity),
        )
        dedup.commit()
    finally:
        dedup.close()
    semantic_item_id, semantic_chunk_id = _seed_semantic_document(
        state_directory / "semantic.sqlite3",
        source_kind="docx",
        file_key=file_key,
        path=source,
    )
    semantic_hit = _publish_semantic_document_hit(
        state_directory / "semantic.sqlite3",
        semantic_chunk_id,
    )

    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"
    plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    summary = apply_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )
    destination = (
        destination_root
        / "Empresas"
        / "SERINTRA"
        / "Gestion_y_administracion"
        / "Formatos_y_registros"
        / source.name
    )

    assert summary.applied == 1
    assert summary.cache_synced == 1
    with document_catalog_database(
        state_directory / "document_catalog.sqlite3", readonly=True
    ) as catalog:
        sync_payload = json.loads(
            str(catalog.execute("SELECT cache_sync_json FROM organization_plans").fetchone()[0])
        )
    assert next(
        result for result in sync_payload["databases"] if result["database"] == "semantic"
    ) == {
        "database": "semantic",
        "detail": None,
        "status": "synced",
        "updated_rows": 1,
    }
    docx = sqlite3.connect(state_directory / "docx.sqlite3")
    framework = sqlite3.connect(state_directory / "framework.sqlite3")
    dedup = sqlite3.connect(state_directory / "dedup.sqlite3")
    semantic = sqlite3.connect(state_directory / "semantic.sqlite3")
    try:
        assert docx.execute("SELECT path FROM documents").fetchone()[0] == str(destination)
        assert docx.execute("SELECT path FROM docx_inventory").fetchone()[0] == str(destination)
        assert docx.execute("SELECT path FROM document_fts").fetchone()[0] == str(destination)
        assert framework.execute("SELECT path FROM route_candidates").fetchone()[0] == str(
            destination
        )
        assert framework.execute("SELECT path FROM review_candidates").fetchone()[0] == str(
            destination
        )
        corrected = framework.execute(
            "SELECT source_path,target_path FROM file_actions WHERE action_id=1"
        ).fetchone()
        referenced = framework.execute(
            "SELECT target_path FROM file_actions WHERE action_id=2"
        ).fetchone()[0]
        assert corrected == (str(destination), str(destination.with_suffix(".pdf")))
        assert referenced == str(destination)
        assert dedup.execute("SELECT path FROM files").fetchone()[0] == str(destination)
        assert dedup.execute("SELECT path FROM planned_duplicate_members").fetchone()[0] == str(
            destination
        )
        assert dedup.execute("SELECT keep_path FROM planned_duplicate_groups").fetchone()[0] == str(
            destination
        )
        semantic_path, semantic_updated_ns = semantic.execute(
            "SELECT path,updated_ns FROM semantic_items WHERE item_id=?",
            (semantic_item_id,),
        ).fetchone()
        assert semantic_path == str(destination)
        assert semantic_updated_ns > 12
        assert semantic.execute(
            "SELECT path FROM semantic_item_revisions WHERE item_id=?",
            (semantic_item_id,),
        ).fetchone()[0] == str(source)
    finally:
        docx.close()
        framework.close()
        dedup.close()
        semantic.close()

    resolved = resolve_search_hits(
        state_directory / "semantic.sqlite3",
        (semantic_hit,),
    )
    assert resolved[0].path == str(destination)
    assert resolved[0].path != str(source)

    retry = synchronize_moved_document(
        state_directory,
        source_kind="docx",
        file_key=file_key,
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(snapshot.volume_id),
        file_id=str(snapshot.file_id),
    )
    semantic_retry = next(result for result in retry.databases if result.database == "semantic")
    assert retry.complete
    assert semantic_retry.status == "synced"
    assert semantic_retry.updated_rows == 0
    with sqlite3.connect(state_directory / "semantic.sqlite3") as semantic:
        assert semantic.execute(
            "SELECT path,updated_ns FROM semantic_items WHERE item_id=?",
            (semantic_item_id,),
        ).fetchone() == (str(destination), semantic_updated_ns)


@WINDOWS_MUTATION_ONLY
def test_apply_reports_unexpected_semantic_path_without_rewriting_it(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    unexpected_path = tmp_path / "ruta_ajena" / source.name
    item_id, _ = _seed_semantic_document(
        state_directory / "semantic.sqlite3",
        source_kind="docx",
        file_key=file_key,
        path=unexpected_path,
    )
    update_document_catalog(state_directory)
    catalog_path = state_directory / "document_catalog.sqlite3"
    destination_root = tmp_path / "organizados"
    plan_document_organization(catalog_path, destination_root)

    first = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )
    destination = (
        destination_root
        / "Empresas"
        / "SERINTRA"
        / "Gestion_y_administracion"
        / "Formatos_y_registros"
        / source.name
    )

    assert first.applied == 0
    assert first.cache_synced == 0
    assert first.cache_pending == 1
    assert not source.exists()
    assert destination.is_file()
    with sqlite3.connect(state_directory / "docx.sqlite3") as docx:
        assert docx.execute("SELECT path FROM documents WHERE file_key=?", (file_key,)).fetchone()[
            0
        ] == str(destination)
    with sqlite3.connect(state_directory / "semantic.sqlite3") as semantic:
        assert semantic.execute(
            "SELECT path FROM semantic_items WHERE item_id=?", (item_id,)
        ).fetchone()[0] == str(unexpected_path)
    with document_catalog_database(catalog_path, readonly=True) as catalog:
        plan = catalog.execute(
            "SELECT status,cache_sync_status,cache_sync_json FROM organization_plans"
        ).fetchone()
    sync_payload = json.loads(str(plan["cache_sync_json"]))
    semantic_result = next(
        result for result in sync_payload["databases"] if result["database"] == "semantic"
    )
    assert tuple(plan)[:2] == ("moved_cache_pending", "pending")
    assert semantic_result["status"] == "error"
    assert "neither source nor destination" in semantic_result["detail"]

    second = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )

    assert second.applied == 0
    assert second.cache_pending == 1
    with sqlite3.connect(state_directory / "docx.sqlite3") as docx:
        assert docx.execute("SELECT path FROM documents WHERE file_key=?", (file_key,)).fetchone()[
            0
        ] == str(destination)
    with sqlite3.connect(state_directory / "semantic.sqlite3") as semantic:
        assert semantic.execute(
            "SELECT path FROM semantic_items WHERE item_id=?", (item_id,)
        ).fetchone()[0] == str(unexpected_path)


@WINDOWS_MUTATION_ONLY
def test_apply_recovers_cache_sync_after_database_becomes_available(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    docx_database = state_directory / "docx.sqlite3"
    _seed_docx(
        docx_database,
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"
    catalog_path = state_directory / "document_catalog.sqlite3"
    plan_document_organization(catalog_path, destination_root)
    unavailable = state_directory / "docx.sqlite3.unavailable"
    docx_database.replace(unavailable)

    first = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )

    assert first.applied == 0
    assert first.cache_pending == 1
    assert not source.exists()
    unavailable.replace(docx_database)
    second = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )

    assert second.applied == 1
    assert second.cache_synced == 1
    with document_catalog_database(catalog_path, readonly=True) as catalog:
        plan = catalog.execute(
            """SELECT status,cache_sync_status,cache_sync_error,cache_sync_json
            FROM organization_plans"""
        ).fetchone()
    sync_payload = json.loads(str(plan["cache_sync_json"]))
    semantic_result = next(
        result for result in sync_payload["databases"] if result["database"] == "semantic"
    )
    assert tuple(plan)[:3] == ("applied", "synced", None)
    assert semantic_result == {
        "database": "semantic",
        "detail": None,
        "status": "absent",
        "updated_rows": 0,
    }
    assert not (state_directory / "semantic.sqlite3").exists()


def test_cache_sync_accepts_valid_semantic_state_without_indexed_item(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    destination = tmp_path / "organizados" / source.name
    destination.parent.mkdir()
    source.replace(destination)
    semantic_path = state_directory / "semantic.sqlite3"
    initialize_semantic_state(semantic_path)

    result = synchronize_moved_document(
        state_directory,
        source_kind="docx",
        file_key=file_key,
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(snapshot.volume_id),
        file_id=str(snapshot.file_id),
    )

    semantic_result = next(
        database for database in result.databases if database.database == "semantic"
    )
    assert result.complete
    assert semantic_result.status == "synced"
    assert semantic_result.updated_rows == 0
    with sqlite3.connect(semantic_path) as semantic:
        assert semantic.execute("SELECT COUNT(*) FROM semantic_items").fetchone()[0] == 0


def test_cache_sync_rejects_incompatible_semantic_schema_transactionally(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"docx fixture")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    destination = tmp_path / "organizados" / source.name
    destination.parent.mkdir()
    source.replace(destination)
    semantic_path = state_directory / "semantic.sqlite3"
    with sqlite3.connect(semantic_path) as semantic:
        semantic.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE semantic_items(
                item_id TEXT PRIMARY KEY,source_kind TEXT NOT NULL,
                source_identity TEXT NOT NULL,path TEXT,updated_ns INTEGER NOT NULL
            );
            PRAGMA user_version=99;
            """
        )
        semantic.execute("INSERT INTO metadata VALUES('schema_version','99')")
        semantic.execute(
            "INSERT INTO semantic_items VALUES(?,?,?,?,?)",
            (f"item:docx:{file_key}", "docx", file_key, str(source), 10),
        )

    result = synchronize_moved_document(
        state_directory,
        source_kind="docx",
        file_key=file_key,
        old_path=str(source),
        new_path=str(destination),
        volume_id=str(snapshot.volume_id),
        file_id=str(snapshot.file_id),
    )

    source_result = next(database for database in result.databases if database.database == "docx")
    semantic_result = next(
        database for database in result.databases if database.database == "semantic"
    )
    assert not result.complete
    assert source_result.status == "synced"
    assert semantic_result.status == "error"
    assert "schema version is not compatible" in str(semantic_result.detail)
    with sqlite3.connect(state_directory / "docx.sqlite3") as docx:
        assert docx.execute("SELECT path FROM documents WHERE file_key=?", (file_key,)).fetchone()[
            0
        ] == str(destination)
    with sqlite3.connect(semantic_path) as semantic:
        assert semantic.execute("SELECT path,updated_ns FROM semantic_items").fetchone() == (
            str(source),
            10,
        )


def test_apply_without_plans_does_not_create_default_directory(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    catalog_path = state_directory / "document_catalog.sqlite3"
    initialize_document_catalog(catalog_path)
    destination_root = tmp_path / "Consulta_Tecnica_Organizada"

    summary = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
    )

    assert summary.selected == 0
    assert not destination_root.exists()


@WINDOWS_MUTATION_ONLY
def test_apply_disambiguates_destination_that_appears_after_plan(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "Formato SERINTRA.docx"
    source.write_bytes(b"source")
    _seed_docx(
        state_directory / "docx.sqlite3",
        source,
        "Formato SERINTRA formulario de control",
        title="Formato SERINTRA",
        author="SERINTRA",
    )
    update_document_catalog(state_directory)
    destination_root = tmp_path / "organizados"
    destination_root.mkdir()
    plan_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
    )
    destination = (
        destination_root
        / "Empresas"
        / "SERINTRA"
        / "Gestion_y_administracion"
        / "Formatos_y_registros"
        / source.name
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    summary = apply_document_organization(
        state_directory / "document_catalog.sqlite3",
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=1,
    )

    assert summary.applied == 1
    assert summary.blocked == 0
    assert not source.exists()
    assert destination.read_bytes() == b"existing"
    with document_catalog_database(
        state_directory / "document_catalog.sqlite3",
        readonly=True,
    ) as catalog:
        plan = catalog.execute(
            "SELECT destination_path,reason,status FROM organization_plans"
        ).fetchone()
    moved_destination = Path(str(plan["destination_path"]))
    assert moved_destination != destination
    assert moved_destination.read_bytes() == b"source"
    assert "identity_disambiguation" in str(plan["reason"])
    assert plan["status"] == "applied"


def test_plan_rejects_destination_intersecting_framework_state(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    catalog_path = state_directory / "document_catalog.sqlite3"
    extended_catalog = Path("\\\\?\\" + os.path.abspath(catalog_path))
    roots = (state_directory / "organized", tmp_path)
    cases = tuple((catalog_path, root) for root in roots)
    if os.name == "nt":
        cases += (
            *((catalog_path, Path("\\\\?\\" + os.path.abspath(root))) for root in roots),
            (extended_catalog, state_directory / "organized"),
        )

    for requested_catalog, organization_root in cases:
        with pytest.raises(ValueError, match="framework state directory"):
            plan_document_organization(requested_catalog, organization_root)
        assert not catalog_path.exists()


def test_plan_rejects_destination_aliasing_framework_state(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    state_alias = tmp_path / "state-alias"
    try:
        state_alias.symlink_to(state_directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    catalog_path = state_directory / "document_catalog.sqlite3"

    with pytest.raises(ValueError, match="framework state directory"):
        plan_document_organization(catalog_path, state_alias / "organized")

    assert not catalog_path.exists()


def test_default_organization_root_uses_latest_completed_analysis(
    tmp_path: Path,
) -> None:
    analysis_root = tmp_path / "informacion"
    analysis_root.mkdir()
    other_root = tmp_path / "route-only"
    other_root.mkdir()
    framework_database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(framework_database)
    try:
        connection.executescript(
            """
            CREATE TABLE initial_runs(
                run_id INTEGER PRIMARY KEY,
                root TEXT NOT NULL,
                status TEXT NOT NULL,
                run_kind TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO initial_runs(run_id,root,status,run_kind) VALUES(?,?,?,?)",
            (
                (1, str(analysis_root), "completed", "initial"),
                (2, str(other_root), "completed", "route_only"),
                (3, str(other_root), "failed", "initial"),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    destination = default_organization_root(framework_database)

    assert destination == analysis_root / "Consulta_Tecnica_Organizada"
    assert not destination.exists()
    assert (
        default_organization_root(
            tmp_path / "missing.sqlite3",
            analysis_root=other_root,
        )
        == other_root / "Consulta_Tecnica_Organizada"
    )


# endregion [03]


# region [04] CLI safety


def test_organization_cli_allows_default_root_and_keeps_apply_separate() -> None:
    parser = build_parser()
    default_plan = parser.parse_args(["--organization-plan"])
    validate_arguments(default_plan)
    assert default_plan.organization_root is None
    apply_error = "cannot be combined with --apply" if os.name == "nt" else LINUX_MUTATION_REASON
    with pytest.raises(SystemExit, match=apply_error):
        validate_arguments(
            parser.parse_args(
                [
                    "--organization-plan",
                    "--organization-root",
                    r"C:\Organizados",
                    "--apply",
                ]
            )
        )

    args = parser.parse_args(
        [
            "--organization-plan",
            "--organization-root",
            r"C:\Organizados",
            "--organization-min-confidence",
            "0.8",
        ]
    )
    validate_arguments(args)
    assert args.organization_plan
    assert not args.organization_apply

    integrated = parser.parse_args(
        [
            "--all",
            "--apply",
            "--organization-root",
            r"C:\Organizados",
            "--organization-min-confidence",
            "0.8",
        ]
    )
    if os.name == "nt":
        validate_arguments(integrated)
        assert integrated.apply
        assert integrated.organization_root == Path(r"C:\Organizados")
    else:
        with pytest.raises(SystemExit, match=LINUX_MUTATION_REASON):
            validate_arguments(integrated)
    with pytest.raises(SystemExit, match="requires an organization command"):
        validate_arguments(parser.parse_args(["--all", "--organization-root", r"C:\Organizados"]))
    route_apply_error = (
        "requires an organization command" if os.name == "nt" else LINUX_MUTATION_REASON
    )
    with pytest.raises(SystemExit, match=route_apply_error):
        validate_arguments(
            parser.parse_args(
                [
                    "--route",
                    "image",
                    "--apply",
                    "--organization-root",
                    r"C:\Organizados",
                ]
            )
        )

    preview = parser.parse_args(
        [
            "--catalog-preview",
            "25",
            "--catalog-authority",
            "IEEE",
            "--catalog-client",
            "ANDRITZ",
            "--catalog-project",
            "Malpaso",
            "--catalog-workstream",
            "embarques_hcn",
        ]
    )
    validate_arguments(preview)
    assert preview.catalog_authority == "IEEE"
    assert preview.catalog_client == "ANDRITZ"
    assert preview.catalog_project == "Malpaso"
    assert preview.catalog_workstream == "embarques_hcn"
    with pytest.raises(SystemExit, match="require --catalog-preview"):
        validate_arguments(parser.parse_args(["--catalog-kind", "normativa"]))


# endregion [04]
