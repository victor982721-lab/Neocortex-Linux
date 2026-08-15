from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    connect_code_state,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_storage_analysis import analyze_code_storage
from _04_Nucleo_Operativo.sqlite_immutable import capture_sqlite_immutable_fence
from tests.test_code_review import _build_state


def _add_completed_runs(database: Path, *, count: int) -> None:
    connection = connect_code_state(database, create=False)
    try:
        first_id = int(
            connection.execute(
                "SELECT COALESCE(MAX(analysis_run_id),0)+1 FROM analysis_runs"
            ).fetchone()[0]
        )
        rows = tuple(
            (
                first_id + offset,
                10_000 + offset,
                20_000 + offset,
                f"storage-synthetic-signature-{offset}",
                1_000_000 + offset,
                2_000_000 + offset,
            )
            for offset in range(count)
        )
        with connection:
            connection.executemany(
                """INSERT INTO analysis_runs(
                analysis_run_id,framework_run_id,scan_id,processing_signature,
                status,started_ns,completed_ns,candidates,processed,cache_hits,
                errors,summary_json,error_type,error_message
                ) VALUES(?,?,?,?,'completed',?,?,0,0,0,0,'{}',NULL,NULL)""",
                rows,
            )
        checkpoint_code_wal(connection)
    finally:
        connection.close()
    remove_checkpointed_code_sidecars(database)


def _add_provider_run(database: Path, *, analysis_run_id: int = 1) -> None:
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            cursor = connection.execute(
                """INSERT INTO external_tool_runs(
                analysis_run_id,project_id,tool_name,tool_version,
                configuration_signature,status,started_ns,completed_ns,
                provenance_json
                ) VALUES(?,NULL,'storage-fixture','1','config','completed',1,2,'{}')""",
                (analysis_run_id,),
            )
            tool_run_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO external_run_contracts(
                tool_run_id,provider_id,provider_schema,source,profile,
                trust_requirement,scope,observed_root,root_identity,
                project_configuration_digest,environment_signature,
                input_signature,comparability_signature,execution_strategy,
                invalidation_strategy,cache_policy,execution,result_digest,
                portable_publication_id,authority,mutation_authority,
                loads_project_configuration,loads_plugins,imports_content,
                executes_content,uses_network,coverage_complete,limitations_json
                ) VALUES(
                ?,'storage-fixture-provider','fixture/v1','external:fixture',
                'protected','untrusted-safe','project','/fixture','root','project',
                'environment','input','comparable','isolated','file_local','none',
                'full','result','publication','advisory',0,0,0,0,0,0,1,'[]'
                )""",
                (tool_run_id,),
            )
        checkpoint_code_wal(connection)
    finally:
        connection.close()
    remove_checkpointed_code_sidecars(database)


def _table(result, name: str):
    return next(item for item in result.tables if item.table_name == name)


def test_storage_analysis_reports_pages_tables_runs_and_retention_without_authority(
    tmp_path: Path,
) -> None:
    database = _build_state(tmp_path / "state", hotspots=False)
    _add_provider_run(database)
    _add_completed_runs(database, count=2)

    result = analyze_code_storage(
        database,
        run_limit=3,
        row_scan_limit=1_000,
        retain_latest_completed_runs=2,
    )

    assert result.status == "ready"
    assert result.schema_version is not None
    assert result.database_file_bytes > 0
    assert result.page_size_bytes >= 512
    assert result.page_count >= 1
    assert result.allocated_bytes == result.page_size_bytes * result.page_count
    assert result.used_page_bytes <= result.allocated_bytes
    assert result.source_fence is not None
    assert result.authority == "advisory"
    assert result.mutation_authority is False

    analysis_runs = _table(result, "analysis_runs")
    assert analysis_runs.temporal_status == "resolved"
    assert analysis_runs.rows.observed_rows == 3
    assert analysis_runs.current_rows is not None
    assert analysis_runs.current_rows.observed_rows == 1
    assert analysis_runs.historical_rows is not None
    assert analysis_runs.historical_rows.observed_rows == 2
    assert analysis_runs.temporal_basis == ("analysis_runs.analysis_run_id_vs_latest_completed")

    assert tuple(item.analysis_run_id for item in result.runs) == (3, 2, 1)
    assert result.runs[0].current is True
    assert all(item.current is False for item in result.runs[1:])
    assert result.growth is not None
    assert result.growth.comparable is True
    assert result.growth.latest_completed_run_id == 3
    assert result.growth.previous_completed_run_id == 2
    assert result.retention is not None
    assert result.retention.retained_run_ids == (3, 2)
    assert result.retention.deletion_supported is False
    assert result.retention.action == "preview_only"


def test_storage_analysis_is_byte_and_sidecar_preserving(
    tmp_path: Path,
) -> None:
    database = _build_state(tmp_path / "state", hotspots=False)
    _add_provider_run(database)
    before_fence = capture_sqlite_immutable_fence(database)
    before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    before_entries = tuple(sorted(path.name for path in database.parent.iterdir()))

    first = analyze_code_storage(database, run_limit=5, row_scan_limit=100)
    second = analyze_code_storage(database, run_limit=5, row_scan_limit=100)

    after_fence = capture_sqlite_immutable_fence(database)
    after_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    after_entries = tuple(sorted(path.name for path in database.parent.iterdir()))
    assert first.status == second.status == "ready"
    assert first.as_payload() == second.as_payload()
    assert after_fence == before_fence
    assert after_digest == before_digest
    assert after_entries == before_entries


def test_large_synthetic_database_remains_bounded_and_marks_lower_bounds(
    tmp_path: Path,
) -> None:
    database = _build_state(tmp_path / "state", hotspots=False)
    _add_completed_runs(database, count=5_000)

    result = analyze_code_storage(
        database,
        run_limit=7,
        row_scan_limit=25,
        retain_latest_completed_runs=5,
    )

    assert result.status == "ready"
    assert len(result.tables) <= 64
    assert len(result.providers) <= 128
    assert len(result.runs) == 7
    assert result.runs_truncated is True
    analysis_runs = _table(result, "analysis_runs")
    assert analysis_runs.rows.observed_rows == 25
    assert analysis_runs.rows.truncated is True
    assert analysis_runs.historical_rows is not None
    assert analysis_runs.historical_rows.observed_rows == 25
    assert analysis_runs.historical_rows.truncated is True
    assert result.retention is not None
    assert result.retention.observations_truncated is True


def test_storage_analysis_reports_provider_current_and_historical_counts(
    tmp_path: Path,
) -> None:
    database = _build_state(tmp_path / "state", hotspots=False)
    _add_provider_run(database)

    original = analyze_code_storage(database, run_limit=5, row_scan_limit=1_000)
    assert original.status == "ready"
    assert original.providers
    assert any(item.current_runs.observed_rows > 0 for item in original.providers)

    _add_completed_runs(database, count=1)
    advanced = analyze_code_storage(database, run_limit=5, row_scan_limit=1_000)

    assert advanced.status == "ready"
    assert tuple(item.provider_id for item in advanced.providers) == tuple(
        item.provider_id for item in original.providers
    )
    assert all(item.current_runs.observed_rows == 0 for item in advanced.providers)
    assert any(item.historical_runs.observed_rows > 0 for item in advanced.providers)


def test_missing_storage_owner_abstains_without_creating_any_artifact(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing" / "code.sqlite3"

    result = analyze_code_storage(database)

    assert result.status == "abstained"
    assert result.reason == "code_state_missing_or_not_regular"
    assert not database.exists()
    assert not database.parent.exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"run_limit": 0}, "run limit"),
        ({"run_limit": 51}, "run limit"),
        ({"row_scan_limit": 0}, "row scan limit"),
        ({"row_scan_limit": 1_000_001}, "row scan limit"),
        (
            {"run_limit": 2, "retain_latest_completed_runs": 3},
            "retention window",
        ),
    ],
)
def test_storage_analysis_rejects_unbounded_requests(
    tmp_path: Path,
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        analyze_code_storage(tmp_path / "code.sqlite3", **kwargs)
