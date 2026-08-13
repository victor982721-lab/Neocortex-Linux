# region [00] Contexto del módulo
# Módulo: tests/test_semantic_text_staging_session.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import itertools
import json
import multiprocessing
import os
import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import semantic_generation_repository, semantic_text_index
from _04_Nucleo_Operativo.semantic_chunking import (
    TextChunkingConfig,
    iter_text_chunks,
)
from _04_Nucleo_Operativo.semantic_generation_worker import batches, run_generation
from _04_Nucleo_Operativo.semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    SemanticItem,
    TextChunk,
    TextSection,
    fingerprint_text,
)
from _04_Nucleo_Operativo.semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
    TextSourceRecord,
    iter_text_sections_with_metadata,
)
from _04_Nucleo_Operativo.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_semantic_item_refresh,
    finalize_text_chunk_refresh,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from _04_Nucleo_Operativo.semantic_work_budget import SemanticWorkBudget
from _04_Nucleo_Operativo.semantic_work_budget import SemanticIndexDeadlineExceeded
# endregion [01]

# region [02] Implementación


CHUNKING = TextChunkingConfig(
    max_chars=128,
    max_terms=32,
    overlap_chars=0,
    overlap_terms=0,
    min_natural_break_chars=32,
)


class _FatalStage(BaseException):
    pass


class _FixtureBackend:
    def __init__(self, model: EmbeddingModelSpec) -> None:
        self._model = model

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return 32

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        return tuple(
            BackendEmbedding(
                request.request_id,
                (1.0, 0.0, 0.0, 0.0),
                {"fixture": "staging-resume"},
            )
            for request in requests
        )


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "staging-model-v1",
        "staging-space-v1",
        EmbeddingModality.TEXT,
        "fixture/staging",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _generation(database: Path, processing_signature: str = "staging-run") -> int:
    model = _model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    return start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
    )


def _records(
    count: int,
    *,
    sections_per_item: int = 1,
) -> tuple[TextSourceRecord, ...]:
    records: list[TextSourceRecord] = []
    for item_index in range(count):
        identity = f"fixture-{item_index:05d}"
        item = SemanticItem(
            item_id=f"item:pdf:{identity}",
            source_kind="pdf",
            source_identity=identity,
            identity_version="fixture-source-v1",
            fingerprint=fingerprint_text(f"source:{identity}"),
            path=f"C:/fixtures/{identity}.pdf",
            provenance={"fixture": True},
        )
        for section_index in range(sections_per_item):
            text = (
                f"Transformador {item_index} sección {section_index}; "
                "mantenimiento preventivo y pruebas del interruptor."
            )
            records.append(
                TextSourceRecord(
                    item,
                    TextSection(
                        "pdf_page",
                        str(section_index + 1),
                        text,
                        {"page": section_index + 1},
                    ),
                )
            )
    return tuple(records)


def _iterator(
    records: Sequence[TextSourceRecord],
):
    def selected(_state: Path, _source_kind: str) -> Iterator[TextSourceRecord]:
        return iter(records)

    return selected


def _stage(
    database: Path,
    generation_id: int,
    records: Sequence[TextSourceRecord],
    *,
    refresh_token: str = "refresh-1",
    cancellation_check=None,
) -> tuple[int, int, int]:
    result = semantic_text_index._stage_source(
        database,
        database.parent,
        "pdf",
        generation_id=generation_id,
        refresh_token=refresh_token,
        chunking=CHUNKING,
        source_record_iterator=_iterator(records),
        cancellation_check=cancellation_check,
    )
    return result[:3]


def _legacy_stage(
    database: Path,
    generation_id: int,
    records: Sequence[TextSourceRecord],
    *,
    refresh_token: str,
) -> tuple[int, int, int]:
    source_items = chunks_staged = queued = 0
    groups = semantic_text_index.grouped_text_records(
        database.parent,
        "pdf",
        source_record_iterator=_iterator(records),
    )
    for item_id, grouped in groups:
        iterator = iter(grouped)
        first = next(iterator)
        upsert_semantic_item(database, first.item, refresh_token=refresh_token)
        source_items += 1
        sections = itertools.chain(
            (first.section,),
            (record.section for record in iterator),
        )
        chunks = iter_text_chunks(
            item_id,
            iter_text_sections_with_metadata(first.item, sections),
            CHUNKING,
        )
        for batch in batches(
            chunks,
            semantic_text_index.STAGING_BATCH_SIZE,
        ):
            chunks_staged += stage_text_chunks(
                database,
                batch,
                refresh_token=refresh_token,
                batch_size=semantic_text_index.STAGING_BATCH_SIZE,
            )
            queued += enqueue_text_chunk_jobs(
                database,
                generation_id,
                (chunk.chunk_id for chunk in batch),
                batch_size=semantic_text_index.STAGING_BATCH_SIZE,
            )
        finalize_text_chunk_refresh(
            database,
            item_id=item_id,
            chunking_signature=CHUNKING.signature,
            refresh_token=refresh_token,
        )
    finalize_semantic_item_refresh(
        database,
        source_kind="pdf",
        refresh_token=refresh_token,
    )
    return source_items, chunks_staged, queued


def _logical_projection(database: Path) -> tuple[tuple[tuple[object, ...], ...], ...]:
    queries = (
        """SELECT item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json,refresh_token,active
        FROM semantic_items ORDER BY item_id""",
        """SELECT chunk_id,item_id,ordinal,section_kind,section_id,start_char,
            end_char,text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,
            refresh_token,active
        FROM text_chunks ORDER BY chunk_id""",
        """SELECT generation_id,model_signature,role,entity_kind,entity_id,
            item_id,content_xxh3_128,content_bytes,content_xxh3_64_guard,status,
            attempts,max_attempts,lease_owner,lease_until_ns,error_type,error_message
        FROM embedding_jobs ORDER BY entity_kind,entity_id""",
    )
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            tuple(tuple(row) for row in connection.execute(query).fetchall()) for query in queries
        )


def _counts(database: Path) -> tuple[int, int, int]:
    with semantic_database(database, readonly=True) as connection:
        return (
            int(connection.execute("SELECT COUNT(*) FROM semantic_items").fetchone()[0]),
            int(connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]),
            int(connection.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0]),
        )


def _assert_building_unpublished(database: Path, generation_id: int) -> None:
    with semantic_database(database, readonly=True) as connection:
        status = connection.execute(
            "SELECT status FROM embedding_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
        heads = connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
    assert status == "building"
    assert heads == 0


def _stage_then_exit_process(database_text: str, generation_id: int, exit_on: int) -> None:
    """Child-only deterministic crash point after a previously committed prefix."""

    database = Path(database_text)
    original_finalize = semantic_text_index._finalize_text_chunk_refresh
    finalized = 0

    def exit_after_committed_prefix(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        chunking_signature: str,
        refresh_token: str,
        updated_ns: int,
    ) -> int:
        nonlocal finalized
        finalized += 1
        if finalized == exit_on:
            os._exit(77)
        return original_finalize(
            connection,
            item_id=item_id,
            chunking_signature=chunking_signature,
            refresh_token=refresh_token,
            updated_ns=updated_ns,
        )

    semantic_text_index._finalize_text_chunk_refresh = exit_after_committed_prefix
    _stage(database, generation_id, _records(130))
    os._exit(78)


def test_persistent_staging_is_logically_equivalent_to_legacy_flow(
    tmp_path: Path,
) -> None:
    records = _records(7, sections_per_item=3)
    legacy = tmp_path / "legacy.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    legacy_generation = _generation(legacy, "legacy")
    candidate_generation = _generation(candidate, "candidate")

    expected = _legacy_stage(
        legacy,
        legacy_generation,
        records,
        refresh_token="equivalent-refresh",
    )
    actual = _stage(
        candidate,
        candidate_generation,
        records,
        refresh_token="equivalent-refresh",
    )

    assert actual == expected
    assert _logical_projection(candidate) == _logical_projection(legacy)


def test_title_chunk_is_appended_versioned_and_path_rename_preserves_body(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    record = _records(1)[0]
    expected_body = next(
        iter_text_chunks(
            record.item.item_id,
            (record.section,),
            CHUNKING,
        )
    )

    assert _stage(database, generation_id, (record,)) == (1, 2, 2)
    with semantic_database(database, readonly=True) as connection:
        first_rows = connection.execute(
            """SELECT chunk_id,ordinal,section_kind,section_id,text_zlib,
                provenance_json FROM text_chunks WHERE active=1
                ORDER BY ordinal"""
        ).fetchall()

    assert len(first_rows) == 2
    assert str(first_rows[0][0]) == expected_body.chunk_id
    assert tuple(str(row[2]) for row in first_rows) == (
        "pdf_page",
        SEMANTIC_TITLE_SECTION_KIND,
    )
    assert int(first_rows[1][1]) == 1
    assert str(first_rows[1][3]) == SEMANTIC_TITLE_POLICY
    assert zlib.decompress(bytes(first_rows[1][4])).decode("utf-8") == ("fixture-00000")
    assert json.loads(str(first_rows[1][5]))["advisory_only"] is True

    renamed_item = replace(
        record.item,
        path="C:/other-parent/renamed-transformer.pdf",
    )
    renamed_record = TextSourceRecord(renamed_item, record.section)
    assert _stage(
        database,
        generation_id,
        (renamed_record,),
        refresh_token="refresh-rename",
    ) == (1, 2, 2)
    with semantic_database(database, readonly=True) as connection:
        active_rows = connection.execute(
            """SELECT chunk_id,section_kind,text_zlib FROM text_chunks
                WHERE active=1 ORDER BY ordinal"""
        ).fetchall()
        inactive_titles = int(
            connection.execute(
                """SELECT COUNT(*) FROM text_chunks
                    WHERE active=0 AND section_kind=?""",
                (SEMANTIC_TITLE_SECTION_KIND,),
            ).fetchone()[0]
        )

    assert str(active_rows[0][0]) == expected_body.chunk_id
    assert zlib.decompress(bytes(active_rows[1][2])).decode("utf-8") == ("renamed-transformer")
    assert inactive_titles == 1


def test_staging_reuses_one_connection_and_commits_bounded_slices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(257)
    real_database = semantic_text_index.semantic_database
    observed = {"connections": 0, "commits": 0}

    class ObservedConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def commit(self) -> None:
            observed["commits"] += 1
            self._connection.commit()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    @contextmanager
    def observed_database(path: Path, *, readonly: bool = False):
        observed["connections"] += 1
        with real_database(path, readonly=readonly) as connection:
            yield ObservedConnection(connection)

    monkeypatch.setattr(semantic_text_index, "semantic_database", observed_database)

    assert _stage(database, generation_id, records) == (257, 514, 514)
    assert observed == {"connections": 1, "commits": 5}
    assert _counts(database) == (257, 514, 514)


def test_restage_preserves_completed_job_and_does_not_duplicate_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(1)
    assert _stage(database, generation_id, records) == (1, 2, 2)
    leases = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="fixture-worker",
    )
    for lease in leases:
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="fixture-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            provenance={"fixture": True},
        )

    assert _stage(database, generation_id, records) == (1, 2, 0)
    with semantic_database(database, readonly=True) as connection:
        statuses = tuple(
            row[0]
            for row in connection.execute("SELECT status FROM embedding_jobs ORDER BY job_id")
        )
    assert _counts(database) == (1, 2, 2)
    assert statuses == ("done", "done")


@pytest.mark.parametrize("error_type", [RuntimeError, _FatalStage])
def test_fault_rolls_back_current_slice_and_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)

    def fail_enqueue(*_args: object, **_kwargs: object) -> int:
        raise error_type("injected staging fault")

    monkeypatch.setattr(
        semantic_text_index,
        "_enqueue_text_chunk_batch_bounded",
        fail_enqueue,
    )
    with pytest.raises(error_type, match="injected staging fault"):
        _stage(database, generation_id, _records(1))

    assert _counts(database) == (0, 0, 0)
    _assert_building_unpublished(database, generation_id)


def test_internal_queue_phase_failure_rolls_back_current_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)

    def fail_after_job_upsert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected post-upsert queue fault")

    monkeypatch.setattr(
        semantic_generation_repository,
        "_reset_queue_jobs_pending",
        fail_after_job_upsert,
    )
    with pytest.raises(RuntimeError, match="injected post-upsert queue fault"):
        _stage(database, generation_id, _records(1))

    assert _counts(database) == (0, 0, 0)
    _assert_building_unpublished(database, generation_id)


def test_partial_crash_keeps_committed_prefix_resumable_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(130)
    original_finalize = semantic_text_index._finalize_text_chunk_refresh
    finalized = 0

    def fail_after_committed_prefix(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        chunking_signature: str,
        refresh_token: str,
        updated_ns: int,
    ) -> int:
        nonlocal finalized
        finalized += 1
        if finalized == 129:
            raise RuntimeError("injected partial crash")
        return original_finalize(
            connection,
            item_id=item_id,
            chunking_signature=chunking_signature,
            refresh_token=refresh_token,
            updated_ns=updated_ns,
        )

    monkeypatch.setattr(
        semantic_text_index,
        "_finalize_text_chunk_refresh",
        fail_after_committed_prefix,
    )
    with pytest.raises(RuntimeError, match="injected partial crash"):
        _stage(database, generation_id, records)

    assert _counts(database) == (128, 256, 256)
    _assert_building_unpublished(database, generation_id)

    monkeypatch.setattr(
        semantic_text_index,
        "_finalize_text_chunk_refresh",
        original_finalize,
    )
    assert _stage(database, generation_id, records) == (130, 260, 260)
    assert _counts(database) == (130, 260, 260)
    _assert_building_unpublished(database, generation_id)


def test_crash_resume_then_worker_atomically_publishes_complete_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(130)
    original_finalize = semantic_text_index._finalize_text_chunk_refresh
    finalized = 0

    def fail_after_committed_prefix(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        chunking_signature: str,
        refresh_token: str,
        updated_ns: int,
    ) -> int:
        nonlocal finalized
        finalized += 1
        if finalized == 129:
            raise RuntimeError("injected integrated crash")
        return original_finalize(
            connection,
            item_id=item_id,
            chunking_signature=chunking_signature,
            refresh_token=refresh_token,
            updated_ns=updated_ns,
        )

    monkeypatch.setattr(
        semantic_text_index,
        "_finalize_text_chunk_refresh",
        fail_after_committed_prefix,
    )
    with pytest.raises(RuntimeError, match="injected integrated crash"):
        _stage(database, generation_id, records)
    assert _counts(database) == (128, 256, 256)
    _assert_building_unpublished(database, generation_id)

    monkeypatch.setattr(
        semantic_text_index,
        "_finalize_text_chunk_refresh",
        original_finalize,
    )
    assert _stage(database, generation_id, records) == (130, 260, 260)
    work = run_generation(
        database,
        generation_id,
        _FixtureBackend(_model()),
        queued=260,
    )

    assert work.summary.status == "ready"
    assert work.embedded == 260
    assert work.failed == 0
    with semantic_database(database, readonly=True) as connection:
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (_model().model_signature,),
        ).fetchone()
        members = connection.execute(
            "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
    assert head is not None
    assert int(head[0]) == generation_id
    assert int(members) == 260


def test_process_death_preserves_committed_prefix_and_resume_publishes_atomically(
    tmp_path: Path,
) -> None:
    """Distinguish process-death safety from exception rollback behavior."""

    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_stage_then_exit_process,
        args=(str(database), generation_id, 129),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("process-death staging child exceeded its hard timeout")
    assert process.exitcode == 77
    assert _counts(database) == (128, 256, 256)
    _assert_building_unpublished(database, generation_id)

    records = _records(130)
    assert _stage(database, generation_id, records) == (130, 260, 260)
    work = run_generation(
        database,
        generation_id,
        _FixtureBackend(_model()),
        queued=260,
    )

    assert work.summary.status == "ready"
    assert work.embedded == 260
    assert work.failed == 0
    with semantic_database(database, readonly=True) as connection:
        head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (_model().model_signature,),
            ).fetchone()[0]
        )
        members = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
    assert head == generation_id
    assert members == 260


def test_cancellation_rolls_back_current_slice_and_preserves_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    armed = False
    original_enqueue = semantic_text_index._enqueue_text_chunk_batch_bounded

    def enqueue_and_arm(
        connection: sqlite3.Connection,
        generation_id: int,
        chunk_ids: tuple[str, ...],
        *,
        max_new_jobs: int | None,
        max_attempts: int = 3,
        now_ns: int,
    ):
        nonlocal armed
        result = original_enqueue(
            connection,
            generation_id,
            chunk_ids,
            max_new_jobs=max_new_jobs,
            max_attempts=max_attempts,
            now_ns=now_ns,
        )
        armed = True
        return result

    def cancellation_check() -> None:
        if armed:
            raise KeyboardInterrupt("cancel staging")

    monkeypatch.setattr(
        semantic_text_index,
        "_enqueue_text_chunk_batch_bounded",
        enqueue_and_arm,
    )
    with pytest.raises(KeyboardInterrupt, match="cancel staging"):
        _stage(
            database,
            generation_id,
            _records(1),
            cancellation_check=cancellation_check,
        )

    assert _counts(database) == (0, 0, 0)
    _assert_building_unpublished(database, generation_id)


def test_single_large_item_is_sliced_without_unbounded_chunk_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(1, sections_per_item=260)
    original_stage_batch = semantic_text_index._stage_text_chunk_batch
    observed_batch_sizes: list[int] = []

    def observe_batch(
        connection: sqlite3.Connection,
        chunks: Sequence[TextChunk],
        *,
        refresh_token: str,
        updated_ns: int,
    ) -> int:
        observed_batch_sizes.append(len(chunks))
        return original_stage_batch(
            connection,
            chunks,
            refresh_token=refresh_token,
            updated_ns=updated_ns,
        )

    monkeypatch.setattr(
        semantic_text_index,
        "_stage_text_chunk_batch",
        observe_batch,
    )

    assert _stage(database, generation_id, records) == (1, 261, 261)
    assert observed_batch_sizes == [128, 128, 5]
    assert _counts(database) == (1, 261, 261)


def test_write_lock_is_released_after_every_bounded_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database)
    records = _records(1, sections_per_item=260)
    real_database = semantic_text_index.semantic_database
    successful_probe_writers = 0

    class ReleasingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def commit(self) -> None:
            nonlocal successful_probe_writers
            self._connection.commit()
            probe = sqlite3.connect(database, timeout=0.1)
            try:
                probe.execute("PRAGMA busy_timeout=100")
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            finally:
                probe.close()
            successful_probe_writers += 1

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    @contextmanager
    def observed_database(
        path: Path,
        *,
        readonly: bool = False,
    ) -> Iterator[ReleasingConnection]:
        with real_database(path, readonly=readonly) as connection:
            yield ReleasingConnection(connection)

    monkeypatch.setattr(semantic_text_index, "semantic_database", observed_database)

    assert _stage(database, generation_id, records) == (1, 261, 261)
    assert successful_probe_writers == 3
    assert _counts(database) == (1, 261, 261)


def test_new_job_budget_pauses_large_item_and_replay_resumes_without_recharging(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database, "bounded-job-resume")
    records = _records(1, sections_per_item=260)
    first_budget = SemanticWorkBudget(max_items=1, max_new_jobs=129)

    first = semantic_text_index._stage_source(
        database,
        database.parent,
        "pdf",
        generation_id=generation_id,
        refresh_token="generation:bounded-job-resume:source:pdf",
        chunking=CHUNKING,
        source_record_iterator=_iterator(records),
        work_budget=first_budget,
    )

    assert first == (1, 256, 129, False)
    assert first_budget.new_jobs_admitted == 129
    assert first_budget.truncation_reason == "max_new_jobs"
    assert _counts(database) == (1, 256, 129)
    _assert_building_unpublished(database, generation_id)

    second_budget = SemanticWorkBudget(max_items=1, max_new_jobs=132)
    second = semantic_text_index._stage_source(
        database,
        database.parent,
        "pdf",
        generation_id=generation_id,
        refresh_token="generation:bounded-job-resume:source:pdf",
        chunking=CHUNKING,
        source_record_iterator=_iterator(records),
        work_budget=second_budget,
    )

    assert second == (1, 261, 261, True)
    assert second_budget.new_jobs_admitted == 132
    assert second_budget.truncation_reason is None
    assert _counts(database) == (1, 261, 261)


def test_deadline_inside_large_item_keeps_committed_prefix_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    generation_id = _generation(database, "bounded-deadline-resume")
    records = _records(1, sections_per_item=260)
    clock = [0.0]
    budget = SemanticWorkBudget(
        max_items=1,
        max_new_jobs=260,
        deadline=10.0,
        _clock=lambda: clock[0],
    )
    original_enqueue = semantic_text_index._enqueue_text_chunk_batch_bounded
    calls = 0

    def expire_during_second_slice(*args, **kwargs):
        nonlocal calls
        result = original_enqueue(*args, **kwargs)
        calls += 1
        if calls == 2:
            clock[0] = 20.0
        return result

    monkeypatch.setattr(
        semantic_text_index,
        "_enqueue_text_chunk_batch_bounded",
        expire_during_second_slice,
    )

    with pytest.raises(SemanticIndexDeadlineExceeded):
        semantic_text_index._stage_source(
            database,
            database.parent,
            "pdf",
            generation_id=generation_id,
            refresh_token="generation:bounded-deadline-resume:source:pdf",
            chunking=CHUNKING,
            source_record_iterator=_iterator(records),
            work_budget=budget,
        )

    assert budget.truncation_reason == "time_budget"
    assert _counts(database) == (1, 128, 128)
    _assert_building_unpublished(database, generation_id)

    resumed = semantic_text_index._stage_source(
        database,
        database.parent,
        "pdf",
        generation_id=generation_id,
        refresh_token="generation:bounded-deadline-resume:source:pdf",
        chunking=CHUNKING,
        source_record_iterator=_iterator(records),
        work_budget=SemanticWorkBudget(max_items=1, max_new_jobs=133),
    )

    assert resumed == (1, 261, 261, True)
    assert _counts(database) == (1, 261, 261)


# endregion [02]
