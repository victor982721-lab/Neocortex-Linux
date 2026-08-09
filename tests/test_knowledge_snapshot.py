"""Cross-owner logical snapshots use only bounded read-only observations."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_snapshot.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import _04_Nucleo_Operativo.knowledge_snapshot as knowledge_snapshot
from _02_Deduplicacion import inventory_schema as inventory_schema_module
from _02_Deduplicacion.inventory_schema import initialize_inventory_schema
from _04_Nucleo_Operativo import framework_schema as framework_schema_module
from _04_Nucleo_Operativo.archive_state import initialize_archive_state
from _04_Nucleo_Operativo.code_schema import initialize_code_state
from _04_Nucleo_Operativo.document_catalog import initialize_document_catalog
from _04_Nucleo_Operativo.knowledge_contracts import (
    OwnerAvailability,
    SnapshotConsistency,
)
from _04_Nucleo_Operativo.knowledge_snapshot import (
    KnowledgeStatePaths,
    KnowledgeStateRootError,
    collect_knowledge_snapshot,
)
from _04_Nucleo_Operativo.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from _04_Nucleo_Operativo.semantic_state import (
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)
from _04_Nucleo_Operativo.text_state import initialize_text_state
# endregion [01]

# region [02] Implementación


def _owner(snapshot, name: str):
    return next(owner for owner in snapshot.owners if owner.owner == name)


def _published_fixture(state: Path) -> None:
    state.mkdir()

    inventory = state / "dedup.sqlite3"
    initialize_inventory_schema(inventory)
    with sqlite3.connect(inventory) as connection:
        connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            completed_ns,files_seen,directories_seen,bytes_seen,skipped_links,
            excluded_directories,errors,status)
            VALUES('C:/Corpus',NULL,NULL,NULL,1,2,0,0,0,0,0,0,'complete')"""
        )
        scan_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES('C:/Corpus',?,'volume','journal',7,1,8)""",
            (scan_id,),
        )

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """INSERT INTO catalog_generations(
            source_kind,status,started_ns,completed_ns,published_ns)
            VALUES('pdf','published',1,2,3)"""
        )
        generation = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """INSERT INTO catalog_publications(source_kind,generation_id,published_ns)
            VALUES('pdf',?,3)""",
            (generation,),
        )

    semantic = state / "semantic.sqlite3"
    initialize_semantic_state(semantic)
    model = EmbeddingModelSpec(
        "snapshot-model-v1",
        "snapshot-space-v1",
        EmbeddingModality.TEXT,
        "fixture/snapshot",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    register_embedding_model(semantic, model, allow_test_provider=True)
    with semantic_database(semantic) as connection:
        connection.execute(
            """INSERT INTO embedding_generations(
            model_signature,processing_signature,status,provenance_json,cursor_json,
            started_ns,completed_ns,pending_count,leased_count,done_count,
            error_count,stale_count,base_generation_id,base_clone_complete)
            VALUES(?,?,'ready','{}','{}',1,2,0,0,0,0,0,NULL,1)""",
            (model.model_signature, "fixture-generation"),
        )
        generation = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """INSERT INTO published_embedding_heads(
            model_signature,generation_id,published_ns) VALUES(?,?,3)""",
            (model.model_signature, generation),
        )

    initialize_code_state(state / "code.sqlite3")


def _legacy_read_compatible_fixture(state: Path) -> None:
    state.mkdir()
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        inventory_schema_module._build_v7_schema(connection)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','7')")
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        framework_schema_module._build_v19_exact_schema(connection)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','19')")


def _set_duplicate_plan_summary(
    inventory: Path,
    *,
    completed_ns: int | None,
    group_count: int = 0,
    redundant_files: int = 0,
    reclaimable_bytes: int = 0,
) -> None:
    with sqlite3.connect(inventory) as connection:
        scan_id = int(
            connection.execute(
                "SELECT scan_id FROM inventory_checkpoints WHERE root='C:/Corpus'"
            ).fetchone()[0]
        )
        if completed_ns is None:
            connection.execute("DELETE FROM duplicate_plan_summaries WHERE scan_id=?", (scan_id,))
            return
        connection.execute(
            """INSERT OR REPLACE INTO duplicate_plan_summaries(
            scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns)
            VALUES(?,?,?,?,?)""",
            (
                scan_id,
                group_count,
                redundant_files,
                reclaimable_bytes,
                completed_ns,
            ),
        )


def test_public_snapshot_allows_missing_root_without_creation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "missing-state"

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert all(owner.state is OwnerAvailability.ABSENT for owner in snapshot.owners)
    assert not state.exists()


def test_snapshot_cancels_between_absent_owners_without_creating_state(
    tmp_path: Path,
) -> None:
    class Cancelled(RuntimeError):
        pass

    state = tmp_path / "missing-state"
    checkpoints = 0

    def cancel_between_owners() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 6:
            raise Cancelled("cancel between owners")

    with pytest.raises(Cancelled, match="between owners"):
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
            cancellation_check=cancel_between_owners,
        )

    assert checkpoints == 6
    assert not state.exists()


def test_snapshot_cancellation_rolls_back_and_closes_readonly_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(RuntimeError):
        pass

    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    opened: list[sqlite3.Connection] = []
    real_connect_readonly = knowledge_snapshot._connect_readonly
    armed = False

    def track_readonly_connection(path: Path) -> sqlite3.Connection:
        connection = real_connect_readonly(path)
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        knowledge_snapshot,
        "_connect_readonly",
        track_readonly_connection,
    )

    def arm_after_first_observation(owner: str, attempt: int) -> None:
        nonlocal armed
        if owner == "inventory" and attempt == 1:
            armed = True

    def cancel_second_observation() -> None:
        if armed:
            raise Cancelled("cancel inside second read transaction")

    with pytest.raises(Cancelled, match="second read transaction"):
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
            cancellation_check=cancel_second_observation,
            _between_observations=arm_after_first_observation,
        )

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    with sqlite3.connect(inventory, timeout=1) as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 0
        connection.execute("BEGIN IMMEDIATE")
        assert connection.in_transaction
        connection.execute("ROLLBACK")
    assert not inventory.with_name(f"{inventory.name}-journal").exists()


@pytest.mark.parametrize("cancel_on_observation", (1, 2))
def test_snapshot_sqlite_progress_interrupts_long_owner_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_on_observation: int,
) -> None:
    class QueryCancelled(RuntimeError):
        pass

    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    code_before = code.read_bytes()
    original_observation = knowledge_snapshot._logical_observation
    real_connect_readonly = knowledge_snapshot._connect_readonly
    opened: list[sqlite3.Connection] = []
    entered_long_query = False
    query_completed = False
    observation_calls = 0
    progress_calls = 0
    cancellation = QueryCancelled("cancel inside snapshot SQLite query")

    def track_readonly_connection(path: Path) -> sqlite3.Connection:
        connection = real_connect_readonly(path)
        opened.append(connection)
        return connection

    def long_observation(connection: sqlite3.Connection, spec: Any) -> Any:
        nonlocal entered_long_query, observation_calls, query_completed
        if spec.owner == "code":
            observation_calls += 1
        if spec.owner == "code" and observation_calls == cancel_on_observation:
            entered_long_query = True
            connection.execute(
                """WITH RECURSIVE sequence(value) AS (
                SELECT 1
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value < 100000
                )
                SELECT SUM(value) FROM sequence"""
            ).fetchone()
            query_completed = True
        return original_observation(connection, spec)

    def cancel_long_query() -> None:
        nonlocal progress_calls
        if not entered_long_query:
            return
        progress_calls += 1
        if progress_calls == 1:
            raise cancellation

    monkeypatch.setattr(
        knowledge_snapshot,
        "_connect_readonly",
        track_readonly_connection,
    )
    monkeypatch.setattr(
        knowledge_snapshot,
        "_logical_observation",
        long_observation,
    )

    with pytest.raises(QueryCancelled) as raised:
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
            cancellation_check=cancel_long_query,
        )

    assert raised.value is cancellation
    cause = raised.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError)
    assert getattr(cause, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT
    assert entered_long_query
    assert not query_completed
    assert observation_calls == cancel_on_observation
    assert progress_calls == 1
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    with sqlite3.connect(code, timeout=1) as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 0
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    assert code.read_bytes() == code_before
    assert not code.with_name(f"{code.name}-journal").exists()


def test_state_paths_reject_existing_root_with_missing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = (tmp_path / "broken-root").absolute()
    paths = KnowledgeStatePaths.from_directory(state)
    real_lstat = knowledge_snapshot.os.lstat
    real_stat = knowledge_snapshot.os.stat

    def report_existing(
        path: Any,
        *args: Any,
        **kwargs: Any,
    ) -> knowledge_snapshot.os.stat_result:
        if Path(path) == state:
            return real_lstat(tmp_path)
        return real_lstat(path, *args, **kwargs)

    def report_missing_target(
        path: Any,
        *args: Any,
        **kwargs: Any,
    ) -> knowledge_snapshot.os.stat_result:
        if Path(path) == state:
            raise FileNotFoundError(2, "target not found", str(state))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(knowledge_snapshot.os, "lstat", report_existing)
    monkeypatch.setattr(knowledge_snapshot.os, "stat", report_missing_target)

    with pytest.raises(KnowledgeStateRootError) as raised:
        paths.validate_roots()

    assert raised.value.root == state
    assert raised.value.reason == "is inaccessible"


def test_snapshot_rejects_root_presence_change_between_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = KnowledgeStatePaths.from_directory(state)
    validations = 0

    def changing_presence(_paths: KnowledgeStatePaths) -> tuple[Path, ...]:
        nonlocal validations
        validations += 1
        return () if validations == 1 else (state.absolute(),)

    monkeypatch.setattr(
        KnowledgeStatePaths,
        "validate_roots",
        changing_presence,
    )

    with pytest.raises(KnowledgeStateRootError) as raised:
        collect_knowledge_snapshot(paths, source_version="0.7.0")

    assert raised.value.root == state.absolute()
    assert raised.value.reason == "changed during snapshot capture"
    assert validations == 2


def test_public_snapshot_rejects_existing_non_directory_root(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state-file"
    original = b"not a state directory"
    state.write_bytes(original)

    with pytest.raises(KnowledgeStateRootError) as raised:
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
        )

    assert raised.value.root == state.resolve()
    assert raised.value.reason == "is not a directory"
    assert state.read_bytes() == original


def test_public_snapshot_rejects_non_file_owner_state_path(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    invalid_owner_path = state / "pdf.sqlite3"
    invalid_owner_path.mkdir()

    with pytest.raises(KnowledgeStateRootError) as raised:
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
        )

    assert raised.value.root == state.absolute()
    assert raised.value.reason == "contains a non-file owner state path"
    assert invalid_owner_path.is_dir()


def test_snapshot_lazily_validates_an_existing_image_owner(tmp_path: Path) -> None:
    from _04_Nucleo_Operativo.image_state import (
        SCHEMA_VERSION,
        initialize_image_state,
    )

    state = tmp_path / "state"
    state.mkdir()
    initialize_image_state(state / "image.sqlite3")

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    image_owner = _owner(snapshot, "image")
    assert image_owner.state is OwnerAvailability.AVAILABLE
    assert image_owner.expected_schema_version == SCHEMA_VERSION
    assert image_owner.observed_schema_version == SCHEMA_VERSION


def test_snapshot_collects_real_heads_and_marks_absent_owners(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _published_fixture(state)

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 1
    assert _owner(snapshot, "inventory").state is OwnerAvailability.AVAILABLE
    assert _owner(snapshot, "catalog").publications[0].scope == "pdf"
    assert _owner(snapshot, "semantic").publications[0].model_signature == ("snapshot-model-v1")
    assert snapshot.active_models[0].vector_space == "snapshot-space-v1"
    assert _owner(snapshot, "code").watermarks
    assert _owner(snapshot, "pdf").state is OwnerAvailability.ABSENT
    assert not (state / "pdf.sqlite3").exists()


def test_snapshot_reads_safe_previous_framework_and_abstains_inventory(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _legacy_read_compatible_fixture(state)
    inventory = state / "dedup.sqlite3"
    framework = state / "framework.sqlite3"
    inventory_before = inventory.read_bytes()
    framework_before = framework.read_bytes()

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.2",
    )

    inventory_owner = _owner(snapshot, "inventory")
    framework_owner = _owner(snapshot, "framework")
    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 1
    assert inventory_owner.state is OwnerAvailability.INCOMPATIBLE
    assert inventory_owner.observed_schema_version == 7
    assert inventory_owner.error_code == "legacy_schema"
    assert inventory_owner.publications == ()
    assert framework_owner.state is OwnerAvailability.AVAILABLE
    assert framework_owner.observed_schema_version == 19
    assert framework_owner.warning == "legacy_schema_read_compatible:19->20"
    assert inventory.read_bytes() == inventory_before
    assert framework.read_bytes() == framework_before


def test_snapshot_rejects_extended_previous_framework_schema(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _legacy_read_compatible_fixture(state)
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        connection.execute("ALTER TABLE initial_runs ADD COLUMN unexpected TEXT")

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.2",
    )

    framework_owner = _owner(snapshot, "framework")
    assert framework_owner.state is OwnerAvailability.INCOMPATIBLE
    assert "v19 schema contract validation failed" in (framework_owner.warning or "")


def test_snapshot_distinguishes_absent_future_and_corrupt_without_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    future = state / "pdf.sqlite3"
    with sqlite3.connect(future) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','12')")
    future_before = future.read_bytes()
    corrupt = state / "docx.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    corrupt_before = corrupt.read_bytes()

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    assert _owner(snapshot, "inventory").state is OwnerAvailability.ABSENT
    assert _owner(snapshot, "pdf").state is OwnerAvailability.FUTURE
    assert _owner(snapshot, "docx").state is OwnerAvailability.CORRUPT
    assert future.read_bytes() == future_before
    assert corrupt.read_bytes() == corrupt_before
    assert not (state / "dedup.sqlite3").exists()


def test_snapshot_retries_once_after_external_owner_change(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    changed = False

    def mutate(owner: str, attempt: int) -> None:
        nonlocal changed
        if owner != "code" or attempt != 1 or changed:
            return
        changed = True
        with sqlite3.connect(code) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                framework_run_id,scan_id,processing_signature,status,started_ns)
                VALUES(1,1,'fixture','completed',1)"""
            )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        _between_observations=mutate,
    )

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 2


def test_snapshot_retries_after_commit_without_logical_watermark_change(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    changed = False

    def mutate_metadata(owner: str, attempt: int) -> None:
        nonlocal changed
        if owner != "code" or attempt != 1 or changed:
            return
        changed = True
        with sqlite3.connect(code) as connection:
            connection.execute("INSERT INTO metadata(key,value) VALUES('snapshot_probe','1')")

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.2",
        _between_observations=mutate_metadata,
    )

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 2
    assert changed


def test_snapshot_observes_cancellation_before_global_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(RuntimeError):
        pass

    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    changed = False
    retry_pending = False
    original_changed_owners = knowledge_snapshot._changed_vector_owners

    def mutate(owner: str, attempt: int) -> None:
        nonlocal changed
        if owner != "code" or attempt != 1 or changed:
            return
        changed = True
        with sqlite3.connect(code) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                framework_run_id,scan_id,processing_signature,status,started_ns)
                VALUES(1,1,'fixture','completed',1)"""
            )

    def mark_retry(*args: Any, **kwargs: Any) -> frozenset[str]:
        nonlocal retry_pending
        changed_owners = original_changed_owners(*args, **kwargs)
        retry_pending = bool(changed_owners)
        return changed_owners

    def cancel_retry() -> None:
        if retry_pending:
            raise Cancelled("cancel before retry")

    monkeypatch.setattr(
        knowledge_snapshot,
        "_changed_vector_owners",
        mark_retry,
    )

    with pytest.raises(Cancelled, match="before retry"):
        collect_knowledge_snapshot(
            KnowledgeStatePaths.from_directory(state),
            source_version="0.7.0",
            cancellation_check=cancel_retry,
            _between_observations=mutate,
        )

    assert changed
    assert retry_pending


def test_snapshot_reports_changed_after_second_bounded_attempt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = state / "code.sqlite3"
    initialize_code_state(code)
    writes = 0

    def mutate(owner: str, _attempt: int) -> None:
        nonlocal writes
        if owner != "code":
            return
        writes += 1
        with sqlite3.connect(code) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                framework_run_id,scan_id,processing_signature,status,started_ns)
                VALUES(1,?,'fixture','completed',?)""",
                (writes, writes),
            )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        _between_observations=mutate,
    )

    assert snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    assert snapshot.attempts == 2
    assert "code" in snapshot.changed_owners
    assert writes == 2


def test_snapshot_retries_when_inventory_duplicate_plan_is_cleared(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    _set_duplicate_plan_summary(
        inventory,
        completed_ns=40,
        group_count=1,
        redundant_files=2,
        reclaimable_bytes=300,
    )
    paths = KnowledgeStatePaths.from_directory(state)
    before = collect_knowledge_snapshot(paths, source_version="0.7.0")
    before_inventory = _owner(before, "inventory")
    assert before_inventory.publications[0].model_signature == ("duplicate-plan-v1:40:1:2:300")
    changed = False

    def clear_plan(owner: str, attempt: int) -> None:
        nonlocal changed
        if owner != "inventory" or attempt != 1 or changed:
            return
        changed = True
        _set_duplicate_plan_summary(inventory, completed_ns=None)

    snapshot = collect_knowledge_snapshot(
        paths,
        source_version="0.7.0",
        _between_observations=clear_plan,
    )
    inventory_snapshot = _owner(snapshot, "inventory")

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 2
    assert changed
    assert inventory_snapshot.publications[0].publication_id == "inventory-scan:1"
    assert inventory_snapshot.publications[0].model_signature is None
    assert inventory_snapshot.watermarks == before_inventory.watermarks
    assert snapshot.snapshot_id != before.snapshot_id


def test_snapshot_reports_changed_when_inventory_plan_rebuilds_on_retry(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    _set_duplicate_plan_summary(
        inventory,
        completed_ns=40,
        group_count=1,
        redundant_files=2,
        reclaimable_bytes=300,
    )
    mutations = 0

    def rebuild_plan(owner: str, attempt: int) -> None:
        nonlocal mutations
        if owner != "inventory":
            return
        mutations += 1
        if attempt == 1:
            _set_duplicate_plan_summary(inventory, completed_ns=None)
            return
        _set_duplicate_plan_summary(
            inventory,
            completed_ns=99,
            group_count=2,
            redundant_files=4,
            reclaimable_bytes=600,
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        _between_observations=rebuild_plan,
    )
    inventory_snapshot = _owner(snapshot, "inventory")

    assert snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    assert snapshot.attempts == 2
    assert snapshot.changed_owners == ("inventory",)
    assert mutations == 2
    assert inventory_snapshot.warning == "logical_watermark_changed"
    assert inventory_snapshot.publications[0].model_signature == ("duplicate-plan-v1:99:2:4:600")


def test_snapshot_retries_when_later_owner_changes_captured_inventory(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    changed = False

    def mutate_inventory_from_code(owner: str, attempt: int) -> None:
        nonlocal changed
        if owner != "code" or attempt != 1 or changed:
            return
        changed = True
        _set_duplicate_plan_summary(
            inventory,
            completed_ns=70,
            group_count=1,
            redundant_files=3,
            reclaimable_bytes=400,
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        _between_observations=mutate_inventory_from_code,
    )

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.attempts == 2
    assert changed
    assert _owner(snapshot, "inventory").publications[0].model_signature == (
        "duplicate-plan-v1:70:1:3:400"
    )


def test_snapshot_reports_cross_owner_skew_after_second_attempt(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    writes = 0

    def keep_changing_inventory_from_code(owner: str, _attempt: int) -> None:
        nonlocal writes
        if owner != "code":
            return
        writes += 1
        _set_duplicate_plan_summary(
            inventory,
            completed_ns=70 + writes,
            group_count=writes,
            redundant_files=writes + 2,
            reclaimable_bytes=400 + writes,
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        _between_observations=keep_changing_inventory_from_code,
    )
    inventory_snapshot = _owner(snapshot, "inventory")

    assert snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    assert snapshot.attempts == 2
    assert snapshot.changed_owners == ("inventory",)
    assert writes == 2
    assert inventory_snapshot.warning == "logical_vector_changed"
    assert inventory_snapshot.publications[0].model_signature == ("duplicate-plan-v1:72:2:4:402")


def test_snapshot_rejects_catalog_head_source_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE catalog_publications SET source_kind='docx' WHERE source_kind='pdf'"
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )
    catalog = _owner(snapshot, "catalog")

    assert catalog.state is OwnerAvailability.INCOMPATIBLE
    assert catalog.warning is not None
    assert "source kind mismatches" in catalog.warning


def test_snapshot_rejects_semantic_head_model_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    semantic = state / "semantic.sqlite3"
    other_model = EmbeddingModelSpec(
        "snapshot-model-v2",
        "snapshot-space-v2",
        EmbeddingModality.TEXT,
        "fixture/snapshot-other",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    register_embedding_model(semantic, other_model, allow_test_provider=True)
    with sqlite3.connect(semantic) as connection:
        connection.execute(
            "UPDATE published_embedding_heads SET model_signature=?",
            (other_model.model_signature,),
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )
    semantic_snapshot = _owner(snapshot, "semantic")

    assert semantic_snapshot.state is OwnerAvailability.INCOMPATIBLE
    assert semantic_snapshot.warning is not None
    assert "model signature mismatches" in semantic_snapshot.warning


def test_snapshot_rejects_catalog_publication_orphan(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE catalog_publications SET generation_id=9999 WHERE source_kind='pdf'"
        )

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )
    catalog = _owner(snapshot, "catalog")

    assert catalog.state is OwnerAvailability.INCOMPATIBLE
    assert catalog.warning is not None
    assert "publication points to a missing generation" in catalog.warning


def test_snapshot_rejects_invalid_inventory_checkpoint_head(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _published_fixture(state)
    inventory = state / "dedup.sqlite3"
    paths = KnowledgeStatePaths.from_directory(state)
    with sqlite3.connect(inventory) as connection:
        connection.execute("UPDATE scans SET status='partial'")

    incomplete = collect_knowledge_snapshot(paths, source_version="0.7.0")
    incomplete_inventory = _owner(incomplete, "inventory")
    assert incomplete_inventory.state is OwnerAvailability.INCOMPATIBLE
    assert incomplete_inventory.warning is not None
    assert "non-complete scan" in incomplete_inventory.warning

    with sqlite3.connect(inventory) as connection:
        connection.execute("UPDATE scans SET status='complete',root='D:/Elsewhere'")

    mismatched = collect_knowledge_snapshot(paths, source_version="0.7.0")
    mismatched_inventory = _owner(mismatched, "inventory")
    assert mismatched_inventory.state is OwnerAvailability.INCOMPATIBLE
    assert mismatched_inventory.warning is not None
    assert "root-mismatched scan" in mismatched_inventory.warning


def test_archive_owner_is_additive_only_after_archive_state_exists(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    paths = KnowledgeStatePaths.from_directory(state)

    absent = collect_knowledge_snapshot(paths, source_version="0.7.0")

    assert all(owner.owner != "archive" for owner in absent.owners)
    state.mkdir()
    initialize_archive_state(state / "archive.sqlite3")

    available = collect_knowledge_snapshot(paths, source_version="0.7.0")

    archive = _owner(available, "archive")
    assert archive.state is OwnerAvailability.AVAILABLE
    assert archive.observed_schema_version == 1
    assert {mark.name: mark.value for mark in archive.watermarks}["current_rows"] == "0"


def test_text_owner_is_additive_only_after_text_state_exists(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = KnowledgeStatePaths.from_directory(state)

    absent = collect_knowledge_snapshot(paths, source_version="0.7.0")
    assert all(owner.owner != "text" for owner in absent.owners)

    initialize_text_state(state / "text.sqlite3")
    available = collect_knowledge_snapshot(paths, source_version="0.7.0")

    text = _owner(available, "text")
    assert text.state is OwnerAvailability.AVAILABLE
    assert text.observed_schema_version == 1
    assert {mark.name: mark.value for mark in text.watermarks}["current_rows"] == "0"


# endregion [02]
