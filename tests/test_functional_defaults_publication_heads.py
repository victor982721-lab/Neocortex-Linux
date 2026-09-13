"""Focused functional coverage for the fresh owner-head observer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import neocortex.semantic.semantic_publication_heads as publication_heads
from neocortex.code.code_state import CodeState
from neocortex.semantic.semantic_generation_repository import (
    finalize_embedding_generation,
    prepare_embedding_generation,
    start_embedding_generation,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic.semantic_publication_heads import (
    PublicationHeadsError,
    PublicationHeadsSchemaError,
    observe_integrated_owner_heads,
    observe_semantic_generation_heads,
)
from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.semantic.semantic_state import register_embedding_model


def _model(signature: str, modality: EmbeddingModality) -> EmbeddingModelSpec:
    roles = (
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE)
        if modality is EmbeddingModality.TEXT
        else (EmbeddingRole.IMAGE,)
    )
    return EmbeddingModelSpec(
        signature,
        f"space:{signature}",
        modality,
        f"fixture/{signature}",
        "1",
        4,
        "test-deterministic",
        roles,
    )


def _empty_generation(
    database: Path,
    model: EmbeddingModelSpec,
    *,
    processing_signature: str,
    started_ns: int,
) -> int:
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        materialize_base=True,
        started_ns=started_ns,
    )
    assert prepare_embedding_generation(
        database,
        generation_id,
        enumeration_complete=True,
    ) is None
    finalized = finalize_embedding_generation(
        database,
        generation_id,
        completed_ns=started_ns + 10,
    )
    assert finalized.status == "ready"
    return generation_id


def _semantic_fixture(path: Path) -> tuple[Path, EmbeddingModelSpec, EmbeddingModelSpec]:
    path.mkdir()
    database = path / "semantic.sqlite3"
    initialize_semantic_state(database)
    text_model = _model("fixture-text", EmbeddingModality.TEXT)
    image_model = _model("fixture-image", EmbeddingModality.IMAGE)
    register_embedding_model(database, text_model, allow_test_provider=True)
    register_embedding_model(database, image_model, allow_test_provider=True)
    return database, text_model, image_model


def test_missing_databases_are_an_explicit_empty_baseline_without_creation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"

    observed = observe_integrated_owner_heads(state, include_code=True)

    assert tuple(head.owner for head in observed) == ("semantic", "code")
    assert all(head.revision == 0 for head in observed)
    assert not (state / "semantic.sqlite3").exists()
    assert not (state / "code.sqlite3").exists()
    assert observe_semantic_generation_heads(state) == ()

    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    empty_database = observe_integrated_owner_heads(state, include_code=True)
    assert empty_database[0] == observed[0]
    assert empty_database[1] == observed[1]
    assert empty_database[0].schema_version == 8

    with CodeState(state / "code.sqlite3"):
        pass
    empty_code_database = observe_integrated_owner_heads(state, include_code=True)
    assert empty_code_database[1] == observed[1]


def test_all_published_text_and_image_heads_are_observed_not_just_max_generation(
    tmp_path: Path,
) -> None:
    database, text_model, image_model = _semantic_fixture(tmp_path / "state")
    text_generation = _empty_generation(
        database,
        text_model,
        processing_signature="fixture-text-v1",
        started_ns=100,
    )
    image_generation = _empty_generation(
        database,
        image_model,
        processing_signature="fixture-image-v1",
        started_ns=200,
    )

    legacy_heads = observe_semantic_generation_heads(database.parent)
    assert legacy_heads == (
        (image_model.model_signature, image_generation),
        (text_model.model_signature, text_generation),
    )
    observed = observe_integrated_owner_heads(database.parent)[0]
    assert observed.revision == max(text_generation, image_generation)
    assert observed.schema_version == 8
    assert observed.digest_sha256


def test_changing_a_non_maximum_model_head_changes_the_aggregate_digest(
    tmp_path: Path,
) -> None:
    database, text_model, image_model = _semantic_fixture(tmp_path / "state")
    first_text = _empty_generation(
        database,
        text_model,
        processing_signature="fixture-text-v1",
        started_ns=100,
    )
    _empty_generation(
        database,
        image_model,
        processing_signature="fixture-image-v1",
        started_ns=200,
    )
    _empty_generation(
        database,
        text_model,
        processing_signature="fixture-text-v2",
        started_ns=300,
    )
    second_image = _empty_generation(
        database,
        image_model,
        processing_signature="fixture-image-v2",
        started_ns=400,
    )

    before = observe_integrated_owner_heads(database.parent)[0]
    assert before.revision == second_image
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE published_embedding_heads SET generation_id=? "
            "WHERE model_signature=?",
            (first_text, text_model.model_signature),
        )
    after = observe_integrated_owner_heads(database.parent)[0]

    assert after.revision == before.revision
    assert after.digest_sha256 != before.digest_sha256
    assert observe_semantic_generation_heads(database.parent) == (
        (image_model.model_signature, second_image),
        (text_model.model_signature, first_text),
    )


def test_empty_code_graph_owner_is_observed_through_its_published_graph_head(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    code_database = state / "code.sqlite3"
    with CodeState(code_database) as code:
        store = code.graph_generation_store
        store.create_input_snapshot("fixture:snapshot", 0, (), created_ns=1)
        store.start_generation("fixture:snapshot", "fixture:generation", created_ns=2)
        store.complete_generation("fixture:generation", completed_ns=3)
        store.compare_and_swap_head(
            "default",
            expected_revision=0,
            expected_generation_id=None,
            generation_id="fixture:generation",
        )

    semantic, observed_code = observe_integrated_owner_heads(state, include_code=True)
    assert semantic.revision == 0
    assert observed_code.owner == "code"
    assert observed_code.revision == 1
    assert observed_code.digest_sha256


def test_building_head_and_future_schema_fail_closed_with_typed_errors(
    tmp_path: Path,
) -> None:
    database, text_model, _image_model = _semantic_fixture(tmp_path / "state")
    building = start_embedding_generation(
        database,
        model_signature=text_model.model_signature,
        processing_signature="fixture-building",
        materialize_base=True,
        started_ns=100,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO published_embedding_heads(model_signature,generation_id,published_ns) "
            "VALUES(?,?,?)",
            (text_model.model_signature, building, 110),
        )
    with pytest.raises(PublicationHeadsSchemaError):
        observe_integrated_owner_heads(database.parent)

    # Restore the fixture's owner before exercising a future-version refusal.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM published_embedding_heads")
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(PublicationHeadsError):
        observe_integrated_owner_heads(database.parent)


def test_unknown_nonempty_database_is_not_downgraded_to_the_empty_baseline(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "semantic.sqlite3") as connection:
        connection.execute("CREATE TABLE unknown_state(value TEXT)")
    with pytest.raises(PublicationHeadsSchemaError):
        observe_integrated_owner_heads(state)


def test_snapshot_budget_and_cooperative_cancellation_are_typed_and_bounded(
    tmp_path: Path,
) -> None:
    database, text_model, _image_model = _semantic_fixture(tmp_path / "state")
    _empty_generation(
        database,
        text_model,
        processing_signature="fixture-budget",
        started_ns=100,
    )
    writer = sqlite3.connect(database)
    try:
        writer.execute(
            "INSERT INTO metadata(key,value) VALUES('budget-fixture','active-wal')"
        )
        writer.commit()
        with pytest.raises(PublicationHeadsError):
            observe_integrated_owner_heads(
                database.parent,
                snapshot_budget=SQLiteSnapshotBudget(max_temporary_bytes=1),
            )
    finally:
        writer.close()

    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return True

    with pytest.raises(PublicationHeadsError):
        observe_integrated_owner_heads(database.parent, cancellation_check=cancel)
    assert calls >= 1

    with pytest.raises(PublicationHeadsError, match="deadline"):
        observe_integrated_owner_heads(database.parent, deadline_monotonic=0.0)


def test_sql_validation_interruption_preserves_cancellation_not_schema_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    original_validate = publication_heads._validate_semantic_read_schema
    entered_validation = False
    calls = 0

    def cancel_inside_validation() -> bool:
        nonlocal calls
        calls += 1
        return entered_validation

    def validate_inside_wrapper(connection: sqlite3.Connection) -> int:
        nonlocal entered_validation
        entered_validation = True
        return original_validate(connection)

    monkeypatch.setattr(
        publication_heads,
        "_validate_semantic_read_schema",
        validate_inside_wrapper,
    )
    with pytest.raises(PublicationHeadsError) as raised:
        observe_integrated_owner_heads(
            state,
            cancellation_check=cancel_inside_validation,
        )
    assert calls >= 1
    assert not isinstance(raised.value, PublicationHeadsSchemaError)


def test_code_sql_validation_interruption_preserves_cancellation_not_schema_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    initialize_semantic_state(state / "semantic.sqlite3")
    with CodeState(state / "code.sqlite3"):
        pass
    original_validate = publication_heads.validate_code_schema
    entered_validation = False
    calls = 0

    def cancel_inside_code_validation() -> bool:
        nonlocal calls
        calls += 1
        return entered_validation

    def validate_inside_wrapper(connection: sqlite3.Connection) -> None:
        nonlocal entered_validation
        entered_validation = True
        original_validate(connection)

    monkeypatch.setattr(
        publication_heads,
        "validate_code_schema",
        validate_inside_wrapper,
    )
    with pytest.raises(PublicationHeadsError) as raised:
        observe_integrated_owner_heads(
            state,
            include_code=True,
            cancellation_check=cancel_inside_code_validation,
        )
    assert calls >= 1
    assert not isinstance(raised.value, PublicationHeadsSchemaError)
