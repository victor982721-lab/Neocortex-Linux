from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import neocortex.semantic.derivation_projection as projection_module
from neocortex.semantic.derivation_contracts import (
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
    WorkOutcome,
    WorkReceipt,
)
from neocortex.semantic.derivation_projection import (
    DerivationProjectionEvent,
    projection_event_from_semantic_outbox,
    rebuild_derivation_projection,
)
from neocortex.knowledge.knowledge_contracts import (
    ResourceRef,
    RevisionRef,
    RevisionState,
)


def _revision(resource_id: str, revision_id: str, producer: str) -> RevisionRef:
    return RevisionRef(
        resource_id,
        revision_id,
        producer,
        f"{producer}-revision-v1",
        None,
        RevisionState.CURRENT,
    )


def _receipt(
    *,
    receipt_id: str,
    owner: str,
    stage_id: str,
    signature: str,
    input_binding: InputBinding,
    output: OutputBinding,
    causation_id: str | None = None,
    execution_mode: WorkExecutionMode = WorkExecutionMode.EXECUTED,
) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=receipt_id,
        owner=owner,
        stage=StageDescriptor(stage_id, "v1", signature),
        inputs=(input_binding,),
        outputs=(output,),
        effective_configuration=(),
        runtime=(("python", "3.14"),),
        started_at_utc="2026-08-11T10:00:00Z",
        finished_at_utc="2026-08-11T10:00:01Z",
        duration_ns=1_000_000_000,
        attempt=1,
        outcome=WorkOutcome.SUCCEEDED,
        execution_mode=execution_mode,
        reproducibility=ReproducibilityClass.ENVIRONMENT_BOUND,
        run_id="run:fixture",
        correlation_id="correlation:fixture",
        causation_id=causation_id,
    )


def _events() -> tuple[DerivationProjectionEvent, ...]:
    resource = ResourceRef("resource:file:fixture", "text", "text")
    physical_revision = _revision(
        resource.resource_id,
        "revision:text:fixture",
        "inventory",
    )
    text_materialization = MaterializationRef(
        "text",
        "text_representation",
        "materialization:text:fixture",
        2,
        resource,
        physical_revision,
    )
    text_receipt = _receipt(
        receipt_id="receipt:text:fixture",
        owner="text",
        stage_id="text.extract",
        signature="text-signature-v1",
        input_binding=InputBinding("source", physical_revision, "a" * 32),
        output=OutputBinding("document", text_materialization, "b" * 32),
    )

    chunk_revision = _revision(
        resource.resource_id,
        "revision:semantic-chunk:fixture",
        "semantic.text.chunk",
    )
    chunk_materialization = MaterializationRef(
        "semantic",
        "text_chunk",
        "materialization:semantic-chunk:fixture",
        7,
        resource,
        chunk_revision,
    )
    chunk_receipt = _receipt(
        receipt_id="receipt:semantic-chunk:fixture",
        owner="semantic",
        stage_id="semantic.text.chunk",
        signature="chunker-v1",
        input_binding=InputBinding(
            "text_representation",
            physical_revision,
            "b" * 32,
            materialization=text_materialization,
        ),
        output=OutputBinding("chunk", chunk_materialization, "c" * 32),
        causation_id=text_receipt.receipt_id,
    )

    embedding_revision = _revision(
        resource.resource_id,
        "revision:semantic-embedding:fixture",
        "semantic.embedding",
    )
    embedding_materialization = MaterializationRef(
        "semantic",
        "embedding",
        "materialization:embedding:fixture",
        7,
        resource,
        embedding_revision,
        generation=4,
    )
    embedding_receipt = _receipt(
        receipt_id="receipt:semantic-embedding:fixture",
        owner="semantic",
        stage_id="semantic.embedding",
        signature="embedding-model-v1",
        input_binding=InputBinding(
            "chunk",
            chunk_revision,
            "c" * 32,
            materialization=chunk_materialization,
        ),
        output=OutputBinding("embedding", embedding_materialization, "d" * 32),
        causation_id=chunk_receipt.receipt_id,
    )
    return (
        DerivationProjectionEvent("text", 1, "event:text:1", text_receipt.to_dict()),
        DerivationProjectionEvent("semantic", 1, "event:semantic:1", chunk_receipt.to_dict()),
        DerivationProjectionEvent("semantic", 2, "event:semantic:2", embedding_receipt.to_dict()),
    )


def test_projection_is_idempotent_and_rebuilds_exactly_after_discard() -> None:
    events = _events()

    first = rebuild_derivation_projection(events)
    rebuilt = rebuild_derivation_projection(events)
    with_duplicate = rebuild_derivation_projection((*events, events[-1]))

    assert rebuilt == first
    assert with_duplicate.events_applied == 3
    assert with_duplicate.duplicate_events_ignored == 1
    assert with_duplicate.nodes == first.nodes
    assert with_duplicate.edges == first.edges


def test_projection_event_identity_is_owner_local() -> None:
    text_event, semantic_event, _embedding_event = _events()

    projection = rebuild_derivation_projection(
        (
            replace(text_event, event_id="1"),
            replace(semantic_event, event_id="1"),
        )
    )

    assert projection.events_applied == 2
    assert {
        (owner, event_id) for owner, event_id, _fingerprint in projection.event_fingerprints
    } == {
        ("text", "1"),
        ("semantic", "1"),
    }


def test_projection_rejects_same_receipt_id_with_changed_contract() -> None:
    original = _events()[0]
    changed = dict(original.receipt)
    changed["effective_configuration"] = {"max_text_chars": 123}

    with pytest.raises(ValueError, match="conflicting facts"):
        rebuild_derivation_projection(
            (
                original,
                DerivationProjectionEvent("text", 2, "event:text:changed", changed),
            )
        )


def test_projection_rejects_same_revision_id_with_changed_contract() -> None:
    original = _events()[0]
    receipt = WorkReceipt.from_dict(original.receipt)
    original_input = receipt.inputs[0]
    changed_revision = replace(original_input.revision, generation=7)
    changed_materialization = replace(
        receipt.outputs[0].materialization,
        revision=changed_revision,
    )
    changed = replace(
        receipt,
        receipt_id="receipt:text:changed-revision",
        inputs=(replace(original_input, revision=changed_revision),),
        outputs=(replace(receipt.outputs[0], materialization=changed_materialization),),
    )

    with pytest.raises(ValueError, match="conflicting facts"):
        rebuild_derivation_projection(
            (
                original,
                DerivationProjectionEvent(
                    "text",
                    2,
                    "event:text:changed-revision",
                    changed.to_dict(),
                ),
            )
        )


def test_projection_rejects_same_materialization_id_with_changed_contract() -> None:
    original = _events()[0]
    receipt = WorkReceipt.from_dict(original.receipt)
    resource = ResourceRef("resource:file:other", "text", "text")
    revision = _revision(resource.resource_id, "revision:text:other", "inventory")
    changed_materialization = replace(
        receipt.outputs[0].materialization,
        resource=resource,
        revision=revision,
    )
    changed = replace(
        receipt,
        receipt_id="receipt:text:changed-materialization",
        inputs=(InputBinding("source", revision, "e" * 32),),
        outputs=(replace(receipt.outputs[0], materialization=changed_materialization),),
    )

    with pytest.raises(ValueError, match="conflicting facts"):
        rebuild_derivation_projection(
            (
                original,
                DerivationProjectionEvent(
                    "text",
                    2,
                    "event:text:changed-materialization",
                    changed.to_dict(),
                ),
            )
        )


def test_projection_explains_embedding_back_to_physical_revision() -> None:
    projection = rebuild_derivation_projection(_events())

    explanation = projection.explain("materialization:embedding:fixture")
    node_ids = {node.node_id for node in explanation.nodes}

    assert "revision:text:fixture" in node_ids
    assert "receipt:text:fixture" in node_ids
    assert "materialization:text:fixture" in node_ids
    assert "receipt:semantic-chunk:fixture" in node_ids
    assert "materialization:semantic-chunk:fixture" in node_ids
    assert "receipt:semantic-embedding:fixture" in node_ids


def test_chunker_change_stales_chunks_and_embeddings_but_reuses_text() -> None:
    projection = rebuild_derivation_projection(_events())

    impact = projection.impact("semantic.text.chunk", "chunker-v2")

    assert "receipt:semantic-chunk:fixture" in impact.stale_node_ids
    assert "materialization:semantic-chunk:fixture" in impact.stale_node_ids
    assert "receipt:semantic-embedding:fixture" in impact.stale_node_ids
    assert "materialization:embedding:fixture" in impact.stale_node_ids
    assert "revision:text:fixture" in impact.reusable_node_ids
    assert "materialization:text:fixture" in impact.reusable_node_ids


def test_projection_rejects_non_receipt_outbox_payload() -> None:
    event = DerivationProjectionEvent(
        "text",
        1,
        "event:invalid",
        {"schema_version": 1, "kind": "progress_event"},
    )

    try:
        rebuild_derivation_projection((event,))
    except ValueError as exc:
        assert "WorkReceipt v1" in str(exc)
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("non-receipt event was accepted")


def test_projection_enforces_cumulative_bytes_nodes_and_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _events()
    monkeypatch.setattr(projection_module, "MAX_DERIVATION_TOTAL_BYTES", 1)
    with pytest.raises(ValueError, match="cumulative byte bound"):
        rebuild_derivation_projection(events)

    monkeypatch.setattr(projection_module, "MAX_DERIVATION_TOTAL_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(projection_module, "MAX_DERIVATION_GRAPH_NODES", 2)
    with pytest.raises(ValueError, match="node bound"):
        rebuild_derivation_projection(events)

    monkeypatch.setattr(projection_module, "MAX_DERIVATION_GRAPH_NODES", 250_000)
    monkeypatch.setattr(projection_module, "MAX_DERIVATION_GRAPH_EDGES", 1)
    with pytest.raises(ValueError, match="edge bound"):
        rebuild_derivation_projection(events)


def test_projection_distinguishes_execution_from_exact_output_reuse() -> None:
    original = _events()[0]
    original_receipt = original.receipt
    stage = original_receipt["stage"]
    inputs = original_receipt["inputs"]
    outputs = original_receipt["outputs"]
    assert isinstance(stage, dict)
    assert isinstance(inputs, list)
    assert isinstance(outputs, list)
    reused = dict(original_receipt)
    reused["receipt_id"] = "receipt:text:cache-hit"
    reused["execution_mode"] = "cache_hit"
    reused["causation_id"] = "receipt:text:fixture"

    projection = rebuild_derivation_projection(
        (
            original,
            DerivationProjectionEvent("text", 2, "event:text:2", reused),
        )
    )

    relations = {(edge.receipt_id, edge.target_id, edge.relation) for edge in projection.edges}
    assert (
        "receipt:text:fixture",
        "materialization:text:fixture",
        "produced",
    ) in relations
    assert (
        "receipt:text:cache-hit",
        "materialization:text:fixture",
        "reused",
    ) in relations
    assert any(
        edge.source_id == "receipt:text:fixture"
        and edge.target_id == "receipt:text:cache-hit"
        and edge.relation == "caused"
        for edge in projection.edges
    )


def test_semantic_owner_outbox_adapter_preserves_canonical_receipt() -> None:
    receipt = _events()[1].receipt

    event = projection_event_from_semantic_outbox(SimpleNamespace(event_id=7, receipt=receipt))

    assert event.owner == "semantic"
    assert event.cursor == 7
    assert event.event_id == "semantic:7"
    assert event.receipt is receipt


def test_impact_reuse_is_limited_to_ancestors_of_the_affected_chain() -> None:
    events = _events()
    unrelated_revision = _revision(
        "resource:file:unrelated",
        "revision:text:unrelated",
        "inventory",
    )
    unrelated_materialization = MaterializationRef(
        "text",
        "text_representation",
        "materialization:text:unrelated",
        2,
    )
    unrelated_receipt = _receipt(
        receipt_id="receipt:text:unrelated",
        owner="text",
        stage_id="text.extract",
        signature="text-signature-current",
        input_binding=InputBinding("source", unrelated_revision, "e" * 32),
        output=OutputBinding("document", unrelated_materialization, "f" * 32),
    )
    projection = rebuild_derivation_projection(
        (
            *events,
            DerivationProjectionEvent(
                "text",
                2,
                "event:text:unrelated",
                unrelated_receipt.to_dict(),
            ),
        )
    )

    impact = projection.impact("semantic.text.chunk", "chunker-v2")

    assert "revision:text:fixture" in impact.reusable_node_ids
    assert "materialization:text:fixture" in impact.reusable_node_ids
    assert "revision:text:unrelated" not in impact.reusable_node_ids
    assert "materialization:text:unrelated" not in impact.reusable_node_ids
