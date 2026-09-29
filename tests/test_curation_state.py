"""Focused persistence contract tests for Catalog-owned Fast Curation state."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.documents import document_catalog_schema as schema
from neocortex.documents.curation_state import (
    CurationDecisionRecord,
    CurationEmbeddingCacheKey,
    CurationEmbeddingCacheRecord,
    CurationPhysicalIdentityMismatch,
    CurationTopCandidate,
    read_current_curation_decision,
    read_current_curation_decisions_page,
    read_embedding_cache,
    rebind_curation_decision,
    upsert_curation_batch,
    upsert_curation_decision_batch,
    upsert_embedding_cache_batch,
)
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract


def _insert_document(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    file_key: str,
    path: str,
) -> None:
    columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")]
    values: dict[str, object] = {
        "source_kind": source_kind,
        "file_key": file_key,
        "path": path,
        "volume_id": "volume",
        "file_id": file_key,
        "size": 1,
        "mtime_ns": 1,
        "birthtime_ns": -1,
        "source_status": "complete",
        "processing_signature": "source-v1",
        "text_fingerprint": None,
        "classifier_signature": "classifier-v1",
        "primary_kind": "report",
        "primary_subtype": None,
        "primary_authority": None,
        "primary_organization": None,
        "primary_client": None,
        "primary_project": None,
        "primary_workstream": None,
        "confidence": 0.9,
        "uncertainty": "none",
        "standard_references_json": "[]",
        "organizations_json": "[]",
        "clients_json": "[]",
        "projects_json": "[]",
        "workstreams_json": "[]",
        "topics_json": "[]",
        "equipment_json": "[]",
        "activities_json": "[]",
        "classification_json": "{}",
        "catalog_status": "classified",
        "error_type": None,
        "error_message": None,
        "active": 1,
        "last_seen_catalog_run_id": 1,
        "updated_ns": 1,
        "resource_binding_json": None,
    }
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO documents({','.join(columns)}) VALUES({placeholders})",
        tuple(values[column] for column in columns),
    )


def _embedding(index: int = 0) -> CurationEmbeddingCacheRecord:
    key = CurationEmbeddingCacheKey(
        representation_sha256=f"{index + 1:064x}",
        representation_version="representation-v1",
        model_signature="model-v1",
        role="document",
        vector_space="curation-text",
        dimensions=3,
    )
    return CurationEmbeddingCacheRecord.from_values(
        key,
        (3.0, 4.0, 0.0),
        metadata={"representation": "content-only"},
        created_ns=10 + index,
    )


def _decision(file_key: str = "pdf:1") -> CurationDecisionRecord:
    return CurationDecisionRecord(
        source_kind="pdf",
        file_key=file_key,
        source_binding={"input": "derived-text-v1"},
        input_signature="input-v1",
        semantic_representation_fingerprint="a" * 64,
        representation_version="representation-v1",
        model_signature="model-v1",
        role="document",
        vector_space="curation-text",
        dimensions=3,
        ontology_version="ontology-v1",
        prototype_version="prototype-v1",
        policy_version="policy-v1",
        calibration_version="calibration-v1",
        decision="CLASSIFIED",
        top1_label="tests",
        top1_score=0.91,
        top2_label="manuals",
        top2_score=0.2,
        margin=0.71,
        top_k=(CurationTopCandidate("tests", 0.91), CurationTopCandidate("manuals", 0.2)),
        evidence={"source": "fast-curation"},
        context_provenance={"original_path": "/corpus/original.pdf"},
        created_ns=20,
    )


def _physical_binding(path: str, *, file_id: str = "inode-1") -> dict[str, object]:
    return {
        "source_kind": "pdf",
        "file_key": "pdf:1",
        "physical_identity": {
            "volume_id": "volume-1",
            "file_id": file_id,
            "birthtime_ns": 7,
        },
        "physical_anchor_path": path,
    }


def _physical_decision(
    binding: dict[str, object],
    *,
    input_signature: str = "input-v1",
    representation_fingerprint: str = "a" * 64,
    original_path: str,
    actual_path: str,
) -> CurationDecisionRecord:
    return replace(
        _decision(),
        source_binding=binding,
        input_signature=input_signature,
        semantic_representation_fingerprint=representation_fingerprint,
        context_provenance={
            "original_path": original_path,
            "actual_path": actual_path,
        },
        created_ns=30,
        updated_ns=30,
    )


def test_embedding_cache_is_content_keyed_and_stores_normalized_float32(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    record = _embedding()
    with document_catalog_database(database) as connection:
        assert upsert_embedding_cache_batch(connection, [record]) == 1
        loaded = read_embedding_cache(connection, record.key)
        assert loaded is not None
        assert loaded.vector[0] == pytest.approx(0.6, abs=1e-6)
        assert loaded.vector[1] == pytest.approx(0.8, abs=1e-6)
        assert len(loaded.vector_bytes) == 12
        assert "path" not in loaded.metadata


def test_decision_is_current_by_catalog_key_and_path_is_only_context(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            path="/corpus/old.pdf",
        )
        assert upsert_curation_decision_batch(connection, [_decision()]) == 1
        loaded = read_current_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
        )
        assert loaded is not None
        assert loaded.context_provenance["original_path"] == "/corpus/original.pdf"
        assert not hasattr(loaded, "path")
        connection.execute(
            "UPDATE documents SET path='/corpus/renamed.pdf' "
            "WHERE source_kind='pdf' AND file_key='pdf:1'"
        )
        rebound = read_current_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
        )
        assert rebound is not None
        assert rebound.file_key == "pdf:1"


def test_combined_batch_validates_before_writing_and_decision_page_is_bounded(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            path="/corpus/1.pdf",
        )
        with pytest.raises(ValueError, match="top_k"):
            replace(
                _decision(),
                top_k=tuple(CurationTopCandidate(str(i), 0.1) for i in range(6)),
            )
        assert connection.execute("SELECT COUNT(*) FROM curator_decisions").fetchone()[0] == 0
        assert upsert_curation_batch(connection, decisions=[_decision()]) == (0, 1)
        page = read_current_curation_decisions_page(connection, limit=1)
        assert page.items[0].file_key == "pdf:1"
        assert page.has_more is False


def test_original_provenance_survives_catalog_rebind_and_rename(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    old_path = "/corpus/original.pdf"
    new_path = "/corpus/Corpus_ordenado/renamed.pdf"
    first_binding = _physical_binding(old_path)
    second_binding = _physical_binding(new_path)
    first = _physical_decision(
        first_binding,
        original_path=old_path,
        actual_path=old_path,
    )
    second = _physical_decision(
        second_binding,
        original_path=new_path,
        actual_path=new_path,
    )
    with document_catalog_database(database) as connection:
        _insert_document(connection, source_kind="pdf", file_key="pdf:1", path=old_path)
        assert upsert_curation_batch(
            connection,
            embeddings=[_embedding()],
            decisions=[first],
        ) == (1, 1)
        connection.execute(
            "UPDATE documents SET path=? WHERE source_kind='pdf' AND file_key='pdf:1'",
            (new_path,),
        )
        assert upsert_curation_batch(
            connection,
            embeddings=[_embedding()],
            decisions=[second],
        ) == (1, 1)
        loaded = read_current_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
        )
        assert loaded is not None
        assert loaded.original_path == old_path
        assert loaded.actual_path == new_path
        assert loaded.original_source_binding == first_binding
        assert loaded.source_original_binding == first_binding
        assert loaded.source_binding == second_binding
        assert connection.execute(
            "SELECT COUNT(*) FROM curator_embedding_cache"
        ).fetchone()[0] == 1


def test_content_change_keeps_origin_but_physical_identity_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    old_path = "/corpus/original.pdf"
    with document_catalog_database(database) as connection:
        _insert_document(connection, source_kind="pdf", file_key="pdf:1", path=old_path)
        first = _physical_decision(
            _physical_binding(old_path),
            original_path=old_path,
            actual_path=old_path,
        )
        upsert_curation_decision_batch(connection, [first])
        changed = _physical_decision(
            _physical_binding(old_path),
            input_signature="input-v2",
            representation_fingerprint="c" * 64,
            original_path=old_path,
            actual_path=old_path,
        )
        upsert_curation_decision_batch(connection, [changed])
        current = read_current_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
        )
        assert current is not None
        assert current.input_signature == "input-v2"
        assert current.original_path == old_path
        mismatched = _physical_decision(
            _physical_binding(old_path, file_id="inode-2"),
            original_path=old_path,
            actual_path=old_path,
        )
        with pytest.raises(CurationPhysicalIdentityMismatch):
            upsert_curation_decision_batch(connection, [mismatched])


def test_prototype_set_fingerprint_getter_is_pure_and_version_gate_is_fail_closed() -> None:
    from neocortex.semantic.fast_curation_policy import (
        CalibrationParameters,
        FastCurationPolicy,
    )
    from neocortex.semantic.fast_curation_policy_bundle import (
        FastCurationPolicyBundle,
        validate_record_versions,
    )

    fingerprint = "a" * 64
    policy = FastCurationPolicy.from_calibration(
        CalibrationParameters(
            "calibration-v1",
            "model-v1",
            True,
            {"document_kind": 0.7},
            {"document_kind": 0.1},
        ),
        policy_version="policy-v1",
    )
    bundle = FastCurationPolicyBundle(
        policy,
        "model-v1",
        "representation-v1",
        "ontology-v1",
        "prototype-v1",
        "policy-v1",
        "calibration-v1",
        fingerprint,
        "fixture-scope",
    )
    current = replace(
        _decision(),
        evidence={"source": "fast-curation", "prototype_set_fingerprint": fingerprint},
    )
    assert current.prototype_set_fingerprint == fingerprint
    assert validate_record_versions(current, bundle, source_input_signature="input-v1").valid

    missing = replace(current, evidence={"source": "fast-curation"})
    assert missing.prototype_set_fingerprint == ""
    assert validate_record_versions(missing, bundle).reason == "stale_prototype_set_fingerprint"

    stale = replace(
        current,
        evidence={"source": "fast-curation", "prototype_set_fingerprint": "b" * 64},
    )
    assert stale.prototype_set_fingerprint == "b" * 64
    assert validate_record_versions(stale, bundle).reason == "stale_prototype_set_fingerprint"


def test_rebind_helper_updates_current_binding_without_touching_origin_or_cache(
    tmp_path: Path,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    old_path = "/corpus/original.pdf"
    new_path = "/corpus/Corpus_ordenado/renamed.pdf"
    old_binding = _physical_binding(old_path)
    new_binding = _physical_binding(new_path)
    receipt = {
        "organization_receipt_schema": "neocortex.organization-move-receipt/v1",
        "source_identity": "sha256:fixture",
        "source_path": old_path,
        "target_path": new_path,
        "source_absent": True,
        "target_identity": {
            "path": new_path,
            "size": 1,
            "mtime_ns": 1,
            "birthtime_ns": 7,
            "volume_id": "volume-1",
            "file_id": "inode-1",
        },
    }
    with document_catalog_database(database) as connection:
        _insert_document(connection, source_kind="pdf", file_key="pdf:1", path=old_path)
        connection.execute(
            "UPDATE documents SET resource_binding_json=? "
            "WHERE source_kind='pdf' AND file_key='pdf:1'",
            (json.dumps(old_binding, sort_keys=True),),
        )
        first = _physical_decision(
            old_binding,
            original_path=old_path,
            actual_path=old_path,
        )
        upsert_curation_batch(connection, embeddings=[_embedding()], decisions=[first])
        connection.execute(
            "UPDATE documents SET path=?,resource_binding_json=? "
            "WHERE source_kind='pdf' AND file_key='pdf:1'",
            (new_path, json.dumps(new_binding, sort_keys=True)),
        )
        rebound = rebind_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            source_path=old_path,
            target_path=new_path,
            receipt=receipt,
            current_source_binding=new_binding,
        )
        assert rebound.source_binding == new_binding
        assert rebound.original_path == old_path
        assert rebound.actual_path == new_path
        assert rebound.input_signature == first.input_signature
        assert connection.execute(
            "SELECT COUNT(*) FROM curator_embedding_cache"
        ).fetchone()[0] == 1


def test_v12_to_v13_migration_is_additive_and_contract_is_exact(tmp_path: Path) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        schema._create_v12_schema(connection)
        schema._set_schema_version(connection, 12)
        connection.execute("INSERT INTO metadata(key,value) VALUES('sentinel','kept')")
        connection.commit()
    initialize_document_catalog(database)
    with document_catalog_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key='sentinel'"
            ).fetchone()[0]
            == "kept"
        )
        assert connection.execute("SELECT COUNT(*) FROM curator_embedding_cache").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM curator_decisions").fetchone()[0] == 0
        validate_sqlite_schema_contract(
            connection,
            schema.document_catalog_schema_contract(),
            label="document catalog",
            exact=True,
        )


def test_v13_migration_failure_rolls_back_curation_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        schema._create_v12_schema(connection)
        schema._set_schema_version(connection, 12)
        connection.commit()

    monkeypatch.setattr(
        schema,
        "_V13_CURATION_DDL",
        (schema._V13_CURATION_DDL[0], "CREATE TABLE broken_curation_ddl("),
    )
    with pytest.raises(sqlite3.OperationalError):
        initialize_document_catalog(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "12"
        )
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='curator_embedding_cache'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='broken_curation_ddl'"
        ).fetchone() is None

    monkeypatch.setattr(schema, "_V13_CURATION_DDL", schema._CURRENT_SCHEMA_DDL[-4:])
    initialize_document_catalog(database)


def test_catalog_lifecycle_declares_curation_tables_without_new_owner() -> None:
    from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY
    from neocortex.persistence.state_lifecycle_policy import owner_lifecycle_rules

    catalog_store = STATE_STORE_REGISTRY.by_owner("catalog")
    assert catalog_store.expected_schema_version == schema.CATALOG_SCHEMA_VERSION
    assert all("curator" not in store.state_owner_id for store in STATE_STORE_REGISTRY.stores)
    rules = {rule.table: rule for rule in owner_lifecycle_rules("catalog")}
    assert rules["curator_embedding_cache"].role == "derived"
    assert rules["curator_embedding_cache"].reset_action == "owner-transform"
    assert rules["curator_decisions"].role == "authoritative"
    assert rules["curator_decisions"].reset_action == "preserve"
