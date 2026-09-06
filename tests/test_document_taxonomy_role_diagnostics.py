"""Document role, topics and issuer claims are different evidence dimensions."""

from dataclasses import asdict

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
