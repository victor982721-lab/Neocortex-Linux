"""Real-SQLite regressions for catalog filtering and exact evidence."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_catalog_search.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.document_catalog import initialize_document_catalog
from _04_Nucleo_Operativo.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    PublicationHead,
    RevisionState,
)
from _04_Nucleo_Operativo.knowledge_planner import (
    KnowledgeQuery,
    plan_knowledge_query,
)
from _04_Nucleo_Operativo.knowledge_search import (
    _catalog_ranking,
    execute_knowledge_search,
)
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths
# endregion [01]

# region [02] Implementación


_ALPHA_FILE_KEY = "00000000000000000000000000000001:00000000000000000000000000000002"
_BETA_FILE_KEY = "00000000000000000000000000000003:00000000000000000000000000000004"
_CATALOG_ONLY_FILE_KEY = "00000000000000000000000000000005:00000000000000000000000000000006"


def _snapshot(*, catalog_heads: bool = True) -> KnowledgeSnapshot:
    publications = (PublicationHead("pdf", "catalog:1", 1),) if catalog_heads else ()
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
            OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
            OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
            OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
            OwnerSnapshot(
                "catalog",
                OwnerAvailability.AVAILABLE,
                6,
                6,
                publications=publications,
            ),
            OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
        ),
    )


def _create_pdf_state(state: Path) -> None:
    state.mkdir(parents=True)
    with sqlite3.connect(state / "pdf.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,
                size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        connection.executemany(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
            (
                (
                    _ALPHA_FILE_KEY,
                    "C:/docs/alpha.pdf",
                    "done",
                    0,
                    100,
                    20,
                    10,
                    "pdf-v11:alpha",
                    7,
                ),
                (
                    _BETA_FILE_KEY,
                    "C:/docs/beta.pdf",
                    "done",
                    0,
                    100,
                    21,
                    11,
                    "pdf-v11:beta",
                    7,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO page_fts VALUES(?,?,?,?)",
            (
                (
                    _ALPHA_FILE_KEY,
                    "C:/docs/alpha.pdf",
                    1,
                    "protección interruptor proyecto alpha",
                ),
                (
                    _BETA_FILE_KEY,
                    "C:/docs/beta.pdf",
                    1,
                    "protección interruptor proyecto beta",
                ),
            ),
        )


def _insert_catalog_document(
    connection: sqlite3.Connection,
    *,
    file_key: str,
    path: str,
    volume_id: int,
    file_id: int,
    birthtime_ns: int,
    project: str,
    standard_references_json: str = "[]",
    source_status: str = "done",
    catalog_status: str = "classified",
    uncertainty: str = "baja",
) -> None:
    connection.execute(
        """
        INSERT INTO catalog_generation_documents(
            generation_id,source_kind,file_key,path,volume_id,file_id,size,
            mtime_ns,birthtime_ns,source_status,processing_signature,
            classifier_signature,primary_kind,primary_subtype,primary_project,
            confidence,uncertainty,standard_references_json,organizations_json,
            clients_json,projects_json,workstreams_json,topics_json,
            equipment_json,activities_json,classification_json,catalog_status,
            active,last_seen_catalog_run_id,updated_ns
        ) VALUES(
            1,'pdf',:file_key,:path,:volume_id,:file_id,100,20,:birthtime_ns,
            :source_status,'pdf-v11:fixture','classifier-v1','estudio',
            'coordinacion',:project,0.9,:uncertainty,:standard_references_json,
            '[]','[]',:projects_json,'[]','[]','[]','[]','{}',:catalog_status,
            1,7,40
        )
        """,
        {
            "file_key": file_key,
            "path": path,
            "volume_id": str(volume_id),
            "file_id": str(file_id),
            "birthtime_ns": birthtime_ns,
            "project": project,
            "projects_json": json.dumps(
                [{"label": project}],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "standard_references_json": standard_references_json,
            "source_status": source_status,
            "catalog_status": catalog_status,
            "uncertainty": uncertainty,
        },
    )


def _create_catalog_state(
    state: Path,
    *,
    source_status: str = "done",
    catalog_status: str = "classified",
    uncertainty: str = "baja",
    include_filter_rows: bool = False,
) -> None:
    state.mkdir(parents=True, exist_ok=True)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        connection.executescript(
            """
            INSERT INTO catalog_generations(
                generation_id,source_kind,status,started_ns,completed_ns,
                published_ns
            ) VALUES(1,'pdf','published',1,2,3);
            INSERT INTO catalog_publications(
                source_kind,generation_id,published_ns
            ) VALUES('pdf',1,3);
            """
        )
        _insert_catalog_document(
            connection,
            file_key=_ALPHA_FILE_KEY,
            path="C:/docs/alpha.pdf",
            volume_id=1,
            file_id=2,
            birthtime_ns=10,
            project="Alpha",
            standard_references_json='[{"identifier":"IEC-61850"}]',
            source_status=source_status,
            catalog_status=catalog_status,
            uncertainty=uncertainty,
        )
        if include_filter_rows:
            _insert_catalog_document(
                connection,
                file_key=_BETA_FILE_KEY,
                path="C:/docs/beta.pdf",
                volume_id=3,
                file_id=4,
                birthtime_ns=11,
                project="Beta",
            )
            _insert_catalog_document(
                connection,
                file_key=_CATALOG_ONLY_FILE_KEY,
                path="C:/docs/catalog-only.pdf",
                volume_id=5,
                file_id=6,
                birthtime_ns=12,
                project="Alpha",
            )


def test_catalog_metadata_filters_membership_without_becoming_relevance(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_pdf_state(state)
    _create_catalog_state(state, include_filter_rows=True)
    paths = KnowledgeStatePaths.from_directory(state)
    snapshot = _snapshot()

    result = execute_knowledge_search(
        paths,
        plan_knowledge_query(
            KnowledgeQuery(
                "protección interruptor",
                formats=("pdf",),
                project="Alpha",
                limit=10,
            )
        ),
        snapshot,
    )

    assert {hit.resource.current_path for hit in result.hits} == {"C:/docs/alpha.pdf"}
    assert {signal.source for hit in result.hits for signal in hit.signals} == {"fts_pdf"}
    catalog_report = next(
        ranking for ranking in result.rankings if ranking.name == "catalog_metadata"
    )
    assert catalog_report.returned == 2
    assert catalog_report.complete

    catalog_only = execute_knowledge_search(
        paths,
        plan_knowledge_query(
            KnowledgeQuery(
                "contenido lexical inexistente",
                formats=("pdf",),
                project="Alpha",
                limit=10,
            )
        ),
        snapshot,
    )
    assert catalog_only.hits == ()


@pytest.mark.parametrize(
    ("source_status", "catalog_status", "uncertainty", "expected_warning"),
    (
        ("partial", "classified", "baja", "catalog_source_status:partial"),
        ("done", "review", "baja", "catalog_status:review"),
        ("done", "classified", "alta", "catalog_uncertainty:alta"),
    ),
)
def test_catalog_quality_is_partial_and_observable(
    tmp_path: Path,
    source_status: str,
    catalog_status: str,
    uncertainty: str,
    expected_warning: str,
) -> None:
    state = tmp_path / "state"
    _create_catalog_state(
        state,
        source_status=source_status,
        catalog_status=catalog_status,
        uncertainty=uncertainty,
    )
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "protección interruptor",
            formats=("pdf",),
            project="Alpha",
        )
    )

    candidates, report = _catalog_ranking(
        KnowledgeStatePaths.from_directory(state),
        plan,
        _snapshot(),
    )

    assert len(candidates) == 1
    assert candidates[0].revision.state is RevisionState.PARTIAL
    assert expected_warning in candidates[0].warnings
    assert report.executed
    assert report.available
    assert not report.complete
    assert report.reason == "catalog_partial_or_review"


def test_required_project_filter_without_catalog_heads_is_incomplete(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_pdf_state(state)
    _create_catalog_state(state)

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(
            KnowledgeQuery(
                "protección interruptor",
                formats=("pdf",),
                project="Alpha",
            )
        ),
        _snapshot(catalog_heads=False),
    )

    assert result.hits == ()
    assert not result.complete
    report = next(ranking for ranking in result.rankings if ranking.name == "catalog_metadata")
    assert not report.executed
    assert not report.available
    assert not report.complete
    assert report.reason == "catalog_has_no_publication_heads"


@pytest.mark.parametrize(
    ("query_text", "identifier", "ranking_prefix"),
    (
        (
            "IEC-61850",
            ("standard_identifier", "IEC-61850"),
            "exact_catalog_identifier:",
        ),
        (
            "C:/docs/alpha.pdf",
            ("path", "C:/docs/alpha.pdf"),
            "exact_catalog_path:",
        ),
    ),
)
def test_exact_catalog_evidence_comes_only_from_typed_adapter(
    tmp_path: Path,
    query_text: str,
    identifier: tuple[str, str],
    ranking_prefix: str,
) -> None:
    state = tmp_path / "state"
    _create_pdf_state(state)
    _create_catalog_state(state, include_filter_rows=True)

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery(query_text, formats=("pdf",), limit=8)),
        _snapshot(),
    )

    exact_hits = [
        hit
        for hit in result.hits
        if hit.resource.owner == "catalog" and identifier in hit.evidence.identifiers
    ]
    assert len(exact_hits) == 1
    assert {signal.source for signal in exact_hits[0].signals} == {
        next(
            ranking.name
            for ranking in result.rankings
            if ranking.name.startswith(ranking_prefix) and ranking.returned == 1
        )
    }
    assert all(signal.score_kind == "exact" for signal in exact_hits[0].signals)
    assert all(signal.source != "catalog_metadata" for hit in result.hits for signal in hit.signals)
    assert any(ranking.name == "catalog_metadata" for ranking in result.rankings)


def test_catalog_execution_uses_the_planned_candidate_limit(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_catalog_state(state, include_filter_rows=True)
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        _insert_catalog_document(
            connection,
            file_key=("00000000000000000000000000000007:00000000000000000000000000000008"),
            path="C:/docs/delta.pdf",
            volume_id=7,
            file_id=8,
            birthtime_ns=13,
            project="Delta",
        )
    plan = plan_knowledge_query(KnowledgeQuery("proteccion", formats=("pdf",), limit=1))
    step = next(value for value in plan.steps if value.channel == "catalog")

    candidates, report = _catalog_ranking(
        KnowledgeStatePaths.from_directory(state),
        plan,
        _snapshot(),
    )

    assert step.candidate_limit == 3
    assert len(candidates) == step.candidate_limit
    assert report.returned == step.candidate_limit
    assert report.rows_scanned == step.candidate_limit + 1
    assert report.complete
    assert report.reason is None
    assert report.result_window_full


# endregion [02]
