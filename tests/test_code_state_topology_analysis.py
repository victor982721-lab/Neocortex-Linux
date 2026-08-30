from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.code.code_state_topology_analysis import (
    TEXT_TERMINAL_PUBLICATION_QUESTION,
    CodeStateTopologyResolutionError,
    analyze_text_terminal_publication,
    parse_code_state_topology_payload,
    resolve_state_topology_questions,
    state_topology_questions,
)
from neocortex.semantic.derivation_contracts import (
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
)
from neocortex.knowledge.knowledge_contracts import (
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.semantic.semantic_models import fingerprint_text
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationAttemptStart,
    begin_text_derivation_attempt,
    compute_text_fts_fingerprint,
    compute_text_representation_fingerprint,
    succeed_text_derivation_attempt,
)
from neocortex.capabilities.formats.text.text_state import (
    TEXT_SCHEMA_VERSION,
    initialize_text_state,
    text_database,
)


def _contracts(suffix: str) -> tuple[ResourceRef, RevisionRef, InputBinding, StageDescriptor]:
    resource = ResourceRef(f"resource:text:{suffix}", "text", "text")
    revision = RevisionRef(
        resource.resource_id,
        f"revision:text:{suffix}",
        "text.source",
        "raw-input-v1",
        None,
        RevisionState.CURRENT,
        "2026-08-12T00:00:00Z",
    )
    binding = InputBinding("source", revision, f"raw-{suffix}")
    stage = StageDescriptor(
        "text.extract",
        "1",
        "psig-text-v1",
        implementation_digest="xxh3-128:implementation",
    )
    return resource, revision, binding, stage


def _start(attempt_id: str, suffix: str) -> TextDerivationAttemptStart:
    _resource, _revision, binding, stage = _contracts(suffix)
    return TextDerivationAttemptStart(
        attempt_id=attempt_id,
        stage=stage,
        inputs=(binding,),
        effective_configuration=(("max_text_chars", 1_000),),
        runtime=(("python", "3.14"),),
        started_at_utc="2026-08-12T00:00:00Z",
        started_monotonic_ns=10,
        attempt=1,
        run_id=f"run:{attempt_id}",
        correlation_id=f"correlation:{attempt_id}",
        causation_id=None,
        recorded_ns=1,
    )


def _outputs(suffix: str, file_key: str) -> tuple[OutputBinding, ...]:
    resource, revision, _binding, _stage = _contracts(suffix)
    text = "contenido"
    return (
        OutputBinding(
            "text_representation",
            MaterializationRef(
                "text",
                "text_representation",
                f"materialization:text:representation:{suffix}",
                TEXT_SCHEMA_VERSION,
                resource,
                revision,
            ),
            compute_text_representation_fingerprint(
                text=text,
                content_kind="txt",
                media_type="text/plain",
                title=None,
                author=None,
                metadata={},
                truncated=False,
                detail=None,
            ),
        ),
        OutputBinding(
            "text_fts",
            MaterializationRef(
                "text",
                "text_fts",
                f"materialization:text:fts:{suffix}",
                TEXT_SCHEMA_VERSION,
                resource,
                revision,
            ),
            compute_text_fts_fingerprint(
                file_key,
                text=text,
                content_kind="txt",
                title=None,
                author=None,
            ),
        ),
    )


def _insert_document(connection: sqlite3.Connection, file_key: str) -> None:
    text = "contenido"
    fingerprint = fingerprint_text(text)
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
        updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            file_key,
            f"/fixture/{file_key}.txt",
            1,
            1,
            -1,
            "psig-text-v1",
            "complete",
            "txt",
            "text/plain",
            zlib.compress(text.encode("utf-8")),
            len(text),
            fingerprint.xxh3_128,
            1,
            1,
        ),
    )
    connection.execute(
        """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
        VALUES(?,?,?,?,?,?)""",
        (file_key, f"/fixture/{file_key}.txt", "txt", "", "", text),
    )


def _publish_success(path: Path, *, attempt_id: str, suffix: str, file_key: str) -> None:
    begin_text_derivation_attempt(path, _start(attempt_id, suffix))
    with text_database(path, create=False) as connection:
        _insert_document(connection, file_key)
        succeed_text_derivation_attempt(
            connection,
            attempt_id,
            receipt_id=f"receipt:{attempt_id}",
            outputs=_outputs(suffix, file_key),
            finished_at_utc="2026-08-12T00:00:01Z",
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key=file_key,
        )
        connection.commit()


def _state_with_controls(state_directory: Path) -> Path:
    state_directory.mkdir()
    path = state_directory / "text.sqlite3"
    initialize_text_state(path)
    _publish_success(path, attempt_id="success", suffix="success", file_key="file-success")
    begin_text_derivation_attempt(path, _start("running-control", "running"))
    with text_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,last_seen_run_id,updated_ns,revision_id)
            VALUES('legacy-control','/fixture/legacy.txt',1,1,-1,'legacy','complete',
            'txt','text/plain',1,1,NULL)"""
        )
        connection.commit()
    return path


def test_exact_text_closure_preserves_running_and_legacy_negative_controls(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    path = _state_with_controls(state)
    before_bytes = path.read_bytes()
    before_names = tuple(sorted(item.name for item in state.iterdir()))

    result = analyze_text_terminal_publication(state, source_version="fixture-v14")

    assert result.status == "ready"
    assert result.observation == "relationally_closed"
    assert result.closure is not None
    assert result.closure.running_attempts == 1
    assert result.closure.terminal_attempts == 1
    assert result.closure.receipts == result.closure.outbox_events == 1
    assert result.closure.attributed_documents == 1
    assert result.closure.legacy_unattributed_documents == 1
    assert result.closure.relationally_closed is True
    assert result.inference_status == "abstained"
    assert result.decision_readiness == "experiment_required"
    assert result.decision is None
    assert path.read_bytes() == before_bytes
    assert tuple(sorted(item.name for item in state.iterdir())) == before_names


def test_orphan_outbox_is_an_observation_not_an_invented_defect(tmp_path: Path) -> None:
    state = tmp_path / "state"
    path = _state_with_controls(state)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO text_derivation_outbox(
            event_id,event_type,attempt_id,receipt_id,occurred_ns,payload_json)
            VALUES('rogue-event','text.work_succeeded.v1','absent-attempt',
            'absent-receipt',3,'{}')"""
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = analyze_text_terminal_publication(state, source_version="fixture-v14")

    assert result.status == "ready"
    assert result.observation == "relational_delta_observed"
    assert result.closure is not None
    assert result.closure.orphan_outbox_event_count == 1
    assert result.closure.orphan_outbox_event_ids == ("rogue-event",)
    assert result.inference_status == "abstained"
    assert result.decision is None
    assert result.mutation_authority is False


def test_rollback_leaves_only_the_committed_running_boundary(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "text.sqlite3"
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("rollback", "rollback"))
    with text_database(path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_document(connection, "file-rollback")
        succeed_text_derivation_attempt(
            connection,
            "rollback",
            receipt_id="receipt:rollback",
            outputs=_outputs("rollback", "file-rollback"),
            finished_at_utc="2026-08-12T00:00:01Z",
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-rollback",
        )
        connection.rollback()

    result = analyze_text_terminal_publication(state, source_version="fixture-v14")

    assert result.status == "ready"
    assert result.closure is not None
    assert result.closure.running_attempts == 1
    assert result.closure.terminal_attempts == 0
    assert result.closure.receipts == result.closure.outbox_events == 0
    assert result.closure.attributed_documents == 0
    assert result.closure.relationally_closed is True


def test_delta_examples_are_bounded_but_exact_count_drives_closure(tmp_path: Path) -> None:
    state = tmp_path / "state"
    path = _state_with_controls(state)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executemany(
            """INSERT INTO text_derivation_outbox(
            event_id,event_type,attempt_id,receipt_id,occurred_ns,payload_json)
            VALUES(?,?,?,?,?,'{}')""",
            (
                (
                    f"rogue-event-{index:03d}",
                    "text.work_succeeded.v1",
                    f"absent-attempt-{index:03d}",
                    f"absent-receipt-{index:03d}",
                    index + 3,
                )
                for index in range(105)
            ),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = analyze_text_terminal_publication(state, source_version="fixture-v14")

    assert result.status == "ready"
    assert result.closure is not None
    assert result.closure.orphan_outbox_event_count == 105
    assert len(result.closure.orphan_outbox_event_ids) == 100
    assert result.closure.orphan_outbox_events_truncated is True
    assert result.closure.relationally_closed is False


def test_question_registry_links_contract_relation_and_counterevidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _state_with_controls(state)
    analysis = analyze_text_terminal_publication(state, source_version="fixture-v14")

    specs, evaluations = state_topology_questions(analysis, rank=1)

    assert specs == (TEXT_TERMINAL_PUBLICATION_QUESTION,)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.subject.subject_kind == "transaction_scope"
    assert evaluation.subject.source_owner_id == "text"
    assert tuple(item.evidence_kind for item in evaluation.evidence) == (
        "contract",
        "internal_relation",
        "internal_fact",
    )
    assert evaluation.question_readiness == "ready"
    assert evaluation.counterevidence_status == "evaluated"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None
    assert evaluation.inferences == ()

    assert resolve_state_topology_questions(state, analysis, rank=1) == (
        specs,
        evaluations,
    )
    begin_text_derivation_attempt(
        state / "text.sqlite3",
        _start("later-running", "later-running"),
    )
    with pytest.raises(CodeStateTopologyResolutionError, match="disagrees"):
        resolve_state_topology_questions(state, analysis, rank=1)


def test_missing_or_future_text_state_abstains_without_partial_evidence(
    tmp_path: Path,
) -> None:
    missing = analyze_text_terminal_publication(
        tmp_path / "missing",
        source_version="fixture-v14",
    )
    assert missing.status == "abstained"
    assert missing.closure is None
    assert missing.snapshot_id is None
    missing_specs, missing_evaluations = state_topology_questions(missing, rank=1)
    assert missing_specs == (TEXT_TERMINAL_PUBLICATION_QUESTION,)
    assert missing_evaluations[0].question_readiness == "abstained"
    assert missing_evaluations[0].decision_readiness == "abstained"
    assert missing_evaluations[0].evidence == ()

    state = tmp_path / "state"
    state.mkdir()
    path = state / "text.sqlite3"
    initialize_text_state(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
    future = analyze_text_terminal_publication(state, source_version="fixture-v14")
    assert future.status == "abstained"
    assert future.closure is None
    assert future.decision_readiness == "abstained"


def test_wire_round_trip_rejects_invented_authority_and_closure(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _state_with_controls(state)
    result = analyze_text_terminal_publication(state, source_version="fixture-v14")
    payload = json.loads(json.dumps(result.as_payload()))

    assert parse_code_state_topology_payload(payload) == result

    payload["authority"] = "decision"
    with pytest.raises(ValueError, match="advisory"):
        parse_code_state_topology_payload(payload)

    payload = json.loads(json.dumps(result.as_payload()))
    payload["closure"]["relationally_closed"] = False
    with pytest.raises(ValueError, match="not derived"):
        parse_code_state_topology_payload(payload)
