"""Adversarial model/generation binding checks for Semantic publication heads.

The fixtures intentionally create two independently valid text heads before
performing one database-local head corruption.  Readers and the Code writer
must reject only the crossed or malformed boundary; immutable member, receipt,
and Code facts remain available for diagnosis.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.code.code_schema import readonly_code_database
from neocortex.code.search.code_semantic_links import (
    CodeSemanticLinkError,
    code_semantic_search_availability,
    synchronize_code_embedding_links,
)
from neocortex.semantic.semantic_config import (
    compact_multilingual_text_model,
    multilingual_text_model,
)
from neocortex.semantic.semantic_lineage_repository import explain_text_chunk_lineage
from neocortex.semantic.semantic_models import (
    EmbeddingModelSpec,
    TextChunk,
    encode_vector,
)
from neocortex.semantic.semantic_publication_heads import (
    PublicationHeadsError,
    observe_integrated_owner_heads,
    observe_semantic_generation_heads,
)
from neocortex.semantic.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
    start_embedding_generation,
)
from tests.test_semantic_generation_control_projection import _migrate_v5_to_v7
from tests.test_semantic_generation_publication_v6 import _create_populated_v5
from tests.test_semantic_state import _stage_text_item, _text_model
from tests.test_semantic_v7_read_compatibility import (
    _create_code_owner,
    _insert_current_code_link,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


@dataclass(frozen=True, slots=True)
class _TwoModelFixture:
    database: Path
    model_a: EmbeddingModelSpec
    generation_a: int
    chunk_a: TextChunk
    model_b: EmbeddingModelSpec
    generation_b: int
    chunk_b: TextChunk


def _owner_files(database: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    }


def _code_domain_rows(code: Path) -> tuple[tuple[object, ...], ...]:
    with readonly_code_database(code) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                """SELECT chunk_id,semantic_item_id,model_signature,vector_space,
                    generation_id,active,provenance_json
                FROM embedding_links
                ORDER BY chunk_id,model_signature,generation_id"""
            ).fetchall()
        )


def _code_snapshot(code: Path) -> tuple[dict[str, bytes], tuple[tuple[object, ...], ...]]:
    return _owner_files(code), _code_domain_rows(code)


def _zero_vector(model: EmbeddingModelSpec) -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (model.dimensions - 1)


def _publish_api_generation(
    database: Path,
    model: EmbeddingModelSpec,
    *,
    item_id: str,
    generation_tag: str,
    started_ns: int,
) -> tuple[int, TextChunk]:
    _item, chunk = _stage_text_item(
        database,
        item_id,
        f"Contenido de prueba para el head {generation_tag}.",
        refresh=f"head-binding-refresh-{generation_tag}",
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"head-binding-generation-{generation_tag}",
        provenance={"fixture": "semantic-head-model-binding", "head": generation_tag},
        started_ns=started_ns,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=started_ns + 1) == 1
    lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id=f"head-binding-worker-{generation_tag}",
        now_ns=started_ns + 2,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id=f"head-binding-worker-{generation_tag}",
        vector=_zero_vector(model),
        now_ns=started_ns + 3,
    )
    summary = finalize_embedding_generation(database, generation_id, completed_ns=started_ns + 4)
    assert summary.status == "ready"
    assert summary.model_signature == model.model_signature
    return generation_id, chunk


def _create_two_model_fixture(tmp_path: Path) -> _TwoModelFixture:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    model_a = multilingual_text_model()
    model_b = compact_multilingual_text_model()
    register_embedding_model(database, model_a)
    register_embedding_model(database, model_b)
    generation_a, chunk_a = _publish_api_generation(
        database,
        model_a,
        item_id="head-binding-model-a-item",
        generation_tag="model-a",
        started_ns=100,
    )
    generation_b, chunk_b = _publish_api_generation(
        database,
        model_b,
        item_id="head-binding-model-b-item",
        generation_tag="model-b",
        started_ns=200,
    )
    return _TwoModelFixture(
        database,
        model_a,
        generation_a,
        chunk_a,
        model_b,
        generation_b,
        chunk_b,
    )


def _assert_two_valid_heads(fixture: _TwoModelFixture) -> None:
    assert set(observe_semantic_generation_heads(fixture.database.parent)) == {
        (fixture.model_a.model_signature, fixture.generation_a),
        (fixture.model_b.model_signature, fixture.generation_b),
    }
    with semantic_database(fixture.database, readonly=True) as connection:
        rows = connection.execute(
            """SELECT h.model_signature,h.generation_id,
                g.model_signature,m.model_signature,member.model_signature
            FROM published_embedding_heads h
            JOIN embedding_generations g ON g.generation_id=h.generation_id
            JOIN embedding_models m ON m.model_signature=h.model_signature
            JOIN embedding_generation_members member
              ON member.generation_id=g.generation_id
            ORDER BY h.model_signature"""
        ).fetchall()
    by_model = {str(row[0]): tuple(row) for row in rows}
    assert by_model == {
        fixture.model_a.model_signature: (
            fixture.model_a.model_signature,
            fixture.generation_a,
            fixture.model_a.model_signature,
            fixture.model_a.model_signature,
            fixture.model_a.model_signature,
        ),
        fixture.model_b.model_signature: (
            fixture.model_b.model_signature,
            fixture.generation_b,
            fixture.model_b.model_signature,
            fixture.model_b.model_signature,
            fixture.model_b.model_signature,
        ),
    }


def _cross_head_a_to_b(fixture: _TwoModelFixture) -> None:
    """Leave one head A -> generation B, with FK enforcement still enabled."""

    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        connection.execute(
            "DELETE FROM published_embedding_heads WHERE model_signature=?",
            (fixture.model_b.model_signature,),
        )
        connection.execute(
            "UPDATE published_embedding_heads SET generation_id=? WHERE model_signature=?",
            (fixture.generation_b, fixture.model_a.model_signature),
        )
        connection.commit()


def _create_legacy_two_model_fixture(tmp_path: Path) -> _TwoModelFixture:
    """Extend the canonical v5->v6/v7 member-only fixture with model B."""

    database = tmp_path / "semantic.sqlite3"
    model_a, chunk_a = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    model_b = _text_model("legacy-head-model-b", "legacy-head-space-b")
    with semantic_database(database, readonly=True) as connection:
        revision = connection.execute(
            """SELECT item_revision_id,chunk_revision_id
            FROM embedding_generation_members
            WHERE entity_kind='text_chunk' AND entity_id=?
            ORDER BY member_id LIMIT 1""",
            (chunk_a.chunk_id,),
        ).fetchone()
    assert revision is not None
    vector_blob, original_norm = encode_vector(
        _zero_vector(model_b),
        model_b.dimensions,
        model_b.vector_dtype,
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        connection.execute(
            """INSERT INTO vector_spaces(
                vector_space,dimensions,distance,normalization,created_ns)
            VALUES(?,?,?,?,?)""",
            (model_b.vector_space, model_b.dimensions, model_b.distance, model_b.normalization, 20),
        )
        connection.execute(
            """INSERT INTO embedding_models(
                model_signature,vector_space,modality,model_id,model_version,
                dimensions,provider,supported_roles_json,vector_dtype,
                normalization,distance,provenance_json,active,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                model_b.model_signature,
                model_b.vector_space,
                model_b.modality.value,
                model_b.model_id,
                model_b.model_version,
                model_b.dimensions,
                model_b.provider,
                json.dumps([role.value for role in model_b.supported_roles]),
                model_b.vector_dtype.value,
                model_b.normalization,
                model_b.distance,
                json.dumps(model_b.provenance, sort_keys=True),
                1,
                20,
            ),
        )
        generation = connection.execute(
            """INSERT INTO embedding_generations(
                model_signature,processing_signature,status,provenance_json,
                cursor_json,started_ns,completed_ns,pending_count,leased_count,
                done_count,error_count,stale_count,base_generation_id,
                base_clone_complete)
            VALUES(?,?,'ready','{}','{}',?,?,0,0,1,0,0,NULL,1)""",
            (model_b.model_signature, "legacy-head-model-b-generation", 21, 22),
        )
        generation_b = int(generation.lastrowid)
        payload = connection.execute(
            """INSERT INTO vector_payloads(
                model_signature,content_xxh3_128,content_bytes,
                content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
                original_norm,provenance_json,legacy_before_receipts,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                model_b.model_signature,
                chunk_a.fingerprint.xxh3_128,
                chunk_a.fingerprint.byte_count,
                chunk_a.fingerprint.xxh3_64_guard,
                model_b.dimensions,
                model_b.vector_dtype.value,
                vector_blob,
                original_norm,
                "{}",
                1,
                23,
            ),
        )
        payload_id = int(payload.lastrowid)
        connection.execute(
            """INSERT INTO embedding_generation_members(
                generation_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,provenance_json,updated_ns)
            SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?""",
            (
                generation_b,
                model_b.model_signature,
                "text_chunk",
                chunk_a.chunk_id,
                "legacy-document",
                int(revision[0]),
                int(revision[1]),
                payload_id,
                chunk_a.fingerprint.xxh3_128,
                chunk_a.fingerprint.byte_count,
                chunk_a.fingerprint.xxh3_64_guard,
                "{}",
                24,
            ),
        )
        connection.execute(
            "INSERT INTO published_embedding_heads(model_signature,generation_id,published_ns) "
            "VALUES(?,?,?)",
            (model_b.model_signature, generation_b, 25),
        )
        connection.commit()
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_chunk_derivations WHERE chunk_revision_id=?",
            (int(revision[1]),),
        ).fetchone()[0] == 0
        a_head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (model_a.model_signature,),
        ).fetchone()
    assert a_head is not None
    return _TwoModelFixture(
        database,
        model_a,
        int(a_head[0]),
        chunk_a,
        model_b,
        generation_b,
        chunk_a,
    )


@pytest.mark.parametrize("slot", ("a", "b"))
def test_two_model_api_publication_binds_each_head_to_its_model_generation_and_member(
    tmp_path: Path,
    slot: str,
) -> None:
    fixture = _create_two_model_fixture(tmp_path)
    _assert_two_valid_heads(fixture)
    selected_model, selected_generation, selected_chunk = (
        (fixture.model_a, fixture.generation_a, fixture.chunk_a)
        if slot == "a"
        else (fixture.model_b, fixture.generation_b, fixture.chunk_b)
    )
    lineage = explain_text_chunk_lineage(
        fixture.database,
        chunk_id=selected_chunk.chunk_id,
        model_signature=selected_model.model_signature,
    )
    assert lineage.embeddings
    assert lineage.embeddings[0].generation_id == selected_generation
    assert lineage.embeddings[0].model_signature == selected_model.model_signature
    assert lineage.embeddings[0].published is True


@pytest.mark.parametrize("slot", ("a", "b"))
def test_publication_bridges_abstain_on_cross_model_head_in_each_direction(
    tmp_path: Path,
    slot: str,
) -> None:
    fixture = _create_two_model_fixture(tmp_path)
    _assert_two_valid_heads(fixture)
    if slot == "a":
        _cross_head_a_to_b(fixture)
    else:
        with closing(sqlite3.connect(fixture.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            connection.execute(
                "DELETE FROM published_embedding_heads WHERE model_signature=?",
                (fixture.model_a.model_signature,),
            )
            connection.execute(
                "UPDATE published_embedding_heads SET generation_id=? WHERE model_signature=?",
                (fixture.generation_a, fixture.model_b.model_signature),
            )
            connection.commit()
    before = _owner_files(fixture.database)
    with pytest.raises(PublicationHeadsError):
        observe_semantic_generation_heads(fixture.database.parent)
    with pytest.raises(PublicationHeadsError):
        observe_integrated_owner_heads(fixture.database.parent, include_code=False)
    assert _owner_files(fixture.database) == before


@pytest.mark.parametrize("published_only", (False, True))
def test_modern_lineage_preserves_chunk_receipts_but_not_crossed_embedding_publication(
    tmp_path: Path,
    published_only: bool,
) -> None:
    fixture = _create_two_model_fixture(tmp_path)
    _assert_two_valid_heads(fixture)
    _cross_head_a_to_b(fixture)
    before = _owner_files(fixture.database)
    lineage = explain_text_chunk_lineage(
        fixture.database,
        chunk_id=fixture.chunk_b.chunk_id,
        model_signature=fixture.model_b.model_signature,
        published_only=published_only,
    )
    assert lineage.published is True
    assert lineage.origins and lineage.origins[0].published is True
    assert lineage.origins[0].publication_receipt_id is not None
    if published_only:
        assert lineage.embeddings == ()
        assert lineage.embedding_count == 0
    else:
        assert len(lineage.embeddings) == 1
        assert lineage.embeddings[0].published is False
        assert lineage.embeddings[0].model_signature == fixture.model_b.model_signature
    assert _owner_files(fixture.database) == before


@pytest.mark.parametrize("published_only", (False, True))
def test_legacy_lineage_fallback_requires_matching_legacy_model_head(
    tmp_path: Path,
    published_only: bool,
) -> None:
    fixture = _create_legacy_two_model_fixture(tmp_path)
    _assert_two_valid_heads(fixture)
    _cross_head_a_to_b(fixture)
    before = _owner_files(fixture.database)
    lineage = explain_text_chunk_lineage(
        fixture.database,
        chunk_id=fixture.chunk_b.chunk_id,
        model_signature=fixture.model_b.model_signature,
        published_only=published_only,
    )
    assert lineage.origins == ()
    assert lineage.published is False
    if published_only:
        assert lineage.embeddings == ()
    else:
        assert len(lineage.embeddings) == 1
        assert lineage.embeddings[0].published is False
        assert lineage.embeddings[0].lineage_status == "legacy_unattributed"
    assert _owner_files(fixture.database) == before


def test_code_availability_rejects_crossed_model_with_real_current_link(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixture = _create_two_model_fixture(state_directory)
    _assert_two_valid_heads(fixture)
    _cross_head_a_to_b(fixture)
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=fixture.model_a, generation_id=fixture.generation_b)
    before = _code_snapshot(code)
    available = code_semantic_search_availability(state_directory, verify_model_cache=False)
    assert available.available is False
    assert available.reason == "default_profile_head_not_published"
    assert available.current_links == 0
    assert _code_snapshot(code) == before


def test_code_writer_rejects_crossed_model_head_without_mutating_code(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixture = _create_two_model_fixture(state_directory)
    _assert_two_valid_heads(fixture)
    _cross_head_a_to_b(fixture)
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=fixture.model_a, generation_id=fixture.generation_b)
    before = _code_snapshot(code)
    with pytest.raises(CodeSemanticLinkError, match=r"(?i)(model|binding|head)"):
        synchronize_code_embedding_links(
            state_directory,
            generation_id=fixture.generation_b,
            model_signature=fixture.model_a.model_signature,
        )
    assert _code_snapshot(code) == before


@pytest.mark.parametrize("status", ("ready_partial", "failed"))
def test_published_nonready_head_is_not_available_and_bridge_abstains(
    tmp_path: Path,
    status: str,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixture = _create_two_model_fixture(state_directory)
    candidate = start_embedding_generation(
        fixture.database,
        model_signature=fixture.model_a.model_signature,
        processing_signature=f"head-binding-{status}",
        started_ns=300,
    )
    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE embedding_generations SET status=?,completed_ns=? WHERE generation_id=?",
            (status, 301, candidate),
        )
        connection.execute(
            "UPDATE published_embedding_heads SET generation_id=? WHERE model_signature=?",
            (candidate, fixture.model_a.model_signature),
        )
        connection.commit()
    with pytest.raises(PublicationHeadsError):
        observe_semantic_generation_heads(fixture.database.parent)
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=fixture.model_a, generation_id=candidate)
    before = _code_snapshot(code)
    available = code_semantic_search_availability(state_directory, verify_model_cache=False)
    assert (available.available, available.reason, available.generation_id) == (
        False,
        "default_profile_head_not_published",
        None,
    )
    assert _code_snapshot(code) == before


def test_code_writer_rejects_v7_head_without_mutating_code(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    semantic = state_directory / "semantic.sqlite3"
    model, _chunk = _create_populated_v5(semantic)
    _migrate_v5_to_v7(semantic)
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=model, generation_id=1)
    before = _code_snapshot(code)
    with pytest.raises(CodeSemanticLinkError, match=r"(?i)(schema|8|contract)"):
        synchronize_code_embedding_links(
            state_directory,
            generation_id=1,
            model_signature=model.model_signature,
        )
    assert _code_snapshot(code) == before


@pytest.mark.parametrize("mutation", ("index_missing", "trigger_missing", "metadata_noncanonical"))
def test_code_writer_rejects_v8_contract_drift_without_mutating_code(
    tmp_path: Path,
    mutation: str,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    fixture = _create_two_model_fixture(state_directory)
    _assert_two_valid_heads(fixture)
    semantic = fixture.database
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=fixture.model_a, generation_id=fixture.generation_a)
    with closing(sqlite3.connect(semantic)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if mutation == "index_missing":
            connection.execute("DROP INDEX embedding_jobs_source_dirty_idx")
        elif mutation == "trigger_missing":
            connection.execute("DROP TRIGGER text_chunks_embedding_jobs_source_dirty_update")
        elif mutation == "metadata_noncanonical":
            connection.execute("UPDATE metadata SET value='08' WHERE key='schema_version'")
        else:  # pragma: no cover - parameter table is exhaustive
            raise AssertionError(mutation)
        connection.commit()
    before = _code_snapshot(code)
    with pytest.raises(CodeSemanticLinkError, match=r"(?i)(schema|contract|incompatible)"):
        synchronize_code_embedding_links(
            state_directory,
            generation_id=fixture.generation_a,
            model_signature=fixture.model_a.model_signature,
        )
    assert _code_snapshot(code) == before


__all__ = [
    "_create_two_model_fixture",
]
