"""Exact Knowledge owner lookup is bounded, pinned where possible and honest."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_exact.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.deduplication.fingerprinting import FULL_ALGORITHM, PARTIAL_ALGORITHM
from neocortex.deduplication.persistence.schema import initialize_inventory_schema
from neocortex.knowledge import knowledge_exact as knowledge_exact_module
from neocortex.code.code_schema import initialize_code_state
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.foundation.file_identity import FileIdentity
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PublicationHead,
    RevisionState,
)
from neocortex.knowledge.knowledge_exact import (
    ExactLookupKind,
    ExactLookupRequest,
    ExactLookupResult,
    ExactLookupStatus,
    ExactLookupTerm,
    classify_plan_exact_terms,
    lookup_exact,
    lookup_plan_exact,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_models import fingerprint_text
from neocortex.platform_policy import sqlite_path_collation
# endregion [01]

# region [02] Implementación


def _snapshot(*owners: OwnerSnapshot) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T04:00:00Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def _legacy_plan_with_exact_terms(
    plan: KnowledgePlan,
    exact_terms: tuple[str, ...],
) -> KnowledgePlan:
    fingerprint = zlib.crc32("\0".join(exact_terms).encode("utf-8"))
    return replace(
        plan,
        plan_id=f"knowledge-plan-v1:test-exact-terms:{fingerprint:08x}",
        exact_terms=exact_terms,
    )


def _absent_owner(name: str, schema: int) -> OwnerSnapshot:
    return OwnerSnapshot(name, OwnerAvailability.ABSENT, schema)


def _inventory_owner() -> OwnerSnapshot:
    return OwnerSnapshot(
        "inventory",
        OwnerAvailability.AVAILABLE,
        7,
        7,
        publications=(PublicationHead("C:/docs", "inventory-scan:1", 1),),
        watermarks=(
            LogicalWatermark("published_roots", "1"),
            LogicalWatermark("latest_checkpoint_updated_ns", "3"),
        ),
    )


def _code_owner() -> OwnerSnapshot:
    return OwnerSnapshot(
        "code",
        OwnerAvailability.AVAILABLE,
        2,
        2,
        watermarks=(
            LogicalWatermark("current_files", "1"),
            LogicalWatermark("latest_version_id", "1"),
            LogicalWatermark("latest_analysis_run_id", "1"),
            LogicalWatermark("visibility", "best_effort_non_generational"),
        ),
    )


def _catalog_owner() -> OwnerSnapshot:
    return OwnerSnapshot(
        "catalog",
        OwnerAvailability.AVAILABLE,
        6,
        6,
        publications=(PublicationHead("pdf", "catalog:1", 1),),
    )


def _create_inventory(path: Path) -> str:
    initialize_inventory_schema(path)
    full_digest = "11" * 16

    def blob(value: int) -> bytes:
        return value.to_bytes(16, "little")

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
            bytes_seen,skipped_links,excluded_directories,errors,status)
            VALUES(1,'C:/docs',1,2,1,0,100,0,0,0,'complete')"""
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('C:/docs',1,'C:','journal',10,1,3)"""
        )
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,?,?,?)""",
            ("C:/docs/A%_# report.pdf", blob(1), blob(2), 100, 20, 10),
        )
        connection.executemany(
            """INSERT INTO fingerprints(
            volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest)
            VALUES(?,?,?,?,?,?,?)""",
            (
                (
                    blob(1),
                    blob(2),
                    100,
                    20,
                    10,
                    FULL_ALGORITHM,
                    bytes.fromhex(full_digest),
                ),
                (
                    blob(1),
                    blob(2),
                    100,
                    20,
                    10,
                    PARTIAL_ALGORITHM,
                    bytes.fromhex(full_digest),
                ),
                (
                    blob(1),
                    blob(2),
                    99,
                    19,
                    10,
                    FULL_ALGORITHM,
                    bytes.fromhex(full_digest),
                ),
            ),
        )
    return full_digest


def _insert_code_version(connection: sqlite3.Connection) -> None:
    text = "def validate(value: int) -> int:\n    return value\n"
    raw = text.encode("utf-8")
    connection.execute(
        """INSERT INTO analysis_runs(
        analysis_run_id,framework_run_id,scan_id,processing_signature,status,
        started_ns,completed_ns,candidates,processed,cache_hits,errors,summary_json)
        VALUES(1,1,1,'code-fixture','completed',1,2,1,1,0,0,'{}')"""
    )
    connection.execute(
        """INSERT INTO files(
        file_id,volume_id,physical_file_id,current_path,current_version_id,status,
        first_seen_run_id,last_seen_run_id)
        VALUES(1,'1','2','C:/src/control.py',NULL,'current',1,1)"""
    )
    connection.execute(
        """INSERT INTO file_versions(
        version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
        raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
        normalized_xxh3_128,token_xxh3_128,structure_xxh3_128,encoding,
        language,artifact_kind,generated,vendored,classification_confidence,
        classification_evidence_json,analysis_status,processing_signature,
        analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,
        text_truncated,provenance_json,first_observed_run_id,last_observed_run_id,
        valid_from_ns)
        VALUES(1,1,'C:/src/control.py',?,20,10,?,?,?,?,?,?,?,'utf-8',
        'python','source',0,0,1.0,'["fixture"]','complete','code-fixture',
        'python-ast','1','python-ast',?,?,0,'{}',1,1,1)""",
        (
            len(raw),
            "aa" * 16,
            "bb" * 8,
            "cc" * 16,
            "dd" * 8,
            "ee" * 16,
            "ff" * 16,
            "12" * 16,
            sqlite3.Binary(zlib.compress(raw)),
            len(text),
        ),
    )
    connection.execute("UPDATE files SET current_version_id=1 WHERE file_id=1")
    connection.execute(
        """INSERT INTO symbols(
        symbol_id,version_id,parent_symbol_id,kind,name,qualified_name,signature,
        visibility,docstring,confirmed,complexity,start_line,start_column,
        end_line,end_column,start_byte,end_byte,metadata_json)
        VALUES(1,1,NULL,'function','validate','control.validate',
        'validate(value: int) -> int','public',NULL,1,1,1,0,2,16,0,48,'{}')"""
    )


def _create_code(path: Path) -> None:
    initialize_code_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_code_version(connection)


def _insert_catalog_document(
    connection: sqlite3.Connection,
    *,
    generation: int,
    file_identity: FileIdentity,
    path: str,
    references: str,
    source_kind: str = "pdf",
) -> None:
    connection.execute(
        """INSERT INTO catalog_generation_documents(
        generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
        birthtime_ns,source_status,processing_signature,classifier_signature,
        primary_kind,primary_subtype,primary_project,confidence,uncertainty,
        standard_references_json,organizations_json,clients_json,projects_json,
        workstreams_json,topics_json,equipment_json,activities_json,
        classification_json,catalog_status,active,last_seen_catalog_run_id,
        updated_ns)
        VALUES(?,?,?,?,?,?,100,20,10,'done','pdf-v11:fixture',
        'classifier-v1','estudio',NULL,NULL,0.9,'baja',?,'[]','[]','[]','[]',
        '[]','[]','[]','{}','classified',1,1,40)""",
        (
            generation,
            source_kind,
            file_identity.packed_key,
            path,
            str(file_identity.volume_id),
            str(file_identity.file_id),
            references,
        ),
    )


def _create_catalog(path: Path) -> None:
    initialize_document_catalog(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            """INSERT INTO catalog_generations(
            generation_id,source_kind,status,started_ns,completed_ns,published_ns)
            VALUES(?,'pdf',?,?,?,?)""",
            (
                (1, "superseded", 1, 2, 3),
                (2, "published", 4, 5, 6),
            ),
        )
        connection.execute(
            """INSERT INTO catalog_publications(
            source_kind,generation_id,published_ns) VALUES('pdf',2,6)"""
        )
        _insert_catalog_document(
            connection,
            generation=1,
            file_identity=FileIdentity(1, 2),
            path="C:/docs/proteccion.pdf",
            references='[{"authority":"IEC","identifier":"IEC-61850"},{"identifier":"SN-2048"}]',
        )
        _insert_catalog_document(
            connection,
            generation=2,
            file_identity=FileIdentity(3, 4),
            path="C:/docs/new.pdf",
            references='[{"identifier":"IEC-99999"}]',
        )


LOOKUP_ORCHESTRATION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "knowledge_exact" / "lookup_exact_orchestration_v1.json"
)


def _state_file_bytes(state: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(state.iterdir()) if path.is_file()}


def test_private_code_lookup_signature_is_frozen() -> None:
    assert str(inspect.signature(knowledge_exact_module._lookup_code)) == (
        "(path: 'Path', owner: 'OwnerSnapshot', "
        "terms: 'Sequence[ExactLookupTerm]', control: '_QueryControl', "
        "per_term_limit: 'int', path_scope: 'tuple[str, ...] | None') -> "
        "'tuple[list[ExactEvidenceMatch], list[ExactOwnerReport]]'"
    )


def test_lookup_exact_orchestration_preserves_primary_state_bytes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    full_digest = _create_inventory(state / "dedup.sqlite3")
    _create_code(state / "code.sqlite3")
    _create_catalog(state / "document_catalog.sqlite3")
    paths = KnowledgeStatePaths.from_directory(state)
    terms = (
        ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/proteccion.pdf"),
        ExactLookupTerm(ExactLookupKind.NAME, "A%_# report.pdf"),
        ExactLookupTerm(ExactLookupKind.HASH, full_digest),
        ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),
        ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-61850"),
        ExactLookupTerm(ExactLookupKind.SERIAL, "SN-2048"),
    )
    before = _state_file_bytes(state)
    clock_values = iter((100, 110, 200, 215, 300, 321))

    result = lookup_exact(
        paths,
        _snapshot(_catalog_owner(), _inventory_owner(), _code_owner()),
        ExactLookupRequest(
            terms,
            limit=10,
            max_observed_rows=100,
            owner_scope=("catalog", "inventory", "code"),
        ),
        clock_ns=lambda: next(clock_values),
    )

    characterization = {
        "result_xxh3_128": fingerprint_text(result.to_json()).xxh3_128,
        "matches": [
            [
                match.term.kind.value,
                match.resource.owner,
                match.resource.current_path,
                match.source_rank,
            ]
            for match in result.matches
        ],
        "reports": [
            [
                report.owner,
                report.term.kind.value,
                report.status.value,
                report.executed,
                report.available,
            ]
            for report in result.reports
        ],
        "owner_timings": [timing.to_dict() for timing in result.owner_timings],
        "summary": {
            "complete": result.complete,
            "truncated": result.truncated,
            "omitted_matches": result.omitted_matches,
            "rows_observed": result.rows_observed,
            "sqlite_steps": result.sqlite_steps,
            "warnings": list(result.warnings),
        },
    }
    fixture = json.loads(LOOKUP_ORCHESTRATION_FIXTURE.read_text(encoding="utf-8"))

    assert fixture["schema"] == "neocortex-lookup-exact-orchestration/v1"
    assert characterization == fixture["expected"]
    after = _state_file_bytes(state)
    database_names = {
        "code.sqlite3",
        "dedup.sqlite3",
        "document_catalog.sqlite3",
    }
    allowed_entries = database_names | {
        f"{name}{suffix}" for name in database_names for suffix in ("-wal", "-shm")
    }

    # SQLite may maintain WAL/SHM metadata for a read-only WAL owner.  Freeze
    # primary bytes and reject every filesystem effect outside those sidecars.
    assert {name: before[name] for name in database_names} == {
        name: after[name] for name in database_names
    }
    assert set(before) <= allowed_entries
    assert set(after) <= allowed_entries
    assert all(
        payload == b""
        for name, payload in after.items()
        if name not in before and name.endswith("-wal")
    )


def test_plan_exact_terms_are_typed_and_serial_variants_are_deduplicated() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(r"C:\Corpus\report-2026.pdf serial SN-2048 symbol control.validate")
    )

    terms = classify_plan_exact_terms(plan)

    assert [(term.kind, term.value) for term in terms] == [
        (ExactLookupKind.PATH, r"C:\Corpus\report-2026.pdf"),
        (ExactLookupKind.SERIAL, "SN-2048"),
        (ExactLookupKind.SYMBOL, "control.validate"),
    ]


@pytest.mark.parametrize(
    "surface",
    (
        "C:/docs/a.pdf",
        "/srv/docs/a.pdf",
        "./docs/a.pdf",
        "../docs/a.pdf",
        "docs/a.pdf",
    ),
)
def test_exact_path_typing_covers_drive_posix_and_relative_paths(surface: str) -> None:
    base = plan_knowledge_query(KnowledgeQuery("lookup"))
    plan = _legacy_plan_with_exact_terms(base, (surface,))

    assert classify_plan_exact_terms(plan)[0].kind is ExactLookupKind.PATH


@pytest.mark.parametrize(
    "surface",
    (
        "manual.dwg",
        "archive.zip",
        "table.csv",
        "deploy.ps1",
        "photo.webp",
        "recording.opus",
        "A%_# report.custom9",
    ),
)
def test_arbitrary_reasonable_extensions_are_names_without_code_evidence(
    surface: str,
) -> None:
    base = plan_knowledge_query(KnowledgeQuery("lookup"))
    plan = _legacy_plan_with_exact_terms(base, (surface,))

    assert classify_plan_exact_terms(plan)[0].kind is ExactLookupKind.NAME


def test_code_context_types_bare_camel_and_snake_case_as_symbols() -> None:
    base = plan_knowledge_query(KnowledgeQuery("definition KnowledgeSnapshot calculate_breaker"))
    plan = _legacy_plan_with_exact_terms(
        base,
        ("KnowledgeSnapshot", "calculate_breaker"),
    )

    assert [(term.kind, term.value) for term in classify_plan_exact_terms(plan)] == [
        (ExactLookupKind.SYMBOL, "KnowledgeSnapshot"),
        (ExactLookupKind.SYMBOL, "calculate_breaker"),
    ]


@pytest.mark.parametrize(
    "surface",
    (
        "snapshot.validate",
        "snake_case.method",
        "snmp.client",
        "serial_port.open",
    ),
)
def test_serial_prefix_words_require_a_full_serial_boundary(surface: str) -> None:
    base = plan_knowledge_query(KnowledgeQuery("symbol lookup"))
    plan = _legacy_plan_with_exact_terms(base, (surface,))

    assert classify_plan_exact_terms(plan)[0].kind is ExactLookupKind.SYMBOL


@pytest.mark.parametrize(
    ("source_kinds", "formats", "expected_owners"),
    (
        ((), (), {"inventory", "code", "catalog"}),
        (("code",), (), {"inventory", "code"}),
        (("pdf",), (), {"inventory", "catalog"}),
        (("office",), (), {"inventory", "catalog"}),
        ((), ("ps1",), {"inventory", "code"}),
        ((), ("opus",), {"inventory", "catalog"}),
        ((), ("webp",), {"inventory"}),
    ),
)
def test_plan_source_and_format_filters_scope_exact_owners_before_lookup(
    tmp_path: Path,
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    expected_owners: set[str],
) -> None:
    base = plan_knowledge_query(
        KnowledgeQuery("lookup", source_kinds=source_kinds, formats=formats)
    )
    plan = _legacy_plan_with_exact_terms(base, ("C:/docs/a.pdf",))
    snapshot = _snapshot(
        _absent_owner("inventory", 7),
        _absent_owner("code", 2),
        _absent_owner("catalog", 6),
    )

    result = lookup_plan_exact(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        snapshot,
    )

    assert result is not None
    assert {report.owner for report in result.reports} == expected_owners
    assert not (tmp_path / "state").exists()


def test_catalog_lookup_uses_captured_generation_and_exact_json_element(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_catalog(state / "document_catalog.sqlite3")
    snapshot = _snapshot(
        _catalog_owner(),
        _absent_owner("inventory", 7),
        _absent_owner("code", 2),
    )
    paths = KnowledgeStatePaths.from_directory(state)

    exact = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-61850"),),
            limit=5,
        ),
    )
    prefix = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-6185"),),
            limit=5,
        ),
    )

    assert exact.complete
    assert len(exact.matches) == 1
    assert exact.matches[0].generation == 1
    assert exact.matches[0].resource.current_path == "C:/docs/proteccion.pdf"
    assert (
        "standard_identifier",
        "IEC-61850",
    ) in exact.matches[0].evidence.identifiers
    assert prefix.complete
    assert prefix.matches == ()


def _assert_catalog_multi_term_reports(
    result: ExactLookupResult,
    terms: tuple[ExactLookupTerm, ...],
) -> None:
    assert result.complete
    assert [report.term for report in result.reports] == list(terms)
    assert [report.returned for report in result.reports] == [1, 1, 1]
    assert all(report.status is ExactLookupStatus.COMPLETE for report in result.reports)
    assert all(report.rows_observed == 1 for report in result.reports)
    assert all(report.sqlite_steps > 0 for report in result.reports)
    assert result.reports[0].sqlite_steps > result.reports[1].sqlite_steps
    assert result.reports[1].warnings == ("catalog_has_no_standard_identifier_index",)
    assert result.reports[2].warnings == ("catalog_has_no_basename_index",)


def _assert_catalog_multi_term_matches(
    result: ExactLookupResult,
    terms: tuple[ExactLookupTerm, ...],
) -> None:
    assert [match.term for match in result.matches] == list(terms)
    assert all(match.resource.current_path == "C:/docs/proteccion.pdf" for match in result.matches)
    assert all(match.source_rank == 1 for match in result.matches)
    assert all(match.generation == 1 for match in result.matches)
    assert all(match.model_signature == "classifier-v1" for match in result.matches)
    assert all(match.evidence.extractor == "document-catalog" for match in result.matches)
    assert all(match.evidence.extractor_version == "6" for match in result.matches)
    assert len({match.evidence.evidence_id for match in result.matches}) == 3


def test_catalog_multi_term_contract_preserves_order_provenance_and_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    terms = (
        ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/proteccion.pdf"),
        ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-61850"),
        ExactLookupTerm(ExactLookupKind.NAME, "proteccion.pdf"),
    )
    before = catalog.read_bytes()
    wal = catalog.with_name(f"{catalog.name}-wal")
    wal_before = wal.read_bytes() if wal.exists() else None

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_catalog_owner()),
        ExactLookupRequest(terms, limit=5, owner_scope=("catalog",)),
    )

    _assert_catalog_multi_term_reports(result, terms)
    _assert_catalog_multi_term_matches(result, terms)
    assert catalog.read_bytes() == before
    assert (wal.read_bytes() if wal.exists() else None) == wal_before


def test_serial_without_contractual_field_is_unsupported_not_catalog_identifier(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_catalog(state / "document_catalog.sqlite3")
    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_catalog_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.SERIAL, "SN-2048"),),
            limit=5,
        ),
    )

    assert result.matches == ()
    assert not result.complete
    assert len(result.reports) == 1
    assert result.reports[0].status is ExactLookupStatus.UNSUPPORTED
    assert result.reports[0].reason == "serial_field_not_contractual_in_phase1_owners"


def test_catalog_invalid_json_and_hostile_identifier_fail_closed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_catalog_owner())

    hostile = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (
                ExactLookupTerm(
                    ExactLookupKind.IDENTIFIER,
                    "IEC-61850') OR 1=1 --",
                ),
            ),
            limit=5,
        ),
    )
    with closing(sqlite3.connect(catalog)) as connection, connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM catalog_generation_documents").fetchone()[0]
            == 2
        )
        connection.execute(
            """UPDATE catalog_generation_documents
            SET standard_references_json='not-json' WHERE generation_id=1"""
        )
    malformed = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-61850"),),
            limit=5,
        ),
    )

    assert hostile.matches == ()
    assert hostile.complete
    assert malformed.matches == ()
    assert not malformed.complete
    assert malformed.reports[0].reason == "catalog_identifier_json_invalid"


def test_catalog_row_budget_and_unicode_nocase_cannot_report_false_complete(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        for index in range(5):
            _insert_catalog_document(
                connection,
                generation=1,
                file_identity=FileIdentity(10 + index, 20 + index),
                path=f"C:/docs/many-{index}.pdf",
                references='[{"identifier":"IEC-MANY"}]',
            )

    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_catalog_owner())
    bounded = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-MANY"),),
            limit=5,
            max_observed_rows=5,
        ),
    )
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            """UPDATE catalog_generation_documents
            SET standard_references_json=?
            WHERE generation_id=1 AND file_key=?""",
            ('[{"identifier":"ÉT-1"}]', FileIdentity(1, 2).packed_key),
        )
    unicode_case = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "ét-1"),),
            limit=5,
        ),
    )

    assert bounded.truncated
    assert not bounded.complete
    assert bounded.reports[0].status is ExactLookupStatus.PARTIAL
    assert bounded.reports[0].reason == "exact_result_limit_reached"
    assert unicode_case.matches == ()
    assert not unicode_case.complete
    assert unicode_case.reports[0].reason == "unicode_casefold_not_provable"


@pytest.mark.parametrize(
    ("assignment", "expected_warning"),
    (
        ("source_status='partial'", "catalog_source_status_partial"),
        ("uncertainty='alta'", "catalog_uncertainty_requires_review"),
        ("catalog_status='review'", "catalog_review_required"),
        ("catalog_status='error'", "catalog_classification_error"),
    ),
)
def test_catalog_quality_states_are_observably_partial(
    tmp_path: Path,
    assignment: str,
    expected_warning: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            f"""UPDATE catalog_generation_documents SET {assignment}
            WHERE generation_id=1"""
        )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_catalog_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/proteccion.pdf"),),
            owner_scope=("catalog",),
        ),
    )

    assert len(result.matches) == 1
    assert result.matches[0].revision.state is RevisionState.PARTIAL
    assert expected_warning in result.matches[0].warnings
    assert result.reports[0].status is ExactLookupStatus.PARTIAL
    assert expected_warning in result.reports[0].warnings


def test_catalog_identifier_error_coverage_cannot_report_false_absence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            """UPDATE catalog_generation_documents
            SET catalog_status='error',uncertainty='alta',
                standard_references_json='[]'
            WHERE generation_id=1"""
        )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_catalog_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-NEVER"),),
            owner_scope=("catalog",),
        ),
    )

    assert result.matches == ()
    assert not result.complete
    assert result.reports[0].status is ExactLookupStatus.PARTIAL
    assert result.reports[0].reason == "catalog_identifier_coverage_incomplete"


def test_catalog_source_alias_is_applied_before_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    catalog = state / "document_catalog.sqlite3"
    _create_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            """INSERT INTO catalog_generations(
            generation_id,source_kind,status,started_ns,completed_ns,published_ns)
            VALUES(3,'xlsx','published',7,8,9)"""
        )
        _insert_catalog_document(
            connection,
            generation=1,
            file_identity=FileIdentity(9, 10),
            path="C:/docs/A.pdf",
            references='[{"identifier":"IEC-FILTER"}]',
        )
        _insert_catalog_document(
            connection,
            generation=3,
            file_identity=FileIdentity(11, 12),
            path="C:/docs/Z.xlsx",
            references='[{"identifier":"IEC-FILTER"}]',
            source_kind="xlsx",
        )
    owner = replace(
        _catalog_owner(),
        publications=(
            PublicationHead("pdf", "catalog:1", 1),
            PublicationHead("xlsx", "catalog:3", 3),
        ),
    )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(owner),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-FILTER"),),
            limit=1,
            owner_scope=("catalog",),
            source_kinds=("office",),
        ),
    )

    assert [match.resource.current_path for match in result.matches] == ["C:/docs/Z.xlsx"]
    assert result.reports[0].rows_observed == 1


def test_inventory_path_name_and_full_hash_are_constrained_but_partial(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    digest = _create_inventory(state / "dedup.sqlite3")
    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(
            _inventory_owner(),
            _absent_owner("code", 2),
            _absent_owner("catalog", 6),
        ),
        ExactLookupRequest(
            (
                ExactLookupTerm(
                    ExactLookupKind.PATH,
                    "C:/docs/A%_# report.pdf",
                ),
                ExactLookupTerm(ExactLookupKind.NAME, "A%_# report.pdf"),
                ExactLookupTerm(ExactLookupKind.HASH, digest),
            ),
            limit=10,
        ),
    )

    inventory_matches = [match for match in result.matches if match.resource.owner == "inventory"]
    assert len(inventory_matches) == 3
    assert {match.term.kind for match in inventory_matches} == {
        ExactLookupKind.PATH,
        ExactLookupKind.NAME,
        ExactLookupKind.HASH,
    }
    assert {match.resource.resource_id for match in inventory_matches} == {"resource:file:1:2:10"}
    hash_match = next(
        match for match in inventory_matches if match.term.kind is ExactLookupKind.HASH
    )
    assert hash_match.evidence.identifiers == ((FULL_ALGORITHM, digest),)
    assert all(
        report.status is ExactLookupStatus.PARTIAL
        for report in result.reports
        if report.owner == "inventory"
    )
    assert not result.complete


def test_incompatible_inventory_snapshot_never_opens_exact_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inventory = state / "dedup.sqlite3"
    inventory.write_bytes(b"must not be opened as SQLite")
    before = inventory.read_bytes()

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(
            OwnerSnapshot(
                "inventory",
                OwnerAvailability.INCOMPATIBLE,
                8,
                7,
                error_code="schema_too_old",
            ),
        ),
        ExactLookupRequest(
            (
                ExactLookupTerm(
                    ExactLookupKind.PATH,
                    "C:/docs/A%_# report.pdf",
                ),
            ),
            owner_scope=("inventory",),
        ),
    )

    assert result.matches == ()
    assert len(result.reports) == 1
    assert result.reports[0].status is ExactLookupStatus.UNAVAILABLE
    assert result.reports[0].reason == "owner_unavailable:incompatible"
    assert not result.reports[0].executed
    assert inventory.read_bytes() == before


def test_inventory_checkpoint_change_after_snapshot_abstains(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inventory = state / "dedup.sqlite3"
    _create_inventory(inventory)
    with closing(sqlite3.connect(inventory)) as connection, connection:
        connection.execute("UPDATE inventory_checkpoints SET updated_ns=4 WHERE root='C:/docs'")

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_inventory_owner()),
        ExactLookupRequest(
            (
                ExactLookupTerm(
                    ExactLookupKind.PATH,
                    "C:/docs/A%_# report.pdf",
                ),
            ),
            limit=5,
        ),
    )

    assert result.matches == ()
    assert result.reports[0].status is ExactLookupStatus.PARTIAL
    assert result.reports[0].reason == "inventory_changed_after_snapshot"


def test_stable_match_ids_use_observed_resource_evidence_not_query_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_exact_module,
        "_PATH_COLLATION",
        sqlite_path_collation(platform_name="nt"),
    )
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory(state / "dedup.sqlite3")
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_inventory_owner())

    upper = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/A%_# report.pdf"),),
            owner_scope=("inventory",),
        ),
    ).matches[0]
    lower = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.PATH, "c:/docs/a%_# REPORT.pdf"),),
            owner_scope=("inventory",),
        ),
    ).matches[0]

    assert upper.term.term_id != lower.term.term_id
    assert upper.revision.revision_id == lower.revision.revision_id
    assert upper.evidence.evidence_id == lower.evidence.evidence_id
    assert upper.match_id == lower.match_id


def test_owner_lookahead_contributes_to_omitted_match_count(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inventory = state / "dedup.sqlite3"
    _create_inventory(inventory)

    def blob(value: int) -> bytes:
        return value.to_bytes(16, "little")

    with closing(sqlite3.connect(inventory)) as connection, connection:
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,?,?,?)""",
            ("C:/other/A%_# report.pdf", blob(3), blob(4), 100, 20, 10),
        )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_inventory_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.NAME, "A%_# report.pdf"),),
            limit=1,
            owner_scope=("inventory",),
        ),
    )

    assert len(result.matches) == 1
    assert result.truncated
    assert result.omitted_matches == 1
    assert result.reports[0].rows_observed == 2


def test_inventory_format_and_source_predicates_precede_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inventory = state / "dedup.sqlite3"
    digest = _create_inventory(inventory)

    def blob(value: int) -> bytes:
        return value.to_bytes(16, "little")

    with closing(sqlite3.connect(inventory)) as connection, connection:
        connection.execute("UPDATE files SET path='C:/docs/A.docx' WHERE scan_id=1")
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,?,?,?)""",
            ("C:/docs/Z.pdf", blob(3), blob(4), 100, 20, 10),
        )
        connection.execute(
            """INSERT INTO fingerprints(
            volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest)
            VALUES(?,?,?,?,?,?,?)""",
            (blob(3), blob(4), 100, 20, 10, FULL_ALGORITHM, bytes.fromhex(digest)),
        )
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_inventory_owner())
    term = ExactLookupTerm(ExactLookupKind.HASH, digest)

    by_format = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (term,),
            limit=1,
            owner_scope=("inventory",),
            formats=("pdf",),
        ),
    )
    by_source = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (term,),
            limit=1,
            owner_scope=("inventory",),
            source_kinds=("pdf",),
        ),
    )

    assert [match.resource.current_path for match in by_format.matches] == ["C:/docs/Z.pdf"]
    assert [match.resource.current_path for match in by_source.matches] == ["C:/docs/Z.pdf"]


def test_invalid_row_does_not_count_as_omitted_valid_match(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inventory = state / "dedup.sqlite3"
    _create_inventory(inventory)

    def blob(value: int) -> bytes:
        return value.to_bytes(16, "little")

    with closing(sqlite3.connect(inventory)) as connection, connection:
        connection.execute(
            "UPDATE files SET volume_id=? WHERE scan_id=1",
            (sqlite3.Binary(b"invalid"),),
        )
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(1,?,?,?,?,?,?)""",
            ("C:/other/A%_# report.pdf", blob(3), blob(4), 100, 20, 10),
        )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_inventory_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.NAME, "A%_# report.pdf"),),
            limit=1,
            owner_scope=("inventory",),
        ),
    )

    assert len(result.matches) == 1
    assert result.truncated
    assert result.omitted_matches == 0
    assert result.reports[0].omitted_matches == 0


def test_preflight_row_and_nonmultiple_vm_budgets_fail_partial_not_by_assertion(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory(state / "dedup.sqlite3")
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_inventory_owner())
    term = ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/A%_# report.pdf")

    row_bounded = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (term,),
            limit=1,
            max_observed_rows=1,
            owner_scope=("inventory",),
        ),
    )
    step_bounded = lookup_exact(
        paths,
        snapshot,
        ExactLookupRequest(
            (term,),
            limit=1,
            max_observed_rows=5,
            max_sqlite_steps=1_500,
            owner_scope=("inventory",),
        ),
    )

    assert row_bounded.truncated
    assert row_bounded.reports[0].reason == "exact_work_budget_exhausted"
    assert step_bounded.truncated
    assert step_bounded.sqlite_steps <= 1_500


def test_global_budget_is_shared_in_fixed_owner_order(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory(state / "dedup.sqlite3")
    _create_code(state / "code.sqlite3")
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot(_code_owner(), _inventory_owner())
    term = ExactLookupTerm(ExactLookupKind.PATH, "C:/docs/A%_# report.pdf")
    requests = (
        ExactLookupRequest(
            (term,),
            limit=1,
            max_observed_rows=1,
            owner_scope=("code", "inventory"),
        ),
        ExactLookupRequest(
            (term,),
            limit=1,
            max_sqlite_steps=knowledge_exact_module.SQLITE_PROGRESS_INTERVAL,
            owner_scope=("code", "inventory"),
        ),
    )

    for request in requests:
        result = lookup_exact(paths, snapshot, request)
        code_report = next(report for report in result.reports if report.owner == "code")

        assert code_report.executed is False
        assert code_report.truncated is True
        assert code_report.reason == "exact_global_work_budget_exhausted"
        assert [timing.owner for timing in result.owner_timings] == [
            "inventory",
            "code",
        ]
        assert result.owner_timings[-1].executed is False
        assert result.rows_observed <= request.max_observed_rows
        assert result.sqlite_steps <= request.max_sqlite_steps


def test_code_exact_path_hash_and_symbol_need_no_fts_chunks(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_code(state / "code.sqlite3")
    snapshot = _snapshot(
        _code_owner(),
        _absent_owner("inventory", 7),
        _absent_owner("catalog", 6),
    )
    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        snapshot,
        ExactLookupRequest(
            (
                ExactLookupTerm(ExactLookupKind.PATH, "C:/src/control.py"),
                ExactLookupTerm(ExactLookupKind.NAME, "control.py"),
                ExactLookupTerm(ExactLookupKind.HASH, "aa" * 16),
                ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),
            ),
            limit=10,
        ),
    )
    prefix = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        snapshot,
        ExactLookupRequest(
            (
                ExactLookupTerm(
                    ExactLookupKind.SYMBOL,
                    "control.validate_extra",
                ),
            ),
            limit=5,
        ),
    )

    code_matches = [match for match in result.matches if match.resource.owner == "code"]
    assert len(code_matches) == 4
    assert {match.resource.resource_id for match in code_matches} == {"resource:file:1:2:10"}
    symbol = next(match for match in code_matches if match.term.kind is ExactLookupKind.SYMBOL)
    assert symbol.evidence.symbol == "control.validate"
    assert symbol.evidence.start_line == 1
    assert all(
        report.status is ExactLookupStatus.PARTIAL
        for report in result.reports
        if report.owner == "code"
    )
    assert prefix.matches == ()
    assert prefix.reports[0].reason == "code_owner_non_generational"


def test_code_exact_reads_committed_wal_without_mutating_owner_bytes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    writer = sqlite3.connect(code)
    try:
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.execute("PRAGMA foreign_keys=ON")
        _insert_code_version(writer)
        writer.commit()
    finally:
        writer.close()

    wal = Path(f"{code}-wal")
    primary_before = code.read_bytes()
    wal_before = wal.read_bytes()
    assert wal_before

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_code_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),),
            owner_scope=("code",),
        ),
    )

    assert [match.evidence.symbol for match in result.matches] == ["control.validate"]
    assert code.read_bytes() == primary_before
    assert wal.read_bytes() == wal_before


def test_unconfirmed_code_symbol_is_explicitly_partial(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    _create_code(code)
    with closing(sqlite3.connect(code)) as connection, connection:
        connection.execute("UPDATE symbols SET confirmed=0 WHERE symbol_id=1")

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_code_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),),
            owner_scope=("code",),
        ),
    )

    assert len(result.matches) == 1
    assert result.matches[0].revision.state is RevisionState.PARTIAL
    assert "code_symbol_unconfirmed" in result.matches[0].warnings
    assert result.reports[0].reason == "code_symbol_unconfirmed"
    assert "code_symbol_unconfirmed" in result.reports[0].warnings


def test_code_format_predicate_precedes_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    _create_code(code)
    with closing(sqlite3.connect(code)) as connection, connection:
        connection.execute("UPDATE files SET current_path='C:/src/A.js' WHERE file_id=1")
        connection.execute(
            "UPDATE file_versions SET path_observed='C:/src/A.js' WHERE version_id=1"
        )
        connection.execute(
            """INSERT INTO files(
            file_id,volume_id,physical_file_id,current_path,current_version_id,
            status,first_seen_run_id,last_seen_run_id)
            VALUES(2,'3','4','C:/src/Z.py',NULL,'current',1,1)"""
        )
        connection.execute(
            """INSERT INTO file_versions(
            version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
            raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
            normalized_xxh3_128,token_xxh3_128,structure_xxh3_128,encoding,
            language,artifact_kind,generated,vendored,classification_confidence,
            classification_evidence_json,analysis_status,processing_signature,
            analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,
            text_truncated,provenance_json,first_observed_run_id,last_observed_run_id,
            valid_from_ns,invalidated_ns)
            SELECT 2,2,'C:/src/Z.py',size,mtime_ns,birthtime_ns,
            raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
            normalized_xxh3_128,token_xxh3_128,structure_xxh3_128,encoding,
            'python',artifact_kind,generated,vendored,classification_confidence,
            classification_evidence_json,analysis_status,processing_signature,
            analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,
            text_truncated,provenance_json,first_observed_run_id,last_observed_run_id,
            valid_from_ns,invalidated_ns FROM file_versions WHERE version_id=1"""
        )
        connection.execute("UPDATE files SET current_version_id=2 WHERE file_id=2")
    owner = replace(
        _code_owner(),
        watermarks=(
            LogicalWatermark("current_files", "2"),
            LogicalWatermark("latest_version_id", "2"),
            LogicalWatermark("latest_analysis_run_id", "1"),
            LogicalWatermark("visibility", "best_effort_non_generational"),
        ),
    )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(owner),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.HASH, "aa" * 16),),
            limit=1,
            owner_scope=("code",),
            formats=("python",),
        ),
    )

    assert [match.resource.current_path for match in result.matches] == ["C:/src/Z.py"]


def test_code_watermark_change_after_snapshot_abstains(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    _create_code(code)
    with closing(sqlite3.connect(code)) as connection, connection:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors,
            summary_json)
            VALUES(2,2,2,'new-run','completed',3,4,0,0,0,0,'{}')"""
        )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_code_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),),
            limit=5,
        ),
    )

    assert result.matches == ()
    assert result.reports[0].status is ExactLookupStatus.PARTIAL
    assert result.reports[0].reason == "code_changed_after_snapshot"


def test_missing_available_owner_does_not_create_state(tmp_path: Path) -> None:
    state = tmp_path / "missing-state"
    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        _snapshot(_code_owner()),
        ExactLookupRequest(
            (ExactLookupTerm(ExactLookupKind.SYMBOL, "control.validate"),),
            limit=5,
        ),
    )

    assert result.matches == ()
    assert result.reports[0].status is ExactLookupStatus.PARTIAL
    assert result.reports[0].reason is not None
    assert result.reports[0].reason.startswith("owner_read_failed:")
    assert not state.exists()


def test_cancellation_propagates_before_owner_read(tmp_path: Path) -> None:
    class Cancelled(RuntimeError):
        pass

    calls = 0
    failure = Cancelled("stop")

    def cancel() -> None:
        nonlocal calls
        calls += 1
        raise failure

    with pytest.raises(Cancelled, match="stop") as caught:
        lookup_exact(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_absent_owner("catalog", 6)),
            ExactLookupRequest(
                (ExactLookupTerm(ExactLookupKind.IDENTIFIER, "IEC-61850"),),
                limit=5,
            ),
            cancellation_check=cancel,
        )
    assert calls == 1
    assert caught.value is failure


def test_cancellation_inside_owner_query_is_not_downgraded_to_partial(
    tmp_path: Path,
) -> None:
    class Cancelled(RuntimeError):
        pass

    state = tmp_path / "state"
    state.mkdir()
    _create_inventory(state / "dedup.sqlite3")
    calls = 0
    failure = Cancelled("stop during owner query")

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise failure

    with pytest.raises(Cancelled, match="stop during owner query") as caught:
        lookup_exact(
            KnowledgeStatePaths.from_directory(state),
            _snapshot(_inventory_owner()),
            ExactLookupRequest(
                (
                    ExactLookupTerm(
                        ExactLookupKind.PATH,
                        "C:/docs/A%_# report.pdf",
                    ),
                ),
                limit=5,
            ),
            cancellation_check=cancel,
        )
    assert calls >= 3
    assert caught.value is failure


def test_request_bounds_and_parameterized_hostile_identifier() -> None:
    with pytest.raises(ValueError, match="at most 64"):
        ExactLookupRequest(
            tuple(
                ExactLookupTerm(ExactLookupKind.IDENTIFIER, f"IEC-{index}") for index in range(65)
            )
        )
    term = ExactLookupTerm(
        ExactLookupKind.IDENTIFIER,
        "IEC-61850') OR 1=1 --",
    )
    assert term.value == "IEC-61850') OR 1=1 --"


def test_exactly_sixty_four_terms_are_processed_deterministically(
    tmp_path: Path,
) -> None:
    terms = tuple(
        ExactLookupTerm(ExactLookupKind.IDENTIFIER, f"IEC-{index:04d}") for index in range(64)
    )
    result = lookup_exact(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_absent_owner("catalog", 6)),
        ExactLookupRequest(terms, owner_scope=("catalog",)),
    )

    assert len(result.reports) == 64
    assert [report.term for report in result.reports] == list(terms)


def test_plan_exact_candidate_limit_override_is_backward_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[ExactLookupRequest] = []

    def capture_request(
        _paths: KnowledgeStatePaths,
        _snapshot_value: KnowledgeSnapshot,
        request: ExactLookupRequest,
        **_kwargs: object,
    ) -> None:
        observed.append(request)
        return None

    monkeypatch.setattr(knowledge_exact_module, "lookup_exact", capture_request)
    plan = plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=2))
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    snapshot = _snapshot()

    default_result = lookup_plan_exact(
        paths,
        plan,
        snapshot,
        max_observed_rows=1,
    )
    override_result = lookup_plan_exact(
        paths,
        plan,
        snapshot,
        candidate_limit=7,
        max_observed_rows=1,
    )

    assert default_result is None
    assert override_result is None
    assert [request.limit for request in observed] == [plan.limit, 7]
    assert [request.max_observed_rows for request in observed] == [plan.limit, 7]


# endregion [02]
