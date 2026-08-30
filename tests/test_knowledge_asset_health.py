"""Causal, bounded, read-only Knowledge asset health over isolated state."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.foundation.file_identity import encode_file_identity
from neocortex.knowledge.knowledge_asset_health import inspect_knowledge_asset_health
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KNOWLEDGE_ASSET_HEALTH_SCHEMA,
    MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES,
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthStage,
    KnowledgeAssetHealthState,
    parse_knowledge_asset_resource_id,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.capabilities.formats.text.text_state import initialize_text_state


@dataclass(frozen=True, slots=True)
class _HealthFixture:
    paths: KnowledgeStatePaths
    query: KnowledgeAssetHealthQuery
    file_key: str


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def _quiesce(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
            VALUES(1,'/corpus/docs/asset.txt',?,?,800,123,-1)""",
            (_blob(11), _blob(3)),
        )
    _quiesce(path)


def _create_text(path: Path, *, file_key: str) -> None:
    initialize_text_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO text_input_revisions(
            revision_id,resource_id,producer,processing_signature,generation,
            revision_state,observed_at_utc,fingerprint_algorithm,fingerprint,recorded_ns)
            VALUES('revision:text:fixture','resource:file:11:3:-1','text-route-v2',
            'text-route-fixture-v1',1,'current','2026-08-15T00:00:00Z',
            'xxh3_128',?,10)""",
            ("a" * 32,),
        )
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
            text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
            last_seen_run_id,updated_ns,revision_id)
            VALUES(?,'/corpus/docs/asset.txt',800,123,-1,'text-route-fixture-v1',
            'complete','plain_text','text/plain','Asset',NULL,'{}',NULL,12,?,0,
            NULL,NULL,NULL,0,1,20,'revision:text:fixture')""",
            (file_key, "b" * 32),
        )
    _quiesce(path)


def _create_catalog(path: Path, *, file_key: str) -> None:
    initialize_document_catalog(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO catalog_generations(
            generation_id,catalog_run_id,source_kind,base_generation_id,status,
            started_ns,completed_ns,published_ns,error_type,error_message)
            VALUES(1,NULL,'text',NULL,'published',1,2,3,NULL,NULL)"""
        )
        connection.execute(
            """INSERT INTO catalog_publications(
            source_kind,generation_id,published_ns) VALUES('text',1,3)"""
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
            VALUES(1,'text',?,'/corpus/docs/asset.txt','11','3',800,123,-1,
            'complete','text-route-fixture-v1',?,'classifier-v1','otro',NULL,NULL,NULL,
            NULL,NULL,NULL,0.9,'baja','[]','[]','[]','[]','[]','[]','[]','[]','{}',
            'classified',NULL,NULL,1,1,30)""",
            (file_key, "b" * 32),
        )
    _quiesce(path)


def _create_health_fixture(tmp_path: Path) -> _HealthFixture:
    root = tmp_path / "state"
    root.mkdir()
    paths = KnowledgeStatePaths.from_directory(root)
    file_key = encode_file_identity(11, 3)
    _create_inventory(paths.inventory)
    assert paths.text is not None
    _create_text(paths.text, file_key=file_key)
    _create_catalog(paths.catalog, file_key=file_key)
    return _HealthFixture(
        paths,
        KnowledgeAssetHealthQuery("resource:file:11:3:-1"),
        file_key,
    )


@pytest.mark.parametrize(
    "value",
    (
        "/corpus/docs/asset.txt",
        "resource:file:01:3:-1",
        "resource:file:11:3:-2",
        f"resource:file:{2**128}:3:-1",
        f"resource:file:11:3:{2**63}",
        "resource:file:11:3:-1:extra",
    ),
)
def test_selector_is_canonical_and_rejects_paths_or_out_of_range_values(value: str) -> None:
    with pytest.raises(ValueError):
        KnowledgeAssetHealthQuery(value)


def test_aligned_published_text_trace_is_healthy_and_replay_deterministic(
    tmp_path: Path,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    before = _state_fingerprint(fixture.paths.inventory.parent)

    first = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    second = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert first.health is KnowledgeAssetHealthState.HEALTHY
    assert first.completeness is KnowledgeAssetHealthCompleteness.COMPLETE
    assert first.reason_code == "causal_trace_aligned"
    assert first.snapshot_consistency == "stable"
    assert first.attempts == 1
    assert tuple(fact.stage for fact in first.facts) == tuple(KnowledgeAssetHealthStage)
    assert tuple(fact.owner for fact in first.facts) == (
        "inventory",
        "text",
        "catalog",
        "knowledge",
    )
    assert first.facts[1].record_id == f"text:{fixture.file_key}"
    assert {value.name: value.value for value in first.facts[2].values}["source_kind"] == "text"
    assert first.gaps == ()
    assert first.counterevidence == ()
    assert first.examples == ()
    assert first.read_only is True
    assert first.advisory_only is True
    assert first.mutation_authorized is False
    assert first.to_json() == second.to_json()
    assert first.to_dict()["schema"] == KNOWLEDGE_ASSET_HEALTH_SCHEMA
    assert parse_knowledge_asset_resource_id(first.resource_id).resource_id == first.resource_id
    assert _state_fingerprint(fixture.paths.inventory.parent) == before


@pytest.mark.parametrize("mismatch", ("identity", "processing_signature"))
def test_identity_or_processing_signature_mismatch_never_cross_joins(
    tmp_path: Path,
    mismatch: str,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    with closing(sqlite3.connect(fixture.paths.catalog)) as connection, connection:
        if mismatch == "identity":
            connection.execute(
                "UPDATE catalog_generation_documents SET volume_id='99' WHERE generation_id=1"
            )
        else:
            connection.execute(
                """UPDATE catalog_generation_documents
                SET processing_signature='different-signature' WHERE generation_id=1"""
            )
    _quiesce(fixture.paths.catalog)

    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert report.health is not KnowledgeAssetHealthState.HEALTHY
    if mismatch == "identity":
        assert "published_catalog_record_missing" in report.gaps
        assert all(fact.stage is not KnowledgeAssetHealthStage.CATALOG for fact in report.facts)
        assert all(
            fact.stage is not KnowledgeAssetHealthStage.KNOWLEDGE_SEARCH for fact in report.facts
        )
    else:
        assert report.health is KnowledgeAssetHealthState.DEGRADED
        assert "text_catalog_projection_mismatch" in report.counterevidence


@pytest.mark.parametrize(
    "case",
    ("missing", "future", "corrupt", "unpublished_inventory", "unpublished_catalog"),
)
def test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    if case == "missing":
        assert fixture.paths.text is not None
        fixture.paths.text.unlink()
    elif case == "future":
        with closing(sqlite3.connect(fixture.paths.catalog)) as connection, connection:
            connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        _quiesce(fixture.paths.catalog)
    elif case == "corrupt":
        fixture.paths.catalog.write_bytes(b"not-a-sqlite-database")
    elif case == "unpublished_inventory":
        with closing(sqlite3.connect(fixture.paths.inventory)) as connection, connection:
            connection.execute("UPDATE inventory_checkpoints SET valid=0")
        _quiesce(fixture.paths.inventory)
    else:
        with closing(sqlite3.connect(fixture.paths.catalog)) as connection, connection:
            connection.execute("DELETE FROM catalog_publications WHERE source_kind='text'")
        _quiesce(fixture.paths.catalog)

    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert report.health is not KnowledgeAssetHealthState.HEALTHY
    if case in {"future", "corrupt"}:
        assert report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
        assert report.reason_code == "required_owner_evidence_unavailable"
    elif case == "unpublished_inventory":
        assert "published_inventory_record_missing" in report.gaps
        assert "unpublished_inventory_record_observed" in report.counterevidence
        assert report.examples
    elif case == "unpublished_catalog":
        assert "published_catalog_record_missing" in report.gaps
        assert "unpublished_catalog_record_observed" in report.counterevidence
        assert report.examples


@pytest.mark.parametrize(
    ("source_status", "catalog_status", "expected"),
    (
        ("error", "error", KnowledgeAssetHealthState.FAILED),
        ("protected", "classified", KnowledgeAssetHealthState.PROTECTED),
        ("partial", "review", KnowledgeAssetHealthState.DEGRADED),
    ),
)
def test_typed_statuses_drive_health_without_error_message_inference(
    tmp_path: Path,
    source_status: str,
    catalog_status: str,
    expected: KnowledgeAssetHealthState,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    assert fixture.paths.text is not None
    with closing(sqlite3.connect(fixture.paths.text)) as connection, connection:
        connection.execute(
            """UPDATE documents SET status=?,error_type='opaque-code',
            error_message='words cannot classify health'""",
            (source_status,),
        )
    _quiesce(fixture.paths.text)
    with closing(sqlite3.connect(fixture.paths.catalog)) as connection, connection:
        connection.execute(
            """UPDATE catalog_generation_documents
            SET source_status=?,catalog_status=?,error_type='opaque-code',
            error_message='words cannot classify health'""",
            (source_status, catalog_status),
        )
    _quiesce(fixture.paths.catalog)

    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert report.health is expected
    assert report.completeness is KnowledgeAssetHealthCompleteness.COMPLETE


def test_second_fact_snapshot_change_abstains_after_one_bounded_retry(
    tmp_path: Path,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    assert fixture.paths.text is not None
    mutations = 0

    def change_exact_fact(_attempt: int) -> None:
        nonlocal mutations
        mutations += 1
        with closing(sqlite3.connect(fixture.paths.text)) as connection, connection:
            connection.execute("UPDATE documents SET updated_ns=updated_ns+1")
        _quiesce(fixture.paths.text)

    report = inspect_knowledge_asset_health(
        fixture.paths,
        fixture.query,
        _between_observations=change_exact_fact,
    )

    assert mutations == 2
    assert report.health is KnowledgeAssetHealthState.UNKNOWN
    assert report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
    assert report.reason_code == "snapshot_changed"
    assert report.snapshot_consistency == "snapshot_changed"
    assert report.attempts == 2
    assert "fact_snapshot_changed" in report.counterevidence


def test_active_wal_abstains_without_touching_owner_sidecars(tmp_path: Path) -> None:
    fixture = _create_health_fixture(tmp_path)
    assert fixture.paths.text is not None
    connection = sqlite3.connect(fixture.paths.text)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE documents SET updated_ns=updated_ns+1")
        connection.commit()
        wal = Path(f"{fixture.paths.text}-wal")
        assert wal.stat().st_size > 0
        before = _state_fingerprint(fixture.paths.text.parent)

        report = inspect_knowledge_asset_health(fixture.paths, fixture.query)

        after = _state_fingerprint(fixture.paths.text.parent)
        assert report.health is KnowledgeAssetHealthState.UNKNOWN
        assert report.completeness is KnowledgeAssetHealthCompleteness.ABSTAINED
        assert report.reason_code == "required_owner_evidence_unavailable"
        assert "text_owner_not_quiescent" in report.gaps
        assert after == before
    finally:
        connection.close()


def test_examples_are_bounded_truncated_and_replay_stable(tmp_path: Path) -> None:
    fixture = _create_health_fixture(tmp_path)
    with closing(sqlite3.connect(fixture.paths.inventory)) as connection, connection:
        connection.execute("UPDATE inventory_checkpoints SET valid=0")
        for scan_id in range(2, 13):
            connection.execute(
                """INSERT INTO scans(
                scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
                bytes_seen,skipped_links,excluded_directories,errors,status,
                inventory_policy_signature)
                VALUES(?, ?,1,2,1,1,800,0,0,0,'complete','fixture-v1')""",
                (scan_id, f"/corpus-{scan_id}"),
            )
            connection.execute(
                """INSERT INTO files(
                scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                VALUES(?,?,?,?,800,123,-1)""",
                (
                    scan_id,
                    f"/corpus-{scan_id}/asset.txt",
                    _blob(11),
                    _blob(3),
                ),
            )
    _quiesce(fixture.paths.inventory)

    first = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    second = inspect_knowledge_asset_health(fixture.paths, fixture.query)

    assert len(first.examples) == MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES
    assert first.examples_truncated is True
    assert first.to_json() == second.to_json()
    assert first.health is not KnowledgeAssetHealthState.HEALTHY
