"""Semantic outbox v1/v2 envelope, hydration, and corruption regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.semantic import semantic_lineage_repository
from neocortex.semantic import semantic_schema
from neocortex.semantic.derivation_contracts import WorkReceipt
from neocortex.semantic.derivation_projection import (
    projection_event_from_semantic_outbox,
    rebuild_derivation_projection,
)
from neocortex.semantic.semantic_models import canonical_json
from neocortex.semantic.semantic_state import (
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)
from tests.test_semantic_derivation_lineage import (
    _execute,
    _generation,
    _model,
    _stage,
)
from tests.test_semantic_generation_control_projection import _migrate_v5_to_v7
from tests.test_semantic_generation_publication_v6 import _create_populated_v5
from tests.test_semantic_receipt_schema_compatibility import (
    _binding_payload,
    _record_collision,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_EVENT_V1 = "neocortex.semantic-derivation-event/v1"
_EVENT_V2 = "neocortex.semantic-derivation-event/v2"
_OUTBOX_UPDATE_TRIGGER = "semantic_derivation_outbox_no_update"
_RECEIPT_UPDATE_TRIGGER = "semantic_work_receipts_no_update"


def _initialize_v9_fixture_owner(database: Path):
    """Build an exact historical v9 owner without current-schema initialization."""

    with semantic_schema.semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        semantic_schema._build_exact_schema(connection, 9)
        semantic_schema._store_schema_version(connection, 9)
        for _name, statement in semantic_schema._SEMANTIC_PERFORMANCE_INDEXES:
            connection.execute(statement)
    model = _model()
    register_embedding_model(database, model, allow_test_provider=True)
    return model


def _v9_fixture(tmp_path: Path, *, item_id: str = "outbox-v9-item") -> dict[str, object]:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize_v9_fixture_owner(database)
    chunk = _stage(
        database,
        item_id=item_id,
        source_revision_id=f"revision:text:{item_id}:v9",
        text="fixture real para envelope Semantic referenciado",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature=f"outbox-v9-generation:{item_id}",
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=110) == 1
    _execute(database, generation_id, now_ns=120)
    assert finalize_embedding_generation(database, generation_id, completed_ns=130).status == "ready"
    with semantic_database(database, readonly=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert version == 9
    return {"database": database, "chunk": chunk, "generation_id": generation_id}


def _legacy_fixture(tmp_path: Path, version: int) -> Path:
    database = tmp_path / f"semantic-v{version}.sqlite3"
    model, _legacy_chunk = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    if version == 8:
        with semantic_database(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            semantic_schema._migrate_to_v8(connection, 8)
            semantic_schema._store_schema_version(connection, 8)
    else:
        assert version == 7
    chunk = _stage(
        database,
        item_id=f"legacy-outbox-v{version}",
        source_revision_id=f"revision:text:legacy-outbox:v{version}",
        text=f"receipt v{version} preservado como envelope v1",
        ordinal=version,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature=f"legacy-outbox-generation-v{version}",
        started_ns=200,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=210) == 1
    _execute(database, generation_id, now_ns=220)
    assert finalize_embedding_generation(database, generation_id, completed_ns=230).status == "ready"
    with semantic_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == version
    return database


def _stored_rows(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with semantic_database(database, readonly=True) as connection:
        return {
            "receipts": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT receipt_id,receipt_key,receipt_json,committed_ns "
                    "FROM semantic_work_receipts ORDER BY receipt_id"
                )
            ),
            "outbox": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT event_id,receipt_id,event_kind,aggregate_kind,aggregate_id,"
                    "payload_json,committed_ns FROM semantic_derivation_outbox "
                    "ORDER BY event_id"
                )
            ),
        }


def _first_receipt_row(database: Path) -> sqlite3.Row:
    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM semantic_work_receipts ORDER BY receipt_id LIMIT 1"
        ).fetchone()
    assert row is not None
    return row


def _event_rows(database: Path) -> tuple[sqlite3.Row, ...]:
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            connection.execute(
                """SELECT event.event_id,event.receipt_id,event.event_kind,
                    event.aggregate_kind,event.aggregate_id,event.payload_json,
                    event.committed_ns,receipt.receipt_key,receipt.receipt_json
                FROM semantic_derivation_outbox event
                JOIN semantic_work_receipts receipt ON receipt.receipt_id=event.receipt_id
                ORDER BY event.event_id"""
            ).fetchall()
        )


def _referenced_payload_for_row(row: sqlite3.Row) -> str:
    return canonical_json(
        {
            "schema": _EVENT_V2,
            "owner": "semantic",
            "receipt_ref": {
                "owner": "semantic",
                "receipt_id": int(row["receipt_id"]),
                "receipt_key": str(row["receipt_key"]),
                "receipt_sha256": "sha256:"
                + hashlib.sha256(str(row["receipt_json"]).encode("utf-8")).hexdigest(),
                "contract": "neocortex.work-receipt/v1",
            },
            "event_kind": str(row["event_kind"]),
            "aggregate_kind": str(row["aggregate_kind"]),
            "aggregate_id": str(row["aggregate_id"]),
            "committed_ns": int(row["committed_ns"]),
        }
    )


def _restore_outbox_update_trigger(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""CREATE TRIGGER {_OUTBOX_UPDATE_TRIGGER}
        BEFORE UPDATE ON semantic_derivation_outbox BEGIN
            SELECT RAISE(ABORT,'semantic derivation outbox is append-only');
        END"""
    )


def _restore_receipt_update_trigger(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""CREATE TRIGGER {_RECEIPT_UPDATE_TRIGGER}
        BEFORE UPDATE ON semantic_work_receipts BEGIN
            SELECT RAISE(ABORT,'semantic work receipts are append-only');
        END"""
    )


def _rewrite_outbox_payload(
    database: Path,
    event_id: int,
    payload_json: str,
    *,
    receipt_id: int | None = None,
    foreign_keys: bool = True,
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        connection.execute(f"DROP TRIGGER {_OUTBOX_UPDATE_TRIGGER}")
        if receipt_id is None:
            connection.execute(
                "UPDATE semantic_derivation_outbox SET payload_json=? WHERE event_id=?",
                (payload_json, event_id),
            )
        else:
            connection.execute(
                "UPDATE semantic_derivation_outbox SET receipt_id=? WHERE event_id=?",
                (receipt_id, event_id),
            )
        _restore_outbox_update_trigger(connection)
        connection.commit()


def _rewrite_receipt(database: Path, receipt_id: int, receipt_json: str) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"DROP TRIGGER {_RECEIPT_UPDATE_TRIGGER}")
        connection.execute(
            "UPDATE semantic_work_receipts SET receipt_json=? WHERE receipt_id=?",
            (receipt_json, receipt_id),
        )
        _restore_receipt_update_trigger(connection)
        connection.commit()


def _mutate_v2(database: Path, mutation: str) -> None:
    row = _event_rows(database)[0]
    event_id = int(row["event_id"])
    receipt_id = int(row["receipt_id"])
    payload_raw = str(row["payload_json"])
    payload = json.loads(payload_raw)
    if mutation == "wrong_id":
        payload["receipt_ref"]["receipt_id"] = receipt_id + 1
    elif mutation == "wrong_key":
        payload["receipt_ref"]["receipt_key"] = "wrong-receipt-key"
    elif mutation == "wrong_digest":
        payload["receipt_ref"]["receipt_sha256"] = "sha256:" + "0" * 64
    elif mutation == "future_schema":
        payload["schema"] = "neocortex.semantic-derivation-event/v3"
    elif mutation == "missing_ref":
        payload.pop("receipt_ref")
    elif mutation == "wrong_owner":
        payload["receipt_ref"]["owner"] = "text"
    elif mutation == "wrong_contract":
        payload["receipt_ref"]["contract"] = "other.contract/v1"
    elif mutation == "extra_key":
        payload["unexpected"] = True
    elif mutation == "double_key":
        needle = '"owner":"semantic"'
        if needle not in payload_raw:
            needle = '"owner": "semantic"'
        assert needle in payload_raw
        _rewrite_outbox_payload(
            database,
            event_id,
            payload_raw.replace(needle, needle + "," + needle, 1),
        )
        return
    elif mutation == "noncanonical_payload":
        _rewrite_outbox_payload(database, event_id, payload_raw + " ")
        return
    elif mutation == "committed_mismatch":
        payload["committed_ns"] = int(payload["committed_ns"]) + 1
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"DROP TRIGGER {_OUTBOX_UPDATE_TRIGGER}")
            connection.execute(
                "UPDATE semantic_derivation_outbox SET payload_json=?,committed_ns=? "
                "WHERE event_id=?",
                (canonical_json(payload), int(payload["committed_ns"]), event_id),
            )
            _restore_outbox_update_trigger(connection)
            connection.commit()
        return
    elif mutation == "columns_mismatch":
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"DROP TRIGGER {_OUTBOX_UPDATE_TRIGGER}")
            connection.execute(
                "UPDATE semantic_derivation_outbox SET event_kind=? WHERE event_id=?",
                ("wrong-event-kind", event_id),
            )
            _restore_outbox_update_trigger(connection)
            connection.commit()
        return
    elif mutation == "orphan":
        _rewrite_outbox_payload(
            database,
            event_id,
            payload_raw,
            receipt_id=9_999_999,
            foreign_keys=False,
        )
        return
    elif mutation == "noncanonical_receipt":
        _rewrite_receipt(database, receipt_id, str(row["receipt_json"]) + " ")
        return
    elif mutation == "future_receipt_schema":
        receipt = json.loads(str(row["receipt_json"]))
        receipt["runtime"]["semantic_schema"] = "999"
        _rewrite_receipt(database, receipt_id, canonical_json(receipt))
        return
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(mutation)
    _rewrite_outbox_payload(database, event_id, canonical_json(payload))


def test_v9_writer_stores_strict_compact_v2_and_reader_hydrates_the_v1_logical_event(
    tmp_path: Path,
) -> None:
    fixture = _v9_fixture(tmp_path)
    database = fixture["database"]
    assert isinstance(database, Path)
    rows = _event_rows(database)
    events = semantic_lineage_repository.read_semantic_derivation_outbox(database, limit=100)
    assert len(events) == len(rows) > 0
    by_id = {int(row["event_id"]): row for row in rows}
    for event in events:
        row = by_id[event.event_id]
        wire = json.loads(str(row["payload_json"]))
        receipt = json.loads(str(row["receipt_json"]))
        assert set(wire) == {
            "schema",
            "owner",
            "receipt_ref",
            "event_kind",
            "aggregate_kind",
            "aggregate_id",
            "committed_ns",
        }
        assert wire["schema"] == _EVENT_V2
        assert wire["owner"] == "semantic"
        assert set(wire["receipt_ref"]) == {
            "owner",
            "receipt_id",
            "receipt_key",
            "receipt_sha256",
            "contract",
        }
        ref = wire["receipt_ref"]
        assert ref == {
            "owner": "semantic",
            "receipt_id": int(row["receipt_id"]),
            "receipt_key": str(row["receipt_key"]),
            "receipt_sha256": "sha256:" + hashlib.sha256(
                str(row["receipt_json"]).encode("utf-8")
            ).hexdigest(),
            "contract": "neocortex.work-receipt/v1",
        }
        assert str(row["payload_json"]) == canonical_json(wire)
        assert str(row["receipt_json"]) == canonical_json(receipt)
        logical_v1 = {
            "schema": _EVENT_V1,
            "receipt_id": int(row["receipt_id"]),
            "receipt_key": str(row["receipt_key"]),
            "event_kind": str(row["event_kind"]),
            "aggregate_kind": str(row["aggregate_kind"]),
            "aggregate_id": str(row["aggregate_id"]),
            "receipt": receipt,
        }
        assert event.payload == logical_v1
        assert event.receipt == receipt
        assert event.committed_ns == int(row["committed_ns"])
        assert event.receipt["causation_id"] == receipt["causation_id"]

    with semantic_database(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE semantic_derivation_outbox SET committed_ns=committed_ns+1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM semantic_derivation_outbox")
    with semantic_database(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE semantic_work_receipts SET committed_ns=committed_ns+1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM semantic_work_receipts")


def test_v9_hydrated_projection_replay_is_idempotent_and_preserves_full_receipts(
    tmp_path: Path,
) -> None:
    fixture = _v9_fixture(tmp_path, item_id="projection-v9")
    database = fixture["database"]
    assert isinstance(database, Path)
    events = semantic_lineage_repository.read_semantic_derivation_outbox(database, limit=100)
    adapted = tuple(projection_event_from_semantic_outbox(event) for event in events)
    projection = rebuild_derivation_projection(adapted)
    replay = rebuild_derivation_projection(adapted + adapted)
    assert replay.events_applied == projection.events_applied == len(adapted)
    assert replay.duplicate_events_ignored == len(adapted)
    assert replay.nodes == projection.nodes
    assert replay.edges == projection.edges
    assert replay.event_fingerprints == projection.event_fingerprints
    assert all(event.receipt["kind"] == "work_receipt" for event in events)


@pytest.mark.parametrize("version", (7, 8))
def test_legacy_v1_events_and_receipt_bytes_survive_migration_to_current_and_retry(
    tmp_path: Path,
    version: int,
) -> None:
    database = _legacy_fixture(tmp_path, version)
    before = _stored_rows(database)
    before_payloads = tuple(str(row[5]) for row in before["outbox"])
    assert before_payloads
    assert all(json.loads(payload)["schema"] == _EVENT_V1 for payload in before_payloads)
    with semantic_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == version

    initialize_semantic_state(database)
    after_migration = _stored_rows(database)
    assert after_migration == before
    with semantic_database(database, readonly=True) as connection:
        assert (
            int(connection.execute("PRAGMA user_version").fetchone()[0])
            == semantic_schema.SEMANTIC_SCHEMA_VERSION
        )
        assert str(
            connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        ) == str(semantic_schema.SEMANTIC_SCHEMA_VERSION)
        assert tuple(
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == tuple(range(1, semantic_schema.SEMANTIC_SCHEMA_VERSION + 1))

    receipt_row = _first_receipt_row(database)
    result, _kwargs = _record_collision(database, receipt_row)
    assert result == int(receipt_row["receipt_id"])
    assert _stored_rows(database) == before
    events = semantic_lineage_repository.read_semantic_derivation_outbox(database, limit=100)
    assert events
    assert all(event.payload["schema"] == _EVENT_V1 for event in events)
    assert all(event.payload["receipt"] == event.receipt for event in events)


def test_new_v9_v2_receipt_key_retry_is_idempotent_and_keeps_storage_unchanged(
    tmp_path: Path,
) -> None:
    fixture = _v9_fixture(tmp_path, item_id="v2-retry")
    database = fixture["database"]
    assert isinstance(database, Path)
    before = _stored_rows(database)
    receipt_row = _first_receipt_row(database)
    result, _kwargs = _record_collision(database, receipt_row)
    assert result == int(receipt_row["receipt_id"])
    assert _stored_rows(database) == before


@pytest.mark.parametrize("version", (7, 8))
def test_legacy_owner_rejects_v2_wire_for_read_and_collision_write(
    tmp_path: Path,
    version: int,
) -> None:
    database = _legacy_fixture(tmp_path, version)
    row = _event_rows(database)[0]
    _rewrite_outbox_payload(database, int(row["event_id"]), _referenced_payload_for_row(row))
    with pytest.raises((semantic_schema.SemanticStateError, ValueError), match=r"(?i)(schema|v2|unsupported)"):
        semantic_lineage_repository.read_semantic_derivation_outbox(database, limit=100)
    before_collision = _stored_rows(database)
    with pytest.raises((semantic_schema.SemanticStateError, ValueError), match=r"(?i)(schema|v2|unsupported)"):
        _record_collision(database, _first_receipt_row(database))
    assert _stored_rows(database) == before_collision


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_id",
        "wrong_key",
        "wrong_digest",
        "future_schema",
        "missing_ref",
        "wrong_owner",
        "wrong_contract",
        "extra_key",
        "double_key",
        "noncanonical_payload",
        "committed_mismatch",
        "columns_mismatch",
        "orphan",
        "noncanonical_receipt",
        "future_receipt_schema",
    ),
)
def test_v2_reader_rejects_reference_row_and_receipt_corruption_without_repair(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = _v9_fixture(tmp_path, item_id=f"corrupt-{mutation}")
    database = fixture["database"]
    assert isinstance(database, Path)
    _mutate_v2(database, mutation)
    corrupted = _stored_rows(database)
    with pytest.raises((semantic_schema.SemanticStateError, ValueError)):
        semantic_lineage_repository.read_semantic_derivation_outbox(database, limit=100)
    assert _stored_rows(database) == corrupted


def test_v9_writer_rolls_back_receipt_and_v2_outbox_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _v9_fixture(tmp_path, item_id="rollback-v9")
    database = fixture["database"]
    assert isinstance(database, Path)
    before = _stored_rows(database)
    with semantic_database(database, readonly=True) as connection:
        before_chunks = int(connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0])
    real_record = semantic_lineage_repository._record_work_receipt

    def fail_after_record(*args: object, **kwargs: object) -> int:
        real_record(*args, **kwargs)
        raise RuntimeError("fixture outbox interruption")

    monkeypatch.setattr(semantic_lineage_repository, "_record_work_receipt", fail_after_record)
    with pytest.raises(RuntimeError, match="fixture outbox interruption"):
        _stage(
            database,
            item_id="rollback-v9-new-item",
            source_revision_id="revision:text:rollback-v9-new",
            text="chunk rollback must remove receipt and outbox",
            ordinal=2,
            publish=False,
        )
    assert _stored_rows(database) == before
    with semantic_database(database, readonly=True) as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]) == before_chunks


def _create_large_v2_events(database: Path, count: int = 14) -> tuple[int, ...]:
    row = _first_receipt_row(database)
    base_receipt = WorkReceipt.from_json(str(row["receipt_json"]))
    base_input = _binding_payload(base_receipt.inputs[0], output=False)
    base_output = _binding_payload(base_receipt.outputs[0], output=True)
    inputs: list[dict[str, object]] = []
    input_bytes = 0
    for index in range(4_096):
        binding = dict(base_input)
        binding["binding_name"] = f"large-input-{index:04d}"
        size = len(
            canonical_json(
                {
                    key: (
                        value.to_dict()
                        if hasattr(value, "to_dict")
                        else value
                    )
                    for key, value in binding.items()
                }
            ).encode("utf-8")
        )
        if inputs and input_bytes + size > 650_000:
            break
        inputs.append(binding)
        input_bytes += size
    assert 600_000 < input_bytes <= 650_000
    with semantic_database(database) as connection:
        event_ids: list[int] = []
        for index in range(count):
            semantic_lineage_repository._record_work_receipt(
                connection,
                receipt_key=f"large-v2-receipt-{index}",
                stage_id="semantic.fixture.large",
                stage_version="large-v2-v1",
                processing_signature="large-v2-receipt-fixture",
                status="succeeded",
                execution_mode="executed",
                reproducibility_class="environment_bound",
                entity_kind="large_fixture",
                entity_id=f"large-{index}",
                inputs=tuple(inputs),
                outputs=(base_output,),
                effective_config={},
                provider={"provider": "fixture"},
                item_revision_id=None,
                chunk_revision_id=None,
                generation_id=None,
                model_signature=None,
                payload_id=None,
                job_id=None,
                attempt=1,
                started_ns=1_000 + index,
                finished_ns=1_000 + index,
                error=None,
                causation_receipt_id=None,
                event_kind="semantic_large_fixture",
                aggregate_kind="large_fixture",
                aggregate_id=f"large-{index}",
                committed_ns=1_000 + index,
            )
            stored_receipt = connection.execute(
                "SELECT receipt_json FROM semantic_work_receipts WHERE receipt_key=?",
                (f"large-v2-receipt-{index}",),
            ).fetchone()
            assert stored_receipt is not None
            receipt_size = len(str(stored_receipt[0]).encode("utf-8"))
            assert 600_000 < receipt_size < 1_000_000
            event_ids.append(
                int(
                    connection.execute(
                        "SELECT event_id FROM semantic_derivation_outbox "
                        "WHERE aggregate_id=?",
                        (f"large-{index}",),
                    ).fetchone()[0]
                )
            )
    return tuple(event_ids)


def test_v2_page_budget_charges_hydrated_v1_receipt_and_defers_corrupt_row_beyond_cutpoint(
    tmp_path: Path,
) -> None:
    fixture = _v9_fixture(tmp_path, item_id="budget-v9")
    database = fixture["database"]
    assert isinstance(database, Path)
    with semantic_database(database, readonly=True) as connection:
        cursor = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_id),0) FROM semantic_derivation_outbox"
            ).fetchone()[0]
        )
    event_ids = _create_large_v2_events(database)
    with semantic_database(database, readonly=True) as connection:
        rows = tuple(
            connection.execute(
                """SELECT event.event_id,event.receipt_id,event.event_kind,
                    event.aggregate_kind,event.aggregate_id,event.committed_ns,
                    event.payload_json,receipt.receipt_json,receipt.receipt_key
                FROM semantic_derivation_outbox event
                JOIN semantic_work_receipts receipt ON receipt.receipt_id=event.receipt_id
                WHERE event.event_id>? ORDER BY event.event_id""",
                (cursor,),
            ).fetchall()
        )
    assert tuple(int(row[0]) for row in rows) == event_ids
    row_costs = tuple(
        len(
            canonical_json(
                {
                    "schema": _EVENT_V1,
                    "receipt_id": int(row[1]),
                    "receipt_key": str(row[8]),
                    "event_kind": str(row[2]),
                    "aggregate_kind": str(row[3]),
                    "aggregate_id": str(row[4]),
                    "receipt": json.loads(str(row[7])),
                }
            ).encode("utf-8")
        )
        + len(str(row[7]).encode("utf-8"))
        for row in rows
    )
    compact_wire_bytes = sum(len(str(row[6]).encode("utf-8")) for row in rows)
    assert compact_wire_bytes < semantic_lineage_repository._MAX_OUTBOX_PAGE_BYTES
    assert sum(row_costs) > semantic_lineage_repository._MAX_OUTBOX_PAGE_BYTES
    expected_ids: list[int] = []
    captured = 0
    for row, cost in zip(rows, row_costs, strict=True):
        if expected_ids and captured + cost > semantic_lineage_repository._MAX_OUTBOX_PAGE_BYTES:
            break
        expected_ids.append(int(row[0]))
        captured += cost
    assert 0 < len(expected_ids) < len(rows)

    # Corrupt the first excluded row, not an arbitrary later page: the next
    # cursor must reach exactly this row while the current page must not.
    corrupt_row = rows[len(expected_ids)]
    corrupt_payload = json.loads(str(corrupt_row[6]))
    corrupt_payload["receipt_ref"]["receipt_sha256"] = "sha256:" + "f" * 64
    _rewrite_outbox_payload(database, int(corrupt_row[0]), canonical_json(corrupt_payload))

    page = semantic_lineage_repository.read_semantic_derivation_outbox(
        database,
        after_event_id=cursor,
        limit=100,
    )
    assert tuple(event.event_id for event in page) == tuple(expected_ids)
    assert page[-1].event_id < int(corrupt_row[0])
    with pytest.raises((semantic_schema.SemanticStateError, ValueError)):
        semantic_lineage_repository.read_semantic_derivation_outbox(
            database,
            after_event_id=page[-1].event_id,
            limit=100,
        )


@pytest.mark.parametrize("corruption", ("future_wire", "malformed_wire"))
def test_unknown_compact_wire_does_not_shrink_the_logical_page_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    fixture = _v9_fixture(tmp_path)
    database = fixture["database"]
    assert isinstance(database, Path)
    rows = _event_rows(database)
    first, excluded = rows[:2]

    def logical_row_cost(row: sqlite3.Row) -> int:
        payload = {
            "schema": _EVENT_V1,
            "receipt_id": int(row["receipt_id"]),
            "receipt_key": str(row["receipt_key"]),
            "event_kind": str(row["event_kind"]),
            "aggregate_kind": str(row["aggregate_kind"]),
            "aggregate_id": str(row["aggregate_id"]),
            "receipt": json.loads(str(row["receipt_json"])),
        }
        return len(canonical_json(payload).encode("utf-8")) + len(
            str(row["receipt_json"]).encode("utf-8")
        )

    # One byte below the second complete logical row.  Invalid tiny wire must
    # not make that row enter a page that could not admit the valid envelope.
    budget = logical_row_cost(first) + logical_row_cost(excluded) - 1
    monkeypatch.setattr(semantic_lineage_repository, "_MAX_OUTBOX_PAGE_BYTES", budget)
    payload = json.loads(str(excluded["payload_json"]))
    if corruption == "future_wire":
        payload["schema"] = "neocortex.semantic-derivation-event/v999"
        raw = canonical_json(payload)
    else:
        raw = "{invalid compact envelope"
    _rewrite_outbox_payload(database, int(excluded["event_id"]), raw)

    page = semantic_lineage_repository.read_semantic_derivation_outbox(database)
    assert tuple(event.event_id for event in page) == (int(first["event_id"]),)
    with pytest.raises(semantic_schema.SemanticStateError):
        semantic_lineage_repository.read_semantic_derivation_outbox(
            database, after_event_id=page[-1].event_id,
        )


@pytest.mark.parametrize("version", (7, 8, 9))
def test_receipt_replay_preserves_legacy_outbox_allocator_sequence(
    tmp_path: Path, version: int,
) -> None:
    database = (
        _v9_fixture(tmp_path)["database"]
        if version == 9 else _legacy_fixture(tmp_path, version)
    )
    assert isinstance(database, Path)
    receipt_row = _first_receipt_row(database)
    before_rows = _stored_rows(database)

    def sequences() -> dict[str, int]:
        with semantic_database(database, readonly=True) as connection:
            return dict(connection.execute(
                "SELECT name,seq FROM sqlite_sequence "
                "WHERE name IN ('semantic_work_receipts','semantic_derivation_outbox')"
            ))

    before = sequences()
    _record_collision(database, receipt_row)
    assert _stored_rows(database) == before_rows
    assert sequences() == {name: value + 1 for name, value in before.items()}


@pytest.mark.parametrize("version", (7, 8))
def test_writer_rejects_same_owner_input_locator_newer_than_its_receipt(
    tmp_path: Path, version: int,
) -> None:
    database = _legacy_fixture(tmp_path, version)
    row = _first_receipt_row(database)
    receipt = WorkReceipt.from_json(str(row["receipt_json"]))
    _receipt_id, kwargs = _record_collision(database, row)
    future = replace(receipt.outputs[0].materialization, schema_version=version + 1)
    kwargs["receipt_key"] = f"future-input-locator-{version}"
    kwargs["inputs"] = (*kwargs["inputs"], {
        "kind": future.kind,
        "binding_name": "future-semantic-input",
        "materialization_ref": future,
        "fingerprint": {"algorithm": "sha256", "value": "f" * 64},
    })
    before = database.read_bytes()
    with pytest.raises((semantic_schema.SemanticStateError, ValueError)):
        with semantic_database(database) as connection:
            semantic_lineage_repository._record_work_receipt(connection, **kwargs)
    assert database.read_bytes() == before


@pytest.mark.parametrize("version", (7, 8))
@pytest.mark.parametrize("migrate_owner", (False, True))
def test_reader_and_retry_reject_future_semantic_output_even_after_owner_upgrade(
    tmp_path: Path, version: int, migrate_owner: bool,
) -> None:
    database = _legacy_fixture(tmp_path, version)
    row = _first_receipt_row(database)
    body = json.loads(str(row["receipt_json"]))
    materialization = body["outputs"][0]["materialization"]
    assert materialization["owner"] == "semantic"
    assert materialization["owner_schema_version"] == version
    materialization["owner_schema_version"] = version + 1
    event = next(x for x in _event_rows(database) if x["receipt_id"] == row["receipt_id"])
    wire = json.loads(str(event["payload_json"]))
    wire["receipt"] = body
    _rewrite_receipt(database, int(row["receipt_id"]), canonical_json(body))
    _rewrite_outbox_payload(database, int(event["event_id"]), canonical_json(wire))
    if migrate_owner:
        initialize_semantic_state(database)
    before = database.read_bytes()
    with pytest.raises((semantic_schema.SemanticStateError, ValueError)):
        semantic_lineage_repository.read_semantic_derivation_outbox(database)
    with pytest.raises((semantic_schema.SemanticStateError, ValueError)):
        _record_collision(database, _first_receipt_row(database))
    assert database.read_bytes() == before
