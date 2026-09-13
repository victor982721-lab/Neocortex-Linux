"""Read compatibility for Semantic schema v7, v8, and current schema v9.

These tests exercise the bounded read consumers that may accept a validated
legacy Semantic owner.  Protocol/current writers are exercised at exact v8/v9
for transition coverage, while the Code-link writer remains v9-only.  Every owner
is a temporary fixture and every compatibility read is checked for byte stability.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_route import CodeRoute
from neocortex.code.search.code_semantic_links import (
    CodeSemanticLinkError,
    code_semantic_search_availability,
    synchronize_code_embedding_links,
)
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_snapshot import (
    KnowledgeStatePaths,
    collect_knowledge_snapshot,
)
from neocortex.semantic import semantic_schema
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.semantic.semantic_lineage_repository import explain_text_chunk_lineage
from neocortex.semantic.semantic_models import EmbeddingModelSpec
from neocortex.semantic.semantic_plan_errors import SemanticPlanBlocked
from neocortex.semantic.semantic_planner import plan_semantic_index
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    generation_summary,
    register_embedding_model,
    semantic_database,
    start_embedding_generation,
)
from tests.test_code_semantic_search import _FrameworkState, _Inventory
from tests.test_semantic_generation_control_projection import _migrate_v5_to_v7
from tests.test_semantic_generation_publication_v6 import _create_populated_v5
from tests.test_semantic_planner import _create_pdf_state
from tests.test_semantic_state import _stage_text_item, _text_model


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _database_files(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in root.glob("*.sqlite3*")
        if path.is_file()
    }


def _create_empty_version(database: Path, version: int) -> None:
    if version not in {7, 8, 9}:
        raise ValueError(f"unsupported Semantic fixture version: {version}")
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for target in range(1, version + 1):
            getattr(semantic_schema, f"_migrate_to_v{target}")(connection, target)
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )
        connection.commit()
    finally:
        connection.close()


def _create_empty_v7(database: Path) -> None:
    _create_empty_version(database, 7)


def _create_v8_empty(database: Path) -> None:
    """Build an actual v8 owner; do not initialize it as current v9."""

    _create_empty_version(database, 8)


def _create_v9_empty(database: Path) -> None:
    _create_empty_version(database, 9)


def _insert_semantic_head(
    database: Path,
    model: EmbeddingModelSpec,
) -> tuple[EmbeddingModelSpec, int]:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """INSERT INTO vector_spaces(
                vector_space,dimensions,distance,normalization,created_ns)
            VALUES(?,?,?,?,?)""",
            (
                model.vector_space,
                model.dimensions,
                model.distance,
                model.normalization,
                1,
            ),
        )
        connection.execute(
            """INSERT INTO embedding_models(
                model_signature,vector_space,modality,model_id,model_version,
                dimensions,provider,supported_roles_json,vector_dtype,
                normalization,distance,provenance_json,active,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                model.model_signature,
                model.vector_space,
                model.modality.value,
                model.model_id,
                model.model_version,
                model.dimensions,
                model.provider,
                json.dumps([role.value for role in model.supported_roles]),
                model.vector_dtype.value,
                model.normalization,
                model.distance,
                "{}",
                1,
                1,
            ),
        )
        generation = connection.execute(
            """INSERT INTO embedding_generations(
                model_signature,processing_signature,status,provenance_json,
                cursor_json,started_ns,completed_ns,pending_count,leased_count,
                done_count,error_count,stale_count,base_generation_id,
                base_clone_complete)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                model.model_signature,
                "v7-read-generation",
                "ready",
                "{}",
                "{}",
                1,
                2,
                0,
                0,
                0,
                0,
                0,
                None,
                1,
            ),
        )
        generation_id = int(generation.lastrowid)
        connection.execute(
            "INSERT INTO published_embedding_heads(model_signature,generation_id,published_ns) "
            "VALUES(?,?,?)",
            (model.model_signature, generation_id, 2),
        )
        connection.commit()
    return model, generation_id


def _create_v7_default_head(database: Path) -> tuple[EmbeddingModelSpec, int]:
    _create_empty_v7(database)
    return _insert_semantic_head(database, multilingual_text_model())


def _create_api_published_head(
    database: Path,
    *,
    version: int,
) -> tuple[EmbeddingModelSpec, int, object]:
    if version == 7:
        _create_empty_v7(database)
    elif version == 8:
        _create_v8_empty(database)
    elif version == 9:
        _create_v9_empty(database)
    else:
        raise ValueError(f"unsupported Semantic fixture version: {version}")
    model = _text_model(
        f"compatibility-api-v{version}",
        f"compatibility-api-space-v{version}",
    )
    register_embedding_model(database, model, allow_test_provider=True)
    _item, chunk = _stage_text_item(
        database,
        f"compatibility-api-item-v{version}",
        f"Contenido de publicación API para Semantic v{version}.",
        refresh=f"compatibility-api-refresh-v{version}",
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"compatibility-api-generation-v{version}",
        provenance={"fixture": "compatibility-api", "version": version},
        started_ns=20,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=21) == 1
    lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id=f"compatibility-api-worker-v{version}",
        now_ns=22,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id=f"compatibility-api-worker-v{version}",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=23,
    )
    assert finalize_embedding_generation(database, generation_id, completed_ns=24).status == "ready"
    return model, generation_id, chunk


def _semantic_owner(snapshot: KnowledgeSnapshot) -> OwnerSnapshot:
    return next(owner for owner in snapshot.owners if owner.owner == "semantic")


@pytest.mark.parametrize("version", (7, 8, 9))
def test_semantic_read_schema_accepts_only_exact_valid_v7_v8_or_v9(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    {
        7: _create_empty_v7,
        8: _create_v8_empty,
        9: _create_v9_empty,
    }[version](database)
    before = database.read_bytes()

    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == version

    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("version", "mutation"),
    (
        (7, "metadata_missing"),
        (7, "metadata_mismatch"),
        (7, "history_missing"),
        (7, "unknown_object"),
        (7, "trigger_missing"),
        (8, "index_missing"),
        (8, "trigger_missing"),
        (9, "index_missing"),
        (9, "trigger_missing"),
        (9, "future_version"),
    ),
)
def test_semantic_read_schema_rejects_malformed_legacy_current_and_future_without_writes(
    tmp_path: Path,
    version: int,
    mutation: str,
) -> None:
    database = tmp_path / f"semantic-{version}-{mutation}.sqlite3"
    {
        7: _create_empty_v7,
        8: _create_v8_empty,
        9: _create_v9_empty,
    }[version](database)
    with closing(sqlite3.connect(database)) as connection:
        if mutation == "metadata_missing":
            connection.execute("DELETE FROM metadata WHERE key='schema_version'")
        elif mutation == "metadata_mismatch":
            connection.execute(
                "UPDATE metadata SET value='6' WHERE key='schema_version'"
            )
        elif mutation == "history_missing":
            connection.execute("DELETE FROM schema_migrations WHERE version=7")
        elif mutation == "unknown_object":
            connection.execute("CREATE TABLE unexpected_semantic_read_object(value TEXT)")
        elif mutation == "trigger_missing":
            trigger = (
                "semantic_work_receipts_no_update"
                if version == 7
                else "text_chunks_embedding_jobs_source_dirty_update"
            )
            connection.execute(f"DROP TRIGGER {trigger}")
        elif mutation == "index_missing":
            connection.execute("DROP INDEX embedding_jobs_source_dirty_idx")
        elif mutation == "future_version":
            connection.execute("PRAGMA user_version=10")
            connection.execute(
                "UPDATE metadata SET value='10' WHERE key='schema_version'"
            )
        else:  # pragma: no cover - parameter table is exhaustive
            raise AssertionError(mutation)
        connection.commit()
    before = database.read_bytes()

    with semantic_database(database, readonly=True) as connection:
        with pytest.raises(SemanticStateError):
            semantic_schema._validate_semantic_read_schema(connection)

    assert database.read_bytes() == before


@pytest.mark.parametrize("version", (7, 8, 9))
def test_semantic_lineage_reads_v7_v8_and_v9_without_rewriting_state(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / "semantic-v7-populated.sqlite3"
    model, _generation_id, chunk = _create_api_published_head(
        database,
        version=version,
    )
    before = _database_files(tmp_path)

    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == version
    lineage = explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        model_signature=model.model_signature,
    )
    assert lineage.chunk_id == chunk.chunk_id
    assert lineage.embeddings
    assert lineage.embeddings[0].generation_id == 1
    assert lineage.embeddings[0].published is True
    assert _database_files(tmp_path) == before


def test_v7_terminal_member_only_head_keeps_stored_counts_for_readers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-v7-terminal.sqlite3"
    model, _chunk = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    before = _database_files(tmp_path)

    with semantic_database(database, readonly=True) as connection:
        generation = connection.execute(
            "SELECT generation_id,status,pending_count,leased_count,done_count,"
            "error_count,stale_count FROM embedding_generations "
            "WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
        jobs = int(connection.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0])
        members = int(
            connection.execute("SELECT COUNT(*) FROM embedding_generation_members").fetchone()[0]
        )
    assert generation is not None
    assert tuple(generation[1:]) == ("ready", 0, 0, 1, 0, 0)
    assert (jobs, members) == (0, 1)
    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == 7
    summary = generation_summary(database, int(generation[0]))
    assert (summary.status, summary.pending, summary.leased, summary.done) == (
        "ready",
        0,
        0,
        1,
    )
    assert _database_files(tmp_path) == before


@pytest.mark.parametrize("version", (7, 8, 9))
def test_knowledge_snapshot_accepts_v7_v8_or_v9_semantic_owner_and_preserves_warning(
    tmp_path: Path,
    version: int,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "semantic.sqlite3"
    {
        7: _create_empty_v7,
        8: _create_v8_empty,
        9: _create_v9_empty,
    }[version](database)
    before = _database_files(state_directory)

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state_directory),
        source_version=f"fixture-v{version}",
    )
    semantic = _semantic_owner(snapshot)
    assert semantic.state is OwnerAvailability.AVAILABLE
    assert semantic.expected_schema_version == 9
    assert semantic.observed_schema_version == version
    assert snapshot.consistency is SnapshotConsistency.STABLE
    if version < 9:
        assert semantic.warning is not None
        assert f"legacy_schema_read_compatible:{version}->9" in semantic.warning
    else:
        assert semantic.warning is None
    assert _database_files(state_directory) == before


@pytest.mark.parametrize("version", (7, 8, 9))
def test_publication_heads_read_v7_v8_and_v9_with_schema_bound_digest(
    tmp_path: Path,
    version: int,
) -> None:
    from neocortex.semantic.semantic_publication_heads import (
        observe_integrated_owner_heads,
        observe_semantic_generation_heads,
    )

    database = tmp_path / "semantic.sqlite3"
    model, generation_id, _chunk = _create_api_published_head(
        database,
        version=version,
    )
    before = _database_files(tmp_path)

    assert observe_semantic_generation_heads(tmp_path) == ((model.model_signature, generation_id),)
    owner = observe_integrated_owner_heads(tmp_path, include_code=False)[0]
    assert (owner.owner, owner.schema_version, owner.revision) == (
        "semantic",
        version,
        generation_id,
    )
    assert len(owner.digest_sha256) == 64
    assert _database_files(tmp_path) == before


def test_empty_publication_head_digest_distinguishes_observed_v7_v8_and_v9(
    tmp_path: Path,
) -> None:
    from neocortex.semantic.semantic_publication_heads import (
        observe_integrated_owner_heads,
        observe_semantic_generation_heads,
    )

    v7_root = tmp_path / "v7"
    v7_root.mkdir()
    _create_empty_v7(v7_root / "semantic.sqlite3")
    v8_root = tmp_path / "v8"
    v8_root.mkdir()
    _create_v8_empty(v8_root / "semantic.sqlite3")
    v9_root = tmp_path / "v9"
    v9_root.mkdir()
    _create_v9_empty(v9_root / "semantic.sqlite3")
    before7 = _database_files(v7_root)
    before8 = _database_files(v8_root)
    before9 = _database_files(v9_root)

    assert observe_semantic_generation_heads(v7_root) == ()
    assert observe_semantic_generation_heads(v8_root) == ()
    assert observe_semantic_generation_heads(v9_root) == ()
    v7_head = observe_integrated_owner_heads(v7_root, include_code=False)[0]
    v8_head = observe_integrated_owner_heads(v8_root, include_code=False)[0]
    v9_head = observe_integrated_owner_heads(v9_root, include_code=False)[0]
    assert (v7_head.revision, v7_head.schema_version) == (0, 7)
    assert (v8_head.revision, v8_head.schema_version) == (0, 8)
    assert (v9_head.revision, v9_head.schema_version) == (0, 9)
    assert len({v7_head.digest_sha256, v8_head.digest_sha256, v9_head.digest_sha256}) == 3
    assert _database_files(v7_root) == before7
    assert _database_files(v8_root) == before8
    assert _database_files(v9_root) == before9


def _create_code_owner(state_directory: Path) -> Path:
    source = state_directory.parent / "availability.py"
    source.write_text("def current_status():\n    return 'ready'\n", encoding="utf-8")
    code_path = state_directory / "code.sqlite3"
    CodeRoute(
        CodeRouteConfig(
            state_path=code_path,
            dedup_path=state_directory / "dedup.sqlite3",
        ),
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()
    return code_path


def _insert_current_code_link(
    code_path: Path,
    *,
    model: EmbeddingModelSpec,
    generation_id: int,
    active: int = 1,
) -> int:
    with closing(sqlite3.connect(code_path)) as connection:
        chunk_id = int(
            connection.execute(
                """SELECT chunk.chunk_id FROM code_chunks chunk
                JOIN file_versions version ON version.version_id=chunk.version_id
                JOIN files file ON file.current_version_id=version.version_id
                WHERE file.status='current' AND version.invalidated_ns IS NULL
                ORDER BY chunk.chunk_id LIMIT 1"""
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO embedding_links(
                chunk_id,semantic_item_id,model_signature,vector_space,
                generation_id,active,provenance_json)
            VALUES(?,?,?,?,?,?,?)""",
            (
                chunk_id,
                "item:code:compatibility",
                model.model_signature,
                model.vector_space,
                generation_id,
                active,
                "{}",
            ),
        )
        connection.commit()
    return chunk_id


@pytest.mark.parametrize("version", (7, 8))
def test_code_read_availability_accepts_legacy_v7_v8_but_writer_rejects_them(
    tmp_path: Path,
    version: int,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    semantic = state_directory / "semantic.sqlite3"
    if version == 7:
        model, generation_id = _create_v7_default_head(semantic)
    else:
        _create_v8_empty(semantic)
        model, generation_id = _insert_semantic_head(semantic, multilingual_text_model())
    code = _create_code_owner(state_directory)
    _insert_current_code_link(code, model=model, generation_id=generation_id)
    code_before_writer = code.read_bytes()

    available = code_semantic_search_availability(
        state_directory,
        verify_model_cache=False,
    )
    assert available.available is True
    assert available.generation_id == generation_id
    assert available.current_links == 1

    with pytest.raises(CodeSemanticLinkError, match=r"schema.*9|9.*schema"):
        synchronize_code_embedding_links(
            state_directory,
            generation_id=generation_id,
            model_signature=model.model_signature,
        )
    assert code.read_bytes() == code_before_writer

    with closing(sqlite3.connect(code)) as connection:
        connection.execute("UPDATE embedding_links SET active=0")
        connection.commit()
    no_current = code_semantic_search_availability(
        state_directory,
        verify_model_cache=False,
    )
    assert (no_current.available, no_current.reason, no_current.generation_id) == (
        False,
        "no_current_default_profile_links",
        None,
    )


def test_semantic_plan_reuses_v7_cache_version_and_fenced_digest_read_only(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("Contenido PDF para plan compatible v7.",))
    semantic = tmp_path / "semantic.sqlite3"
    _create_empty_v7(semantic)
    model = multilingual_text_model()
    register_embedding_model(semantic, model)
    before = _database_files(tmp_path)

    first = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        text_model=model,
        embed_ocr_text=False,
    )
    second = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("pdf",),
        text_model=model,
        embed_ocr_text=False,
    )
    assert first.semantic_schema_version == 7
    assert second.semantic_schema_version == 7
    assert first.semantic_snapshot_xxh3_128 == second.semantic_snapshot_xxh3_128
    assert first.plan_signature == second.plan_signature
    assert _database_files(tmp_path) == before


def test_semantic_plan_rejects_future_schema_as_typed_block_without_migration(
    tmp_path: Path,
) -> None:
    _create_pdf_state(tmp_path, ("Contenido PDF para plan futuro.",))
    semantic = tmp_path / "semantic.sqlite3"
    _create_v9_empty(semantic)
    with closing(sqlite3.connect(semantic)) as connection:
        connection.execute("PRAGMA user_version=10")
        connection.execute(
            "UPDATE metadata SET value='10' WHERE key='schema_version'"
        )
        connection.commit()
    before = _database_files(tmp_path)
    with pytest.raises(SemanticPlanBlocked):
        plan_semantic_index(
            tmp_path,
            scope="text",
            source_kinds=("pdf",),
            text_model=multilingual_text_model(),
            embed_ocr_text=False,
        )
    assert _database_files(tmp_path) == before
