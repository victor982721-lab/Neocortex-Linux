from __future__ import annotations

import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.capabilities.formats.text.text_derivation_repository as text_derivation_repository_module
import neocortex.capabilities.formats.text.text_state as text_state_module
from neocortex.semantic.derivation_contracts import (
    CapabilityFailure,
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
    WorkOutcome,
)
from neocortex.knowledge.knowledge_contracts import (
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationAttemptStart,
    TextDerivationIntegrityError,
    abandon_running_text_derivations,
    begin_text_derivation_attempt,
    cancel_text_derivation_attempt,
    compute_text_fts_fingerprint,
    compute_text_representation_fingerprint,
    fail_text_derivation_attempt,
    read_reusable_text_derivation,
    read_text_derivation_dependents,
    read_text_derivation_dependents_page,
    read_text_derivation_impact,
    read_text_derivation_outbox,
    read_text_document_lineage,
    read_text_work_receipts,
    resolve_text_lineage_identifier,
    succeed_text_derivation_attempt,
)
from neocortex.capabilities.formats.text.text_state import (
    TEXT_SCHEMA_VERSION,
    initialize_text_state,
    text_database,
)
from neocortex.semantic.semantic_models import fingerprint_text


STARTED = "2026-08-11T00:00:00Z"
FINISHED = "2026-08-11T00:00:01Z"


def _source_contracts(
    *,
    fingerprint: str = "raw-a",
) -> tuple[ResourceRef, RevisionRef, InputBinding, StageDescriptor]:
    resource = ResourceRef("resource:text:fixture", "text", "text")
    revision = RevisionRef(
        resource.resource_id,
        f"revision:text:{fingerprint}",
        "text.source",
        "raw-input-v1",
        None,
        RevisionState.CURRENT,
        STARTED,
    )
    binding = InputBinding("source", revision, fingerprint)
    stage = StageDescriptor(
        "text.extract",
        "1",
        "psig-text-v1",
        implementation_digest="xxh3-128:implementation",
    )
    return resource, revision, binding, stage


def _start(
    attempt_id: str,
    *,
    recorded_ns: int = 1,
    causation_id: str | None = None,
    fingerprint: str = "raw-a",
) -> TextDerivationAttemptStart:
    _resource, _revision, binding, stage = _source_contracts(fingerprint=fingerprint)
    return TextDerivationAttemptStart(
        attempt_id=attempt_id,
        stage=stage,
        inputs=(binding,),
        effective_configuration=(("max_text_chars", 1_000),),
        runtime=(("python", "3.14"),),
        started_at_utc=STARTED,
        started_monotonic_ns=10,
        attempt=1,
        run_id=f"run:{attempt_id}",
        correlation_id=f"correlation:{attempt_id}",
        causation_id=causation_id,
        recorded_ns=recorded_ns,
    )


def _outputs() -> tuple[OutputBinding, ...]:
    resource, revision, _binding, _stage = _source_contracts()
    representation_fingerprint = compute_text_representation_fingerprint(
        text="contenido",
        content_kind="txt",
        media_type="text/plain",
        title=None,
        author=None,
        metadata={},
        truncated=False,
        detail=None,
    )
    fts_fingerprint = compute_text_fts_fingerprint(
        "file-a",
        text="contenido",
        content_kind="txt",
        title=None,
        author=None,
    )
    return (
        OutputBinding(
            "text_representation",
            MaterializationRef(
                "text",
                "text_representation",
                "materialization:text:text-representation:raw-a",
                TEXT_SCHEMA_VERSION,
                resource,
                revision,
            ),
            representation_fingerprint,
        ),
        OutputBinding(
            "text_fts",
            MaterializationRef(
                "text",
                "text_fts",
                "materialization:text:text-fts:raw-a",
                TEXT_SCHEMA_VERSION,
                resource,
                revision,
            ),
            fts_fingerprint,
        ),
    )


def _insert_document(connection: sqlite3.Connection, *, revision_id: str | None = None) -> None:
    text = "contenido"
    fingerprint = fingerprint_text(text)
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
        updated_ns,revision_id)
        VALUES('file-a','/fixture/a.txt',1,1,-1,'psig-text-v1','complete',
        'txt','text/plain',?,?,?,1,1,?)""",
        (zlib.compress(text.encode("utf-8")), len(text), fingerprint.xxh3_128, revision_id),
    )
    connection.execute(
        """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
        VALUES('file-a','/fixture/a.txt','txt','','',?)""",
        (text,),
    )


def _create_populated_v1(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        text_state_module._create_text_v1_schema(connection)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,last_seen_run_id,updated_ns)
            VALUES('legacy','/legacy.txt',7,10,-1,'legacy-psig','complete',
            'txt','text/plain',3,12)"""
        )
        connection.execute(
            """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
            VALUES('legacy','/legacy.txt','txt','','','evidencia legacy')"""
        )


def test_fresh_schema_two_is_exact_idempotent_and_has_owner_lineage_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    before = path.read_bytes()

    initialize_text_state(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("2",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        assert "revision_id" in columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='text_derivation_outbox'"
        ).fetchone() == (1,)


def test_published_document_revision_cannot_be_downgraded_to_legacy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    start = _start("published")
    begin_text_derivation_attempt(path, start)
    revision_id = start.inputs[0].revision.revision_id
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_document(connection, revision_id=revision_id)
        with pytest.raises(sqlite3.IntegrityError, match="cannot become legacy"):
            connection.execute("UPDATE documents SET revision_id=NULL WHERE file_key='file-a'")


def test_populated_v1_migrates_transactionally_without_inventing_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    _create_populated_v1(path)

    initialize_text_state(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT path,status,revision_id FROM documents WHERE file_key='legacy'"
        ).fetchone() == ("/legacy.txt", "complete", None)
        assert connection.execute(
            "SELECT body FROM document_fts WHERE file_key='legacy'"
        ).fetchone() == ("evidencia legacy",)
        assert connection.execute("SELECT COUNT(*) FROM text_work_receipts").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    lineage = read_text_document_lineage(path, "legacy")
    assert lineage is not None
    assert lineage.attribution == "legacy_unattributed"
    assert lineage.revision is None
    assert lineage.receipts == ()


def test_failed_v1_migration_rolls_back_document_and_schema_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "text.sqlite3"
    _create_populated_v1(path)
    original = text_state_module._migrate_text_v1_to_v2

    def fail_after_migration(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("fault after migration")

    monkeypatch.setattr(text_state_module, "_migrate_text_v1_to_v2", fail_after_migration)

    with pytest.raises(RuntimeError, match="fault after migration"):
        initialize_text_state(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("1",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='text_derivation_outbox'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT path FROM documents WHERE file_key='legacy'"
        ).fetchone() == ("/legacy.txt",)


def test_lineage_readers_reject_v1_and_future_schema_without_writes(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _create_populated_v1(legacy)
    legacy_before = legacy.read_bytes()
    with pytest.raises(RuntimeError, match="incompatible with schema 2"):
        read_text_document_lineage(legacy, "legacy")
    assert legacy.read_bytes() == legacy_before

    future = tmp_path / "future.sqlite3"
    initialize_text_state(future)
    with sqlite3.connect(future) as connection:
        connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
    future_before = future.read_bytes()
    with pytest.raises(RuntimeError, match="incompatible with schema 2"):
        read_text_derivation_outbox(future)
    assert future.read_bytes() == future_before


def test_success_commits_document_two_heads_receipt_and_outbox_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("attempt-1"))

    with text_database(path, create=False) as connection:
        _insert_document(connection)
        receipt = succeed_text_derivation_attempt(
            connection,
            "attempt-1",
            receipt_id="receipt-1",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        assert connection.in_transaction
        connection.commit()

    assert receipt.outcome is WorkOutcome.SUCCEEDED
    lineage = read_text_document_lineage(path, "file-a")
    assert lineage is not None
    assert lineage.attribution == "attributed"
    assert lineage.revision is not None
    assert lineage.revision.revision_id == "revision:text:raw-a"
    assert {item.materialization.kind for item in lineage.materializations} == {
        "text_representation",
        "text_fts",
    }
    assert all(item.current_head for item in lineage.materializations)
    impact = read_text_derivation_impact(
        path,
        stage_id="text.extract",
        processing_signature="changed",
    )
    assert impact.stale == lineage.materializations
    bounded_impact = read_text_derivation_impact(
        path,
        stage_id="text.extract",
        processing_signature="changed",
        limit=1,
    )
    assert bounded_impact.total_count == 2
    assert bounded_impact.truncated is True
    assert len(bounded_impact.stale) == 1
    events = read_text_derivation_outbox(path)
    assert [(item.sequence, item.receipt_id) for item in events] == [(1, "receipt-1")]
    assert read_text_derivation_outbox(path, after_sequence=1) == ()
    stored = read_text_work_receipts(path, ("receipt-1",))
    assert stored[0].payload_json == receipt.to_json()
    for identifier in (
        "file-a",
        "/fixture/a.txt",
        "revision:text:raw-a",
        "materialization:text:text-representation:raw-a",
        "receipt-1",
    ):
        assert resolve_text_lineage_identifier(path, identifier) == "file-a"


def test_online_sqlite_backup_preserves_schema_receipts_and_lineage(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("attempt-backup"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "attempt-backup",
            receipt_id="receipt-backup",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.commit()

    with sqlite3.connect(path) as source, sqlite3.connect(restored) as destination:
        source.backup(destination)

    lineage = read_text_document_lineage(restored, "file-a")
    assert lineage is not None
    assert lineage.receipts == ("receipt-backup",)
    assert len(lineage.materializations) == 2
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_uncommitted_terminal_receipt_is_invisible_to_concurrent_reader(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("producer-concurrent"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "producer-concurrent",
            receipt_id="receipt-producer-concurrent",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.commit()
    reusable = read_reusable_text_derivation(
        path,
        "file-a",
        stage_id="text.extract",
        processing_signature="psig-text-v1",
    )
    assert reusable is not None
    begin_text_derivation_attempt(
        path,
        _start(
            "cache-concurrent",
            recorded_ns=3,
            causation_id=reusable.producer_receipt_id,
        ),
    )

    with text_database(path, create=False) as writer:
        writer.execute("BEGIN IMMEDIATE")
        succeed_text_derivation_attempt(
            writer,
            "cache-concurrent",
            receipt_id="receipt-cache-concurrent",
            outputs=reusable.outputs,
            finished_at_utc=FINISHED,
            duration_ns=1,
            execution_mode=WorkExecutionMode.CACHE_HIT,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=4,
            document_file_key="file-a",
        )
        concurrent = read_text_document_lineage(path, "file-a")
        assert concurrent is not None
        assert concurrent.receipts == ("receipt-producer-concurrent",)
        writer.commit()

    published = read_text_document_lineage(path, "file-a")
    assert published is not None
    assert published.receipts == (
        "receipt-producer-concurrent",
        "receipt-cache-concurrent",
    )


def test_terminal_rollback_keeps_running_attempt_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("attempt-rollback"))

    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "attempt-rollback",
            receipt_id="receipt-rollback",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.rollback()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT status FROM text_derivation_attempts").fetchone() == (
            "running",
        )
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM text_work_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM text_derivation_outbox").fetchone() == (0,)


def test_output_revision_outside_receipt_inputs_fails_before_publication(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("attempt-mismatch"))
    valid = _outputs()
    materialization = valid[0].materialization
    assert materialization.resource is not None
    mismatched_revision = RevisionRef(
        materialization.resource.resource_id,
        "revision:text:not-an-input",
        "text.source",
        "raw-input-v1",
        None,
        RevisionState.CURRENT,
        STARTED,
    )
    invalid_output = OutputBinding(
        valid[0].name,
        MaterializationRef(
            materialization.owner,
            materialization.kind,
            "materialization:text:mismatched",
            materialization.schema_version,
            materialization.resource,
            mismatched_revision,
        ),
        valid[0].fingerprint,
    )

    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="must match one receipt input exactly"):
            succeed_text_derivation_attempt(
                connection,
                "attempt-mismatch",
                receipt_id="receipt-mismatch",
                outputs=(invalid_output, valid[1]),
                finished_at_utc=FINISHED,
                duration_ns=100,
                execution_mode=WorkExecutionMode.EXECUTED,
                reproducibility=ReproducibilityClass.EXACT,
                terminal_ns=2,
            )
        connection.rollback()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT status FROM text_derivation_attempts").fetchone() == (
            "running",
        )
        assert connection.execute("SELECT COUNT(*) FROM text_work_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM text_materializations").fetchone() == (0,)


def test_cache_hit_reuses_original_materializations_and_records_causation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("producer"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "producer",
            receipt_id="receipt-producer",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.commit()

    reusable = read_reusable_text_derivation(
        path,
        "file-a",
        stage_id="text.extract",
        processing_signature="psig-text-v1",
    )
    assert reusable is not None
    assert reusable.producer_receipt_id == "receipt-producer"
    begin_text_derivation_attempt(
        path,
        _start("cache", recorded_ns=3, causation_id=reusable.producer_receipt_id),
    )
    with text_database(path, create=False) as connection:
        connection.execute("UPDATE documents SET updated_ns=2 WHERE file_key='file-a'")
        cached = succeed_text_derivation_attempt(
            connection,
            "cache",
            receipt_id="receipt-cache",
            outputs=reusable.outputs,
            finished_at_utc=FINISHED,
            duration_ns=1,
            execution_mode=WorkExecutionMode.CACHE_HIT,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=4,
            document_file_key="file-a",
        )
        connection.commit()

    assert cached.causation_id == "receipt-producer"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_materializations").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM text_materialization_heads").fetchone() == (
            2,
        )
    dependents = read_text_derivation_dependents(path, reusable.revision.revision_id)
    assert [item.receipt_id for item in dependents] == ["receipt-producer", "receipt-cache"]
    assert len(dependents[1].outputs) == 2

    lineage_window = read_text_document_lineage(path, "file-a", limit=1)
    assert lineage_window is not None
    assert lineage_window.receipt_count == 2
    assert len(lineage_window.receipts) == 1
    assert lineage_window.materialization_count == 2
    assert len(lineage_window.materializations) == 1
    assert lineage_window.to_dict()["receipt_window_truncated"] is True
    assert lineage_window.to_dict()["materialization_window_truncated"] is True

    dependency_page = read_text_derivation_dependents_page(
        path,
        reusable.revision.revision_id,
        limit=1,
    )
    assert dependency_page.total_count == 2
    assert dependency_page.truncated is True
    assert [item.receipt_id for item in dependency_page.items] == ["receipt-producer"]
    assert len(dependency_page.items[0].outputs) == 2

    begin_text_derivation_attempt(
        path,
        _start("replay", recorded_ns=5, causation_id=reusable.producer_receipt_id),
    )
    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        replayed = succeed_text_derivation_attempt(
            connection,
            "replay",
            receipt_id="receipt-replay",
            outputs=reusable.outputs,
            finished_at_utc=FINISHED,
            duration_ns=1,
            execution_mode=WorkExecutionMode.REPLAY,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=6,
            document_file_key="file-a",
        )
        connection.commit()
    assert replayed.execution_mode is WorkExecutionMode.REPLAY
    assert replayed.causation_id == "receipt-producer"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_materializations").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM text_work_receipts").fetchone() == (3,)


@pytest.mark.parametrize(
    ("terminal", "expected"),
    (
        (fail_text_derivation_attempt, WorkOutcome.FAILED),
        (cancel_text_derivation_attempt, WorkOutcome.CANCELLED),
    ),
)
def test_failure_and_cancellation_publish_no_outputs(
    tmp_path: Path,
    terminal,
    expected: WorkOutcome,
) -> None:
    path = tmp_path / f"{expected.value}.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start(f"attempt-{expected.value}"))
    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = terminal(
            connection,
            f"attempt-{expected.value}",
            receipt_id=f"receipt-{expected.value}",
            finished_at_utc=FINISHED,
            duration_ns=10,
            reproducibility=ReproducibilityClass.BEST_EFFORT,
            failure=CapabilityFailure(
                "text.extract",
                expected.value,
                f"Text attempt {expected.value}",
                expected is WorkOutcome.FAILED,
            ),
            terminal_ns=2,
        )
        connection.commit()
    assert receipt.outcome is expected
    assert receipt.outputs == ()
    assert read_text_derivation_outbox(path)[0].event_type == f"text.work_{expected.value}.v1"


def test_abandon_reconciles_only_selected_running_attempts(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    first = _start("old-1", recorded_ns=1)
    second = _start("old-2", recorded_ns=2)
    begin_text_derivation_attempt(path, first)
    begin_text_derivation_attempt(path, second)

    abandoned = abandon_running_text_derivations(
        path,
        finished_at_utc=FINISHED,
        terminal_ns=3,
        correlation_id=first.correlation_id,
    )

    assert len(abandoned) == 1
    assert abandoned[0].outcome is WorkOutcome.ABANDONED
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM text_derivation_attempts WHERE attempt_id='old-1'"
        ).fetchone() == ("abandoned",)
        assert connection.execute(
            "SELECT status FROM text_derivation_attempts WHERE attempt_id='old-2'"
        ).fetchone() == ("running",)


def test_abandon_reconciles_a_bounded_page_per_transaction(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    for index in range(3):
        begin_text_derivation_attempt(
            path,
            _start(f"old-{index}", recorded_ns=index + 1),
        )

    first_page = abandon_running_text_derivations(
        path,
        finished_at_utc=FINISHED,
        terminal_ns=10,
        limit=2,
    )
    second_page = abandon_running_text_derivations(
        path,
        finished_at_utc=FINISHED,
        terminal_ns=10,
        limit=2,
    )

    assert [receipt.correlation_id for receipt in first_page] == [
        "correlation:old-0",
        "correlation:old-1",
    ]
    assert [receipt.correlation_id for receipt in second_page] == ["correlation:old-2"]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM text_derivation_attempts WHERE status='running'"
        ).fetchone() == (0,)


def test_outbox_reader_pages_by_bytes_and_preserves_cursor_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("old-0", recorded_ns=1))
    begin_text_derivation_attempt(path, _start("old-1", recorded_ns=2))
    abandon_running_text_derivations(
        path,
        finished_at_utc=FINISHED,
        terminal_ns=10,
    )
    monkeypatch.setattr(
        text_derivation_repository_module,
        "_TEXT_OUTBOX_PAGE_BYTES",
        1,
    )

    first_page = read_text_derivation_outbox(path, limit=1_000)
    second_page = read_text_derivation_outbox(
        path,
        after_sequence=first_page[-1].sequence,
        limit=1_000,
    )

    assert len(first_page) == len(second_page) == 1
    assert first_page[0].sequence < second_page[0].sequence


def test_revision_identity_conflict_rolls_back_new_attempt(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("first"))
    _resource, revision, _binding, stage = _source_contracts()
    conflicting = TextDerivationAttemptStart(
        attempt_id="conflict",
        stage=stage,
        inputs=(InputBinding("source", revision, "different-raw-fingerprint"),),
        effective_configuration=(),
        runtime=(("python", "3.14"),),
        started_at_utc=STARTED,
        started_monotonic_ns=20,
        attempt=2,
        run_id="run:conflict",
        correlation_id="correlation:conflict",
        recorded_ns=2,
    )

    with pytest.raises(ValueError, match="immutable Text input revision conflicts"):
        begin_text_derivation_attempt(path, conflicting)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_derivation_attempts").fetchone() == (
            1,
        )


@pytest.mark.parametrize(
    "replacement",
    (
        {"effective_configuration": tuple((f"key-{index}", index) for index in range(129))},
        {"runtime": tuple((f"runtime-{index}", "value") for index in range(129))},
        {"attempt_id": "a" * 513},
        {"run_id": "r" * 513},
        {"correlation_id": "c" * 513},
        {"causation_id": "z" * 513},
        {"effective_configuration": (("large", "x" * 4_097),)},
    ),
)
def test_attempt_start_rejects_facts_that_cannot_be_terminalized(
    replacement: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(_start("bounded-start"), **replacement)


def test_attempt_start_rejects_receipt_payload_that_exceeds_projection_bound() -> None:
    base = _start("oversized-start")
    revision = replace(
        base.inputs[0].revision,
        processing_signature="s" * 4_096,
    )
    inputs = tuple(InputBinding(f"source-{index}", revision, "raw") for index in range(300))

    with pytest.raises(ValueError, match="WorkReceipt JSON cannot exceed"):
        replace(base, inputs=inputs)


def test_outbox_and_receipts_are_database_enforced_append_only(tmp_path: Path) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("failure"))
    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fail_text_derivation_attempt(
            connection,
            "failure",
            receipt_id="receipt-failure",
            finished_at_utc=FINISHED,
            duration_ns=1,
            reproducibility=ReproducibilityClass.BEST_EFFORT,
            failure=CapabilityFailure("text.extract", "failed", "failed", True),
            terminal_ns=2,
        )
        connection.commit()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM text_derivation_outbox")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE text_work_receipts SET outcome='cancelled'")


def test_normalized_derivation_facts_are_database_enforced_immutable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("immutable-success"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "immutable-success",
            receipt_id="receipt-immutable-success",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.commit()

    statements = (
        "UPDATE text_input_revisions SET fingerprint='forged'",
        "DELETE FROM text_input_revisions",
        "UPDATE text_derivation_attempts SET stage_version='forged'",
        "DELETE FROM text_derivation_attempts",
        "UPDATE text_derivation_input_bindings SET fingerprint='forged'",
        "DELETE FROM text_derivation_input_bindings",
        "UPDATE text_materializations SET fingerprint='forged'",
        "DELETE FROM text_materializations",
        "UPDATE text_derivation_output_bindings SET fingerprint='forged'",
        "DELETE FROM text_derivation_output_bindings",
    )
    with sqlite3.connect(path) as connection:
        for statement in statements:
            with pytest.raises(
                sqlite3.IntegrityError,
                match=r"immutable|append-only|transition",
            ):
                connection.execute(statement)
            connection.rollback()


def test_running_attempt_facts_cannot_be_rewritten_before_terminalization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("immutable-running"))

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="terminal transition only"):
            connection.execute(
                "UPDATE text_derivation_attempts SET recorded_ns=99 "
                "WHERE attempt_id='immutable-running'"
            )


def test_lineage_reader_reconciles_normalized_outputs_with_canonical_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("reconcile-output"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        succeed_text_derivation_attempt(
            connection,
            "reconcile-output",
            receipt_id="receipt-reconcile-output",
            outputs=_outputs(),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-a",
        )
        connection.commit()

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """DROP TRIGGER text_derivation_output_bindings_no_update;
            UPDATE text_derivation_output_bindings SET fingerprint='forged-fingerprint'
            WHERE binding_name='text_representation';
            CREATE TRIGGER text_derivation_output_bindings_no_update
            BEFORE UPDATE ON text_derivation_output_bindings BEGIN
                SELECT RAISE(ABORT,'Text derivation output bindings are immutable');
            END;"""
        )

    with pytest.raises(TextDerivationIntegrityError, match="contradicts"):
        read_text_document_lineage(path, "file-a")


def test_receipt_reconciliation_is_set_based_not_per_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    receipt_ids: list[str] = []
    for index in range(20):
        attempt_id = f"batch-failure-{index}"
        receipt_id = f"receipt-batch-failure-{index}"
        receipt_ids.append(receipt_id)
        begin_text_derivation_attempt(path, _start(attempt_id, recorded_ns=index + 1))
        with text_database(path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            fail_text_derivation_attempt(
                connection,
                attempt_id,
                receipt_id=receipt_id,
                finished_at_utc=FINISHED,
                duration_ns=1,
                reproducibility=ReproducibilityClass.BEST_EFFORT,
                failure=CapabilityFailure("text.extract", "failed", "failed", True),
                terminal_ns=index + 100,
            )
            connection.commit()

    statements: list[str] = []
    with text_database(path, readonly=True) as connection:
        connection.set_trace_callback(statements.append)
        records = text_derivation_repository_module._validated_terminal_receipts(
            connection,
            tuple(receipt_ids),
        )

    assert set(records) == set(receipt_ids)
    assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) == 5


def test_outbox_reader_rejects_payload_that_diverges_from_owner_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("corrupt-outbox"))
    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fail_text_derivation_attempt(
            connection,
            "corrupt-outbox",
            receipt_id="receipt-corrupt-outbox",
            finished_at_utc=FINISHED,
            duration_ns=1,
            reproducibility=ReproducibilityClass.BEST_EFFORT,
            failure=CapabilityFailure("text.extract", "failed", "failed", True),
            terminal_ns=2,
        )
        connection.commit()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER text_derivation_outbox_no_update")
        connection.execute("UPDATE text_derivation_outbox SET payload_json='{}'")
        connection.execute(
            """CREATE TRIGGER text_derivation_outbox_no_update
            BEFORE UPDATE ON text_derivation_outbox BEGIN
                SELECT RAISE(ABORT,'text derivation outbox is append-only');
            END"""
        )

    with pytest.raises(RuntimeError, match="normalized facts"):
        read_text_derivation_outbox(path)
