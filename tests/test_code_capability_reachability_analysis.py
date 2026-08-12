from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.capabilities import (
    CAPABILITY_MANIFESTS,
    TEXT_BUILTIN_IMPLEMENTATION_ID,
    TEXT_EXTRACT_CAPABILITY_ID,
    TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID,
)

from _04_Nucleo_Operativo import code_capability_reachability_analysis as analysis_module
from _04_Nucleo_Operativo.code_capability_reachability_analysis import (
    CODE_CAPABILITY_REACHABILITY_SCHEMA,
    analyze_capability_reachability,
    capability_reachability_questions,
    parse_capability_reachability_payload,
)
from _04_Nucleo_Operativo.derivation_contracts import (
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
)
from _04_Nucleo_Operativo.knowledge_contracts import (
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_text
from _04_Nucleo_Operativo.text_derivation_repository import (
    TextDerivationAttemptStart,
    begin_text_derivation_attempt,
    compute_text_fts_fingerprint,
    compute_text_representation_fingerprint,
    succeed_text_derivation_attempt,
)
from _04_Nucleo_Operativo.text_state import (
    TEXT_SCHEMA_VERSION,
    initialize_text_state,
    text_database,
)

STARTED = "2026-08-12T00:00:00Z"
FINISHED = "2026-08-12T00:00:01Z"


def _manifest(implementation_id: str):
    return next(
        item for item in CAPABILITY_MANIFESTS if item.implementation_id == implementation_id
    )


def _start(
    attempt_id: str,
    implementation_id: str = TEXT_BUILTIN_IMPLEMENTATION_ID,
    *,
    provider_override: str | None = None,
) -> TextDerivationAttemptStart:
    manifest = _manifest(implementation_id)
    resource = ResourceRef(f"resource:{attempt_id}", "text", "text")
    revision = RevisionRef(
        resource.resource_id,
        f"revision:{attempt_id}",
        "text.source",
        "raw-input-v1",
        None,
        RevisionState.CURRENT,
        STARTED,
    )
    provider = manifest.provider if provider_override is None else provider_override
    return TextDerivationAttemptStart(
        attempt_id=attempt_id,
        stage=StageDescriptor(
            manifest.capability_id,
            manifest.capability_version,
            f"processing:{attempt_id}",
            implementation_digest="sha256:fixture-implementation",
            provider=provider,
            provider_version=manifest.provider_version,
        ),
        inputs=(InputBinding("source", revision, f"fingerprint:{attempt_id}"),),
        effective_configuration=(
            ("capability_id", manifest.capability_id),
            ("capability_implementation", manifest.implementation_id),
            ("capability_manifest_fingerprint", manifest.contract_fingerprint),
            ("capability_provider", manifest.provider),
            ("capability_provider_version", manifest.provider_version),
        ),
        runtime=(("python", "3.14"),),
        started_at_utc=STARTED,
        started_monotonic_ns=1,
        attempt=1,
        run_id=f"run:{attempt_id}",
        correlation_id=f"correlation:{attempt_id}",
        recorded_ns=1,
    )


def _insert_document(connection: sqlite3.Connection, start: TextDerivationAttemptStart) -> None:
    text = "capability evidence"
    fingerprint = fingerprint_text(text)
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
        updated_ns,revision_id)
        VALUES(?,?,?,?,? ,?,'complete','txt','text/plain',?,?,?,?,?,?)""",
        (
            "file-capability",
            "/fixture/capability.txt",
            len(text),
            1,
            -1,
            start.stage.processing_signature,
            zlib.compress(text.encode()),
            len(text),
            fingerprint.xxh3_128,
            1,
            2,
            start.inputs[0].revision.revision_id,
        ),
    )
    connection.execute(
        """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
        VALUES('file-capability','/fixture/capability.txt','txt','','',?)""",
        (text,),
    )


def _outputs(start: TextDerivationAttemptStart) -> tuple[OutputBinding, ...]:
    text = "capability evidence"
    resource = ResourceRef(start.inputs[0].revision.resource_id, "text", "text")
    revision = start.inputs[0].revision
    return (
        OutputBinding(
            "text_representation",
            MaterializationRef(
                "text",
                "text_representation",
                f"materialization:{start.attempt_id}:representation",
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
                f"materialization:{start.attempt_id}:fts",
                TEXT_SCHEMA_VERSION,
                resource,
                revision,
            ),
            compute_text_fts_fingerprint(
                "file-capability",
                text=text,
                content_kind="txt",
                title=None,
                author=None,
            ),
        ),
    )


def _publish_builtin_result(state: Path) -> None:
    path = state / "text.sqlite3"
    start = _start("attempt:published")
    begin_text_derivation_attempt(path, start)
    with text_database(path, create=False) as connection:
        _insert_document(connection, start)
        succeed_text_derivation_attempt(
            connection,
            start.attempt_id,
            receipt_id="receipt:published",
            outputs=_outputs(start),
            finished_at_utc=FINISHED,
            duration_ns=100,
            execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT,
            terminal_ns=2,
            document_file_key="file-capability",
        )
        connection.commit()


def test_empty_current_text_state_observes_declarations_without_inventing_reachability(
    tmp_path: Path,
) -> None:
    initialize_text_state(tmp_path / "text.sqlite3")
    before = (tmp_path / "text.sqlite3").read_bytes()

    result = analyze_capability_reachability(tmp_path, source_version="source-fixture")

    assert (tmp_path / "text.sqlite3").read_bytes() == before
    assert not tuple(tmp_path.glob("text.sqlite3-*"))
    assert result.status == "ready"
    assert result.total_attempts == 0
    assert result.manifest_count == 2
    assert {item.reachability for item in result.observations} == {"declared_only"}
    assert all(item.route_registered for item in result.observations)
    specs, evaluations = capability_reachability_questions(result, rank_offset=0)
    assert len(specs) == 1
    assert len(evaluations) == 2
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None and not item.mutation_authority for item in evaluations)
    assert parse_capability_reachability_payload(result.as_payload()) == result


def test_published_text_head_is_durable_reachability_not_user_value(tmp_path: Path) -> None:
    initialize_text_state(tmp_path / "text.sqlite3")
    _publish_builtin_result(tmp_path)

    result = analyze_capability_reachability(tmp_path, source_version="source-fixture")

    by_id = {item.implementation_id: item for item in result.observations}
    builtin = by_id[TEXT_BUILTIN_IMPLEMENTATION_ID]
    legacy = by_id[TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID]
    assert builtin.reachability == "published_result_observed"
    assert builtin.attempts == builtin.manifest_matching_attempts == 1
    assert builtin.receipt_linked_terminal_attempts == 1
    assert builtin.outbox_linked_terminal_attempts == 1
    assert builtin.total_output_bindings == 2
    assert builtin.total_current_materialization_heads == 2
    assert builtin.manifest_matching_output_bindings == 2
    assert builtin.manifest_matching_current_heads == 2
    assert builtin.durable_integrity_gap_attempt_ids == ()
    assert legacy.reachability == "declared_only"
    _specs, evaluations = capability_reachability_questions(result, rank_offset=4)
    builtin_evaluation = next(
        item for item in evaluations if item.subject.display_name == TEXT_BUILTIN_IMPLEMENTATION_ID
    )
    assert builtin_evaluation.rank == 5
    assert builtin_evaluation.decision_readiness == "experiment_required"
    assert "user_visible_consumer_observed" in {
        item.requirement_id for item in builtin_evaluation.requirements if item.status == "missing"
    }


def test_provider_name_mismatch_is_preserved_and_never_becomes_change_authority(
    tmp_path: Path,
) -> None:
    initialize_text_state(tmp_path / "text.sqlite3")
    begin_text_derivation_attempt(
        tmp_path / "text.sqlite3",
        _start("attempt:mismatch", provider_override="forged-provider"),
    )

    result = analyze_capability_reachability(tmp_path, source_version="source-fixture")

    builtin = next(
        item
        for item in result.observations
        if item.implementation_id == TEXT_BUILTIN_IMPLEMENTATION_ID
    )
    assert builtin.reachability == "declared_only"
    assert builtin.manifest_matching_attempts == 0
    assert builtin.manifest_mismatch_attempt_ids == ("attempt:mismatch",)
    _specs, evaluations = capability_reachability_questions(result, rank_offset=0)
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision is None for item in evaluations)


def test_missing_future_and_bounded_state_abstain_without_partial_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = analyze_capability_reachability(tmp_path, source_version="source-fixture")
    assert missing.status == "abstained"
    assert capability_reachability_questions(missing, rank_offset=0) == ((), ())

    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    future = analyze_capability_reachability(tmp_path, source_version="source-fixture")
    assert future.status == "abstained"
    assert future.observations == ()

    path.unlink()
    initialize_text_state(path)
    begin_text_derivation_attempt(path, _start("attempt:one"))
    monkeypatch.setattr(analysis_module, "CAPABILITY_REACHABILITY_MAX_ATTEMPTS", 0)
    bounded = analyze_capability_reachability(tmp_path, source_version="source-fixture")
    assert bounded.status == "abstained"
    assert bounded.total_attempts is None


def test_payload_parser_rejects_unknown_fields_and_semantic_forgery(tmp_path: Path) -> None:
    initialize_text_state(tmp_path / "text.sqlite3")
    result = analyze_capability_reachability(tmp_path, source_version="source-fixture")
    payload = result.as_payload()
    forged = dict(payload)
    forged["recommendation"] = "delete capability"
    with pytest.raises(ValueError, match="fields"):
        parse_capability_reachability_payload(forged)

    observation = dict(json.loads(json.dumps(payload))["observations"][0])
    observation["reachability"] = "published_result_observed"
    altered = dict(payload)
    altered["observations"] = [observation, json.loads(json.dumps(payload))["observations"][1]]
    with pytest.raises(ValueError, match="reachability"):
        parse_capability_reachability_payload(altered)


def test_only_text_extract_manifests_are_in_scope() -> None:
    assert TEXT_EXTRACT_CAPABILITY_ID == "text.extract"
    assert {
        item.implementation_id
        for item in CAPABILITY_MANIFESTS
        if item.capability_id == TEXT_EXTRACT_CAPABILITY_ID
    } == {TEXT_BUILTIN_IMPLEMENTATION_ID, TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID}
    assert CODE_CAPABILITY_REACHABILITY_SCHEMA.endswith("/v1")
