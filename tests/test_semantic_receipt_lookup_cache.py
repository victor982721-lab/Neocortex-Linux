"""Connection-local receipt-output lookup cache regressions for Semantic."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic import semantic_lineage_repository as lineage
from neocortex.semantic import semantic_schema
from neocortex.persistence.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from neocortex.semantic.semantic_work_budget import SemanticIndexDeadlineExceeded
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    enqueue_text_chunk_jobs,
    semantic_database,
    start_embedding_generation,
    upsert_semantic_item,
)
from neocortex.semantic.semantic_models import SemanticItem, fingerprint_text


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability(*TEST_CAPABILITIES)


def _v6_fixture_module():
    spec = importlib.util.spec_from_file_location(
        "semantic_generation_publication_v6_fixture",
        Path(__file__).with_name("test_semantic_generation_publication_v6.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("semantic generation fixture is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _target_materialization(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute(
        "SELECT member_id FROM embedding_generation_members ORDER BY member_id LIMIT 1"
    ).fetchone()
    assert row is not None
    member_id = int(row[0])
    return member_id, f"materialization:semantic:embedding-member:{member_id}"


def _insert_output_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_key: str,
    materialization_id: str,
    status: str = "failed",
    fingerprint: str = "fixture",
    fingerprint_algorithm: str = "fixture-v1",
) -> None:
    connection.execute(
        """INSERT INTO semantic_work_receipts(
            receipt_key,contract_version,stage_id,stage_version,
            processing_signature,status,execution_mode,
            reproducibility_class,entity_kind,entity_id,attempt,
            started_ns,finished_ns,duration_ns,receipt_json,committed_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            receipt_key,
            "neocortex.work-receipt/v1",
            "fixture.lookup",
            "fixture-v1",
            "fixture-lookup-v1",
            status,
            "unknown",
            "non_replayable",
            "fixture",
            receipt_key,
            1,
            10,
            10,
            0,
            json.dumps(
                {
                    "outputs": [
                        {
                            "materialization": {"materialization_id": materialization_id},
                            "fingerprint": fingerprint,
                            "fingerprint_algorithm": fingerprint_algorithm,
                        }
                    ]
                },
                separators=(",", ":"),
            ),
            10,
        ),
    )


def test_lookup_refreshes_incrementally_rolls_back_and_reopens(tmp_path: Path) -> None:
    v6 = _v6_fixture_module()
    database = tmp_path / "semantic.sqlite3"
    v6._completed_generation(
        database,
        member_count=1,
        processing_signature="lookup-watermark-v1",
    )
    with semantic_database(database) as connection:
        _member_id, materialization_id = _target_materialization(connection)
        trace: list[str] = []
        connection.set_trace_callback(trace.append)
        first = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        second = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        assert len(first) == len(second) == 1
        assert sum(
            "INSERT INTO _semantic_receipt_output_lookup(" in statement
            for statement in trace
        ) == 1
        watermark = int(
            connection.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()[0]
        )
        _insert_output_receipt(
            connection,
            receipt_key="lookup-committed",
            materialization_id=materialization_id,
        )
        connection.commit()
        committed = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        committed_watermark = int(
            connection.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()[0]
        )
        assert [int(row["receipt_id"]) for row in committed] == [
            int(first[0]["receipt_id"]),
            committed_watermark,
        ]
        assert committed_watermark > watermark

        _insert_output_receipt(
            connection,
            receipt_key="lookup-rolled-back",
            materialization_id=materialization_id,
        )
        uncommitted = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        assert len(uncommitted) == 3
        connection.rollback()
        after_rollback = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        assert len(after_rollback) == 2
        assert int(
            connection.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()[0]
        ) == committed_watermark
        connection.commit()
        with semantic_database(database) as another_writer:
            _insert_output_receipt(
                another_writer,
                receipt_key="lookup-external-commit",
                materialization_id=materialization_id,
            )
        externally_committed = tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, (materialization_id,)
            )
        )
        assert len(externally_committed) == 3

    # TEMP state is connection-local: a reopened writer rebuilds from the
    # committed receipts and cannot inherit the rolled-back row.
    with semantic_database(database) as reopened:
        assert reopened.execute(
            "SELECT 1 FROM sqlite_temp_master "
            "WHERE name='_semantic_receipt_output_lookup'"
        ).fetchone() is None
        rows = tuple(
            lineage._receipt_output_candidates_for_materializations(
                reopened, (materialization_id,)
            )
        )
        assert len(rows) == 3
        assert int(
            reopened.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()[0]
        ) == int(rows[-1]["receipt_id"])
    with semantic_database(database, readonly=True) as query_only:
        assert query_only.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE '_semantic_receipt_output_lookup%'"
        ).fetchone() is None
        assert len(tuple(lineage._receipt_output_candidates_for_materializations(
            query_only, (materialization_id,)
        ))) == 3
        assert query_only.execute("SELECT 1 FROM sqlite_temp_master").fetchone() is None


def test_writer_rebind_reuses_identical_payload_without_new_embedding_job(
    tmp_path: Path,
) -> None:
    v6 = _v6_fixture_module()
    database = tmp_path / "semantic.sqlite3"
    model = v6._initialize(database)
    text = "published transformer record"
    chunk = v6._stage(database, "lookup-rebind", text, 1)
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="lookup-rebind-v1",
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=101) == 1
    v6._complete_jobs(database, generation_id, now_ns=102)
    v6.finalize_embedding_generation(database, generation_id, completed_ns=110)
    with semantic_database(database, readonly=True) as connection:
        payload_count = int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0])
        producer_key = str(connection.execute(
            "SELECT receipt_key FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding'",
            (generation_id,),
        ).fetchone()[0])

    upsert_semantic_item(
        database,
        SemanticItem(
            "lookup-rebind",
            "pdf",
            "identity:lookup-rebind",
            "fixture-v1",
            fingerprint_text(text),
            path="C:/fixtures/moved/lookup-rebind.pdf",
            provenance={"revision": 1},
        ),
        refresh_token="lookup-rebind-moved",
        updated_ns=120,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="lookup-rebind-successor",
        materialize_base=False,
        started_ns=121,
    )
    assert enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=122) == 0
    with semantic_database(database, readonly=True) as connection:
        embedding_receipts = connection.execute(
            """SELECT execution_mode,receipt_json FROM semantic_work_receipts
            WHERE generation_id IN (?,?) AND stage_id='semantic.embedding'
            ORDER BY receipt_id""",
            (generation_id, successor),
        ).fetchall()
        jobs = connection.execute(
            "SELECT status FROM embedding_jobs WHERE generation_id IN (?,?)",
            (generation_id, successor),
        ).fetchall()
        payload_ids = connection.execute(
            "SELECT payload_id FROM embedding_generation_members "
            "WHERE generation_id IN (?,?) ORDER BY generation_id",
            (generation_id, successor),
        ).fetchall()
        assert int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]) == payload_count
    assert [str(row[0]) for row in embedding_receipts] == ["executed", "replay"]
    assert json.loads(str(embedding_receipts[1]["receipt_json"]))["causation_id"] == producer_key
    assert len(payload_ids) == 2 and payload_ids[0][0] == payload_ids[1][0]
    assert [str(row[0]) for row in jobs] == ["done"]


def test_foreign_normalized_duplicate_remains_ambiguous(tmp_path: Path) -> None:
    """Keep output-level ambiguity even when normalized facts are foreign."""

    v6 = _v6_fixture_module()
    database = tmp_path / "foreign.sqlite3"
    _model, generation = v6._completed_generation(
        database, member_count=1, processing_signature="foreign-duplicate"
    )
    with semantic_database(database) as connection:
        member_id, _materialization = _target_materialization(connection)
        producer = connection.execute(
            "SELECT * FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding'",
            (generation,),
        ).fetchone()
        assert producer is not None
        values = {name: producer[name] for name in producer.keys() if name != "receipt_id"}
        values["receipt_key"] = f"{producer['receipt_key']}:foreign-fixture"
        values["entity_kind"] = "foreign-fixture-kind"
        columns = tuple(values)
        connection.execute(
            f"INSERT INTO semantic_work_receipts({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(values[name] for name in columns),
        )
        with pytest.raises(SemanticStateError, match="multiple exact producer receipts"):
            lineage._producer_receipts_for_embedding_members(connection, (member_id,))


def test_selected_exact_output_requires_a_canonical_receipt(tmp_path: Path) -> None:
    v6 = _v6_fixture_module()
    database = tmp_path / "selected-corrupt.sqlite3"
    v6._create_receiptless_current_base(database)
    with semantic_database(database) as connection:
        member_id, materialization_id = _target_materialization(connection)
        binding = lineage._embedding_member_binding_by_id(connection, member_id)
        fingerprint, algorithm = lineage._binding_fingerprint(binding)
        _insert_output_receipt(
            connection,
            receipt_key="selected-corrupt",
            materialization_id=materialization_id,
            status="succeeded",
            fingerprint=fingerprint,
            fingerprint_algorithm=algorithm,
        )
        with pytest.raises(ValueError, match="WorkReceipt"):
            lineage._producer_receipts_for_embedding_members(connection, (member_id,))


def test_warm_lookup_avoids_repeated_history_scan(tmp_path: Path) -> None:
    database = tmp_path / "query-work.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE semantic_work_receipts("
            "receipt_id INTEGER PRIMARY KEY, status TEXT, receipt_json TEXT)"
        )
        output = json.dumps({"outputs": [{
            "materialization": {"materialization_id": "foreign:history"},
            "fingerprint": "fixture",
            "fingerprint_algorithm": "fixture-v1",
        }]})
        connection.executemany(
            "INSERT INTO semantic_work_receipts VALUES(?,'failed',?)",
            ((index, output) for index in range(1, 20_001)),
        )
        connection.commit()
        targets = ("materialization:semantic:embedding-member:1",)
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        connection.set_progress_handler(progress, 100)
        assert tuple(lineage._receipt_output_candidates_for_materializations(
            connection, targets
        )) == ()
        cold_steps = steps
        steps = 0
        for _ in range(10):
            assert tuple(lineage._receipt_output_candidates_for_materializations(
                connection, targets
            )) == ()
        warm_steps = steps
        steps = 0
        for _ in range(10):
            assert tuple(lineage._receipt_output_candidates_uncached(connection, targets)) == ()
        uncached_steps = steps
        connection.set_progress_handler(None, 0)
        assert cold_steps > 0 and uncached_steps > 0
        assert warm_steps * 20 < uncached_steps


@pytest.mark.parametrize("status", ("failed", "cancelled", "abandoned"))
def test_non_success_legacy_output_prevents_receiptless_attestation(
    tmp_path: Path, status: str
) -> None:
    v6 = _v6_fixture_module()
    database = tmp_path / "semantic.sqlite3"
    model, chunk = v6._create_populated_v5(database)
    v6.initialize_semantic_state(database)
    with semantic_database(database) as connection:
        member = connection.execute(
            "SELECT member_id,generation_id FROM embedding_generation_members"
        ).fetchone()
        assert member is not None
        materialization_id = (
            "materialization:semantic:embedding-member:"
            f"{int(member['member_id'])}"
        )
        _insert_output_receipt(
            connection,
            receipt_key="failed-legacy-output",
            materialization_id=materialization_id,
            status=status,
        )
        connection.execute(
            "UPDATE semantic_items SET source_revision_json=?,updated_ns=? WHERE item_id=?",
            ('{"last_seen_run_id":61}', 11, chunk.item_id),
        )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="failed-legacy-successor",
        materialize_base=False,
        started_ns=20,
    )
    with pytest.raises(
        SemanticStateError,
        match="semantic source embedding member has no exact producer receipt",
    ):
        enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=21)
    with semantic_database(database, readonly=True) as connection:
        assert int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_work_receipts
                WHERE stage_id='semantic.vector_payload.legacy_attest'"""
            ).fetchone()[0]
        ) == 0


def test_progress_interrupt_rolls_back_lookup_rows_and_watermark(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    semantic_schema.initialize_semantic_state(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        output = json.dumps(
            {
                "outputs": [
                    {
                        "materialization": {"materialization_id": "foreign:history"},
                        "fingerprint": "foreign",
                        "fingerprint_algorithm": "foreign-v1",
                    }
                ]
            },
            separators=(",", ":"),
        )
        connection.executemany(
            """INSERT INTO semantic_work_receipts(
                receipt_key,contract_version,stage_id,stage_version,
                processing_signature,status,execution_mode,
                reproducibility_class,entity_kind,entity_id,receipt_json,
                committed_ns)
            VALUES(?,'neocortex.work-receipt/v1','fixture.history','v1',
                'fixture-history','failed','unknown','non_replayable',
                'foreign',?,?,1)""",
            ((f"history-{index}", f"foreign-{index}", output) for index in range(20_000)),
        )
        connection.commit()
        callbacks = 0
        insert_started = False
        deadline_error = SemanticIndexDeadlineExceeded("fixture deadline during receipt scan")

        def observe(statement: str) -> None:
            nonlocal insert_started
            if "INSERT INTO _semantic_receipt_output_lookup(" in statement:
                insert_started = True

        def checkpoint() -> None:
            nonlocal callbacks
            if insert_started:
                callbacks += 1
                if callbacks > 10:
                    raise deadline_error

        connection.set_trace_callback(observe)
        bridge = SQLiteCancellationBridge(checkpoint)
        _insert_output_receipt(
            connection,
            receipt_key="owner-rollback",
            materialization_id="foreign:uncommitted",
        )
        with pytest.raises(SemanticIndexDeadlineExceeded) as captured:
            with sqlite_cancellation_scope(connection, bridge):
                with connection:
                    tuple(
                        lineage._receipt_output_candidates_for_materializations(
                            connection, ("materialization:semantic:embedding-member:1",)
                        )
                    )
        connection.set_trace_callback(None)
        assert captured.value is deadline_error and bridge.captured_exception is deadline_error
        assert callbacks > 10 and insert_started
        assert not connection.in_transaction
        assert int(connection.execute("SELECT COUNT(*) FROM semantic_work_receipts").fetchone()[0]) == 20_000
        # The refresh savepoint may roll back the TEMP schema creation itself;
        # either way, no cache row or watermark may survive the interruption.
        state_exists = connection.execute(
            """SELECT 1 FROM sqlite_temp_master
            WHERE type='table' AND name='_semantic_receipt_output_lookup_state'"""
        ).fetchone()
        lookup_exists = connection.execute(
            """SELECT 1 FROM sqlite_temp_master
            WHERE type='table' AND name='_semantic_receipt_output_lookup'"""
        ).fetchone()
        if state_exists is not None or lookup_exists is not None:
            assert state_exists is not None and lookup_exists is not None
            state_row = connection.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()
            assert state_row is None or int(state_row[0]) == 0
            assert int(
                connection.execute("SELECT COUNT(*) FROM _semantic_receipt_output_lookup").fetchone()[0]
            ) == 0
        assert tuple(
            lineage._receipt_output_candidates_for_materializations(
                connection, ("materialization:semantic:embedding-member:1",)
            )
        ) == ()
        assert int(
            connection.execute(
                "SELECT watermark_receipt_id FROM _semantic_receipt_output_lookup_state"
            ).fetchone()[0]
        ) == 20_000
