"""Regression coverage for large, quiescent SQLite owners.

These tests use only temporary fixture state.  The sparse-file enlargement is
intentional: it exercises the real byte-size boundary without allocating a
multi-gigabyte database.  Residual WAL/SHM files are created as fixture input;
the tests never clean or alter a real NeoCortex state owner.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadMode,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION,
    StateResetChangedError,
    StateResetResult,
    StateResetError,
    execute_state_reset,
    plan_state_reset,
)
from neocortex.persistence import state_reset as state_reset_module
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.workflow.retention import planner as retention_module
from neocortex.workflow.retention.planner import RetentionPolicy, plan_retention
from tests.internal_paths_test_support import begin_signed_normal_run


_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LARGE_OWNER_BYTES = 256 * 1024 * 1024 + 1
_QUIESCENT_SHM_BYTES = 32_768


def _inflate_sparse(path: Path) -> None:
    """Make a real SQLite owner exceed the copy budget without writing pages."""

    with path.open("r+b") as stream:
        stream.truncate(_LARGE_OWNER_BYTES)
    assert path.stat().st_size == _LARGE_OWNER_BYTES


def _residual_quiescent_sidecars(path: Path) -> None:
    """Install the exact closed-owner WAL/SHM residual under test."""

    Path(f"{path}-wal").write_bytes(b"")
    Path(f"{path}-shm").write_bytes(b"\0" * _QUIESCENT_SHM_BYTES)
    assert Path(f"{path}-wal").stat().st_size == 0
    assert Path(f"{path}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


def _framework_owner(state: Path) -> Path:
    database = state / "framework.sqlite3"
    with FrameworkState(database):
        pass
    return database


def _semantic_owner(state: Path, *, large: bool = True) -> Path:
    database = state / "semantic.sqlite3"
    initialize_semantic_state(database)
    if large:
        _inflate_sparse(database)
    return database


@contextmanager
def _external_live_writer(database: Path) -> Iterator[subprocess.Popen[str]]:
    """Hold a real WAL writer in another process until the test releases it."""

    script = """
import pathlib
import sqlite3
import sys

database = pathlib.Path(sys.argv[1])
connection = sqlite3.connect(database, timeout=5.0)
try:
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('BEGIN IMMEDIATE')
    wal = pathlib.Path(str(database) + '-wal')
    shm = pathlib.Path(str(database) + '-shm')
    if wal.stat().st_size != 0 or shm.stat().st_size != 32768:
        raise RuntimeError('fixture did not create the canonical WAL/SHM layout')
    print('READY', flush=True)
    sys.stdin.read(1)
    connection.rollback()
finally:
    connection.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "READY":
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"live SQLite fixture failed to start: stdout={ready!r} {stdout!r}, stderr={stderr!r}"
        )
    try:
        assert process.poll() is None
        yield process
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.write("x")
            process.stdin.flush()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert process.returncode == 0, stderr


def _copy_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Count detached snapshot copies while preserving the real copy seam."""

    calls: list[tuple[Path, Path]] = []
    real_copy = sqlite_immutable._copy_regular_file

    def spy(
        source: Path,
        destination: Path,
        *,
        budget_state: object | None = None,
    ) -> None:
        calls.append((Path(source), Path(destination)))
        real_copy(source, destination, budget_state=budget_state)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", spy)
    return calls


def test_state_reset_all_large_quiescent_residual_generates_digest_without_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset planner must use immutable strict reads for this owner."""

    state = tmp_path / "state"
    state.mkdir()
    database = _framework_owner(state)
    _inflate_sparse(database)
    _residual_quiescent_sidecars(database)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")

    copies = _copy_spy(monkeypatch)
    assert preferred_sqlite_read_mode(database) is SQLiteReadMode.IMMUTABLE_STRICT

    plan = plan_state_reset(state, scope="all")

    assert _PLAN_DIGEST.fullmatch(plan.plan_digest)
    assert database.stat().st_size > 256 * 1024 * 1024
    assert copies == []
    assert {target.target_id for target in plan.targets} >= {
        "sqlite:framework",
        "runtime-cache",
    }
    assert all(entry.path.is_relative_to(state) for entry in plan.entries)
    assert Path(f"{database}-wal").stat().st_size == 0
    assert Path(f"{database}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


def test_retention_large_quiescent_owner_without_sidecars_uses_strict_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large sidecar-free owner also bypasses the temporary byte budget."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    assert not tuple(state.glob("semantic.sqlite3-*"))
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    plan = plan_retention(
        state,
        stores=("semantic",),
        now_ns=1_000,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    assert database.stat().st_size > 256 * 1024 * 1024
    assert plan.stores[0].status == "ready"
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert copies == []


def test_scope_all_preserves_recovery_evidence_while_resetting_regenerable_state(
    tmp_path: Path,
) -> None:
    """A preserved uncertain file action is not discarded or a global block."""

    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "source.fixture"
    source.write_text("fixture", encoding="utf-8")
    database = state / "framework.sqlite3"
    with FrameworkState(database) as framework:
        run_id = begin_signed_normal_run(framework, corpus)
        framework.fail_initial_run(run_id)
        action_id = framework.begin_file_action(
            run_id,
            "fixture",
            str(source),
            str(corpus / "target.fixture"),
            None,
            "fixture",
            True,
        )
        framework.require_file_action_recovery((action_id,), "fixture uncertain")

    plan = plan_state_reset(state, scope="all")
    assert plan.active_action_ids == ()
    assert plan.preserved_recovery_action_ids == (action_id,)
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=plan.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
    )
    assert isinstance(result, StateResetResult)
    assert result.status == "applied"
    with FrameworkState(database, existing_only=True) as framework:
        row = framework._connection.execute(
            "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
        ).fetchone()
    assert row is not None and row[0] == "recovery_required"


def test_catalog_stage_reset_keeps_published_rows_under_immutability_triggers(
    tmp_path: Path,
) -> None:
    """Broad reset retires cancelled Catalog generations without aborting."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO catalog_runs(catalog_run_id,source_kind,mode,status,started_ns) "
            "VALUES(1,'docx','fixture','completed',1)"
        )
        connection.execute(
            "INSERT INTO catalog_runs(catalog_run_id,source_kind,mode,status,started_ns) "
            "VALUES(2,'docx','fixture','cancelled',2)"
        )
        connection.execute(
            """INSERT INTO catalog_generations(
                generation_id,catalog_run_id,source_kind,status,started_ns,published_ns
            ) VALUES(1,NULL,'docx','published',1,3)"""
        )
        connection.execute(
            """INSERT INTO catalog_generations(
                generation_id,catalog_run_id,source_kind,status,started_ns
            ) VALUES(2,2,'docx','cancelled',2)"""
        )
        for generation_id, created_ns in ((1, 3), (2, 2)):
            connection.execute(
                """INSERT INTO catalog_generation_manifests(
                    generation_id,source_kind,source_fence_json,created_ns
                ) VALUES(?,?,?,?)""",
                (generation_id, "docx", "{}", created_ns),
            )
            connection.execute(
                """INSERT INTO catalog_generation_documents(
                    generation_id,source_kind,file_key,path,volume_id,file_id,size,
                    mtime_ns,birthtime_ns,source_status,processing_signature,
                    classifier_signature,primary_kind,confidence,uncertainty,standard_references_json,
                    organizations_json,topics_json,classification_json,catalog_status,
                    last_seen_catalog_run_id,updated_ns
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    "docx",
                    f"fixture-{generation_id}",
                    f"/fixture/{generation_id}.docx",
                    "v",
                    str(generation_id),
                    1,
                    1,
                    -1,
                    "complete",
                    "fixture",
                    "fixture",
                    "document",
                    1.0,
                    "baja",
                    "[]",
                    "[]",
                    "[]",
                    "{}",
                    "ready",
                    generation_id,
                    generation_id,
                ),
            )
        connection.execute(
            "INSERT INTO catalog_publications(source_kind,generation_id,published_ns) "
            "VALUES('docx',1,3)"
        )
        connection.commit()

    plan = plan_state_reset(state, scope="all")
    assert plan.staged_owners == ("catalog",)
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=plan.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
    )
    assert isinstance(result, StateResetResult)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM catalog_generations WHERE generation_id=1"
        ).fetchone() == ("published",)
        assert (
            connection.execute("SELECT 1 FROM catalog_generations WHERE generation_id=2").fetchone()
            is None
        )
        assert connection.execute(
            "SELECT generation_id FROM catalog_generation_documents ORDER BY generation_id"
        ).fetchall() == [(1,)]
        assert connection.execute(
            "SELECT generation_id FROM catalog_generation_manifests ORDER BY generation_id"
        ).fetchall() == [(1,)]


def test_catalog_unknown_nonempty_table_is_preserved_by_staged_reset(
    tmp_path: Path,
) -> None:
    """A schema extension cannot silently turn a Catalog owner into a delete target."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE future_payload(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_payload VALUES('preserve-me')")
        connection.execute("CREATE TABLE future_fts_payload(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_fts_payload VALUES('preserve-fts')")
        connection.commit()

    plan = plan_state_reset(state, scope="all")
    assert plan.staged_owners == ("catalog",)
    assert {
        "future_payload",
        "future_fts_payload",
    }.issubset(dict(plan.protected_tables)["catalog"])
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=plan.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
    )
    assert isinstance(result, StateResetResult)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM future_payload").fetchall() == [
            ("preserve-me",)
        ]
        assert connection.execute("SELECT value FROM future_fts_payload").fetchall() == [
            ("preserve-fts",)
        ]


@pytest.mark.parametrize("anomaly", ["missing-manifest", "ancestry-cycle"])
def test_catalog_published_evidence_anomaly_abstains_before_promotion(
    tmp_path: Path,
    anomaly: str,
) -> None:
    """Missing manifests and cyclic ancestry are not resettable evidence."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with sqlite3.connect(database) as connection:
        if anomaly == "missing-manifest":
            connection.execute(
                "INSERT INTO catalog_generations("
                "generation_id,catalog_run_id,source_kind,status,started_ns) "
                "VALUES(1,NULL,'docx','published',1)"
            )
        else:
            connection.execute(
                "INSERT INTO catalog_generations("
                "generation_id,catalog_run_id,source_kind,base_generation_id,status,started_ns) "
                "VALUES(1,NULL,'docx',2,'published',1)"
            )
            connection.execute(
                "INSERT INTO catalog_generations("
                "generation_id,catalog_run_id,source_kind,base_generation_id,status,started_ns) "
                "VALUES(2,NULL,'docx',1,'published',2)"
            )
            for generation_id in (1, 2):
                connection.execute(
                    "INSERT INTO catalog_generation_manifests("
                    "generation_id,source_kind,source_fence_json,created_ns) "
                    "VALUES(?,?,?,?)",
                    (generation_id, "docx", "{}", generation_id),
                )
        connection.commit()

    plan = plan_state_reset(state, scope="all")
    assert "catalog" in plan.staged_owners
    with pytest.raises(StateResetError, match="not reconcilable"):
        execute_state_reset(
            state,
            scope="all",
            apply=True,
            plan_digest=plan.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM catalog_generations").fetchone() == (
            1 if anomaly == "missing-manifest" else 2,
        )


def test_inventory_stage_reset_keeps_duplicate_evidence_and_compacts_owner(
    tmp_path: Path,
) -> None:
    """Broad reset clears inventory caches without discarding plan evidence."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "dedup.sqlite3"
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            INSERT INTO scans(
                scan_id,root,started_ns,completed_ns,status
            ) VALUES
                (1,'/fixture',1,2,'complete'),
                (2,'/regenerable',3,4,'complete');
            INSERT INTO files(
                scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            ) VALUES
                (1,'/fixture/kept',X'01',X'02',1,1,1),
                (2,'/regenerable/file',X'03',X'04',2,2,2);
            INSERT INTO fingerprints(
                volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest
            ) VALUES
                (X'01',X'02',1,1,1,'xxh3',X'05'),
                (X'03',X'04',2,2,2,'xxh3',X'06');
            INSERT INTO inventory_checkpoints(
                root,scan_id,valid,updated_ns
            ) VALUES('/fixture',1,1,5);
            INSERT INTO inventory_generation_heads(
                scan_id,content_digest,created_ns
            ) VALUES(2,X'07',6);
            INSERT INTO inventory_scan_successors(
                predecessor_scan_id,successor_scan_id,created_ns,reason
            ) VALUES(1,2,7,'fixture');
            INSERT INTO duplicate_plan_summaries(
                scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,
                verification_mode,requested_policy,coverage
            ) VALUES(1,1,1,1,8,'full_hash','exact','complete');
            INSERT INTO planned_duplicate_groups(
                group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,
                full_fingerprint,verification_mode
            ) VALUES(1,1,1,'/fixture/kept',1,1,'digest','full_hash');
            INSERT INTO planned_duplicate_members(
                group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            ) VALUES(1,0,'keep','/fixture/kept',X'01',X'02',1,1,1);
            INSERT INTO fingerprint_content_evidence(
                volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,content_digest
            ) VALUES(X'01',X'02',1,1,1,'xxh3',X'08');
            INSERT INTO duplicate_plan_heads(
                scan_id,inventory_content_digest,plan_digest,status,completed_ns
            ) VALUES(2,X'09',X'0a','superseded',9);
            """
        )
        connection.commit()

    plan = plan_state_reset(state, scope="all")
    assert plan.staged_owners == ("inventory",)
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=plan.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
    )
    assert isinstance(result, StateResetResult)
    assert "files" in result.cleared_tables
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT scan_id FROM duplicate_plan_summaries"
        ).fetchall() == [(1,)]
        assert connection.execute(
            "SELECT group_id FROM planned_duplicate_groups"
        ).fetchall() == [(1,)]
        assert connection.execute(
            "SELECT group_id FROM planned_duplicate_members"
        ).fetchall() == [(1,)]
        assert connection.execute(
            "SELECT content_digest FROM fingerprint_content_evidence"
        ).fetchall() == [(b"\x08",)]
        assert connection.execute("SELECT scan_id FROM scans").fetchall() == [(1,)]
        for table in (
            "files",
            "fingerprints",
            "inventory_checkpoints",
            "inventory_generation_heads",
            "inventory_scan_successors",
            "duplicate_plan_heads",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert database.stat().st_size < 2 * 1024 * 1024
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


@pytest.mark.parametrize("orphan_kind", ["summary", "member"])
def test_inventory_stage_reset_abstains_on_orphaned_protected_evidence(
    tmp_path: Path,
    orphan_kind: str,
) -> None:
    """Corrupt protected plan rows must not become durable reset survivors."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "dedup.sqlite3"
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scans(scan_id,root,started_ns,completed_ns,status) "
            "VALUES(1,'/fixture',1,2,'complete')"
        )
        connection.execute(
            "INSERT INTO duplicate_plan_summaries("
            "scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,"
            "verification_mode,requested_policy,coverage) "
            "VALUES(1,1,1,1,3,'full_hash','exact','complete')"
        )
        connection.execute(
            "INSERT INTO planned_duplicate_groups("
            "group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,"
            "full_fingerprint,verification_mode) "
            "VALUES(1,1,1,'/fixture/keep',1,1,'digest','full_hash')"
        )
        if orphan_kind == "summary":
            connection.execute(
                "INSERT INTO duplicate_plan_summaries("
                "scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,"
                "verification_mode,requested_policy,coverage) "
                "VALUES(99,0,0,0,4,'full_hash','exact','complete')"
            )
        else:
            connection.execute(
                "INSERT INTO planned_duplicate_members("
                "group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
                "VALUES(99,0,'keep','/fixture/orphan',X'01',X'02',1,1,1)"
            )
        connection.commit()

    plan = plan_state_reset(state, scope="all")
    with pytest.raises(StateResetError, match="orphan reference"):
        execute_state_reset(
            state,
            scope="all",
            apply=True,
            plan_digest=plan.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_summaries"
        ).fetchone() == (2 if orphan_kind == "summary" else 1,)


def test_inventory_promotion_rejects_new_empty_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar created during promotion is evidence, even when empty."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "dedup.sqlite3"
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scans(scan_id,root,started_ns,completed_ns,status) "
            "VALUES(1,'/fixture',1,2,'complete')"
        )
        connection.execute(
            "INSERT INTO duplicate_plan_summaries("
            "scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,"
            "verification_mode,requested_policy,coverage) "
            "VALUES(1,0,0,0,3,'full_hash','exact','complete')"
        )
        connection.commit()
    plan = plan_state_reset(state, scope="all")
    real_replace = state_reset_module.os.replace

    def replace_then_inject(source: str | Path, destination: str | Path) -> None:
        real_replace(source, destination)
        if Path(destination) == database:
            Path(f"{database}-wal").write_bytes(b"")

    monkeypatch.setattr(state_reset_module.os, "replace", replace_then_inject)
    try:
        with pytest.raises(
            StateResetError,
            match=r"state reset failed|incomplete sidecar|sidecar appeared",
        ):
            execute_state_reset(
                state,
                scope="all",
                apply=True,
                plan_digest=plan.plan_digest,
                confirmation=STATE_RESET_CONFIRMATION,
            )
    finally:
        Path(f"{database}-wal").unlink(missing_ok=True)


def test_inventory_promotion_rejects_changed_planned_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement writer cannot hide behind a reused sidecar pathname."""

    state = tmp_path / "state"
    state.mkdir()
    database = state / "dedup.sqlite3"
    initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scans(scan_id,root,started_ns,completed_ns,status) "
            "VALUES(1,'/fixture',1,2,'complete')"
        )
        connection.execute(
            "INSERT INTO duplicate_plan_summaries("
            "scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,"
            "verification_mode,requested_policy,coverage) "
            "VALUES(1,0,0,0,3,'full_hash','exact','complete')"
        )
        connection.commit()
    Path(f"{database}-wal").write_bytes(b"")
    Path(f"{database}-shm").write_bytes(b"\0" * _QUIESCENT_SHM_BYTES)
    plan = plan_state_reset(state, scope="all")
    real_replace = state_reset_module.os.replace

    def replace_then_mutate(source: str | Path, destination: str | Path) -> None:
        real_replace(source, destination)
        if Path(destination) == database:
            Path(f"{database}-wal").write_bytes(b"new-writer-content")

    monkeypatch.setattr(state_reset_module.os, "replace", replace_then_mutate)
    try:
        with pytest.raises(StateResetChangedError, match="sidecar changed"):
            execute_state_reset(
                state,
                scope="all",
                apply=True,
                plan_digest=plan.plan_digest,
                confirmation=STATE_RESET_CONFIRMATION,
            )
    finally:
        Path(f"{database}-wal").unlink(missing_ok=True)
        Path(f"{database}-shm").unlink(missing_ok=True)


def test_state_reset_rejects_writer_started_after_preview(
    tmp_path: Path,
) -> None:
    """SQLite owner guards close the preview-to-apply writer race."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state, large=False)
    plan = plan_state_reset(state, scope="all")
    assert "semantic" not in plan.staged_owners
    with _external_live_writer(database):
        with pytest.raises(StateResetError, match=r"writer lock|sidecars|digest"):
            execute_state_reset(
                state,
                scope="all",
                apply=True,
                plan_digest=plan.plan_digest,
                confirmation=STATE_RESET_CONFIRMATION,
            )
    assert database.exists()



def test_state_reset_rejects_external_rollback_writer_after_preview(
    tmp_path: Path,
) -> None:
    """An owner without sidecars is still protected from a live writer."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state, large=False)
    plan = plan_state_reset(state, scope="all")
    script = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
print('READY', flush=True)
sys.stdin.read(1)
connection.rollback()
connection.close()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "READY"
        with pytest.raises(StateResetError, match=r"writer lock|digest|sidecars"):
            execute_state_reset(
                state,
                scope="all",
                apply=True,
                plan_digest=plan.plan_digest,
                confirmation=STATE_RESET_CONFIRMATION,
            )
    finally:
        if writer.poll() is None:
            assert writer.stdin is not None
            writer.stdin.write("x")
            writer.stdin.flush()
        writer.wait(timeout=10)
        assert writer.returncode == 0
    assert database.exists()


def test_retention_large_quiescent_residual_uses_zero_copy_and_preserves_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention can inspect a large closed owner without a temp snapshot."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    _residual_quiescent_sidecars(database)
    before = capture_sqlite_read_fence(database)
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    plan = plan_retention(
        state,
        stores=("semantic",),
        now_ns=1_000,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    store = plan.stores[0]
    assert database.stat().st_size > 256 * 1024 * 1024
    assert store.status == "ready"
    assert store.database_bytes == _LARGE_OWNER_BYTES
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert copies == []
    assert capture_sqlite_read_fence(database) == before
    assert Path(f"{database}-wal").stat().st_size == 0
    assert Path(f"{database}-shm").stat().st_size == _QUIESCENT_SHM_BYTES


@pytest.mark.parametrize(
    "layout",
    ("live-writer", "wal-only", "unexpected-shm", "nonempty-wal", "rollback-journal"),
)
def test_retention_active_or_ambiguous_large_owner_stays_within_temp_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    """Unsafe ownership evidence remains fail-closed at the 256 MiB boundary."""

    state = tmp_path / "state"
    state.mkdir()
    database = _semantic_owner(state)
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    copies = _copy_spy(monkeypatch)

    if layout == "live-writer":
        with _external_live_writer(database):
            plan = plan_retention(
                state,
                stores=("semantic",),
                now_ns=1_000,
                policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
            )
    else:
        if layout == "wal-only":
            Path(f"{database}-wal").write_bytes(b"")
        elif layout == "unexpected-shm":
            Path(f"{database}-wal").write_bytes(b"")
            Path(f"{database}-shm").write_bytes(b"\0" * 16)
        elif layout == "nonempty-wal":
            Path(f"{database}-wal").write_bytes(b"active WAL")
        elif layout == "rollback-journal":
            Path(f"{database}-journal").write_bytes(b"active journal")
        else:  # pragma: no cover - parameter invariant
            raise AssertionError(layout)
        plan = plan_retention(
            state,
            stores=("semantic",),
            now_ns=1_000,
            policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
        )

    store = plan.stores[0]
    assert store.status == "blocked"
    assert "temporary bytes budget exhausted" in (store.detail or "")
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 0
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert copies == []


def test_cli_state_reset_all_preview_payload_binds_targets_to_fixture_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The canonical CLI emits a digest and only state-directory targets."""

    from neocortex.api.cli import human

    state = tmp_path / "state"
    state.mkdir()
    database = _framework_owner(state)
    _inflate_sparse(database)
    _residual_quiescent_sidecars(database)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")
    (state / "curation" / "checkpoints").mkdir(parents=True)
    (state / "curation" / "checkpoints" / "fixture.json").write_text("{}", encoding="utf-8")
    copies = _copy_spy(monkeypatch)

    exit_code = human.run_human_command(
        (
            "state",
            "reset",
            "--state-directory",
            str(state),
            "--scope",
            "all",
            "--json",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    payload = json.loads(output.out)
    assert payload["kind"] == "state-reset"
    assert payload["status"] == "preview"
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["scope"] == "all"
    assert result["mode"] == "preview"
    assert _PLAN_DIGEST.fullmatch(result["plan_digest"])
    assert f"--plan-digest {result['plan_digest']}" in result["apply_options"]
    assert "backup_directory" not in result
    target_ids = {target["target_id"] for target in result["targets"]}
    assert target_ids >= {
        "sqlite:framework",
        "runtime-cache",
        "curation-checkpoints",
    }
    for target in result["targets"]:
        for entry in target["entries"]:
            assert Path(entry["path"]).is_relative_to(state)
    assert copies == []
    assert database.stat().st_size > 256 * 1024 * 1024
