"""Ruff-only external evidence over protected Code publications."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_external_evidence as evidence_module
from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.cli_code import _read_code_status_snapshot
from _04_Nucleo_Operativo.code_contracts import CodeRouteConfig, CodeSearchQuery
from _04_Nucleo_Operativo.code_external_evidence import (
    RUFF_CONFIGURATION_SIGNATURE,
    ExternalEvidenceFile,
    RuffEvidenceProvider,
    current_external_status_from_row,
    external_status_digest_payload,
    external_status_from_row,
)
from _04_Nucleo_Operativo.code_publication_diff import compare_code_publications
from _04_Nucleo_Operativo.code_route import CodeRoute
from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    readonly_code_database,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_search import search_code
from _04_Nucleo_Operativo.code_state import CodeState
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


class _Inventory:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths

    def snapshots(self, _scan_id: int):
        for path in self.paths:
            observed = path.stat()
            yield FileSnapshot(
                str(path),
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
            )


class _FrameworkState:
    def begin_route_phase(self, *_args, **_kwargs) -> None:
        return None

    def complete_route_phase(self, *_args, **_kwargs) -> None:
        return None

    def fail_route_phase(self, *_args, **_kwargs) -> None:
        return None


def _source_tree(root: Path, *, count: int = 20) -> tuple[Path, ...]:
    root.mkdir(parents=True)
    paths = []
    for index in range(count):
        path = root / f"module_{index:02d}.py"
        source = "import os\n" if index == 0 else f"value_{index} = {index}\n"
        path.write_text(source, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _route(
    root: Path,
    state: Path,
    paths: tuple[Path, ...],
    *,
    run_id: int,
) -> object:
    return CodeRoute(
        CodeRouteConfig(
            state_path=state / "code.sqlite3",
            dedup_path=state / "dedup.sqlite3",
            max_file_bytes=1024 * 1024,
            max_text_chars=100_000,
            chunk_chars=1024,
            include_generated=False,
            include_vendored=False,
            external_evidence_root=root,
        ),
        _Inventory(paths),
        _FrameworkState(),
        run_id,
        run_id,
    ).run()


def _latest_external(database: Path) -> dict[str, object]:
    with readonly_code_database(database) as connection:
        row = connection.execute(
            """SELECT tool_run_id,analysis_run_id,tool_version,
            configuration_signature,status,provenance_json
            FROM external_tool_runs ORDER BY tool_run_id DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        return external_status_from_row(row).as_payload()


def _latest_external_row(database: Path) -> dict[str, object]:
    with readonly_code_database(database) as connection:
        row = connection.execute(
            """SELECT tool_run_id,analysis_run_id,tool_version,
            configuration_signature,status,provenance_json
            FROM external_tool_runs ORDER BY tool_run_id DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        return dict(row)


def test_ruff_publication_is_visible_and_exact_replay_skips_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root)
    original = {path: path.read_bytes() for path in paths}

    first = _route(root, state, paths, run_id=1)

    assert first.external_tool_runs == 1
    assert first.external_diagnostics == 1
    assert first.external_cache_hits == 0
    assert first.external_errors == 0
    first_status = _latest_external(state / "code.sqlite3")
    assert first_status["status"] == "ready"
    assert first_status["tool_status"] == "completed"
    assert first_status["execution"] == "full"
    assert first_status["eligible_files"] == 20
    assert first_status["diagnostics"] == 1
    assert first_status["gate"] == "baseline"
    first_row = _latest_external_row(state / "code.sqlite3")
    provenance = json.loads(str(first_row["provenance_json"]))
    assert "batches" not in provenance["command"]
    assert (
        provenance["configuration"]["max_provenance_bytes"]
        == evidence_module.RUFF_MAX_PROVENANCE_BYTES
    )
    assert (
        len(str(first_row["provenance_json"]).encode("utf-8"))
        <= evidence_module.RUFF_MAX_PROVENANCE_BYTES
    )
    findings = search_code(
        state / "code.sqlite3",
        CodeSearchQuery(diagnostic="F401", limit=5),
    )
    assert findings
    assert findings[0].path.endswith("module_00.py")
    assert not (root / ".ruff_cache").exists()
    assert {path: path.read_bytes() for path in paths} == original

    second = _route(root, state, paths, run_id=2)

    assert second.cache_hits == 20
    assert second.external_tool_runs == 1
    assert second.external_diagnostics == 1
    assert second.external_cache_hits == 1
    second_status = _latest_external(state / "code.sqlite3")
    assert second_status["status"] == "ready"
    assert second_status["tool_status"] == "skipped"
    assert second_status["execution"] == "cache_replay"
    assert second_status["effective_tool_run_id"] == first_status["tool_run_id"]
    assert second_status["added"] == 0
    assert second_status["resolved"] == 0
    assert second_status["gate"] == "passed"
    with readonly_code_database(state / "code.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM external_tool_runs").fetchone()[0] == 4
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM external_run_contracts
                WHERE provider_id='ruff-protected-basic'"""
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM diagnostics WHERE source='external:ruff'"
            ).fetchone()[0]
            == 1
        )
    assert not (root / ".ruff_cache").exists()
    assert {path: path.read_bytes() for path in paths} == original

    third = _route(root, state, paths, run_id=3)
    assert third.external_cache_hits == 1
    third_status = _latest_external(state / "code.sqlite3")
    assert third_status["execution"] == "cache_replay"
    assert third_status["effective_tool_run_id"] == first_status["tool_run_id"]
    replay_snapshot = _read_code_status_snapshot(state / "code.sqlite3")
    assert replay_snapshot.external_evidence["status"] == "ready"


def test_status_consumes_the_latest_ruff_publication(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root)
    _route(root, state, paths, run_id=1)

    status = _read_code_status_snapshot(state / "code.sqlite3")

    assert status.external_evidence["status"] == "ready"
    assert status.external_evidence["diagnostics"] == 1
    assert status.counts["current_external_diagnostics"] == 1
    assert status.external_evidence["mutation_authority"] is False


def test_status_preserves_path_case_when_verifying_the_projection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "RootUpper"
    source = root / "PackageUpper" / "ModuleUpper.py"
    source.parent.mkdir(parents=True)
    source.write_text("import os\n", encoding="utf-8")
    state = tmp_path / "state"

    _route(root, state, (source,), run_id=1)

    status = _read_code_status_snapshot(state / "code.sqlite3")
    assert status.external_evidence["status"] == "ready"
    assert status.external_evidence["covered_files"] == 1


def test_unicode_paths_share_one_deterministic_input_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    paths = (root / "ß.py", root / "t.py")
    for index, path in enumerate(paths):
        path.write_text(f"value = {index}\n", encoding="utf-8")
    state = tmp_path / "state"

    _route(root, state, paths, run_id=1)
    first = _read_code_status_snapshot(state / "code.sqlite3")
    replay = _route(root, state, paths, run_id=2)
    second = _read_code_status_snapshot(state / "code.sqlite3")

    assert first.external_evidence["status"] == "ready"
    assert second.external_evidence["status"] == "ready"
    assert second.external_evidence["result_digest"] == first.external_evidence["result_digest"]
    assert replay.external_cache_hits == 1


def test_ruff_covers_python_with_partial_internal_parse_status(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root)
    broken = paths[-1]
    broken.write_text("def broken(:\n", encoding="utf-8")

    _route(root, state, paths, run_id=1)

    status = _read_code_status_snapshot(state / "code.sqlite3")
    assert status.external_evidence["status"] == "ready"
    assert status.external_evidence["eligible_files"] == len(paths)
    assert status.external_evidence["covered_files"] == len(paths)
    with readonly_code_database(state / "code.sqlite3") as connection:
        row = connection.execute(
            """SELECT v.analysis_status FROM files f JOIN file_versions v
            ON v.version_id=f.current_version_id WHERE f.current_path=?""",
            (str(broken),),
        ).fetchone()
        assert row is not None
        assert row["analysis_status"] == "partial"
        external = connection.execute(
            """SELECT COUNT(*) FROM diagnostics d JOIN file_versions v
            ON v.version_id=d.version_id JOIN files f
            ON f.current_version_id=v.version_id
            WHERE f.current_path=? AND d.source='external:ruff'""",
            (str(broken),),
        ).fetchone()
        assert external is not None
        assert external[0] >= 1


def test_current_status_abstains_for_stale_configuration_or_tool(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root)
    _route(root, state, paths, run_id=1)
    row = _latest_external_row(state / "code.sqlite3")

    assert current_external_status_from_row(row).status == "ready"
    stale_configuration = {**row, "configuration_signature": "obsolete"}
    stale_tool = {**row, "tool_version": "0.0.0"}

    assert (
        current_external_status_from_row(stale_configuration).reason
        == "external_configuration_stale"
    )
    assert current_external_status_from_row(stale_tool).reason == "external_tool_version_stale"


def test_oversized_provenance_abstains_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: dict[str, object] = {
        "tool_run_id": 1,
        "analysis_run_id": 1,
        "tool_version": RuffEvidenceProvider().tool_version() or "unavailable",
        "configuration_signature": RUFF_CONFIGURATION_SIGNATURE,
        "status": "completed",
        "provenance_json": "x" * (evidence_module.RUFF_MAX_PROVENANCE_BYTES + 1),
    }

    def forbidden_loads(_value):
        raise AssertionError("oversized provenance must not be parsed")

    monkeypatch.setattr(evidence_module.json, "loads", forbidden_loads)

    status = external_status_from_row(row)

    assert status.status == "abstained"
    assert status.reason == "external_evidence_provenance_invalid"


@pytest.mark.parametrize("tamper", ["digest", "count", "record", "root"])
def test_status_rejects_tampered_external_provenance(
    tmp_path: Path,
    tamper: str,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    _route(root, state, _source_tree(root), run_id=1)
    row = _latest_external_row(state / "code.sqlite3")
    provenance = json.loads(str(row["provenance_json"]))
    if tamper == "digest":
        provenance["result"]["digest"] = "external-result-v1:xxh3_128:tampered"
    elif tamper == "count":
        provenance["result"]["diagnostics"] += 1
    elif tamper == "record":
        provenance["result"]["records"][0]["message"] = "tampered"
    else:
        provenance["root"] = "not-an-absolute-root"
    tampered_row = {
        **row,
        "provenance_json": json.dumps(
            provenance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    status = external_status_from_row(tampered_row)

    assert status.status == "abstained"
    assert status.reason == "external_evidence_provenance_invalid"


def test_status_abstains_when_the_current_projection_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    database = state / "code.sqlite3"
    _route(root, state, _source_tree(root), run_id=1)
    with CodeState(database) as owner:
        owner.connection.execute("DELETE FROM diagnostics WHERE source='external:ruff'")
        owner.connection.commit()
        checkpoint_code_wal(owner.connection)
    remove_checkpointed_code_sidecars(database)

    snapshot = _read_code_status_snapshot(database)

    assert snapshot.external_evidence["status"] == "abstained"
    assert snapshot.external_evidence["reason"] == "external_projection_mismatch"


def test_status_rejects_projection_metadata_not_matching_signed_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    database = state / "code.sqlite3"
    _route(root, state, _source_tree(root), run_id=1)
    with CodeState(database) as owner:
        row = owner.connection.execute(
            """SELECT diagnostic_id,metadata_json FROM diagnostics
            WHERE source='external:ruff' ORDER BY diagnostic_id LIMIT 1"""
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row["metadata_json"]))
        metadata["url"] = "https://invalid.example/tampered"
        owner.connection.execute(
            "UPDATE diagnostics SET metadata_json=? WHERE diagnostic_id=?",
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                int(row["diagnostic_id"]),
            ),
        )
        owner.connection.commit()
        checkpoint_code_wal(owner.connection)
    remove_checkpointed_code_sidecars(database)

    snapshot = _read_code_status_snapshot(database)

    assert snapshot.external_evidence["status"] == "abstained"
    assert snapshot.external_evidence["reason"] == "external_projection_mismatch"


def test_exact_replay_repairs_a_tampered_projection(tmp_path: Path) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    database = state / "code.sqlite3"
    paths = _source_tree(root)
    _route(root, state, paths, run_id=1)
    with CodeState(database) as owner:
        updated = owner.connection.execute(
            """UPDATE diagnostics SET message='tampered'
            WHERE source='external:ruff'"""
        )
        assert updated.rowcount == 1
        owner.connection.commit()
        checkpoint_code_wal(owner.connection)
    remove_checkpointed_code_sidecars(database)
    corrupted = _read_code_status_snapshot(database)
    assert corrupted.external_evidence["status"] == "abstained"
    assert corrupted.external_evidence["reason"] == "external_projection_mismatch"

    repaired = _route(root, state, paths, run_id=2)

    assert repaired.cache_hits == 20
    assert repaired.external_cache_hits == 0
    assert repaired.external_errors == 0
    status = _latest_external(database)
    assert status["status"] == "ready"
    assert status["execution"] == "full"
    assert status["gate"] == "passed"
    with readonly_code_database(database) as connection:
        messages = connection.execute(
            """SELECT message FROM diagnostics
            WHERE source='external:ruff' ORDER BY diagnostic_id"""
        ).fetchall()
        assert [str(row["message"]) for row in messages] != ["tampered"]


def test_status_abstains_when_projection_path_cannot_be_relativized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    database = state / "code.sqlite3"
    _route(root, state, _source_tree(root), run_id=1)
    with readonly_code_database(database) as connection:
        current_inputs = evidence_module.read_external_evidence_files(connection, root)

    def fail_relpath(_path: str, _start: str) -> str:
        raise ValueError("incompatible path roots")

    monkeypatch.setattr(
        evidence_module,
        "read_external_evidence_files",
        lambda _connection, _root: current_inputs,
    )
    monkeypatch.setattr(evidence_module.os.path, "relpath", fail_relpath)

    snapshot = _read_code_status_snapshot(database)

    assert snapshot.external_evidence["status"] == "abstained"
    assert snapshot.external_evidence["reason"] == "external_projection_mismatch"


def test_comparable_baseline_is_scoped_to_the_authorized_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _route(first_root, state, _source_tree(first_root), run_id=1)
    second = _route(second_root, state, _source_tree(second_root), run_id=2)

    latest = _latest_external(state / "code.sqlite3")
    assert second.external_cache_hits == 0
    assert latest["gate"] == "baseline"
    assert latest["comparable"] is False


def test_external_input_query_fails_before_unbounded_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root, count=2)
    _route(root, state, paths, run_id=1)

    with CodeState(state / "code.sqlite3") as owner:
        monkeypatch.setattr(evidence_module, "RUFF_MAX_FILES", 1)
        with pytest.raises(ValueError, match="external evidence exceeds 1 files"):
            owner.external_evidence_files(root)


def test_external_input_baseline_match_requires_signature_versions_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    current = _file_record(_source_tree(root, count=1)[0], root)
    inputs = (current,)
    baseline = evidence_module.ExternalEvidenceBaseline(
        tool_run_id=1,
        analysis_run_id=1,
        tool_version="fixture",
        configuration_signature=RUFF_CONFIGURATION_SIGNATURE,
        root=str(root),
        input_signature=evidence_module.external_input_signature(inputs),
        version_ids=(current.version_id,),
        result_digest=evidence_module._external_result_digest(()),
        diagnostic_ids=(),
        records=(),
    )
    monkeypatch.setattr(
        evidence_module,
        "read_external_evidence_files",
        lambda _connection, _root: inputs,
    )

    with sqlite3.connect(":memory:") as connection:
        assert evidence_module._external_inputs_match_baseline(
            connection,
            baseline,
            eligible_files=1,
        )
        assert not evidence_module._external_inputs_match_baseline(
            connection,
            baseline,
            eligible_files=2,
        )

        stale_inputs = (replace(current, version_id=current.version_id + 1),)
        monkeypatch.setattr(
            evidence_module,
            "read_external_evidence_files",
            lambda _connection, _root: stale_inputs,
        )
        assert not evidence_module._external_inputs_match_baseline(
            connection,
            baseline,
            eligible_files=1,
        )

        def unavailable_inputs(
            _connection: sqlite3.Connection,
            _root: Path,
        ) -> tuple[ExternalEvidenceFile, ...]:
            raise ValueError("injected input projection failure")

        monkeypatch.setattr(
            evidence_module,
            "read_external_evidence_files",
            unavailable_inputs,
        )
        assert not evidence_module._external_inputs_match_baseline(
            connection,
            baseline,
            eligible_files=1,
        )


def test_external_projection_record_requires_exact_owner_and_identity(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = _source_tree(root, count=1)[0]
    relative_path = source.relative_to(root).as_posix()
    identity = evidence_module._external_diagnostic_identity(
        relative_path,
        "F401",
        "unused import",
        1,
        0,
        1,
        4,
    )
    diagnostic = evidence_module.ExternalDiagnostic(
        1,
        relative_path,
        "F401",
        "unused import",
        1,
        0,
        1,
        4,
        None,
        False,
        identity,
    )
    baseline = evidence_module.ExternalEvidenceBaseline(
        7,
        1,
        "fixture",
        RUFF_CONFIGURATION_SIGNATURE,
        str(root),
        "fixture-input",
        (1,),
        evidence_module._external_result_digest((diagnostic,)),
        (identity,),
        (diagnostic,),
    )
    expectation = evidence_module._ExternalEvidenceExpectation(7, (diagnostic,))
    metadata = {
        "schema": "neocortex.external-diagnostic/v1",
        "external_tool_run_id": 7,
        "external_diagnostic_identity": identity,
        "relative_path": relative_path,
        "claim_scope": "tool_reported",
        "authority": "advisory",
        "mutation_authority": False,
        "fix_available": False,
        "url": None,
    }
    normalized_root = evidence_module._normalized_absolute_path(str(root))
    assert normalized_root is not None

    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row

        def projection_row(owner: int | bool) -> sqlite3.Row:
            observed_metadata = {**metadata, "external_tool_run_id": owner}
            row = connection.execute(
                """SELECT 1 AS version_id,'F401' AS code,'unused import' AS message,
                1 AS start_line,0 AS start_column,1 AS end_line,4 AS end_column,
                ? AS metadata_json,'ruff' AS tool_name,'fixture' AS tool_version,
                'warning' AS severity,1 AS confirmed,1.0 AS confidence,
                ? AS current_path""",
                (json.dumps(observed_metadata), str(source)),
            ).fetchone()
            assert row is not None
            return row

        assert evidence_module._external_projection_record(
            projection_row(7),
            baseline,
            expectation,
            normalized_root=normalized_root,
        ) == (identity, diagnostic)
        assert (
            evidence_module._external_projection_record(
                projection_row(True),
                baseline,
                expectation,
                normalized_root=normalized_root,
            )
            is None
        )


def test_input_projection_limit_abstains_without_hiding_completed_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    paths = _source_tree(root, count=2)

    def reject_unbounded_projection(
        _owner: CodeState,
        _root: Path,
    ) -> tuple[ExternalEvidenceFile, ...]:
        raise ValueError("external evidence exceeds 1 files")

    monkeypatch.setattr(
        CodeState,
        "external_evidence_files",
        reject_unbounded_projection,
    )

    summary = _route(root, state, paths, run_id=1)

    assert summary.processed == 2
    assert summary.errors == 0
    assert summary.external_tool_runs == 1
    assert summary.external_diagnostics == 0
    assert summary.external_errors == 1
    external = _latest_external(state / "code.sqlite3")
    assert external["status"] == "abstained"
    assert external["tool_status"] == "failed"
    assert external["reason"] == "input_projection_failed"
    with readonly_code_database(state / "code.sqlite3") as connection:
        owner = connection.execute(
            "SELECT status,processed,errors FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"
        ).fetchone()
        assert owner is not None
        assert dict(owner) == {"status": "completed", "processed": 2, "errors": 0}


def test_result_digest_is_portable_across_local_version_ids(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _source_tree(root, count=1)[0]
    first_file = _file_record(path, root)
    second_file = replace(first_file, version_id=999)
    provider = RuffEvidenceProvider()

    first = provider.run(root, (first_file,), baseline=None)
    second = provider.run(root, (second_file,), baseline=None)

    assert first.status == second.status == "completed"
    assert first.provenance["result"]["digest"] == second.provenance["result"]["digest"]


def test_digest_projection_excludes_local_ids_but_includes_the_gate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = tmp_path / "state"
    _route(root, state, _source_tree(root), run_id=1)
    baseline = external_status_from_row(_latest_external_row(state / "code.sqlite3"))
    local_replay = replace(
        baseline,
        tool_run_id=999,
        effective_tool_run_id=1,
        input_signature="database-local-input",
        execution="cache_replay",
    )
    failed_gate = replace(baseline, comparable=True, added=1, gate="failed")

    assert external_status_digest_payload(baseline) == external_status_digest_payload(local_replay)
    assert external_status_digest_payload(baseline) != external_status_digest_payload(failed_gate)
    assert baseline.configuration_signature == RUFF_CONFIGURATION_SIGNATURE


def _file_record(path: Path, root: Path) -> ExternalEvidenceFile:
    raw = path.read_bytes()
    observed = path.stat()
    fingerprint = fingerprint_bytes(raw)
    return ExternalEvidenceFile(
        1,
        str(path),
        path.relative_to(root).as_posix(),
        len(raw),
        observed.st_mtime_ns,
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
    )


def test_ruff_rejects_unowned_output_without_partial_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.py"
    source.write_text("import os\n", encoding="utf-8")
    record = _file_record(source, root)
    payload = json.dumps(
        [
            {
                "code": "F401",
                "filename": str(tmp_path / "outside.py"),
                "fix": None,
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
                "message": "unused import",
                "url": None,
            }
        ]
    ).encode("utf-8")

    def fake_capture(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 1, payload, b"")

    monkeypatch.setattr(evidence_module, "run_bounded_capture", fake_capture)

    publication = RuffEvidenceProvider().run(root, (record,), baseline=None)

    assert publication.status == "failed"
    assert publication.diagnostics == ()
    assert publication.provenance["error"]["reason"] == "result_validation_failed"


def test_ruff_input_fingerprint_change_fails_closed_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    record = _file_record(source, root)
    source.write_text("value = 2\n", encoding="utf-8")
    invoked = False

    def fake_capture(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("Ruff must not start for stale input")

    monkeypatch.setattr(evidence_module, "run_bounded_capture", fake_capture)

    publication = RuffEvidenceProvider().run(root, (record,), baseline=None)

    assert publication.status == "failed"
    assert publication.provenance["error"]["reason"] == "input_validation_failed"
    assert not invoked


def test_ruff_reads_a_verified_staged_copy_instead_of_the_owner_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    record = _file_record(source, root)
    observed_stage: Path | None = None

    def fake_capture(arguments, **kwargs):
        nonlocal observed_stage
        observed_stage = Path(arguments[-1])
        assert observed_stage != source
        assert observed_stage.read_bytes() == source.read_bytes()
        assert Path(kwargs["cwd"]) != root
        return subprocess.CompletedProcess(arguments, 0, b"[]", b"")

    monkeypatch.setattr(evidence_module, "run_bounded_capture", fake_capture)

    publication = RuffEvidenceProvider().run(root, (record,), baseline=None)

    assert publication.status == "completed"
    assert observed_stage is not None
    assert not observed_stage.exists()


def test_staging_never_uses_a_temp_directory_inside_the_owner_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.py"
    source.write_text("import os\n", encoding="utf-8")
    record = _file_record(source, root)
    monkeypatch.setattr(evidence_module.tempfile, "tempdir", str(root))

    unsafe = RuffEvidenceProvider().run(root, (record,), baseline=None)

    assert unsafe.status == "failed"
    assert unsafe.provenance["error"]["reason"] == "unsafe_staging_root"
    assert not tuple(root.glob("neocortex-ruff-*"))

    state = tmp_path / "state"
    _route(root, state, (source,), run_id=1)
    status = _read_code_status_snapshot(state / "code.sqlite3")

    assert status.external_evidence["status"] == "ready"
    assert not tuple(root.glob("neocortex-ruff-*"))


def test_publication_diff_reports_new_ruff_diagnostics_as_a_failed_gate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    paths = _source_tree(root)
    baseline_state = tmp_path / "baseline"
    current_state = tmp_path / "current"
    _route(root, baseline_state, paths, run_id=1)
    paths[1].write_text("import sys\nvalue_1 = 1\n", encoding="utf-8")
    _route(root, current_state, paths, run_id=1)

    result = compare_code_publications(baseline_state, current_state)

    assert result.status == "ready"
    assert result.external_evidence is not None
    assert result.external_evidence.status == "ready"
    assert result.external_evidence.common == 1
    assert result.external_evidence.added == 1
    assert result.external_evidence.resolved == 0
    assert result.external_evidence.gate == "failed"


def test_external_environment_drops_ruff_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUFF_CACHE_DIR", "untrusted-cache")
    monkeypatch.setenv("RUFF_OUTPUT_FORMAT", "text")

    environment = evidence_module._controlled_environment()

    assert not any(key.startswith("RUFF_") for key in environment)
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
