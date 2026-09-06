"""Document role, topics and issuer claims are different evidence dimensions."""

from dataclasses import asdict

import pytest

from neocortex.documents.document_taxonomy import DocumentSignals, classify_document


def test_timestamped_log_mentions_do_not_make_it_an_incident_report() -> None:
    result = classify_document(
        DocumentSignals(
            "text",
            "/fixture/auditoria_codex_app_only_Synapta_20260827.txt",
            "done",
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 INFO stdout: reporte de anomalías de embarques Malpaso ANDRITZ\n"
                "exit_code=0"
            ),
        )
    )
    assert result.primary_kind == result.document_role == "registro_log"
    assert result.taxonomy_status == "outside_taxonomy"
    assert "role_vs_mention:reporte_anomalias" in result.contradictions
    assert result.primary_issuer is None
    assert not result.suggested_stem.endswith("ANDRITZ")
    assert any(
        item.entity == "ANDRITZ" and item.role == "mentioned" for item in result.entity_roles
    )


@pytest.mark.parametrize("media_type", ("text/plain", "application/json"))
def test_same_structured_log_in_archive_uses_observed_text_mime(media_type: str) -> None:
    result = classify_document(
        DocumentSignals(
            "archive",
            "/fixture/container.zip!/audit.dat",
            "indexed",
            metadata=f"content_kind=txt media_type={media_type}",
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 INFO stdout: reporte de anomalías Malpaso\nexit_code=0"
            ),
        )
    )
    assert result.primary_kind == result.document_role == "registro_log"
    assert result.taxonomy_status == "outside_taxonomy"


@pytest.mark.parametrize(
    "source_kind,metadata",
    (
        ("image", "media_type=text/plain"),
        ("archive", "content_kind=image media_type=image/png"),
        ("archive", "content_kind=image media_type=text/plain"),
        ("archive", "content_kind=pdf media_type=application/pdf"),
        ("archive", ""),
        ("archive", "media_type=text/plain media_type=image/png"),
        ("archive", "media_type=text/plain media_type=text/plain"),
    ),
)
def test_archive_log_rule_does_not_promote_ocr_or_ambiguous_mime(
    source_kind: str, metadata: str
) -> None:
    result = classify_document(
        DocumentSignals(
            source_kind,
            "/fixture/container.zip!/audit.txt",
            "indexed",
            metadata=metadata,
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 INFO stdout: reporte de anomalías\nexit_code=0"
            ),
        )
    )
    assert result.document_role != "registro_log"


@pytest.mark.parametrize(
    "heading,expected_role",
    (
        ("Reporte de anomalías", "reporte_anomalias"),
        ("Bitácora de actividades", "registro_bitacora"),
    ),
)
def test_archive_explicit_document_heading_is_not_overridden_by_quoted_logs(
    heading: str,
    expected_role: str,
) -> None:
    result = classify_document(
        DocumentSignals(
            "archive",
            "/fixture/container.zip!/entry.txt",
            "indexed",
            title=heading,
            metadata="content_kind=txt media_type=text/plain",
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 INFO stdout: reporte de anomalías\nexit_code=0"
            ),
        )
    )
    assert result.document_role == expected_role


@pytest.mark.parametrize("source_kind", ("text", "archive"))
@pytest.mark.parametrize("filename_stem", ("Bitácora de actividades", "Reporte de anomalías"))
@pytest.mark.parametrize("title_is_stem", (False, True))
def test_filename_title_does_not_block_structured_log_role(
    source_kind: str,
    filename_stem: str,
    title_is_stem: bool,
) -> None:
    basename = f"{filename_stem}.log"
    prefix = "/fixture/archive.zip!/" if source_kind == "archive" else "/fixture/"
    result = classify_document(
        DocumentSignals(
            source_kind,
            f"{prefix}{basename}",
            "indexed",
            title=filename_stem if title_is_stem else basename,
            metadata="content_kind=log media_type=text/plain",
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 INFO stdout: cache revisada\nexit_code=0"
            ),
        )
    )
    assert result.primary_kind == result.document_role == "registro_log"
    assert "heading:explicit_bitacora_over_quoted_log" not in result.role_evidence


def test_log_filename_or_word_alone_is_not_a_log_role() -> None:
    result = classify_document(
        DocumentSignals(
            "text",
            "/fixture/log.txt",
            "done",
            title="Reporte de anomalías en embarques",
            leading_text="Se observó pérdida de presión en el transformador, según el log del equipo.",
        )
    )
    assert result.primary_kind == "reporte_anomalias"


def test_report_quoting_log_records_keeps_its_explicit_document_role() -> None:
    result = classify_document(
        DocumentSignals(
            "text",
            "/fixture/report.txt",
            "done",
            title="Reporte de anomalías",
            leading_text=(
                "2026-08-27 09:38:49 INFO command: audit\n"
                "2026-08-27 09:38:50 ERROR stdout: alarma de presión\nexit_code=1"
            ),
        )
    )
    assert result.primary_kind == "reporte_anomalias"


def test_report_heading_in_content_precedes_generic_metadata_title() -> None:
    result = classify_document(
        DocumentSignals(
            "docx",
            "/fixture/report.docx",
            "done",
            title="Documento 1",
            leading_text="Reporte de normatividad CFE para tierras físicas. Cita CFE 01J00-01.",
        )
    )
    assert result.primary_kind == "informe_tecnico"
    assert result.primary_issuer is None


def test_standards_report_preserves_technical_role_and_citations_not_issuer() -> None:
    result = classify_document(
        DocumentSignals(
            "docx",
            "/fixture/Reporte_normatividad_CFE_tierras_fisicas_Malpaso.docx",
            "done",
            title="Reporte de normatividad CFE para tierras físicas",
            leading_text="Este informe analiza CFE 01J00-01 y cita IEEE Std 80-2013.",
        )
    )
    assert result.primary_kind == "informe_tecnico"
    assert result.standard_references
    assert result.primary_issuer is None
    assert result.issuer_status == "unknown"
    assert {item.role for item in result.entity_roles} == {"cited"}
    assert not result.suggested_stem.startswith("CFE 01J00-01")
    assert result.contradictions == ("cited_standard_not_document_role",)
    assert asdict(result)["confidence_kind"] == "uncalibrated_heuristic"


def test_explicit_issuer_is_a_declaration_not_verified_provenance() -> None:
    result = classify_document(
        DocumentSignals(
            "pdf",
            "/fixture/report.pdf",
            "done",
            author="ANDRITZ",
            title="Reporte de anomalías",
            leading_text="Emitido por: ANDRITZ. Se observó pérdida de presión.",
        )
    )
    assert result.primary_issuer == "ANDRITZ"
    assert result.issuer_status == "declared"
    assert "issuer_identity_unverified" in result.unknowns
    assert any(
        item.role == "issuer" and item.evidence_kind == "declaration"
        for item in result.entity_roles
    )


def test_insufficient_identification_and_known_outside_taxonomy_are_not_anomalies() -> None:
    unknown = classify_document(DocumentSignals("text", "/fixture/unknown.txt", "done"))
    personal = classify_document(
        DocumentSignals(
            "text",
            "/fixture/personal.txt",
            "done",
            title="Lista de compras",
            leading_text="Lista de compras: manzanas y arroz",
        )
    )
    assert unknown.taxonomy_status == "insufficient_identification"
    assert personal.taxonomy_status == "outside_taxonomy"
    assert personal.document_role == "personal_document"
    assert personal.contradictions == ()
