"""Receipt-schema compatibility and causal-identity regressions for Semantic.

These tests are intentionally separate from the generation-control writer
tests.  They build a real v5 -> v7 owner fixture with the existing migration
helpers, create deterministic v7 receipts with the public state API, then
exercise the v7 -> v8 -> v9 compatibility boundaries.  The only private call is the
fixture-bound receipt collision probe: it is used because no public operation
replays an identical terminal receipt key on demand.

The assertions keep three identities separate:

* the owner schema declared by the current SQLite connection;
* the schema advertised by a newly written Semantic materialization locator;
* the historical owner schema of a causal producer carried through an input.

No assertion treats an upstream text-owner schema as a Semantic schema, and no
test uses a production database, corpus, model, network or generated writer.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from neocortex.semantic import semantic_lineage_repository, semantic_schema
from neocortex.semantic.derivation_contracts import MaterializationRef, WorkReceipt
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    initialize_semantic_state,
    prepare_embedding_generation,
    reuse_cached_jobs,
    semantic_database,
    start_embedding_generation,
)

from tests.test_semantic_derivation_lineage import (
    _execute as execute_one,
    _generation as start_fixture_generation,
    _stage as stage_fixture_item,
)
from tests.test_semantic_generation_control_projection import _migrate_v5_to_v7
from tests.test_semantic_generation_publication_v6 import _create_populated_v5


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _semantic_schema_values(value: object) -> tuple[int, ...]:
    """Collect Semantic materialization locator schemas without rewriting data."""

    found: list[int] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("kind") == "materialization_ref" and node.get("owner") == "semantic":
                value = node.get("owner_schema_version")
                if isinstance(value, int) and not isinstance(value, bool):
                    found.append(value)
            for child in node.values():
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return tuple(found)


def _binding_payload(binding: Any, *, output: bool) -> dict[str, object]:
    materialization = getattr(binding, "materialization", None)
    payload: dict[str, object] = {
        "kind": (
            materialization.kind
            if isinstance(materialization, MaterializationRef)
            else "semantic_input"
        ),
        "binding_name": str(binding.name),
        "fingerprint": {
            "algorithm": str(binding.fingerprint_algorithm),
            "value": str(binding.fingerprint),
        },
    }
    if output:
        if not isinstance(materialization, MaterializationRef):
            raise AssertionError("fixture output lacks its materialization")
        payload["materialization_ref"] = materialization
    else:
        payload["revision_ref"] = binding.revision
        payload["materialization_ref"] = materialization
    return payload


def _receipt_provider(receipt: WorkReceipt) -> dict[str, object] | None:
    stage = receipt.stage
    if stage.provider is None and stage.provider_version is None and stage.model is None:
        return None
    return {
        "provider": stage.provider,
        "provider_version": stage.provider_version,
        "model_id": stage.model,
        "model_version": stage.model_version,
    }


def _v7_fixture(tmp_path: Path, *, item_id: str = "compat-source") -> dict[str, Any]:
    """Build v7 through migrations 1..7 and create one deterministic v7 receipt."""

    database = tmp_path / "semantic.sqlite3"
    model, _legacy_chunk = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    chunk = stage_fixture_item(
        database,
        item_id=item_id,
        source_revision_id=f"revision:text:{item_id}:v7",
        text="contenido determinista para compatibilidad de receipts Semantic",
        ordinal=1,
    )
    generation_id = start_fixture_generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="receipt-schema-v7-generation",
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=110) == 1
    execute_one(database, generation_id, now_ns=120)
    assert finalize_embedding_generation(database, generation_id, completed_ns=130).status == "ready"

    with semantic_database(database, readonly=True) as connection:
        embedding_row = connection.execute(
            """SELECT * FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
            AND entity_id=? ORDER BY receipt_id LIMIT 1""",
            (generation_id, chunk.chunk_id),
        ).fetchone()
        chunk_row = connection.execute(
            """SELECT * FROM semantic_work_receipts
            WHERE stage_id='semantic.text.chunk.materialize'
            AND entity_id=? ORDER BY receipt_id LIMIT 1""",
            (chunk.chunk_id,),
        ).fetchone()
    assert embedding_row is not None
    assert chunk_row is not None
    return {
        "database": database,
        "model": model,
        "chunk": chunk,
        "generation_id": generation_id,
        "embedding_row": embedding_row,
        "chunk_row": chunk_row,
    }


def _migrate_semantic_schema(database: Path, target_version: int) -> None:
    """Advance a v7/v8 fixture to an exact v8 or v9 owner without fresh init."""

    if target_version not in {8, 9}:
        raise ValueError(f"unsupported Semantic target version: {target_version}")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in {7, 8} or current >= target_version:
            raise AssertionError(
                f"fixture must start at v7 or v8 below target, observed {current}"
            )
        for version in range(current + 1, target_version + 1):
            getattr(semantic_schema, f"_migrate_to_v{version}")(connection, version)
            semantic_schema._store_schema_version(connection, version)
        connection.commit()


def _domain_snapshot(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Read stable identity/payload rows used by the migration assertions."""

    with semantic_database(database, readonly=True) as connection:
        return {
            "receipts": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT receipt_id,receipt_key,receipt_json "
                    "FROM semantic_work_receipts ORDER BY receipt_id"
                )
            ),
            "outbox": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT event_id,receipt_id,event_kind,aggregate_kind,aggregate_id,payload_json "
                    "FROM semantic_derivation_outbox ORDER BY event_id"
                )
            ),
            "heads": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT model_signature,generation_id,published_ns "
                    "FROM published_embedding_heads ORDER BY model_signature"
                )
            ),
            "generations": tuple(
                tuple(row)
                for row in connection.execute(
                    """SELECT generation_id,model_signature,status,base_generation_id,
                    base_clone_complete,pending_count,leased_count,done_count,
                    error_count,stale_count FROM embedding_generations
                    ORDER BY generation_id"""
                )
            ),
            "jobs": tuple(
                tuple(row)
                for row in connection.execute(
                    """SELECT job_id,generation_id,model_signature,entity_kind,entity_id,
                    item_id,input_item_revision_id,input_chunk_revision_id,
                    content_xxh3_128,content_bytes,content_xxh3_64_guard,
                    status,attempts,attempt_sequence FROM embedding_jobs
                    ORDER BY job_id"""
                )
            ),
            "members": tuple(
                tuple(row)
                for row in connection.execute(
                    """SELECT member_id,generation_id,model_signature,entity_kind,entity_id,
                    item_revision_id,chunk_revision_id,payload_id,base_member_id
                    FROM embedding_generation_members ORDER BY member_id"""
                )
            ),
            "payloads": tuple(
                tuple(row)
                for row in connection.execute(
                    """SELECT payload_id,model_signature,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
                    original_norm,provenance_json,created_ns FROM vector_payloads
                    ORDER BY payload_id"""
                )
            ),
        }


def _receipt_row_kwargs(
    receipt_row: sqlite3.Row,
    receipt: WorkReceipt,
    outbox_row: sqlite3.Row,
) -> dict[str, object]:
    """Convert one stored receipt back into the private collision seam input."""

    return {
        "receipt_key": str(receipt_row["receipt_key"]),
        "stage_id": receipt.stage.stage_id,
        "stage_version": receipt.stage.stage_version,
        "processing_signature": receipt.stage.processing_signature,
        "status": receipt.outcome.value,
        "execution_mode": receipt.execution_mode.value,
        "reproducibility_class": receipt.reproducibility.value,
        "entity_kind": str(receipt_row["entity_kind"]),
        "entity_id": str(receipt_row["entity_id"]),
        "inputs": tuple(_binding_payload(binding, output=False) for binding in receipt.inputs),
        "outputs": tuple(_binding_payload(binding, output=True) for binding in receipt.outputs),
        "effective_config": dict(receipt.effective_configuration),
        "provider": _receipt_provider(receipt),
        "item_revision_id": receipt_row["item_revision_id"],
        "chunk_revision_id": receipt_row["chunk_revision_id"],
        "generation_id": receipt_row["generation_id"],
        "model_signature": receipt_row["model_signature"],
        "payload_id": receipt_row["payload_id"],
        "job_id": receipt_row["job_id"],
        "attempt": receipt_row["attempt"],
        "started_ns": receipt_row["started_ns"],
        "finished_ns": receipt_row["finished_ns"],
        "error": None,
        "causation_receipt_id": None,
        "event_kind": str(outbox_row["event_kind"]),
        "aggregate_kind": str(outbox_row["aggregate_kind"]),
        "aggregate_id": str(outbox_row["aggregate_id"]),
        "committed_ns": int(receipt_row["committed_ns"]),
    }


def _record_collision(database: Path, receipt_row: sqlite3.Row) -> tuple[int, dict[str, object]]:
    receipt = WorkReceipt.from_json(str(receipt_row["receipt_json"]))
    with semantic_database(database, readonly=True) as connection:
        outbox_row = connection.execute(
            "SELECT * FROM semantic_derivation_outbox WHERE receipt_id=?",
            (int(receipt_row["receipt_id"]),),
        ).fetchone()
    assert outbox_row is not None
    kwargs = _receipt_row_kwargs(receipt_row, receipt, outbox_row)
    with semantic_database(database) as connection:
        result = semantic_lineage_repository._record_work_receipt(connection, **kwargs)
    return result, kwargs


def _binding_by_name(receipt: WorkReceipt, name: str) -> Any:
    for binding in (*receipt.inputs, *receipt.outputs):
        if binding.name == name:
            return binding
    raise AssertionError(f"receipt binding not found: {name}")


def _semantic_binding_schemas(receipt: WorkReceipt) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for binding in (*receipt.inputs, *receipt.outputs):
        materialization = binding.materialization
        if materialization is None:
            continue
        result[binding.name] = (materialization.owner, materialization.schema_version)
    return result


@pytest.mark.parametrize(
    ("source_version", "target_version"),
    ((7, 8), (7, 9), (8, 9)),
)
def test_schema_migration_preserves_receipt_outbox_bytes_and_writes_current_version(
    tmp_path: Path,
    source_version: int,
    target_version: int,
) -> None:
    fixture = _v7_fixture(tmp_path)
    database = fixture["database"]
    model = fixture["model"]
    before = _domain_snapshot(database)
    old_receipt = WorkReceipt.from_json(str(fixture["embedding_row"]["receipt_json"]))
    assert dict(old_receipt.runtime)["semantic_schema"] == "7"
    assert _semantic_schema_values(old_receipt.to_dict())
    assert all(value == 7 for value in _semantic_schema_values(old_receipt.to_dict()))

    with semantic_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 7
        assert str(
            connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        ) == "7"

    if source_version == 8:
        _migrate_semantic_schema(database, source_version)
    _migrate_semantic_schema(database, target_version)
    after_migration = _domain_snapshot(database)
    assert before == after_migration
    with semantic_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == target_version
        assert str(
            connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        ) == str(target_version)
        assert tuple(
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == tuple(range(1, target_version + 1))

    preserved = WorkReceipt.from_json(str(fixture["embedding_row"]["receipt_json"]))
    assert dict(preserved.runtime)["semantic_schema"] == "7"
    assert all(value == 7 for value in _semantic_schema_values(preserved.to_dict()))

    current_chunk = stage_fixture_item(
        database,
        item_id=f"receipt-schema-v{target_version}-source",
        source_revision_id=f"revision:text:receipt-schema-v{target_version}-source:v{target_version}",
        text=f"contenido nuevo para una materializacion Semantic v{target_version}",
        ordinal=2,
    )
    current_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"receipt-schema-v{target_version}-generation",
        provenance={"fixture": "receipt-schema-compatibility", "schema": target_version},
        materialize_base=False,
        started_ns=200,
    )
    assert enqueue_text_chunk_jobs(
        database, current_generation, (current_chunk.chunk_id,), now_ns=210
    ) == 1
    assert (
        prepare_embedding_generation(
            database,
            current_generation,
            enumeration_complete=True,
        )
        is None
    )
    lease = claim_embedding_jobs(
        database,
        current_generation,
        worker_id=f"receipt-schema-v{target_version}-worker",
        limit=1,
        now_ns=220,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id=f"receipt-schema-v{target_version}-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=230,
    )
    assert (
        finalize_embedding_generation(database, current_generation, completed_ns=240).status
        == "ready"
    )
    with semantic_database(database, readonly=True) as connection:
        current = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' AND job_id=?",
            (current_generation, lease.job_id),
        ).fetchone()
    assert current is not None
    current_receipt = WorkReceipt.from_json(str(current[0]))
    assert dict(current_receipt.runtime)["semantic_schema"] == str(target_version)
    assert all(
        value == target_version for value in _semantic_schema_values(current_receipt.to_dict())
    )
    assert dict(old_receipt.runtime)["semantic_schema"] == "7"


@pytest.mark.parametrize("target_version", (8, 9))
def test_repeated_v7_receipt_key_on_current_versions_is_idempotent(
    tmp_path: Path,
    target_version: int,
) -> None:
    fixture = _v7_fixture(tmp_path, item_id="repeat-key-source")
    database = fixture["database"]
    before = _domain_snapshot(database)
    old_receipt_id = int(fixture["chunk_row"]["receipt_id"])
    _migrate_semantic_schema(database, target_version)

    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM semantic_work_receipts WHERE receipt_id=?",
            (old_receipt_id,),
        ).fetchone()
    assert row is not None
    result, _kwargs = _record_collision(database, row)
    assert result == old_receipt_id
    after = _domain_snapshot(database)
    assert after == before
    with semantic_database(database, readonly=True) as connection:
        stored = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts WHERE receipt_id=?",
            (old_receipt_id,),
        ).fetchone()
    assert stored is not None
    assert str(stored[0]) == str(row["receipt_json"])


@pytest.mark.parametrize("target_version", (8, 9))
def test_rebind_and_cache_hit_keep_v7_causal_refs_and_current_locators(
    tmp_path: Path,
    target_version: int,
) -> None:
    fixture = _v7_fixture(tmp_path, item_id="rebind-source")
    database = fixture["database"]
    model = fixture["model"]
    chunk = fixture["chunk"]
    producer_row = fixture["embedding_row"]
    producer_receipt = WorkReceipt.from_json(str(producer_row["receipt_json"]))
    producer_key = str(producer_row["receipt_key"])
    _migrate_semantic_schema(database, target_version)

    # Keep the content-addressed chunk unchanged while advancing only the
    # current source revision.  The next queue pass must therefore rebind the
    # old member instead of running a provider.
    rebound_chunk = stage_fixture_item(
        database,
        item_id=chunk.item_id,
        source_revision_id=f"revision:text:rebind-source:v{target_version}",
        text="contenido determinista para compatibilidad de receipts Semantic",
        ordinal=1,
    )
    assert rebound_chunk.chunk_id == chunk.chunk_id
    replay_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"receipt-schema-v{target_version}-replay",
        provenance={
            "fixture": "receipt-schema-compatibility",
            "mode": "replay",
            "schema": target_version,
        },
        materialize_base=False,
        started_ns=300,
    )
    enqueue_text_chunk_jobs(database, replay_generation, (chunk.chunk_id,), now_ns=310)
    assert (
        prepare_embedding_generation(
            database,
            replay_generation,
            enumeration_complete=True,
        )
        is None
    )
    assert finalize_embedding_generation(database, replay_generation, completed_ns=320).status == "ready"

    with semantic_database(database, readonly=True) as connection:
        replay_row = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' AND entity_id=?",
            (replay_generation, chunk.chunk_id),
        ).fetchone()
    assert replay_row is not None
    replay_receipt = WorkReceipt.from_json(str(replay_row[0]))
    replay_schemas = _semantic_binding_schemas(replay_receipt)
    assert replay_receipt.execution_mode.value == "replay"
    assert replay_receipt.causation_id == producer_key
    assert replay_schemas["source_embedding_member"] == ("semantic", 7)
    assert replay_schemas["semantic_item_snapshot"] == ("semantic", target_version)
    assert replay_schemas["semantic_chunk_snapshot"] == ("semantic", target_version)
    assert replay_schemas["semantic_embedding_member:0"] == ("semantic", target_version)
    assert replay_receipt.outputs
    assert all(
        output.materialization.owner == "semantic"
        and output.materialization.schema_version == target_version
        for output in replay_receipt.outputs
    )
    assert replay_receipt.inputs[0].materialization is not None
    assert replay_receipt.inputs[0].materialization.owner == "semantic"
    assert replay_receipt.inputs[0].materialization.schema_version == target_version
    assert producer_receipt.execution_mode.value == "executed"
    assert all(value == 7 for value in _semantic_schema_values(producer_receipt.to_dict()))

    replacement = stage_fixture_item(
        database,
        item_id="cache-replacement",
        source_revision_id=f"revision:text:cache-replacement:v{target_version}",
        text="contenido determinista para compatibilidad de receipts Semantic",
        ordinal=3,
    )
    cache_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"receipt-schema-v{target_version}-cache-hit",
        provenance={
            "fixture": "receipt-schema-compatibility",
            "mode": "cache_hit",
            "schema": target_version,
        },
        materialize_base=False,
        started_ns=400,
    )
    assert enqueue_text_chunk_jobs(database, cache_generation, (replacement.chunk_id,), now_ns=410) == 1
    assert (
        prepare_embedding_generation(
            database,
            cache_generation,
            enumeration_complete=True,
        )
        is None
    )
    assert reuse_cached_jobs(database, cache_generation, now_ns=420) == 1
    assert finalize_embedding_generation(database, cache_generation, completed_ns=430).status == "ready"
    with semantic_database(database, readonly=True) as connection:
        cache_row = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' AND entity_id=?",
            (cache_generation, replacement.chunk_id),
        ).fetchone()
    assert cache_row is not None
    cache_receipt = WorkReceipt.from_json(str(cache_row[0]))
    cache_schemas = _semantic_binding_schemas(cache_receipt)
    assert cache_receipt.execution_mode.value == "cache_hit"
    assert cache_receipt.causation_id == producer_key
    assert cache_schemas["reused_vector_payload"] == ("semantic", 7)
    assert cache_schemas["semantic_item_snapshot"] == ("semantic", target_version)
    assert cache_schemas["semantic_chunk_snapshot"] == ("semantic", target_version)
    assert cache_schemas["semantic_embedding_member:0"] == ("semantic", target_version)
    assert cache_receipt.outputs
    assert all(
        output.materialization.owner == "semantic"
        and output.materialization.schema_version == target_version
        for output in cache_receipt.outputs
    )


@pytest.mark.parametrize("changed_fact", ("fingerprint", "stage", "id", "causation", "outcome", "attempt"))
def test_current_receipt_key_rejects_changed_causal_facts_without_mutation(
    tmp_path: Path,
    changed_fact: str,
) -> None:
    fixture = _v7_fixture(tmp_path, item_id=f"negative-{changed_fact}")
    database = fixture["database"]
    initialize_semantic_state(database)
    before = _domain_snapshot(database)
    receipt_row = fixture["chunk_row"]
    receipt = WorkReceipt.from_json(str(receipt_row["receipt_json"]))
    with semantic_database(database, readonly=True) as connection:
        outbox_row = connection.execute(
            "SELECT * FROM semantic_derivation_outbox WHERE receipt_id=?",
            (int(receipt_row["receipt_id"]),),
        ).fetchone()
    assert outbox_row is not None
    kwargs = _receipt_row_kwargs(receipt_row, receipt, outbox_row)
    if changed_fact == "fingerprint":
        mutated_inputs = list(kwargs["inputs"])
        assert mutated_inputs and isinstance(mutated_inputs[0], Mapping)
        mutated_inputs[0] = dict(mutated_inputs[0])
        mutated_inputs[0]["fingerprint"] = {
            "algorithm": "xxh3-128",
            "value": "changed-fingerprint",
        }
        kwargs["inputs"] = tuple(mutated_inputs)
    elif changed_fact == "stage":
        kwargs["stage_id"] = "semantic.text.chunk.materialize.changed"
    elif changed_fact == "id":
        kwargs["entity_id"] = str(kwargs["entity_id"]) + ":changed"
    elif changed_fact == "causation":
        kwargs["causation_receipt_id"] = int(receipt_row["receipt_id"])
    elif changed_fact == "outcome":
        kwargs["status"] = "failed"
        kwargs["execution_mode"] = "attempted"
        kwargs["error"] = {"error_type": "fixture", "error_message": "changed outcome"}
    elif changed_fact == "attempt":
        kwargs["attempt"] = int(receipt_row["attempt"]) + 1
    else:  # pragma: no cover - protected by the parametrization
        raise AssertionError(changed_fact)

    with pytest.raises((SemanticStateError, ValueError)):
        with semantic_database(database) as connection:
            semantic_lineage_repository._record_work_receipt(connection, **kwargs)
    assert _domain_snapshot(database) == before


@pytest.mark.parametrize(
    "runtime_mutation",
    ("future", "missing", "malformed", "boolean"),
)
def test_receipt_runtime_schema_metadata_is_strict_and_not_normalized(
    tmp_path: Path,
    runtime_mutation: str,
) -> None:
    fixture = _v7_fixture(tmp_path, item_id=f"runtime-{runtime_mutation}")
    receipt = WorkReceipt.from_json(str(fixture["chunk_row"]["receipt_json"]))
    payload = json.loads(receipt.to_json())
    if runtime_mutation == "future":
        payload["runtime"]["semantic_schema"] = "10"
    elif runtime_mutation == "missing":
        payload["runtime"].pop("semantic_schema")
    elif runtime_mutation == "malformed":
        payload["runtime"] = ["semantic_schema", "7"]
    elif runtime_mutation == "boolean":
        payload["runtime"]["semantic_schema"] = True
    else:  # pragma: no cover - protected by parametrization
        raise AssertionError(runtime_mutation)

    if runtime_mutation in {"malformed", "boolean"}:
        with pytest.raises(ValueError):
            WorkReceipt.from_dict(payload)
        return
    mutated = WorkReceipt.from_dict(payload)
    with pytest.raises(ValueError, match="unsupported owner schema"):
        semantic_lineage_repository._validate_receipt_semantic_locators(mutated)


def test_declared_v7_connection_cannot_read_v8_receipt_owner_as_legacy(
    tmp_path: Path,
) -> None:
    fixture = _v7_fixture(tmp_path, item_id="stored-v8-connection-v7")
    database = fixture["database"]
    initialize_semantic_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            "UPDATE metadata SET value='7' WHERE key='schema_version'"
        )

    with pytest.raises(SemanticStateError):
        semantic_lineage_repository.read_semantic_derivation_outbox(database)


@pytest.mark.parametrize("bad_schema", (10, True))
def test_new_semantic_output_locator_schema_is_rejected_before_digest_or_replace(
    bad_schema: int | bool,
) -> None:
    with pytest.raises(SemanticStateError, match="unsupported"):
        semantic_lineage_repository._output_contracts(
            (),
            generation_id=None,
            schema_version=bad_schema,
        )


@pytest.mark.parametrize("bad_value", (999, True, None))
def test_semantic_locator_metadata_bad_values_are_not_normalized_to_supported_schema(
    bad_value: int | bool | None,
) -> None:
    payload: dict[str, object] = {
        "kind": "materialization_ref",
        "owner": "semantic",
        "materialization_id": "materialization:semantic:fixture",
        "owner_schema_version": bad_value,
    }
    with pytest.raises(SemanticStateError, match="metadata is not 7, 8 or 9"):
        semantic_lineage_repository._normalize_receipt_semantic_schema_metadata(payload)

    missing = dict(payload)
    missing.pop("owner_schema_version")
    with pytest.raises(SemanticStateError, match="metadata is not 7, 8 or 9"):
        semantic_lineage_repository._normalize_receipt_semantic_schema_metadata(missing)


def test_historical_other_owner_materialization_is_not_rebound_to_semantic_schema(
) -> None:
    historical = MaterializationRef(
        owner="text",
        kind="canonical_text",
        materialization_id="materialization:text:historical",
        schema_version=2,
    )
    normalized = semantic_lineage_repository._semantic_materialization_for_schema(
        historical,
        schema_version=8,
    )
    assert normalized is historical
    assert normalized.schema_version == 2
    payload = {
        "kind": "materialization_ref",
        "owner": "text",
        "materialization_id": historical.materialization_id,
        "owner_schema_version": 2,
    }
    assert semantic_lineage_repository._normalize_receipt_semantic_schema_metadata(payload) == payload


@pytest.mark.parametrize("target_version", (8, 9))
def test_same_version_locator_schema_change_is_not_tolerated(
    tmp_path: Path,
    target_version: int,
) -> None:
    """Keep a stored current receipt immutable when only its locator changes."""

    fixture = _v7_fixture(tmp_path, item_id="same-version-locator")
    database = fixture["database"]
    _migrate_semantic_schema(database, target_version)

    # Produce a genuine current receipt first, then alter only the fixture's stored
    # payload and corresponding outbox event.  This is a private temporary
    # fixture, never a production owner; the update trigger is restored before
    # the assertion so the source contract remains active for the probe.
    model = fixture["model"]
    current_chunk = stage_fixture_item(
        database,
        item_id=f"same-version-locator-v{target_version}",
        source_revision_id=f"revision:text:same-version-locator-v{target_version}",
        text=f"contenido nuevo para un receipt Semantic v{target_version} independiente",
        ordinal=2,
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"same-version-locator-v{target_version}",
        provenance={"fixture": "same-version-locator"},
        materialize_base=False,
        started_ns=500,
    )
    enqueued = enqueue_text_chunk_jobs(
        database,
        generation_id,
        (current_chunk.chunk_id,),
        now_ns=510,
    )
    assert enqueued == 1
    assert (
        prepare_embedding_generation(
            database,
            generation_id,
            enumeration_complete=True,
        )
        is None
    )
    lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="same-version-locator-worker",
        limit=1,
        now_ns=520,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="same-version-locator-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=530,
    )
    with semantic_database(database, readonly=True) as connection:
        receipt_row = connection.execute(
            "SELECT * FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' AND job_id=?",
            (generation_id, lease.job_id),
        ).fetchone()
    assert receipt_row is not None
    original_payload = json.loads(str(receipt_row["receipt_json"]))

    def downgrade_semantic_locators(value: object) -> object:
        if isinstance(value, dict):
            result = {key: downgrade_semantic_locators(child) for key, child in value.items()}
            if result.get("kind") == "materialization_ref" and result.get("owner") == "semantic":
                result["owner_schema_version"] = 7
            return result
        if isinstance(value, list):
            return [downgrade_semantic_locators(child) for child in value]
        return value

    changed_receipt = downgrade_semantic_locators(original_payload)
    assert isinstance(changed_receipt, dict)
    changed_json = semantic_lineage_repository.canonical_json(changed_receipt)
    with semantic_database(database) as connection:
        connection.execute("DROP TRIGGER semantic_work_receipts_no_update")
        connection.execute("DROP TRIGGER semantic_derivation_outbox_no_update")
        connection.execute(
            "UPDATE semantic_work_receipts SET receipt_json=? WHERE receipt_id=?",
            (changed_json, int(receipt_row["receipt_id"])),
        )
        outbox = connection.execute(
            "SELECT payload_json FROM semantic_derivation_outbox WHERE receipt_id=?",
            (int(receipt_row["receipt_id"]),),
        ).fetchone()
        assert outbox is not None
        changed_event = json.loads(str(outbox[0]))
        changed_event["receipt"] = changed_receipt
        connection.execute(
            "UPDATE semantic_derivation_outbox SET payload_json=? WHERE receipt_id=?",
            (semantic_lineage_repository.canonical_json(changed_event), int(receipt_row["receipt_id"])),
        )
        connection.execute(
            """CREATE TRIGGER semantic_work_receipts_no_update
            BEFORE UPDATE ON semantic_work_receipts BEGIN
                SELECT RAISE(ABORT,'semantic work receipts are append-only');
            END"""
        )
        connection.execute(
            """CREATE TRIGGER semantic_derivation_outbox_no_update
            BEFORE UPDATE ON semantic_derivation_outbox BEGIN
                SELECT RAISE(ABORT,'semantic derivation outbox is append-only');
            END"""
        )

    with semantic_database(database, readonly=True) as connection:
        changed_row = connection.execute(
            "SELECT * FROM semantic_work_receipts WHERE receipt_id=?",
            (int(receipt_row["receipt_id"]),),
        ).fetchone()
    assert changed_row is not None
    with pytest.raises(SemanticStateError):
        _record_collision(database, changed_row)
