"""Causal Knowledge health for immutable schema-13 PDF state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.foundation.file_identity import encode_file_identity
from neocortex.knowledge.knowledge_asset_health import inspect_knowledge_asset_health
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthStage,
    KnowledgeAssetHealthState,
)
from neocortex.knowledge.knowledge_asset_health_pdf import (
    PDF_STRUCTURAL_RECOVERY_VERSION,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.capabilities.formats.pdf.pdf_schema import PDF_SCHEMA_VERSION
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.capabilities.formats.text.text_state import initialize_text_state


_RESOURCE_ID = "resource:file:11:3:-1"
_PATH = "/corpus/assets/opaque.bin"
_PRIVATE_PAGE_TEXT = "PRIVATE_PAGE_TEXT_MUST_NOT_ESCAPE"
_PRIVATE_METADATA = "PRIVATE_METADATA_MUST_NOT_ESCAPE"
_PRIVATE_ERROR = "PRIVATE_ERROR_MUST_NOT_ESCAPE"


@dataclass(frozen=True, slots=True)
class _PdfHealthFixture:
    paths: KnowledgeStatePaths
    query: KnowledgeAssetHealthQuery
    file_key: str


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def _quiesce(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _mutate_published_catalog(path: Path, statement: str) -> None:
    """Create an inconsistent fixture through an unpublished generation window."""

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE catalog_generations SET status='building' WHERE generation_id=1"
        )
        connection.execute(statement)
        connection.execute(
            "UPDATE catalog_generations SET status='published' WHERE generation_id=1"
        )


def _state_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.name: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def _create_inventory(path: Path) -> None:
    initialize_inventory_schema(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(1,'/corpus',1,2,1,1,800,0,0,0,'complete','fixture-v1')"""
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('/corpus',1,NULL,NULL,NULL,1,3)"""
        )
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,800,123,-1)""",
            (_PATH, _blob(11), _blob(3)),
        )
    _quiesce(path)


def _create_pdf(
    path: Path,
    *,
    file_key: str,
    pages: int = 2,
    status: str = "done",
    metadata: object | None = None,
) -> None:
    initialize_pdf_state(path)
    metadata_json = json.dumps(
        {"private": _PRIVATE_METADATA} if metadata is None else metadata,
        separators=(",", ":"),
    )
    page_start, page_end = (1, 0) if pages == 0 else (1, pages)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            page_count,completed_pages,native_pages,ocr_pages,native_chars,ocr_chars,
            normalized_text_xxh3_128,normalized_text_chars,page_start,page_end,
            is_partial,page_errors_count,metadata_json,error_type,error_message,
            last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_key,
                _PATH,
                800,
                123,
                -1,
                "pdf-route-fixture-v1",
                status,
                pages,
                pages,
                pages,
                0,
                pages * 8,
                0,
                "f" * 32 if pages else None,
                pages * 8,
                page_start,
                page_end,
                0,
                0,
                metadata_json,
                "OpaqueDiagnostic",
                _PRIVATE_ERROR,
                1,
                20,
            ),
        )
        for page_number in range(pages):
            text = f"{_PRIVATE_PAGE_TEXT}:{page_number}"
            blob = zlib.compress(text.encode("utf-8"))
            connection.execute(
                """INSERT INTO pages(
                file_key,page_number,source,text_zlib,text_chars,ocr_provenance_json)
                VALUES(?,?,'native',?,?,NULL)""",
                (file_key, page_number, blob, len(text)),
            )
            connection.execute(
                "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,?,?)",
                (file_key, _PATH, page_number, text),
            )
            connection.execute(
                "INSERT INTO page_fts_state VALUES(?,?,?)",
                (file_key, page_number, f"digest-{page_number}"),
            )
    _quiesce(path)


def _create_catalog(
    path: Path,
    *,
    file_key: str,
    text_fingerprint: str | None,
) -> None:
    initialize_document_catalog(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO catalog_generations(
            generation_id,catalog_run_id,source_kind,base_generation_id,status,
            started_ns,completed_ns,published_ns,error_type,error_message)
            VALUES(1,NULL,'pdf',NULL,'published',1,2,3,NULL,NULL)"""
        )
        connection.execute(
            """INSERT INTO catalog_publications(source_kind,generation_id,published_ns)
            VALUES('pdf',1,3)"""
        )
        connection.execute(
            """INSERT INTO catalog_generation_documents(
            generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns,source_status,processing_signature,text_fingerprint,
            classifier_signature,primary_kind,primary_subtype,primary_authority,
            primary_organization,primary_client,primary_project,primary_workstream,
            confidence,uncertainty,standard_references_json,organizations_json,
            clients_json,projects_json,workstreams_json,topics_json,equipment_json,
            activities_json,classification_json,catalog_status,error_type,error_message,
            active,last_seen_catalog_run_id,updated_ns)
            VALUES(1,'pdf',?,?, '11','3',800,123,-1,'done','pdf-route-fixture-v1',?,
            'classifier-v1','otro',NULL,NULL,NULL,NULL,NULL,NULL,0.9,'baja','[]','[]',
            '[]','[]','[]','[]','[]','[]','{}','classified',NULL,NULL,1,1,30)""",
            (file_key, _PATH, text_fingerprint),
        )
    _quiesce(path)


def _create_fixture(
    root: Path,
    *,
    pages: int = 2,
    with_catalog: bool = True,
    metadata: object | None = None,
) -> _PdfHealthFixture:
    root.mkdir()
    paths = KnowledgeStatePaths.from_directory(root)
    file_key = encode_file_identity(11, 3)
    _create_inventory(paths.inventory)
    _create_pdf(paths.pdf, file_key=file_key, pages=pages, metadata=metadata)
    if with_catalog:
        _create_catalog(
            paths.catalog,
            file_key=file_key,
            text_fingerprint="f" * 32 if pages else None,
        )
    return _PdfHealthFixture(paths, KnowledgeAssetHealthQuery(_RESOURCE_ID), file_key)


def _create_matching_text(path: Path, *, file_key: str) -> None:
    initialize_text_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO text_input_revisions(
            revision_id,resource_id,producer,processing_signature,generation,
            revision_state,observed_at_utc,fingerprint_algorithm,fingerprint,recorded_ns)
            VALUES('revision:text:fixture',?,'text-route-v2','text-route-fixture-v1',
            1,'current','2026-08-15T00:00:00Z','xxh3_128',?,10)""",
            (_RESOURCE_ID, "a" * 32),
        )
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
            text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
            last_seen_run_id,updated_ns,revision_id)
            VALUES(?,?,800,123,-1,'text-route-fixture-v1','complete','plain_text',
            'text/plain','Asset',NULL,'{}',NULL,12,?,0,NULL,NULL,NULL,0,1,20,
            'revision:text:fixture')""",
            (file_key, _PATH, "b" * 32),
        )
    _quiesce(path)


def test_pdf_aligned_full_projection_is_healthy_read_only_and_content_blind(
    tmp_path: Path,
) -> None:
    fixture = _create_fixture(tmp_path / "aligned")
    before = _state_fingerprint(fixture.paths.pdf.parent)

    first = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    second = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert first.health is KnowledgeAssetHealthState.HEALTHY
    assert first.completeness is KnowledgeAssetHealthCompleteness.COMPLETE
    assert first.reason_code == "causal_trace_aligned"
    assert tuple(fact.stage for fact in first.facts) == tuple(KnowledgeAssetHealthStage)
    assert tuple(fact.owner for fact in first.facts) == (
        "inventory",
        "pdf",
        "catalog",
        "knowledge",
    )
    assert first.facts[1].schema_version == PDF_SCHEMA_VERSION == 13
    assert first.to_json() == second.to_json()
    assert _PRIVATE_PAGE_TEXT not in first.to_json()
    assert _PRIVATE_METADATA not in first.to_json()
    assert _PRIVATE_ERROR not in first.to_json()
    assert first.read_only is True and first.mutation_authorized is False
    assert _state_fingerprint(fixture.paths.pdf.parent) == before


def test_pdf_empty_bounded_and_partial_projections_have_typed_health(
    tmp_path: Path,
) -> None:
    empty = _create_fixture(tmp_path / "empty", pages=0)
    empty_report = inspect_knowledge_asset_health(empty.paths, empty.query)
    assert empty_report.health is KnowledgeAssetHealthState.HEALTHY
    assert empty_report.reason_code == "causal_trace_aligned"

    bounded = _create_fixture(tmp_path / "bounded")
    with closing(sqlite3.connect(bounded.paths.pdf)) as connection, connection:
        connection.execute(
            """UPDATE documents SET page_count=4,completed_pages=2,page_start=1,
            page_end=2,is_partial=1"""
        )
    _quiesce(bounded.paths.pdf)
    bounded_report = inspect_knowledge_asset_health(bounded.paths, bounded.query)
    assert bounded_report.health is KnowledgeAssetHealthState.DEGRADED
    assert bounded_report.completeness is KnowledgeAssetHealthCompleteness.COMPLETE
    assert bounded_report.reason_code == "typed_pdf_bounded_range"

    partial = _create_fixture(tmp_path / "partial")
    with closing(sqlite3.connect(partial.paths.pdf)) as connection, connection:
        connection.execute("UPDATE documents SET status='partial',is_partial=1")
        for page_number in range(2):
            connection.execute(
                """INSERT INTO page_staging(
                file_key,processing_signature,page_number,source,text_zlib,text_chars)
                VALUES(?,'pdf-route-fixture-v1',?,'native',?,1)""",
                (partial.file_key, page_number, zlib.compress(b"x")),
            )
    _quiesce(partial.paths.pdf)
    _mutate_published_catalog(
        partial.paths.catalog,
        """UPDATE catalog_generation_documents SET source_status='partial'
        WHERE source_kind='pdf'""",
    )
    _quiesce(partial.paths.catalog)
    partial_report = inspect_knowledge_asset_health(partial.paths, partial.query)
    assert partial_report.health is KnowledgeAssetHealthState.DEGRADED
    assert partial_report.completeness is KnowledgeAssetHealthCompleteness.COMPLETE
    assert partial_report.reason_code == "typed_pdf_status_partial"


def test_pdf_protected_error_processing_and_unknown_states_do_not_invent_catalog(
    tmp_path: Path,
) -> None:
    expected = (
        (
            "protected",
            KnowledgeAssetHealthState.PROTECTED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_source_status_protected",
        ),
        (
            "error",
            KnowledgeAssetHealthState.FAILED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pipeline_status_failed",
        ),
        (
            "processing",
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "pdf_processing_incomplete",
        ),
        (
            "novel-state",
            KnowledgeAssetHealthState.UNKNOWN,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "typed_status_unrecognized",
        ),
    )
    for status, health, completeness, reason in expected:
        fixture = _create_fixture(tmp_path / status, with_catalog=False)
        with closing(sqlite3.connect(fixture.paths.pdf)) as connection, connection:
            connection.execute("UPDATE documents SET status=?", (status,))
        _quiesce(fixture.paths.pdf)

        report = inspect_knowledge_asset_health(fixture.paths, fixture.query)

        assert report.health is health
        assert report.completeness is completeness
        assert report.reason_code == reason
        assert tuple(fact.owner for fact in report.facts) == ("inventory", "pdf")
        assert report.gaps == ()


def test_pdf_projection_recovery_and_terminal_inconsistencies_fail_closed(
    tmp_path: Path,
) -> None:
    top_level = _create_fixture(
        tmp_path / "top-level",
        metadata={"engine": "pdfminer", "fallback": True},
    )
    top_level_report = inspect_knowledge_asset_health(top_level.paths, top_level.query)
    assert top_level_report.health is KnowledgeAssetHealthState.HEALTHY
    source_values = {value.name: value.value for value in top_level_report.facts[1].values}
    assert source_values["recovery_present"] == "false"

    recovered = _create_fixture(
        tmp_path / "recovered",
        metadata={
            "neocortex_recovery": {
                "engine": "qpdf+pymupdf",
                "recovery_version": PDF_STRUCTURAL_RECOVERY_VERSION,
                "primary_error": _PRIVATE_ERROR,
            }
        },
    )
    recovered_report = inspect_knowledge_asset_health(recovered.paths, recovered.query)
    assert recovered_report.health is KnowledgeAssetHealthState.HEALTHY
    recovered_values = {value.name: value.value for value in recovered_report.facts[1].values}
    assert recovered_values["recovery_recognized"] == "true"
    assert _PRIVATE_ERROR not in recovered_report.to_json()

    unrecognized = _create_fixture(
        tmp_path / "unrecognized",
        metadata={
            "neocortex_recovery": {
                "engine": "unknown",
                "recovery_version": PDF_STRUCTURAL_RECOVERY_VERSION,
            }
        },
    )
    unrecognized_report = inspect_knowledge_asset_health(
        unrecognized.paths,
        unrecognized.query,
    )
    assert "pdf_recovery_contract_unrecognized" in unrecognized_report.counterevidence
    assert unrecognized_report.health is not KnowledgeAssetHealthState.HEALTHY

    inconsistent = _create_fixture(tmp_path / "inconsistent")
    with closing(sqlite3.connect(inconsistent.paths.pdf)) as connection, connection:
        connection.execute(
            """UPDATE documents SET path='/different',completed_pages=1,
            page_errors_count=1,metadata_json='not-json'"""
        )
        connection.execute(
            """INSERT INTO page_staging(
            file_key,processing_signature,page_number,source,text_zlib,text_chars)
            VALUES(?,'pdf-route-fixture-v1',0,'native',?,1)""",
            (inconsistent.file_key, zlib.compress(b"x")),
        )
        connection.execute(
            "DELETE FROM page_fts_state WHERE file_key=? AND page_number=1",
            (inconsistent.file_key,),
        )
    _quiesce(inconsistent.paths.pdf)
    _mutate_published_catalog(
        inconsistent.paths.catalog,
        """UPDATE catalog_generation_documents
        SET processing_signature='different-signature' WHERE source_kind='pdf'""",
    )
    _quiesce(inconsistent.paths.catalog)

    inconsistent_report = inspect_knowledge_asset_health(
        inconsistent.paths,
        inconsistent.query,
    )
    assert {
        "inventory_pdf_projection_mismatch",
        "pdf_catalog_projection_mismatch",
        "pdf_completed_pages_mismatch",
        "pdf_page_errors_mismatch",
        "pdf_fts_projection_mismatch",
        "pdf_terminal_staging_present",
        "pdf_metadata_invalid",
    } <= set(inconsistent_report.counterevidence)
    assert inconsistent_report.health is not KnowledgeAssetHealthState.HEALTHY


def test_pdf_dispatch_ambiguity_and_owner_fences_abstain_without_mutation(
    tmp_path: Path,
) -> None:
    ambiguous = _create_fixture(tmp_path / "ambiguous")
    assert ambiguous.paths.text is not None
    _create_matching_text(ambiguous.paths.text, file_key=ambiguous.file_key)
    ambiguity_report = inspect_knowledge_asset_health(ambiguous.paths, ambiguous.query)
    assert ambiguity_report.health is KnowledgeAssetHealthState.UNKNOWN
    assert ambiguity_report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
    assert ambiguity_report.reason_code == "source_owner_identity_ambiguous"
    assert "source_owner_identity_ambiguous" in ambiguity_report.counterevidence

    wal = _create_fixture(tmp_path / "wal")
    connection = sqlite3.connect(wal.paths.pdf)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE documents SET updated_ns=updated_ns+1")
        connection.commit()
        before = _state_fingerprint(wal.paths.pdf.parent)
        wal_report = inspect_knowledge_asset_health(wal.paths, wal.query)
        assert wal_report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
        assert "pdf_owner_not_quiescent" in wal_report.gaps
        assert _state_fingerprint(wal.paths.pdf.parent) == before
    finally:
        connection.close()

    future = _create_fixture(tmp_path / "future")
    with closing(sqlite3.connect(future.paths.pdf)) as connection, connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    _quiesce(future.paths.pdf)
    future_report = inspect_knowledge_asset_health(future.paths, future.query)
    assert future_report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
    assert "pdf_owner_future" in future_report.gaps

    corrupt = _create_fixture(tmp_path / "corrupt")
    corrupt.paths.pdf.write_bytes(b"not-a-sqlite-database")
    corrupt_report = inspect_knowledge_asset_health(corrupt.paths, corrupt.query)
    assert corrupt_report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
    assert "pdf_owner_corrupt" in corrupt_report.gaps

    changing = _create_fixture(tmp_path / "changing")

    def change_pdf_fact(_attempt: int) -> None:
        with closing(sqlite3.connect(changing.paths.pdf)) as connection, connection:
            connection.execute("UPDATE documents SET updated_ns=updated_ns+1")
        _quiesce(changing.paths.pdf)

    changed_report = inspect_knowledge_asset_health(
        changing.paths,
        changing.query,
        _between_observations=change_pdf_fact,
    )
    assert changed_report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
    assert changed_report.reason_code == "snapshot_changed"
    assert "fact_snapshot_changed" in changed_report.counterevidence
