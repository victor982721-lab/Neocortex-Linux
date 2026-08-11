"""Read-only Semantic Phase-0 planning over temporary owner fixtures."""

from __future__ import annotations

import json
import inspect
import math
import sqlite3
import time
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from _02_Deduplicacion.hashing import FULL_ALGORITHM
from _02_Deduplicacion.inventory_schema import initialize_inventory_schema
from _04_Nucleo_Operativo.file_identity import encode_file_identity
from _04_Nucleo_Operativo.cli_app import main as cli_main
from _04_Nucleo_Operativo.image_state import image_database, initialize_image_state
from _04_Nucleo_Operativo.office_state import (
    initialize_office_state,
    office_database,
)
from _04_Nucleo_Operativo.pdf_state import (
    SCHEMA_VERSION as PDF_SCHEMA_VERSION,
    initialize_pdf_state,
    pdf_database,
)
from _04_Nucleo_Operativo.semantic_config import (
    clip_image_model,
    multilingual_text_model,
)
from _04_Nucleo_Operativo.semantic_chunking import TextChunkingConfig
from _04_Nucleo_Operativo.semantic_models import EmbeddingRole, fingerprint_text
from _04_Nucleo_Operativo.semantic_planner import (
    SemanticPlanBlocked,
    SemanticScratchLimitExceeded,
    plan_semantic_index,
    semantic_plan_payload,
)
from _04_Nucleo_Operativo.semantic_service_contracts import SemanticCostCalibration
from _04_Nucleo_Operativo.semantic_state import (
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)
from _04_Nucleo_Operativo.sqlite_paths import readonly_sqlite_uri


# region [01] Temporary owner fixtures


def _create_pdf_state(state_directory: Path, texts: tuple[str, ...]) -> Path:
    database = state_directory / "pdf.sqlite3"
    initialize_pdf_state(database)
    with pdf_database(database) as connection:
        for index, text in enumerate(texts, start=1):
            item_fingerprint = fingerprint_text(text)
            connection.execute(
                """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,page_count,completed_pages,native_pages,ocr_pages,
                native_chars,ocr_chars,normalized_text_xxh3_128,
                normalized_text_chars,is_partial,page_errors_count,
                last_seen_run_id,updated_ns)
                VALUES(?,?,?,?,?,'pdf-fixture-v1','done',1,1,1,0,?,0,?,?,0,0,1,1)""",
                (
                    f"pdf-{index}",
                    f"C:/fixture/{index}.pdf",
                    100 + index,
                    10 + index,
                    5 + index,
                    len(text),
                    item_fingerprint.xxh3_128,
                    len(text),
                ),
            )
            connection.execute(
                """INSERT INTO pages(
                file_key,page_number,source,text_zlib,text_chars)
                VALUES(?,1,'native',?,?)""",
                (f"pdf-{index}", zlib.compress(text.encode("utf-8")), len(text)),
            )
    return database


def _create_semantic_payload(state_directory: Path, text: str) -> Path:
    return _create_semantic_payloads(state_directory, (text,))


def _create_semantic_payloads(
    state_directory: Path,
    texts: tuple[str, ...],
) -> Path:
    database = state_directory / "semantic.sqlite3"
    model = multilingual_text_model()
    initialize_semantic_state(database)
    register_embedding_model(database, model)
    with semantic_database(database) as connection:
        connection.executemany(
            """INSERT INTO vector_payloads(
                model_signature,content_xxh3_128,content_bytes,
                content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
                original_norm,provenance_json,created_ns)
                VALUES(?,?,?,?,?,?,?,1.0,'{}',1)""",
            tuple(
                (
                    model.model_signature,
                    fingerprint.xxh3_128,
                    fingerprint.byte_count,
                    fingerprint.xxh3_64_guard,
                    model.dimensions,
                    model.vector_dtype.value,
                    bytes(model.dimensions * 2),
                )
                for fingerprint in map(fingerprint_text, texts)
            ),
        )
    return database


def _create_office_state(state_directory: Path) -> Path:
    database = state_directory / "office.sqlite3"
    initialize_office_state(database)
    with office_database(database) as connection:
        for index, source_kind in enumerate(("xlsx", "pptx", "odt"), start=1):
            text = f"Contenido {source_kind} para subestación"
            fingerprint = fingerprint_text(text)
            connection.execute(
                """INSERT INTO documents(
                file_key,format,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,text_zlib,text_chars,text_xxh3_128,
                part_count,last_seen_run_id,updated_ns)
                VALUES(?,?,?,?,?,?,'office-fixture-v1','complete',?,?,?,1,1,1)""",
                (
                    f"office-{index}",
                    source_kind,
                    f"C:/fixture/document-{index}.{source_kind}",
                    200 + index,
                    20 + index,
                    10 + index,
                    zlib.compress(text.encode("utf-8")),
                    len(text),
                    fingerprint.xxh3_128,
                ),
            )
        connection.commit()
    return database


def _create_image_state(
    state_directory: Path,
    *,
    include_dedup: bool,
    ocr_text: str | None,
) -> tuple[Path, bytes]:
    database = state_directory / "image.sqlite3"
    initialize_image_state(database)
    file_key = encode_file_identity(1, 2)
    path = r"C:\fixture\not-present.jpg"
    size = 100
    mtime_ns = 20
    birthtime_ns = 10
    raw_digest = b"\x11" * 16
    ocr_payload = None if ocr_text is None else zlib.compress(ocr_text.encode())
    ocr_chars = None if ocr_text is None else len(ocr_text)
    ocr_digest = None if ocr_text is None else fingerprint_text(ocr_text).xxh3_128
    with image_database(database) as connection:
        connection.execute(
            """INSERT INTO images(
            file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,
            processing_signature,status,category,document_candidate,
            ocr_text_zlib,ocr_text_chars,ocr_text_xxh3_128,
            ocr_text_truncated,updated_ns)
            VALUES(?,?,'image/jpeg',?,?,?,?,?,'done','industrial',0,?,?,?,0,1)""",
            (
                file_key,
                path,
                size,
                mtime_ns,
                birthtime_ns,
                1,
                "image-fixture-v1",
                ocr_payload,
                ocr_chars,
                ocr_digest,
            ),
        )
    if include_dedup:
        dedup = state_directory / "dedup.sqlite3"
        initialize_inventory_schema(dedup)
        volume = (1).to_bytes(16, "little")
        file_id = (2).to_bytes(16, "little")
        with sqlite3.connect(dedup) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO scans(
                scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
                bytes_seen,skipped_links,excluded_directories,errors,status,
                inventory_policy_signature)
                VALUES(1,'C:/fixture',1,2,1,0,100,0,0,0,'complete','fixture')"""
            )
            connection.execute(
                """INSERT INTO inventory_checkpoints(
                root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
                VALUES('C:/fixture',1,'fixture-volume','fixture-journal',1,1,2)"""
            )
            connection.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                (1, path, volume, file_id, size, mtime_ns, birthtime_ns),
            )
            connection.execute(
                "INSERT INTO fingerprints VALUES(?,?,?,?,?,?,?)",
                (
                    volume,
                    file_id,
                    size,
                    mtime_ns,
                    birthtime_ns,
                    FULL_ALGORITHM,
                    raw_digest,
                ),
            )
    return database, raw_digest


# endregion [01]


# region [02] Exact text counts, reuse and cost


def test_text_plan_is_deterministic_bounded_and_creates_no_state(
    tmp_path: Path,
) -> None:
    text = "Protección diferencial del transformador"
    source = _create_pdf_state(tmp_path, (text, text))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    first = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        scratch_directory=scratch,
    )
    second = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        scratch_directory=scratch,
    )

    assert first.plan_signature == second.plan_signature
    assert first.content_set_xxh3_128 == second.content_set_xxh3_128
    assert first.resources == 2
    assert first.sections == 4
    assert first.chunks == 4
    assert first.embedding_entities == 4
    assert first.unique_contents == 3
    assert first.new_unique_contents == 3
    assert first.reusable_unique_contents == 0
    assert "title-policy=semantic-content-aware-title-v3" in (
        first.workloads[0].processing_signature
    )
    assert "quality-policy=semantic-text-quality-v1" in (first.workloads[0].processing_signature)
    assert first.input_bytes == (2 * len(text.encode())) + 2
    assert first.unique_input_bytes == len(text.encode()) + 2
    assert first.new_vector_blob_bytes_lower_bound == 3 * 768 * 2
    assert first.model_request_contents_lower_bound == 3
    assert first.model_request_contents_upper_bound == 4
    assert first.estimated_model_seconds_lower_bound is None
    assert first.estimated_model_seconds_upper_bound is None
    assert first.estimated_model_seconds is None
    assert first.cost_complete is False
    assert first.jobs_created == 0
    assert first.state_mutated is False
    assert first.scratch_storage_bytes > 0
    assert first.scratch_storage_bytes <= first.max_scratch_bytes
    assert len(first.source_plans[0].snapshot_xxh3_128) == 32
    assert len(first.semantic_snapshot_xxh3_128) == 32
    assert list(scratch.iterdir()) == []
    assert source.read_bytes() == source_before
    assert not (tmp_path / "semantic.sqlite3").exists()
    assert not (tmp_path / "framework.lock").exists()


def test_text_plan_reuses_existing_payload_without_jobs(tmp_path: Path) -> None:
    text = "Interruptor de potencia"
    _create_pdf_state(tmp_path, (text, text))
    semantic = _create_semantic_payload(tmp_path, text)
    with semantic_database(semantic, readonly=True) as connection:
        before = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("embedding_generations", "embedding_jobs", "vector_payloads")
        )

    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )

    assert plan.unique_contents == 3
    assert plan.reusable_unique_contents == 1
    assert plan.new_unique_contents == 2
    assert plan.new_vector_blob_bytes_lower_bound == 2 * 768 * 2
    assert plan.model_request_contents_lower_bound == 2
    assert plan.model_request_contents_upper_bound == 2
    assert plan.estimated_model_seconds_lower_bound is None
    assert plan.estimated_model_seconds_upper_bound is None
    assert plan.estimated_model_seconds is None
    assert plan.cost_complete is False
    assert plan.cost_calibrated is False
    with semantic_database(semantic, readonly=True) as connection:
        after = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("embedding_generations", "embedding_jobs", "vector_payloads")
        )
    assert after == before


def test_malformed_cached_vector_is_not_counted_as_reusable(tmp_path: Path) -> None:
    text = "Vector truncado"
    _create_pdf_state(tmp_path, (text,))
    semantic = _create_semantic_payload(tmp_path, text)
    with sqlite3.connect(semantic) as connection:
        connection.execute("DROP TRIGGER vector_payloads_no_update")
        connection.execute("UPDATE vector_payloads SET vector_blob=X'00'")
        connection.execute(
            """CREATE TRIGGER vector_payloads_no_update
            BEFORE UPDATE ON vector_payloads BEGIN
                SELECT RAISE(ABORT,'semantic vector payloads are append-only');
            END"""
        )

    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )

    assert plan.reusable_unique_contents == 0
    assert plan.new_unique_contents == 2
    assert plan.model_request_contents_lower_bound == 2


def test_cost_requires_one_exact_execution_calibration(tmp_path: Path) -> None:
    text = "Seccionador de barra"
    _create_pdf_state(tmp_path, (text,))
    uncalibrated = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    workload = uncalibrated.workloads[0]
    calibration = SemanticCostCalibration(
        calibration_signature="fixture-rate-v1",
        execution_signature="cpu-fixture-threads-4-v1",
        processing_signature=workload.processing_signature,
        workload=workload.name,
        model_signature=workload.model_signature,
        role=workload.role,
        contents_per_second=2.0,
        sample_contents=64,
        sample_input_bytes=64 * len(text.encode()),
    )

    mismatch = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(calibration,),
        execution_signature="different-host-v1",
    )
    calibrated = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(calibration,),
        execution_signature=calibration.execution_signature,
    )

    assert mismatch.estimated_model_seconds is None
    assert mismatch.workloads[0].cost_unavailable_reason == ("no_exact_cost_calibration")
    assert calibrated.estimated_model_seconds == pytest.approx(1.0)
    assert calibrated.estimated_model_seconds_lower_bound == pytest.approx(1.0)
    assert calibrated.estimated_model_seconds_upper_bound == pytest.approx(1.0)
    assert calibrated.cost_complete is True
    assert calibrated.workloads[0].cost_calibration_signature == "fixture-rate-v1"


def test_canonical_cli_runs_real_plan_without_lock_or_semantic_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _create_pdf_state(tmp_path, ("Protección de barras",))

    assert (
        cli_main(
            [
                "--state-directory",
                str(tmp_path),
                "--semantic-plan",
                "text",
                "--semantic-source",
                "pdf",
                "--semantic-plan-json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["complete"] is False
    assert payload["resources"] == 1
    assert payload["jobs_created"] == 0
    assert not (tmp_path / "semantic.sqlite3").exists()
    assert not (tmp_path / "framework.lock").exists()


# endregion [02]


# region [03] Fail-closed schemas, cancellation and cache-only images


def test_plan_cancellation_cleans_scratch_and_preserves_owner(tmp_path: Path) -> None:
    source = _create_pdf_state(tmp_path, ("uno", "dos", "tres"))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    checkpoints = 0

    class PlannedCancellation(RuntimeError):
        pass

    def cancel() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise PlannedCancellation("fixture cancelled")

    with pytest.raises(PlannedCancellation, match="fixture cancelled"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            cancellation_check=cancel,
        )

    assert list(scratch.iterdir()) == []
    assert source.read_bytes() == source_before
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_plan_rejects_schema_mismatch_without_migration(tmp_path: Path) -> None:
    database = _create_pdf_state(tmp_path, ("fixture",))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")
    before = database.read_bytes()

    with pytest.raises(SemanticPlanBlocked, match=rf"expected {PDF_SCHEMA_VERSION}"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
        )

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_image_plan_uses_cached_digest_and_separates_ocr(tmp_path: Path) -> None:
    ocr_text = "Transformador industrial"
    _create_image_state(tmp_path, include_dedup=True, ocr_text=ocr_text)

    with patch.object(
        __import__(
            "_04_Nucleo_Operativo.semantic_planner",
            fromlist=["_sources"],
        )._sources,
        "_stream_file_fingerprint",
        side_effect=AssertionError("original image must not be read"),
    ) as source_reader:
        plan = plan_semantic_index(tmp_path, scope="image")

    source_reader.assert_not_called()
    assert plan.resources == 1
    assert plan.sections == 1
    assert plan.chunks == 1
    assert plan.embedding_entities == 2
    assert tuple(workload.name for workload in plan.workloads) == (
        "image",
        "image_ocr",
    )
    assert plan.workloads[0].input_bytes == 100
    assert plan.workloads[1].input_bytes == len(ocr_text.encode())
    assert plan.unique_contents == 2
    assert plan.new_vector_blob_bytes_lower_bound == (512 * 2) + (768 * 2)
    assert plan.originals_verified is False
    assert plan.execution_ready is None
    assert plan.complete is False


def test_image_plan_processing_signatures_match_bounded_producer_contracts(
    tmp_path: Path,
) -> None:
    _create_image_state(
        tmp_path,
        include_dedup=True,
        ocr_text="Transformador industrial",
    )

    plan = plan_semantic_index(tmp_path, scope="image")

    signatures = {workload.name: workload.processing_signature for workload in plan.workloads}
    assert signatures == {
        "image": (
            "neocortex-semantic-pipeline-v2|semantic-source-adapters-v3|"
            "images|enumeration=bounded-v1"
        ),
        "image_ocr": (
            "neocortex-semantic-pipeline-v2|semantic-source-adapters-v3|"
            f"image-ocr|{plan.text_chunking_signature}|"
            "tokenizer-contract=unresolved-v1|"
            "quality-policy=semantic-text-quality-v1|enumeration=bounded-v1"
        ),
    }
    assert plan.estimate_kind == ("model_only_request_range_from_pre_tokenizer_content_projection")


def test_image_plan_blocks_missing_digest_without_reading_original(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=False, ocr_text=None)
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    with patch.object(
        planner_module._sources,
        "_stream_file_fingerprint",
        side_effect=AssertionError("original image must not be read"),
    ) as source_reader:
        with pytest.raises(SemanticPlanBlocked, match="refuses to read"):
            plan_semantic_index(tmp_path, scope="image")

    source_reader.assert_not_called()
    assert not (tmp_path / "semantic.sqlite3").exists()


# endregion [03]


# region [04] Hard scratch bounds, SQL cancellation and model contracts


def test_chunking_signature_versions_the_per_item_chunk_limit() -> None:
    first = TextChunkingConfig(max_chunks_per_item=17)
    second = replace(first, max_chunks_per_item=18)

    assert first.signature.startswith("natural-window-v2|")
    assert "|max-chunks=17" in first.signature
    assert first.signature != second.signature


@pytest.mark.parametrize(
    "maximum",
    (0, -1, True, 1.5, 65_535, 16 * 1024**4 + 1),
)
def test_scratch_byte_bound_is_strictly_validated(
    tmp_path: Path,
    maximum: object,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(ValueError, match="max_scratch_bytes"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            max_scratch_bytes=maximum,  # type: ignore[arg-type]
        )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_scratch_quota_is_inclusive_hard_bounded_and_observable(
    tmp_path: Path,
) -> None:
    texts = tuple(
        f"Contenido industrial único {index:04d} con interruptor y protección"
        for index in range(900)
    )
    source = _create_pdf_state(tmp_path, texts)
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    baseline = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        scratch_directory=scratch,
    )
    peak = baseline.scratch_storage_bytes
    assert peak >= 64 * 1024
    exact = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        scratch_directory=scratch,
        max_scratch_bytes=peak,
    )

    assert exact.scratch_storage_bytes <= peak
    assert exact.plan_signature == baseline.plan_signature
    with pytest.raises(SemanticScratchLimitExceeded, match="max_scratch_bytes"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            max_scratch_bytes=peak - 1,
        )
    assert list(scratch.iterdir()) == []
    assert source.read_bytes() == source_before
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_preexisting_reuse_is_batched_on_the_bounded_scratch_connection(
    tmp_path: Path,
) -> None:
    texts = tuple(f"Payload reutilizable {index:03d}" for index in range(205))
    _create_pdf_state(tmp_path, texts)
    _create_semantic_payloads(tmp_path, texts)
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_mark = planner_module._ContentAccumulator.mark_preexisting_reuse
    batch_sizes: list[int] = []
    observed_names: set[str] = set()

    def observe_mark(accumulator, rows):
        batch_sizes.append(len(rows))
        observed_names.update(entry.name for entry in accumulator._budget.directory.iterdir())
        assert accumulator._connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert int(accumulator._connection.execute("PRAGMA temp_store").fetchone()[0]) == 2
        result = real_mark(accumulator, rows)
        observed_names.update(entry.name for entry in accumulator._budget.directory.iterdir())
        return result

    with patch.object(
        planner_module._ContentAccumulator,
        "mark_preexisting_reuse",
        side_effect=observe_mark,
        autospec=True,
    ):
        plan = plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
        )

    assert sum(batch_sizes) == len(texts)
    assert all(1 <= size <= 100 for size in batch_sizes)
    assert plan.reusable_unique_contents == len(texts)
    assert plan.new_unique_contents == len(texts)
    assert observed_names == {"content-keys.sqlite3"}
    assert plan.scratch_storage_bytes <= plan.max_scratch_bytes


def test_sqlite_full_during_reuse_mark_is_translated_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    text = "Reuse que agota scratch"
    _create_pdf_state(tmp_path, (text,))
    _create_semantic_payload(tmp_path, text)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect

    class FullOnReuseConnection(sqlite3.Connection):
        def executemany(self, sql, parameters, /):
            if "UPDATE content_keys SET reusable=1" in sql:
                error = sqlite3.OperationalError("forced scratch full")
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error
            return super().executemany(sql, parameters)

    def connect_with_full_reuse(database, *args, **kwargs):
        if "content-keys.sqlite3" in str(database):
            kwargs["factory"] = FullOnReuseConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=connect_with_full_reuse,
    ):
        with pytest.raises(SemanticScratchLimitExceeded, match="was exhausted"):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
            )

    assert list(scratch.iterdir()) == []


def test_sql_progress_cancellation_is_re_raised_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    source = _create_pdf_state(
        tmp_path,
        tuple(f"registro SQL único {index}" for index in range(1_200)),
    )
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    sql_callbacks = 0

    class PlannedSQLCancellation(RuntimeError):
        pass

    def cancel_only_inside_sqlite_progress() -> None:
        nonlocal sql_callbacks
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "sqlite_progress":
                sql_callbacks += 1
                raise PlannedSQLCancellation("cancelled from SQLite progress")
            frame = frame.f_back

    with pytest.raises(
        PlannedSQLCancellation,
        match="cancelled from SQLite progress",
    ):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            cancellation_check=cancel_only_inside_sqlite_progress,
        )

    assert sql_callbacks == 1
    assert list(scratch.iterdir()) == []
    assert source.read_bytes() == source_before


def test_locked_owner_retry_is_short_and_cancellable(tmp_path: Path) -> None:
    source = _create_pdf_state(tmp_path, ("Owner bloqueado",))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    retry_checkpoints = 0

    class LockedOwnerCancellation(RuntimeError):
        pass

    def cancel_during_retry() -> None:
        nonlocal retry_checkpoints
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "_retry_busy":
                retry_checkpoints += 1
                if retry_checkpoints == 2:
                    raise LockedOwnerCancellation("cancelled bounded lock retry")
                return
            frame = frame.f_back

    locker = sqlite3.connect(source, timeout=5.0)
    try:
        mode = str(locker.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        assert mode == "delete"
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(
            LockedOwnerCancellation,
            match="cancelled bounded lock retry",
        ):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
                cancellation_check=cancel_during_retry,
            )
        elapsed = time.monotonic() - started
    finally:
        locker.rollback()
        locker.close()

    assert retry_checkpoints == 2
    assert elapsed < 1.0
    assert list(scratch.iterdir()) == []


def test_keyboard_interrupt_cleans_private_scratch(tmp_path: Path) -> None:
    _create_pdf_state(tmp_path, ("uno", "dos", "tres"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    checkpoints = 0

    def interrupt() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 5:
            raise KeyboardInterrupt("fixture interrupt")

    with pytest.raises(KeyboardInterrupt, match="fixture interrupt"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            cancellation_check=interrupt,
        )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_text_workload_rejects_model_without_passage_role(tmp_path: Path) -> None:
    model = replace(
        multilingual_text_model(),
        supported_roles=(EmbeddingRole.QUERY,),
    )

    with pytest.raises(SemanticPlanBlocked, match=r"does not support.*passage"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            text_model=model,
        )

    assert not (tmp_path / "semantic.sqlite3").exists()


def test_model_signature_collision_is_blocked_before_owner_access(
    tmp_path: Path,
) -> None:
    colliding_text_model = replace(
        multilingual_text_model(),
        model_signature=clip_image_model().model_signature,
    )

    with pytest.raises(SemanticPlanBlocked, match="colliding contracts"):
        plan_semantic_index(
            tmp_path,
            scope="all",
            source_kinds=("pdf",),
            text_model=colliding_text_model,
        )


# endregion [04]


# region [05] Snapshot fences and exact calibration identity


@pytest.mark.parametrize(
    "field",
    (
        "vector_space",
        "modality",
        "model_id",
        "model_version",
        "dimensions",
        "provider",
        "supported_roles_json",
        "vector_dtype",
        "normalization",
        "distance",
        "provenance_json",
    ),
)
def test_semantic_cache_rejects_every_model_contract_drift(
    tmp_path: Path,
    field: str,
) -> None:
    text = "Contrato de modelo"
    _create_pdf_state(tmp_path, (text,))
    semantic = _create_semantic_payload(tmp_path, text)
    model = multilingual_text_model()
    with sqlite3.connect(semantic) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        if field == "vector_space":
            connection.execute(
                """INSERT INTO vector_spaces(
                vector_space,dimensions,distance,normalization,created_ns)
                VALUES('drift-space',?,'cosine','l2',1)""",
                (model.dimensions,),
            )
            value: object = "drift-space"
        elif field == "modality":
            value = "image"
            connection.execute(
                "UPDATE embedding_models SET supported_roles_json='[\"image\"]' "
                "WHERE model_signature=?",
                (model.model_signature,),
            )
        elif field == "dimensions":
            value = model.dimensions - 1
        elif field == "supported_roles_json":
            value = '["passage"]'
        elif field == "vector_dtype":
            value = "float32"
        elif field == "normalization":
            value = "none"
        elif field == "distance":
            value = "euclidean"
        elif field == "provenance_json":
            value = '{"drift":true}'
        else:
            value = f"drift-{field}"
        connection.execute(
            f"UPDATE embedding_models SET {field}=? WHERE model_signature=?",
            (value, model.model_signature),
        )

    with pytest.raises(SemanticPlanBlocked, match=r"semantic model|vector-space"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
        )


def test_same_version_owner_ddl_drift_is_rejected(tmp_path: Path) -> None:
    database = _create_pdf_state(tmp_path, ("DDL drift",))
    with sqlite3.connect(database) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        connection.execute("DROP INDEX documents_text_idx")
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == version

    with pytest.raises(SemanticPlanBlocked, match="schema validation failed"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
        )


def test_same_version_semantic_ddl_drift_is_rejected(tmp_path: Path) -> None:
    text = "Semantic DDL drift"
    _create_pdf_state(tmp_path, (text,))
    semantic = _create_semantic_payload(tmp_path, text)
    with sqlite3.connect(semantic) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        connection.execute("DROP INDEX embedding_models_space_idx")
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == version

    with pytest.raises(SemanticPlanBlocked, match="semantic cache validation failed"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
        )


def test_owner_data_version_fence_blocks_mid_plan_mutation(tmp_path: Path) -> None:
    source = _create_pdf_state(tmp_path, ("TOCTOU owner",))
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_validator = planner_module._validate_source_schema
    mutated = False

    def validate_then_mutate(
        connection: sqlite3.Connection,
        source_kind: str,
    ) -> int:
        nonlocal mutated
        version = real_validator(connection, source_kind)
        if not mutated:
            mutated = True
            with sqlite3.connect(source, timeout=5.0) as writer:
                writer.execute(
                    "UPDATE documents SET path=path || '.changed' WHERE file_key='pdf-1'"
                )
        return version

    with patch.object(
        planner_module,
        "_validate_source_schema",
        side_effect=validate_then_mutate,
    ):
        with pytest.raises(SemanticPlanBlocked, match="owner changed during planning"):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
            )


def test_office_views_share_one_connection_transaction_and_snapshot(
    tmp_path: Path,
) -> None:
    office = _create_office_state(tmp_path)
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_projector = planner_module._plan_text_source
    connection_ids: list[int] = []

    def observe_projection(*args, **kwargs):
        connection = kwargs["connection"]
        assert connection.in_transaction
        connection_ids.append(id(connection))
        return real_projector(*args, **kwargs)

    with patch.object(
        planner_module,
        "_plan_text_source",
        side_effect=observe_projection,
    ):
        plan = plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("xlsx", "pptx", "odt"),
        )

    assert len(connection_ids) == 3
    assert len(set(connection_ids)) == 1
    assert tuple(source.database for source in plan.source_plans) == (office,) * 3
    assert len({source.snapshot_xxh3_128 for source in plan.source_plans}) == 1


def test_office_group_fence_blocks_mutation_between_logical_views(
    tmp_path: Path,
) -> None:
    office = _create_office_state(tmp_path)
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_projector = planner_module._plan_text_source
    projections = 0

    def project_then_mutate(*args, **kwargs):
        nonlocal projections
        result = real_projector(*args, **kwargs)
        projections += 1
        if projections == 1:
            with sqlite3.connect(office, timeout=5.0) as writer:
                writer.execute(
                    "UPDATE documents SET path=path || '.changed' WHERE file_key='office-3'"
                )
        return result

    with patch.object(
        planner_module,
        "_plan_text_source",
        side_effect=project_then_mutate,
    ):
        with pytest.raises(SemanticPlanBlocked, match="owner changed during planning"):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("xlsx", "pptx", "odt"),
            )

    assert projections == 3


def test_semantic_data_version_fence_blocks_mid_plan_model_mutation(
    tmp_path: Path,
) -> None:
    text = "TOCTOU semantic"
    _create_pdf_state(tmp_path, (text,))
    semantic = _create_semantic_payload(tmp_path, text)
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_validator = planner_module._validate_semantic_cache
    mutated = False

    def validate_then_mutate(connection, models):
        nonlocal mutated
        version = real_validator(connection, models)
        if not mutated:
            mutated = True
            with sqlite3.connect(semantic, timeout=5.0) as writer:
                writer.execute("UPDATE embedding_models SET provider='drifted-provider'")
        return version

    with patch.object(
        planner_module,
        "_validate_semantic_cache",
        side_effect=validate_then_mutate,
    ):
        with pytest.raises(SemanticPlanBlocked, match="semantic cache changed"):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
            )


@pytest.mark.parametrize(
    "rate",
    (0.0, -1.0, math.nan, math.inf, -math.inf, True),
)
def test_cost_calibration_rejects_nonfinite_or_nonpositive_rate(
    rate: object,
) -> None:
    with pytest.raises(ValueError, match="contents_per_second"):
        SemanticCostCalibration(
            calibration_signature="calibration-v1",
            execution_signature="execution-v1",
            processing_signature="processing-v1",
            workload="text",
            model_signature="model-v1",
            role="passage",
            contents_per_second=rate,  # type: ignore[arg-type]
            sample_contents=1,
            sample_input_bytes=1,
        )


def _calibration_for_plan(
    plan,
    *,
    rate: float = 2.0,
    sample_contents: int = 64,
) -> SemanticCostCalibration:
    workload = plan.workloads[0]
    return SemanticCostCalibration(
        calibration_signature="fixture-calibration-v1",
        execution_signature="fixture-execution-v1",
        processing_signature=workload.processing_signature,
        workload=workload.name,
        model_signature=workload.model_signature,
        role=workload.role,
        contents_per_second=rate,
        sample_contents=sample_contents,
        sample_input_bytes=sample_contents * 20,
    )


def test_request_and_cost_are_ranges_until_duplicate_requests_collapse(
    tmp_path: Path,
) -> None:
    text = "Solicitud duplicada"
    _create_pdf_state(tmp_path, (text, text))
    uncalibrated = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    calibration = _calibration_for_plan(uncalibrated)
    calibrated = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(calibration,),
        execution_signature=calibration.execution_signature,
    )

    assert calibrated.model_request_contents_lower_bound == 3
    assert calibrated.model_request_contents_upper_bound == 4
    assert calibrated.estimated_model_seconds_lower_bound == pytest.approx(1.5)
    assert calibrated.estimated_model_seconds_upper_bound == pytest.approx(2.0)
    assert calibrated.estimated_model_seconds is None
    assert calibrated.cost_complete is False
    assert calibrated.cost_calibrated is True


def test_calibration_is_snapshotted_before_cancellation_callbacks_can_mutate_it(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("Calibración inmutable",))
    uncalibrated = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    calibration = _calibration_for_plan(uncalibrated)
    callbacks = 0

    def mutate_original() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 1:
            object.__setattr__(calibration, "contents_per_second", math.inf)

    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(calibration,),
        execution_signature=calibration.execution_signature,
        cancellation_check=mutate_original,
    )

    assert callbacks > 0
    assert plan.estimated_model_seconds == pytest.approx(1.0)
    assert math.isfinite(plan.estimated_model_seconds or math.inf)


def test_plan_signature_includes_rate_and_sample_of_applied_calibration(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("Firma de calibración",))
    base = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    first_calibration = _calibration_for_plan(base, rate=2.0, sample_contents=64)
    second_calibration = _calibration_for_plan(base, rate=3.0, sample_contents=96)
    first = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(first_calibration,),
        execution_signature=first_calibration.execution_signature,
    )
    second = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(second_calibration,),
        execution_signature=second_calibration.execution_signature,
    )

    assert first.plan_signature != second.plan_signature


def test_duplicate_exact_calibration_key_is_rejected(tmp_path: Path) -> None:
    _create_pdf_state(tmp_path, ("Calibración duplicada",))
    base = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    calibration = _calibration_for_plan(base)

    with pytest.raises(ValueError, match="duplicate exact"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            cost_calibrations=(calibration, calibration),
            execution_signature=calibration.execution_signature,
        )


# endregion [05]


# region [06] Cache-only images, multi-workload ranges and strict payloads


def test_valid_dedup_without_matching_fingerprint_still_blocks_cache_only_image(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    with sqlite3.connect(tmp_path / "dedup.sqlite3") as connection:
        connection.execute("DELETE FROM fingerprints")
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    with patch.object(
        planner_module._sources,
        "_stream_file_fingerprint",
        side_effect=AssertionError("original image must not be read"),
    ) as source_reader:
        with pytest.raises(SemanticPlanBlocked, match="refuses to read"):
            plan_semantic_index(tmp_path, scope="image")

    source_reader.assert_not_called()


def test_missing_image_owner_is_a_domain_block_without_state_creation(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(SemanticPlanBlocked, match="image owner state is missing"):
        plan_semantic_index(
            tmp_path,
            scope="image",
            scratch_directory=scratch,
        )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "image.sqlite3").exists()
    assert not (tmp_path / "semantic.sqlite3").exists()
    assert not (tmp_path / "framework.lock").exists()


def test_all_scope_separates_image_and_ocr_but_reuses_pdf_content(
    tmp_path: Path,
) -> None:
    text = "Protección de transformador"
    _create_pdf_state(tmp_path, (text,))
    _create_image_state(tmp_path, include_dedup=True, ocr_text=text)

    first = plan_semantic_index(
        tmp_path,
        scope="all",
        source_kinds=("pdf",),
    )
    second = plan_semantic_index(
        tmp_path,
        scope="all",
        source_kinds=("pdf",),
    )

    assert first.plan_signature == second.plan_signature
    assert tuple(source.source_kind for source in first.source_plans) == (
        "pdf",
        "image",
    )
    assert tuple(workload.name for workload in first.workloads) == (
        "text",
        "image",
        "image_ocr",
    )
    assert first.resources == 2
    assert first.sections == 3
    assert first.chunks == 3
    assert first.embedding_entities == 4
    assert first.unique_contents == 3
    assert first.new_unique_contents == 3
    assert first.model_request_contents_lower_bound == 3
    assert first.model_request_contents_upper_bound == 4
    assert first.new_vector_blob_bytes_lower_bound == (2 * 768 * 2) + (512 * 2)
    text_workload, image_workload, ocr_workload = first.workloads
    assert (
        text_workload.model_request_contents_lower_bound,
        text_workload.model_request_contents_upper_bound,
    ) == (2, 2)
    assert (
        image_workload.model_request_contents_lower_bound,
        image_workload.model_request_contents_upper_bound,
    ) == (1, 1)
    assert ocr_workload.unique_contents == 1
    assert ocr_workload.planned_reusable_contents == 1
    assert ocr_workload.new_unique_contents == 0
    assert (
        ocr_workload.model_request_contents_lower_bound,
        ocr_workload.model_request_contents_upper_bound,
    ) == (0, 1)
    assert first.originals_verified is False
    assert first.execution_ready is None
    assert first.complete is False
    assert not (tmp_path / "framework.lock").exists()


def test_all_scope_preexisting_text_payload_is_reused_across_pdf_and_ocr(
    tmp_path: Path,
) -> None:
    text = "Protección de transformador preexistente"
    _create_pdf_state(tmp_path, (text,))
    _create_image_state(tmp_path, include_dedup=True, ocr_text=text)
    _create_semantic_payload(tmp_path, text)

    plan = plan_semantic_index(
        tmp_path,
        scope="all",
        source_kinds=("pdf",),
    )

    text_workload, image_workload, ocr_workload = plan.workloads
    assert plan.embedding_entities == 4
    assert plan.unique_contents == 3
    assert plan.reusable_unique_contents == 1
    assert plan.new_unique_contents == 2
    assert plan.model_request_contents_lower_bound == 2
    assert plan.model_request_contents_upper_bound == 2
    assert plan.new_vector_blob_bytes_lower_bound == (768 * 2) + (512 * 2)
    assert text_workload.preexisting_reusable_contents == 1
    assert text_workload.new_unique_contents == 1
    assert image_workload.preexisting_reusable_contents == 0
    assert image_workload.new_unique_contents == 1
    assert ocr_workload.preexisting_reusable_contents == 1
    assert ocr_workload.planned_reusable_contents == 0
    assert ocr_workload.new_unique_contents == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("execution_signature", "different-execution"),
        ("processing_signature", "different-processing"),
        ("workload", "different-workload"),
        ("model_signature", "different-model"),
        ("role", "image"),
    ),
)
def test_calibration_requires_every_exact_identity_field(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _create_pdf_state(tmp_path, ("Identidad exacta",))
    base = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    calibration = replace(_calibration_for_plan(base), **{field: value})
    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        cost_calibrations=(calibration,),
        execution_signature="fixture-execution-v1",
    )

    assert plan.estimated_model_seconds_lower_bound is None
    assert plan.estimated_model_seconds_upper_bound is None
    assert plan.workloads[0].cost_unavailable_reason == "no_exact_cost_calibration"


def test_workload_and_plan_contracts_reject_inconsistent_algebra(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("Álgebra estricta", "Álgebra estricta"))
    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    workload = plan.workloads[0]

    with pytest.raises(ValueError, match="partition"):
        replace(workload, preexisting_reusable_contents=1)
    with pytest.raises(ValueError, match="request lower"):
        replace(workload, model_request_contents_lower_bound=0)
    with pytest.raises(ValueError, match="finite"):
        replace(
            workload,
            estimated_model_seconds_lower_bound=math.inf,
            estimated_model_seconds_upper_bound=math.inf,
        )
    with pytest.raises(ValueError, match="resource aggregate"):
        replace(plan, resources=plan.resources + 1)
    with pytest.raises(ValueError, match="dimensions"):
        replace(workload, dimensions=True)
    with pytest.raises(ValueError, match="schema_version"):
        replace(plan.source_plans[0], schema_version=True)
    with pytest.raises(ValueError, match="semantic_schema_version"):
        replace(plan, semantic_schema_version=True)
    with pytest.raises(ValueError, match="duplicate owners"):
        replace(plan, source_plans=(plan.source_plans[0],) * 2)
    with pytest.raises(ValueError, match="duplicate names"):
        replace(plan, workloads=(workload, workload))
    with pytest.raises(ValueError, match="selected sources"):
        replace(plan, selected_sources=("docx",))
    with pytest.raises(ValueError, match="source input-byte"):
        replace(plan, input_bytes=plan.input_bytes + 1)
    with pytest.raises(ValueError, match="workload input-byte"):
        replace(
            plan,
            workloads=(replace(workload, input_bytes=workload.input_bytes + 1),),
        )
    with pytest.raises(ValueError, match="global new-content"):
        replace(
            plan,
            workloads=(
                replace(
                    workload,
                    planned_reusable_contents=1,
                    new_unique_contents=workload.unique_contents - 1,
                    new_vector_blob_bytes_lower_bound=(
                        (workload.unique_contents - 1) * workload.dimensions * 2
                    ),
                    model_request_contents_lower_bound=(workload.unique_contents - 1),
                ),
            ),
        )


def test_json_payload_refuses_nonfinite_values_even_after_illicit_mutation(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("JSON finito",))
    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
    )
    object.__setattr__(plan, "estimated_model_seconds_lower_bound", math.nan)

    with pytest.raises(ValueError, match="Out of range float values"):
        semantic_plan_payload(plan)


def test_arbitrary_planner_fault_cleans_scratch(tmp_path: Path) -> None:
    _create_pdf_state(tmp_path, ("fault",))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    with patch.object(
        planner_module._ContentAccumulator,
        "flush",
        side_effect=RuntimeError("fixture planner fault"),
    ):
        with pytest.raises(RuntimeError, match="fixture planner fault"):
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
            )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_semantic_sql_progress_cancellation_is_bridged_exactly(
    tmp_path: Path,
) -> None:
    text = "Cancelación Semantic SQL"
    _create_pdf_state(tmp_path, tuple(text + str(index) for index in range(200)))
    _create_semantic_payload(tmp_path, text + "0")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    class SemanticSQLCancellation(RuntimeError):
        pass

    def cancel_in_semantic_sql() -> None:
        frame = inspect.currentframe()
        names: set[str] = set()
        while frame is not None:
            names.add(frame.f_code.co_name)
            frame = frame.f_back
        if {"sqlite_progress", "_semantic_reuse_snapshot"}.issubset(names):
            raise SemanticSQLCancellation("semantic SQL cancelled")

    with pytest.raises(SemanticSQLCancellation, match="semantic SQL cancelled"):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            scratch_directory=scratch,
            cancellation_check=cancel_in_semantic_sql,
        )

    assert list(scratch.iterdir()) == []


# endregion [06]


# region [07] Attached image-owner lifecycle characterization


def test_image_plan_attach_is_readonly_query_only_and_detaches_on_success(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    dedup = tmp_path / "dedup.sqlite3"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect
    attach_parameters: list[tuple[object, ...]] = []
    query_only_values: list[int] = []
    detach_transactions: list[bool] = []
    aliases_after_detach: list[tuple[str, ...]] = []
    close_events: list[None] = []

    class TracingImageConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("ATTACH DATABASE"):
                attach_parameters.append(tuple(parameters))
                cursor = super().execute(sql, parameters)
                query_only_values.append(int(super().execute("PRAGMA query_only").fetchone()[0]))
                return cursor
            if normalized.startswith("DETACH DATABASE"):
                detach_transactions.append(self.in_transaction)
                cursor = super().execute(sql, parameters)
                aliases_after_detach.append(
                    tuple(str(row[1]) for row in super().execute("PRAGMA database_list"))
                )
                return cursor
            return super().execute(sql, parameters)

        def close(self):
            close_events.append(None)
            return super().close()

    def traced_connect(database, *args, **kwargs):
        if "image.sqlite3" in str(database):
            kwargs["factory"] = TracingImageConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=traced_connect,
    ):
        plan = plan_semantic_index(
            tmp_path,
            scope="image",
            embed_ocr_text=False,
            scratch_directory=scratch,
        )

    assert plan.resources == 1
    assert attach_parameters == [(readonly_sqlite_uri(dedup),)]
    assert query_only_values == [1]
    assert detach_transactions == [False]
    assert len(aliases_after_detach) == 1
    assert "dedup" not in aliases_after_detach[0]
    assert close_events == [None]
    assert list(scratch.iterdir()) == []


def test_attach_failure_is_controlled_closes_owner_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect
    events: list[str] = []

    class FailingAttachConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("ATTACH DATABASE"):
                events.append("attach")
                raise sqlite3.OperationalError("forced attach failure")
            if normalized.startswith("DETACH DATABASE"):
                events.append("detach")
            return super().execute(sql, parameters)

        def close(self):
            events.append("close")
            return super().close()

    def failing_connect(database, *args, **kwargs):
        if "image.sqlite3" in str(database):
            kwargs["factory"] = FailingAttachConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=failing_connect,
    ):
        with pytest.raises(
            SemanticPlanBlocked,
            match=r"image owner projection failed.*forced attach failure",
        ):
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert events == ["attach", "close"]
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_image_data_version_fence_blocks_mid_plan_mutation(tmp_path: Path) -> None:
    image_path, _digest = _create_image_state(
        tmp_path,
        include_dedup=True,
        ocr_text=None,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_projector = planner_module._plan_images

    def project_then_mutate(*args, **kwargs):
        result = real_projector(*args, **kwargs)
        writer = sqlite3.connect(image_path, timeout=5.0)
        try:
            with writer:
                writer.execute("UPDATE images SET category='drifted' WHERE status='done'")
        finally:
            writer.close()
        return result

    with patch.object(
        planner_module,
        "_plan_images",
        side_effect=project_then_mutate,
    ):
        with pytest.raises(
            SemanticPlanBlocked,
            match="image owner changed during planning",
        ):
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_attached_dedup_data_version_fence_blocks_mid_plan_mutation(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    dedup = tmp_path / "dedup.sqlite3"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wal_connection = sqlite3.connect(dedup, timeout=5.0)
    try:
        assert str(wal_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    finally:
        wal_connection.close()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_projector = planner_module._plan_images
    replacement_digest = b"\x22" * 16

    def project_then_mutate(*args, **kwargs):
        result = real_projector(*args, **kwargs)
        writer = sqlite3.connect(dedup, timeout=5.0)
        try:
            with writer:
                writer.execute(
                    "UPDATE fingerprints SET digest=? WHERE algorithm=?",
                    (replacement_digest, FULL_ALGORITHM),
                )
        finally:
            writer.close()
        return result

    with patch.object(
        planner_module,
        "_plan_images",
        side_effect=project_then_mutate,
    ):
        with pytest.raises(
            SemanticPlanBlocked,
            match="dedup owner changed during image planning",
        ):
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_dedup_schema_is_revalidated_between_probe_and_attach(
    tmp_path: Path,
) -> None:
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    dedup = tmp_path / "dedup.sqlite3"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_validator = planner_module._validated_dedup_schema

    def validate_then_drift(path, bridge):
        result = real_validator(path, bridge)
        writer = sqlite3.connect(dedup, timeout=5.0)
        try:
            with writer:
                writer.execute("CREATE TABLE planner_schema_drift(id INTEGER PRIMARY KEY)")
        finally:
            writer.close()
        return result

    with patch.object(
        planner_module,
        "_validated_dedup_schema",
        side_effect=validate_then_drift,
    ):
        with pytest.raises(
            SemanticPlanBlocked,
            match=("dedup schema changed between exact validation and image projection"),
        ):
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


# endregion [07]


# region [08] Primary-exception preservation and final cancellation


def test_late_cancellation_before_scratch_commit_is_exact_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    source = _create_pdf_state(tmp_path, ("Cancelación final",))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    class LatePlanCancellation(RuntimeError):
        pass

    primary = LatePlanCancellation("late semantic plan cancellation")
    armed = False
    real_payload = planner_module._plan_payload_for_signature

    def payload_then_arm(*args, **kwargs):
        nonlocal armed
        result = real_payload(*args, **kwargs)
        armed = True
        return result

    def cancel() -> None:
        if armed:
            raise primary

    with patch.object(
        planner_module,
        "_plan_payload_for_signature",
        side_effect=payload_then_arm,
    ):
        with pytest.raises(LatePlanCancellation) as raised:
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
                cancellation_check=cancel,
            )

    assert raised.value is primary
    assert source.read_bytes() == source_before
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_final_cancellation_after_scratch_close_is_exact_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    source = _create_pdf_state(tmp_path, ("Cancelación tras cierre",))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    class FinalPlanCancellation(RuntimeError):
        pass

    real_connect = sqlite3.connect
    primary = FinalPlanCancellation("final semantic plan cancellation")
    armed = False
    close_events: list[None] = []

    class ArmAfterScratchCloseConnection(sqlite3.Connection):
        def close(self):
            nonlocal armed
            result = super().close()
            close_events.append(None)
            armed = True
            return result

    def arming_connect(database, *args, **kwargs):
        if "content-keys.sqlite3" in str(database):
            kwargs["factory"] = ArmAfterScratchCloseConnection
        return real_connect(database, *args, **kwargs)

    def cancel() -> None:
        if armed:
            raise primary

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=arming_connect,
    ):
        with pytest.raises(FinalPlanCancellation) as raised:
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
                cancellation_check=cancel,
            )

    assert raised.value is primary
    assert close_events == [None]
    assert source.read_bytes() == source_before
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_detach_failure_does_not_mask_exact_cancellation(tmp_path: Path) -> None:
    image_path, _digest = _create_image_state(
        tmp_path,
        include_dedup=True,
        ocr_text=None,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    class ExactImageCancellation(RuntimeError):
        pass

    owner_uri = readonly_sqlite_uri(image_path)
    real_connect = sqlite3.connect
    primary = ExactImageCancellation("exact image cancellation")
    detach_error = sqlite3.OperationalError("forced detach failure")
    armed = False
    events: list[str] = []

    def cancel() -> None:
        if armed:
            events.append("cancel")
            raise primary

    class FailingDetachConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("ATTACH DATABASE"):
                cursor = super().execute(sql, parameters)
                events.append("attach")
                return cursor
            if normalized.startswith("DETACH DATABASE"):
                events.append("detach")
                raise detach_error
            return super().execute(sql, parameters)

        def close(self):
            events.append("close")
            return super().close()

    def failing_connect(database, *args, **kwargs):
        if str(database) == owner_uri:
            kwargs["factory"] = FailingDetachConnection
        return real_connect(database, *args, **kwargs)

    def cancel_after_rollback(*args, connection, checkpoint, **kwargs):
        del args, kwargs
        nonlocal armed
        connection.rollback()
        armed = True
        checkpoint()
        raise AssertionError("cancellation checkpoint unexpectedly returned")

    with (
        patch.object(
            planner_module.sqlite3,
            "connect",
            side_effect=failing_connect,
        ),
        patch.object(
            planner_module,
            "_plan_images",
            side_effect=cancel_after_rollback,
        ),
    ):
        with pytest.raises(ExactImageCancellation) as raised:
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
                cancellation_check=cancel,
            )

    assert raised.value is primary
    assert events == ["attach", "cancel", "detach", "close"]
    assert getattr(primary, "__notes__", ()) == [
        "semantic planner dedup detach cleanup failed: OperationalError: forced detach failure"
    ]
    assert list(scratch.iterdir()) == []


def test_detach_failure_does_not_mask_primary_projection_error(
    tmp_path: Path,
) -> None:
    image_path, _digest = _create_image_state(
        tmp_path,
        include_dedup=True,
        ocr_text=None,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    owner_uri = readonly_sqlite_uri(image_path)
    real_connect = sqlite3.connect
    primary = SemanticPlanBlocked("primary image projection failure")
    detach_error = sqlite3.OperationalError("secondary detach failure")
    events: list[str] = []

    class FailingDetachConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("ATTACH DATABASE"):
                cursor = super().execute(sql, parameters)
                events.append("attach")
                return cursor
            if normalized.startswith("DETACH DATABASE"):
                events.append("detach")
                raise detach_error
            return super().execute(sql, parameters)

        def close(self):
            events.append("close")
            return super().close()

    def failing_connect(database, *args, **kwargs):
        if str(database) == owner_uri:
            kwargs["factory"] = FailingDetachConnection
        return real_connect(database, *args, **kwargs)

    def fail_after_rollback(*args, connection, **kwargs):
        del args, kwargs
        connection.rollback()
        raise primary

    with (
        patch.object(
            planner_module.sqlite3,
            "connect",
            side_effect=failing_connect,
        ),
        patch.object(
            planner_module,
            "_plan_images",
            side_effect=fail_after_rollback,
        ),
    ):
        with pytest.raises(SemanticPlanBlocked) as raised:
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert raised.value is primary
    assert events == ["attach", "detach", "close"]
    assert getattr(primary, "__notes__", ()) == [
        "semantic planner dedup detach cleanup failed: OperationalError: secondary detach failure"
    ]
    assert list(scratch.iterdir()) == []


def test_detach_failure_without_primary_is_controlled_and_closes_owner(
    tmp_path: Path,
) -> None:
    image_path, _digest = _create_image_state(
        tmp_path,
        include_dedup=True,
        ocr_text=None,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    owner_uri = readonly_sqlite_uri(image_path)
    real_connect = sqlite3.connect
    detach_error = sqlite3.OperationalError("unique detach failure")
    events: list[str] = []

    class UniqueDetachFailureConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("ATTACH DATABASE"):
                cursor = super().execute(sql, parameters)
                events.append("attach")
                return cursor
            if normalized.startswith("DETACH DATABASE"):
                events.append("detach")
                raise detach_error
            return super().execute(sql, parameters)

        def close(self):
            events.append("close")
            return super().close()

    def failing_connect(database, *args, **kwargs):
        if str(database) == owner_uri:
            kwargs["factory"] = UniqueDetachFailureConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=failing_connect,
    ):
        with pytest.raises(
            SemanticPlanBlocked,
            match=r"image owner projection failed.*unique detach failure",
        ) as raised:
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert raised.value.__cause__ is detach_error
    assert events == ["attach", "detach", "close"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("sql_marker", "preexisting"),
    (
        ("INSERT INTO content_keys", False),
        ("UPDATE content_keys SET reusable=1", True),
    ),
    ids=("insert", "reuse"),
)
def test_accumulator_rollback_failure_does_not_mask_primary_sqlite_error(
    tmp_path: Path,
    sql_marker: str,
    preexisting: bool,
) -> None:
    text = "Primary accumulator error"
    _create_pdf_state(tmp_path, (text,))
    if preexisting:
        _create_semantic_payload(tmp_path, text)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect
    primary = sqlite3.OperationalError("primary accumulator write failure")
    cleanup = RuntimeError("secondary scratch rollback failure")
    rollback_events: list[None] = []

    class FailingRollbackConnection(sqlite3.Connection):
        def executemany(self, sql, parameters, /):
            if sql_marker in sql:
                raise primary
            return super().executemany(sql, parameters)

        def rollback(self):
            rollback_events.append(None)
            super().rollback()
            raise cleanup

    def failing_connect(database, *args, **kwargs):
        if "content-keys.sqlite3" in str(database):
            kwargs["factory"] = FailingRollbackConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=failing_connect,
    ):
        if preexisting:
            with pytest.raises(SemanticPlanBlocked) as blocked_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                )
            assert blocked_raised.value.__cause__ is primary
        else:
            with pytest.raises(sqlite3.OperationalError) as sqlite_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                )
            assert sqlite_raised.value is primary

    assert rollback_events == [None]
    assert getattr(primary, "__notes__", ()) == [
        "semantic planner scratch rollback cleanup failed: "
        "RuntimeError: secondary scratch rollback failure"
    ]
    assert list(scratch.iterdir()) == []


def test_scratch_create_close_failure_does_not_mask_primary_setup_error(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect
    primary = sqlite3.OperationalError("primary scratch setup failure")
    cleanup = RuntimeError("secondary scratch close failure")
    close_events: list[None] = []

    class FailingScratchCloseConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if " ".join(sql.split()).upper() == "PRAGMA SYNCHRONOUS=OFF":
                raise primary
            return super().execute(sql, parameters)

        def close(self):
            close_events.append(None)
            super().close()
            raise cleanup

    def failing_connect(database, *args, **kwargs):
        if "content-keys.sqlite3" in str(database):
            kwargs["factory"] = FailingScratchCloseConnection
        return real_connect(database, *args, **kwargs)

    with patch.object(
        planner_module.sqlite3,
        "connect",
        side_effect=failing_connect,
    ):
        with pytest.raises(sqlite3.OperationalError) as raised:
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    assert raised.value is primary
    assert close_events == [None]
    assert getattr(primary, "__notes__", ()) == [
        "semantic planner scratch setup close cleanup failed: "
        "RuntimeError: secondary scratch close failure"
    ]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("primary_present", (True, False), ids=("primary", "unique"))
def test_readonly_owner_close_failure_preserves_primary_or_surfaces_unique(
    tmp_path: Path,
    primary_present: bool,
) -> None:
    source = _create_pdf_state(tmp_path, ("Cierre de owner",))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    owner_uri = readonly_sqlite_uri(source)
    real_connect = sqlite3.connect
    real_validate = planner_module._validate_source_schema
    primary = SemanticPlanBlocked("primary owner projection failure")
    cleanup = RuntimeError("secondary read-only owner close failure")
    close_events: list[None] = []

    class FailingOwnerCloseConnection(sqlite3.Connection):
        def close(self):
            close_events.append(None)
            super().close()
            raise cleanup

    def failing_connect(database, *args, **kwargs):
        if str(database) == owner_uri:
            kwargs["factory"] = FailingOwnerCloseConnection
        return real_connect(database, *args, **kwargs)

    def maybe_fail_validation(*args, **kwargs):
        if primary_present:
            raise primary
        return real_validate(*args, **kwargs)

    with (
        patch.object(
            planner_module.sqlite3,
            "connect",
            side_effect=failing_connect,
        ),
        patch.object(
            planner_module,
            "_validate_source_schema",
            side_effect=maybe_fail_validation,
        ),
    ):
        if primary_present:
            with pytest.raises(SemanticPlanBlocked) as primary_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                )
            assert primary_raised.value is primary
            assert getattr(primary, "__notes__", ()) == [
                "semantic planner read-only owner close cleanup failed: "
                "RuntimeError: secondary read-only owner close failure"
            ]
        else:
            with pytest.raises(
                SemanticPlanBlocked,
                match=r"owner projection failed.*secondary read-only owner close failure",
            ) as unique_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                )
            assert unique_raised.value.__cause__ is cleanup
            assert getattr(cleanup, "__notes__", ()) == ()

    assert close_events == [None]
    assert source.read_bytes() == source_before
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("primary_present", (True, False), ids=("primary", "unique"))
def test_final_scratch_close_failure_preserves_primary_or_surfaces_unique(
    tmp_path: Path,
    primary_present: bool,
) -> None:
    source = _create_pdf_state(tmp_path, ("Cierre de scratch",))
    source_before = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    real_connect = sqlite3.connect
    real_payload = planner_module._plan_payload_for_signature
    primary = RuntimeError("primary cancellation before scratch close")
    cleanup = RuntimeError("secondary final scratch close failure")
    armed = False
    close_events: list[None] = []

    class FailingFinalScratchCloseConnection(sqlite3.Connection):
        def close(self):
            close_events.append(None)
            super().close()
            raise cleanup

    def failing_connect(database, *args, **kwargs):
        if "content-keys.sqlite3" in str(database):
            kwargs["factory"] = FailingFinalScratchCloseConnection
        return real_connect(database, *args, **kwargs)

    def payload_then_maybe_arm(*args, **kwargs):
        nonlocal armed
        result = real_payload(*args, **kwargs)
        armed = primary_present
        return result

    def cancel() -> None:
        if armed:
            raise primary

    with (
        patch.object(
            planner_module.sqlite3,
            "connect",
            side_effect=failing_connect,
        ),
        patch.object(
            planner_module,
            "_plan_payload_for_signature",
            side_effect=payload_then_maybe_arm,
        ),
    ):
        if primary_present:
            with pytest.raises(RuntimeError) as primary_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                    cancellation_check=cancel,
                )
            assert primary_raised.value is primary
            assert getattr(primary, "__notes__", ()) == [
                "semantic planner final scratch close cleanup failed: "
                "RuntimeError: secondary final scratch close failure"
            ]
        else:
            with pytest.raises(RuntimeError) as unique_raised:
                plan_semantic_index(
                    tmp_path,
                    scope="text",
                    source_kinds=("pdf",),
                    scratch_directory=scratch,
                    cancellation_check=cancel,
                )
            assert unique_raised.value is cleanup
            assert getattr(cleanup, "__notes__", ()) == ()

    assert close_events == [None]
    assert source.read_bytes() == source_before
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()


@pytest.mark.parametrize(
    ("owner_kind", "primary_present"),
    (
        ("text", True),
        ("dedup", True),
        ("semantic", True),
        ("image", True),
        ("text", False),
    ),
    ids=(
        "text-primary",
        "dedup-primary",
        "semantic-primary",
        "image-primary",
        "text-unique",
    ),
)
def test_snapshot_rollback_failure_preserves_primary_or_surfaces_unique(
    tmp_path: Path,
    owner_kind: str,
    primary_present: bool,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    if owner_kind == "text":
        owner_path = _create_pdf_state(tmp_path, ("Rollback de texto",))
        validator_name = "_validate_source_schema"
    elif owner_kind == "dedup":
        _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
        owner_path = tmp_path / "dedup.sqlite3"
        validator_name = "_validate_dedup_schema"
    elif owner_kind == "semantic":
        text = "Rollback semántico"
        _create_pdf_state(tmp_path, (text,))
        owner_path = _create_semantic_payload(tmp_path, text)
        validator_name = "_validate_semantic_cache"
    else:
        owner_path, _digest = _create_image_state(
            tmp_path,
            include_dedup=False,
            ocr_text=None,
        )
        validator_name = "_validate_source_schema"

    owner_before = owner_path.read_bytes()
    owner_uri = readonly_sqlite_uri(owner_path)
    real_connect = sqlite3.connect
    real_validator = getattr(planner_module, validator_name)
    primary = SemanticPlanBlocked(f"primary {owner_kind} snapshot failure")
    cleanup = RuntimeError(f"secondary {owner_kind} snapshot rollback failure")
    rollback_events: list[str] = []

    class FailingSnapshotRollbackConnection(sqlite3.Connection):
        def rollback(self):
            rollback_events.append(owner_kind)
            super().rollback()
            raise cleanup

    def failing_connect(database, *args, **kwargs):
        if str(database) == owner_uri:
            kwargs["factory"] = FailingSnapshotRollbackConnection
        return real_connect(database, *args, **kwargs)

    def maybe_fail_validation(*args, **kwargs):
        if primary_present:
            raise primary
        return real_validator(*args, **kwargs)

    def run_plan() -> None:
        if owner_kind in {"text", "semantic"}:
            plan_semantic_index(
                tmp_path,
                scope="text",
                source_kinds=("pdf",),
                scratch_directory=scratch,
            )
        else:
            plan_semantic_index(
                tmp_path,
                scope="image",
                embed_ocr_text=False,
                scratch_directory=scratch,
            )

    with (
        patch.object(
            planner_module.sqlite3,
            "connect",
            side_effect=failing_connect,
        ),
        patch.object(
            planner_module,
            validator_name,
            side_effect=maybe_fail_validation,
        ),
    ):
        if primary_present:
            with pytest.raises(SemanticPlanBlocked) as primary_raised:
                run_plan()
            assert primary_raised.value is primary
            assert getattr(primary, "__notes__", ()) == [
                f"semantic planner {owner_kind} snapshot rollback cleanup failed: "
                f"RuntimeError: secondary {owner_kind} snapshot rollback failure"
            ]
        else:
            with pytest.raises(
                SemanticPlanBlocked,
                match=r"owner projection failed.*snapshot rollback failure",
            ) as unique_raised:
                run_plan()
            assert unique_raised.value.__cause__ is cleanup
            assert getattr(cleanup, "__notes__", ()) == ()

    assert rollback_events == [owner_kind]
    assert owner_path.read_bytes() == owner_before
    assert list(scratch.iterdir()) == []


# endregion [08]

# region [09] In-module decomposition seams


def test_plan_orchestrator_resolves_modularization_seams_dynamically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_pdf_state(tmp_path, ("Contrato de seam dinámico",))
    _create_image_state(tmp_path, include_dedup=True, ocr_text=None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    from _04_Nucleo_Operativo import semantic_planner as planner_module

    seam_names = (
        "_create_scratch_database",
        "_plan_text_database_group",
        "_validated_dedup_schema",
        "_plan_images",
        "_semantic_reuse_snapshot",
        "_freeze_workload",
        "_plan_payload_for_signature",
    )
    originals = {name: getattr(planner_module, name) for name in seam_names}
    calls = dict.fromkeys(seam_names, 0)

    def tracing_wrapper(name: str):
        original = originals[name]

        def traced(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return traced

    for name in seam_names:
        monkeypatch.setattr(planner_module, name, tracing_wrapper(name))

    plan = plan_semantic_index(
        tmp_path,
        scope="all",
        source_kinds=("pdf",),
        embed_ocr_text=False,
        scratch_directory=scratch,
    )

    assert plan.resources == 2
    assert calls == {
        "_create_scratch_database": 1,
        "_plan_text_database_group": 1,
        "_validated_dedup_schema": 1,
        "_plan_images": 1,
        "_semantic_reuse_snapshot": 1,
        "_freeze_workload": 2,
        "_plan_payload_for_signature": 1,
    }
    assert list(scratch.iterdir()) == []


# endregion [09]
