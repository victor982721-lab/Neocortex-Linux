from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.safety.state_topology_contracts import (
    STATE_STORE_REGISTRY,
    STATE_STORE_REGISTRY_SCHEMA,
    TEXT_DERIVATION_IMPLEMENTATION_BINDING,
    TEXT_DERIVATION_WORKFLOW,
    DurableTransactionBoundaryContract,
    parse_durable_workflow_contract_payload,
    parse_durable_workflow_implementation_binding_payload,
    parse_state_store_registry_payload,
)


def test_public_registry_is_the_exact_knowledge_state_path_contract(tmp_path: Path) -> None:
    root = tmp_path / "state"
    paths = KnowledgeStatePaths.from_directory(root)

    assert STATE_STORE_REGISTRY.schema == STATE_STORE_REGISTRY_SCHEMA
    assert tuple(store.state_owner_id for store in STATE_STORE_REGISTRY.stores) == (
        "inventory",
        "framework",
        "catalog",
        "pdf",
        "docx",
        "office",
        "audio",
        "video",
        "image",
        "semantic",
        "archive",
        "text",
    )
    assert {
        store.state_store_id: getattr(paths, store.knowledge_path_attribute)
        for store in STATE_STORE_REGISTRY.stores
    } == {
        store.state_store_id: root.absolute() / store.database_name
        for store in STATE_STORE_REGISTRY.stores
    }
    assert STATE_STORE_REGISTRY.by_owner("framework").database_name == "framework.sqlite3"
    assert STATE_STORE_REGISTRY.by_owner("text").knowledge_capture_mode == "if_present"


def test_registry_wire_round_trip_and_rejects_invented_owner() -> None:
    payload = json.loads(json.dumps(STATE_STORE_REGISTRY.as_payload()))

    assert parse_state_store_registry_payload(payload) == STATE_STORE_REGISTRY

    payload["stores"][-1]["state_owner_id"] = "semantic"
    with pytest.raises(ValueError, match="cannot repeat"):
        parse_state_store_registry_payload(payload)


def test_text_workflow_declares_exact_owner_local_authority_without_name_inference() -> None:
    begin = TEXT_DERIVATION_WORKFLOW.boundary("text.derivation-attempt-begin")
    terminal = TEXT_DERIVATION_WORKFLOW.boundary("text.terminal-publication")

    assert begin.state_store_id == terminal.state_store_id == "sqlite:text.sqlite3"
    assert begin.state_owner_id == terminal.state_owner_id == "text"
    assert (begin.begin_authority, begin.commit_authority) == ("callee", "callee")
    assert (terminal.begin_authority, terminal.commit_authority) == ("caller", "caller")
    assert terminal.transaction_scope == "single_state_store"
    assert terminal.required_write_tables == (
        "text_work_receipts",
        "text_derivation_attempts",
        "text_derivation_outbox",
    )
    assert "documents" in terminal.conditional_write_tables
    assert (
        parse_durable_workflow_contract_payload(TEXT_DERIVATION_WORKFLOW.as_payload())
        == TEXT_DERIVATION_WORKFLOW
    )
    payload = json.loads(json.dumps(TEXT_DERIVATION_WORKFLOW.as_payload()))
    assert parse_durable_workflow_contract_payload(payload) == TEXT_DERIVATION_WORKFLOW

    with pytest.raises(ValueError, match="unknown transaction boundary"):
        TEXT_DERIVATION_WORKFLOW.boundary("text.unknown")


def test_boundary_cannot_claim_an_owner_that_disagrees_with_the_store() -> None:
    terminal = TEXT_DERIVATION_WORKFLOW.boundary("text.terminal-publication")

    with pytest.raises(ValueError, match="store and state owner disagree"):
        replace(terminal, state_owner_id="semantic")


def test_text_workflow_binds_boundaries_to_exact_code_symbols() -> None:
    begin = TEXT_DERIVATION_IMPLEMENTATION_BINDING.boundary("text.derivation-attempt-begin")
    terminal = TEXT_DERIVATION_IMPLEMENTATION_BINDING.boundary("text.terminal-publication")

    assert begin.qualified_symbols == (
        "text_derivation_repository.begin_text_derivation_attempt_from_connection",
    )
    assert "text_derivation_repository._persist_terminal_receipt" in terminal.qualified_symbols
    payload = json.loads(json.dumps(TEXT_DERIVATION_IMPLEMENTATION_BINDING.as_payload()))
    assert (
        parse_durable_workflow_implementation_binding_payload(payload)
        == TEXT_DERIVATION_IMPLEMENTATION_BINDING
    )
    with pytest.raises(ValueError, match="unbound transaction boundary"):
        TEXT_DERIVATION_IMPLEMENTATION_BINDING.boundary("text.unknown")


def test_boundary_cannot_hide_required_writes_as_conditional() -> None:
    terminal = TEXT_DERIVATION_WORKFLOW.boundary("text.terminal-publication")

    with pytest.raises(ValueError, match="cannot overlap"):
        DurableTransactionBoundaryContract(
            boundary_id="fixture",
            state_store_id=terminal.state_store_id,
            state_owner_id=terminal.state_owner_id,
            begin_authority="caller",
            commit_authority="caller",
            transaction_scope="single_state_store",
            required_read_tables=("text_derivation_attempts",),
            required_write_tables=("text_work_receipts",),
            conditional_write_tables=("text_work_receipts",),
        )
