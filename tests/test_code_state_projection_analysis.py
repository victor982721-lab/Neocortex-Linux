from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_state_projection_analysis as projection_module
from _04_Nucleo_Operativo.code_state_projection_analysis import (
    analyze_text_semantic_projection,
    parse_code_state_projection_payload,
    state_projection_questions,
)
from _04_Nucleo_Operativo.derivation_contracts import (
    MaterializationRef,
)
from _04_Nucleo_Operativo.knowledge_contracts import (
    PhysicalIdentityRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from _04_Nucleo_Operativo.semantic_models import canonical_json
from _04_Nucleo_Operativo.semantic_schema import initialize_semantic_state, semantic_database
from _04_Nucleo_Operativo.text_state import initialize_text_state, text_database


def _source_revision(revision_id: str) -> str:
    resource = ResourceRef(
        resource_id="resource:file:volume:file:-1",
        source_kind="text",
        physical_identity=PhysicalIdentityRef(
            scheme="posix_device_inode_birthtime",
            value="volume:file:-1",
            identity_version=1,
        ),
        current_path="/fixture/document.txt",
        owner="text",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=revision_id,
        producer="text.source",
        processing_signature="text-source-v1",
        generation=None,
        state=RevisionState.CURRENT,
    )
    materialization = MaterializationRef(
        materialization_id=f"materialization:{revision_id}",
        owner="text",
        kind="text_representation",
        schema_version=2,
        resource=resource,
        revision=revision,
    )
    return canonical_json(
        {
            "revision_id": revision_id,
            "owner_revision": {"owner": "text", "revision": revision.to_dict()},
            "consumed_materialization": {
                "materialization": materialization.to_dict(),
            },
        }
    )


def _insert_text_row(
    connection: sqlite3.Connection,
    *,
    file_key: str,
    revision_id: str,
    text_chars: int,
) -> None:
    connection.execute(
        """INSERT INTO text_input_revisions(
        revision_id,resource_id,producer,processing_signature,generation,
        revision_state,observed_at_utc,fingerprint_algorithm,fingerprint,recorded_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            revision_id,
            f"resource:{file_key}",
            "text.source",
            "text-source-v1",
            None,
            "current",
            None,
            "xxh3-128",
            "f" * 32,
            1,
        ),
    )
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,metadata_json,text_zlib,text_chars,text_xxh3_128,
        text_truncated,retryable,last_seen_run_id,updated_ns,revision_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            file_key,
            f"/fixture/{file_key}.txt",
            text_chars,
            1,
            -1,
            "text-route-v1",
            "complete",
            "text",
            "text/plain",
            "{}",
            b"body" if text_chars else None,
            text_chars,
            "a" * 32,
            0,
            0,
            1,
            1,
            revision_id,
        ),
    )


def _build_state(state_directory: Path, *, include_second_revision: bool = True) -> None:
    state_directory.mkdir(parents=True)
    text_path = state_directory / "text.sqlite3"
    semantic_path = state_directory / "semantic.sqlite3"
    initialize_text_state(text_path)
    initialize_semantic_state(semantic_path)
    with text_database(text_path, readonly=False, create=False) as connection:
        _insert_text_row(
            connection,
            file_key="eligible-1",
            revision_id="revision:text:one",
            text_chars=4,
        )
        if include_second_revision:
            _insert_text_row(
                connection,
                file_key="eligible-2",
                revision_id="revision:text:two",
                text_chars=4,
            )
        _insert_text_row(
            connection,
            file_key="empty-control",
            revision_id="revision:text:empty",
            text_chars=0,
        )
        connection.commit()
    with semantic_database(semantic_path) as connection:
        connection.execute(
            "INSERT INTO vector_spaces VALUES(?,?,?,?,?)",
            ("space-v1", 2, "cosine", "l2", 1),
        )
        connection.execute(
            """INSERT INTO embedding_models(
            model_signature,vector_space,modality,model_id,model_version,dimensions,
            provider,supported_roles_json,vector_dtype,normalization,distance,
            provenance_json,active,created_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "model-v1",
                "space-v1",
                "text",
                "fixture",
                "1",
                2,
                "fixture",
                "[]",
                "float32",
                "l2",
                "cosine",
                "{}",
                1,
                1,
            ),
        )
        generation_id = connection.execute(
            """INSERT INTO embedding_generations(
            model_signature,processing_signature,status,provenance_json,cursor_json,
            started_ns,completed_ns) VALUES(?,?,?,?,?,?,?) RETURNING generation_id""",
            ("model-v1", "semantic-v1", "ready", "{}", "{}", 1, 2),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO published_embedding_heads VALUES(?,?,?)",
            ("model-v1", generation_id, 2),
        )
        connection.execute(
            """INSERT INTO semantic_items(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,provenance_json,
            refresh_token,active,updated_ns,source_revision_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "item:one",
                "text",
                "eligible-1",
                "v1",
                "/fixture/eligible-1.txt",
                "a" * 32,
                4,
                "b" * 16,
                "{}",
                "refresh",
                1,
                1,
                _source_revision("revision:text:one"),
            ),
        )
        item_revision_id = connection.execute(
            """INSERT INTO semantic_item_revisions(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,provenance_json,
            source_revision_json,captured_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            RETURNING item_revision_id""",
            (
                "item:one",
                "text",
                "eligible-1",
                "v1",
                "/fixture/eligible-1.txt",
                "a" * 32,
                4,
                "b" * 16,
                "{}",
                _source_revision("revision:text:one"),
                1,
            ),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO semantic_chunk_revisions(
            chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
            text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,captured_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "chunk:one",
                "item:one",
                0,
                "document",
                "fulltext",
                0,
                4,
                b"body",
                4,
                "a" * 32,
                4,
                "b" * 16,
                "chunk-v1",
                "{}",
                1,
            ),
        )
        chunk_revision_id = connection.execute(
            "SELECT chunk_revision_id FROM semantic_chunk_revisions WHERE chunk_id='chunk:one'"
        ).fetchone()[0]
        payload_id = connection.execute(
            """INSERT INTO vector_payloads(
            model_signature,content_xxh3_128,content_bytes,content_xxh3_64_guard,
            dimensions,vector_dtype,vector_blob,original_norm,provenance_json,
            created_ns,legacy_before_receipts) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            RETURNING payload_id""",
            (
                "model-v1",
                "a" * 32,
                4,
                "b" * 16,
                2,
                "float32",
                b"vector",
                1.0,
                "{}",
                1,
                0,
            ),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO embedding_generation_members(
            generation_id,model_signature,entity_kind,entity_id,item_id,
            item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
            content_bytes,content_xxh3_64_guard,provenance_json,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                "model-v1",
                "text_chunk",
                "chunk:one",
                "item:one",
                item_revision_id,
                chunk_revision_id,
                payload_id,
                "a" * 32,
                4,
                "b" * 16,
                "{}",
                1,
            ),
        )


def test_projection_excludes_empty_text_and_observes_exact_alignment(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory, include_second_revision=False)

    result = analyze_text_semantic_projection(state_directory, source_version="fixture")

    assert result.status == "ready"
    assert result.observation == "aligned"
    assert result.complete_text_rows == 2
    assert result.eligible_text_rows == 1
    assert result.excluded_empty_text_rows == 1
    assert result.heads[0].aligned is True
    assert result.heads[0].published_chunks == 1
    assert result.inference_status == "abstained"
    assert result.decision_readiness == "experiment_required"
    assert result.decision is None
    assert result.mutation_authority is False


def test_projection_reports_missing_revision_as_observation_not_defect(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory, include_second_revision=True)

    result = analyze_text_semantic_projection(state_directory, source_version="fixture")

    assert result.status == "ready"
    assert result.observation == "delta_observed"
    assert result.heads[0].missing_revision_ids == ("revision:text:two",)
    assert result.inference_status == "abstained"
    assert result.decision is None
    assert result.next_action_ids == (
        "inspect_build_freshness_recovery_and_reconciliation",
        "design_process_death_experiment_if_delta_persists",
    )


def test_projection_abstains_if_the_owner_vector_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory, include_second_revision=False)
    capture = projection_module.capture_sqlite_immutable_fence
    calls = 0

    def changed_on_final_capture(path: Path):
        nonlocal calls
        calls += 1
        fence = capture(path)
        if calls == 4:
            return replace(fence, main=replace(fence.main, mtime_ns=fence.main.mtime_ns + 1))
        return fence

    monkeypatch.setattr(
        projection_module,
        "capture_sqlite_immutable_fence",
        changed_on_final_capture,
    )

    result = analyze_text_semantic_projection(state_directory, source_version="fixture")

    assert result.status == "abstained"
    assert result.reason == "owner_state_changed_during_projection"
    assert result.heads == ()
    assert result.decision_readiness == "abstained"


def test_projection_wire_rejects_an_invented_alignment(tmp_path: Path) -> None:
    payload = json.loads(
        json.dumps(projection_module.abstained_code_state_projection("fixture").as_payload())
    )
    payload["authority"] = "decision"

    with pytest.raises(ValueError, match="advisory"):
        parse_code_state_projection_payload(payload)


def test_projection_question_links_exact_state_but_requires_recovery_evidence(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    _build_state(state_directory, include_second_revision=False)
    analysis = analyze_text_semantic_projection(state_directory, source_version="fixture")

    specs, evaluations = state_projection_questions(analysis, rank=3)

    assert len(specs) == len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.rank == 3
    assert evaluation.observation_status == "confirmed"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.counterevidence_status == "not_evaluated"
    assert {item.source_record_kind for item in evaluation.evidence} == {
        "stable_cross_owner_snapshot",
        "published_head_revision_projection",
    }
    assert evaluation.decision is None


def test_abstained_projection_question_cannot_publish_partial_evidence() -> None:
    analysis = projection_module.abstained_code_state_projection("fixture")

    _specs, evaluations = state_projection_questions(analysis, rank=1)

    assert evaluations[0].observation_status == "abstained"
    assert evaluations[0].evidence == ()
    assert evaluations[0].decision_readiness == "abstained"
    assert evaluations[0].next_action_ids == ()
