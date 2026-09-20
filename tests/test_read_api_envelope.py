"""Contract coverage for every public read-api producer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from neocortex.api import read_api
from neocortex.api.read_contract import (
    ReadContractError,
    ReadOperation,
    sanitize_untrusted_payload,
    validate_read_payload,
)
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthState,
)
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness,
    OwnerAvailability,
    SnapshotConsistency,
)


class _Snapshot:
    owners = (SimpleNamespace(state=OwnerAvailability.AVAILABLE),)
    consistency = SnapshotConsistency.STABLE

    def to_dict(self) -> dict[str, object]:
        return {"kind": "knowledge_snapshot", "snapshot_id": "fixture-snapshot", "owners": []}


class _Service:
    def status(self, **_kwargs: object) -> _Snapshot:
        return _Snapshot()

    def search(self, _query: object, **_kwargs: object) -> Any:
        return SimpleNamespace(
            complete=True,
            to_dict=lambda: {
                "kind": "knowledge_search_result",
                "complete": True,
                "hits": [],
            },
        )

    def context(self, _query: object, **_kwargs: object) -> Any:
        return SimpleNamespace(
            completeness=KnowledgeCompleteness.COMPLETE,
            to_dict=lambda: {
                "kind": "context_bundle",
                "completeness": "complete",
                "snapshot": {"snapshot_id": "fixture-snapshot"},
                "citation_ids": [],
                "selected_hits": [],
            },
        )


def _binding(tmp_path: Path) -> tuple[read_api.ScopeBinding, ...]:
    return (read_api.ScopeBinding(read_api.ReadScope.PERSONAL, tmp_path / "published"),)


def _assert_envelope(
    payload: dict[str, object],
    operation: ReadOperation,
    *,
    query: str | None = None,
    limit: int | None = None,
) -> None:
    assert {
        "operation",
        "request_id",
        "scope",
        "scope_requested",
        "read_only",
        "coverage",
        "status",
        "exit_code",
        "error",
        "result",
        "observed_epoch",
    } <= payload.keys()
    assert isinstance(payload["observed_epoch"], dict)
    validate_read_payload(
        payload,
        operation,
        scope="personal",
        query=query,
        mode="evidence" if operation in {ReadOperation.SEARCH, ReadOperation.CONTEXT} else None,
        include_history=False if operation in {ReadOperation.SEARCH, ReadOperation.CONTEXT} else None,
        limit=limit,
    )


def test_all_read_api_producers_emit_the_same_v1_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bindings = _binding(tmp_path)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)
    monkeypatch.setattr(read_api, "_service", lambda _binding: _Service())
    monkeypatch.setattr(read_api, "knowledge_search_exit_code", lambda _result: 0)
    monkeypatch.setattr(read_api, "knowledge_context_exit_code", lambda _result: 0)
    monkeypatch.setattr(
        read_api,
        "inspect_derivation_lineage",
        lambda _path, _identifier: {"status": "ready", "exit_code": 0, "read_only": True},
    )
    monkeypatch.setattr(
        read_api,
        "inspect_knowledge_asset_health",
        lambda _path, _resource: SimpleNamespace(
            health=KnowledgeAssetHealthState.HEALTHY,
            completeness=KnowledgeAssetHealthCompleteness.COMPLETE,
            reason_code="fixture",
            to_dict=lambda: {
                "schema": "neocortex.knowledge-asset-health/v1",
                "health": "healthy",
                "completeness": "complete",
                "read_only": True,
            },
        ),
    )

    query = "relay"
    outputs = (
        (read_api.status_payload("personal"), ReadOperation.STATUS, None, None),
        (read_api.search_payload(query, "personal", limit=3), ReadOperation.SEARCH, query, 3),
        (read_api.context_payload(query, "personal", limit=3), ReadOperation.CONTEXT, query, 3),
        (
            read_api.evidence_payload(query, "missing", "personal", limit=3),
            ReadOperation.EVIDENCE,
            query,
            3,
        ),
        (
            read_api.lineage_payload("revision:fixture", "personal"),
            ReadOperation.LINEAGE,
            None,
            None,
        ),
        (
            read_api.asset_health_payload("resource:file:1:2:-1", "personal"),
            ReadOperation.ASSET_HEALTH,
            None,
            None,
        ),
    )
    for payload, operation, expected_query, expected_limit in outputs:
        _assert_envelope(payload, operation, query=expected_query, limit=expected_limit)


def test_read_api_request_id_is_bounded_and_observed_without_creating_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bindings = _binding(tmp_path)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)
    monkeypatch.setattr(read_api, "_service", lambda _binding: _Service())

    payload = read_api.status_payload("personal", request_id="fixture-request-42")

    assert payload["request_id"] == "fixture-request-42"
    assert isinstance(payload["observed_epoch"], dict)
    assert not (tmp_path / "published").exists()
    with pytest.raises(ValueError, match="request_id"):
        read_api.status_payload("personal", request_id="bad\x1b[31m")


def test_stable_evidence_fields_preserve_the_valid_read_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bindings = _binding(tmp_path)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)
    monkeypatch.setattr(read_api, "_service", lambda _binding: _Service())
    monkeypatch.setattr(read_api, "knowledge_context_exit_code", lambda _result: 0)

    payload = read_api.evidence_payload(
        "relay",
        "K1",
        "personal",
        evidence_id="evidence:fixture",
        expected_snapshot_id="fixture-snapshot",
        limit=3,
        request_id="evidence-request-1",
    )

    _assert_envelope(payload, ReadOperation.EVIDENCE, query="relay", limit=3)
    assert payload["request_id"] == "evidence-request-1"
    assert payload["citation_id"] == "K1"
    assert payload["evidence_id"] == "evidence:fixture"
    assert payload["expected_snapshot_id"] == "fixture-snapshot"
    assert payload["found"] is False


def test_read_contract_sanitizes_untrusted_nested_results() -> None:
    payload: dict[str, object] = {
        "schema": "neocortex.read-api/v1",
        "kind": "neocortex_scoped_status",
        "operation": "status",
        "request_id": "fixture-request-1",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "coverage": "complete",
        "status": "ok",
        "exit_code": 0,
        "error": None,
        "result": {"message": "safe\x1b[31m forged"},
        "scopes": [
            {
                "scope": "personal",
                "status": "ready",
                "exit_code": 0,
                "snapshot": {"snippet": "red\x1b[0m\ntext"},
            }
        ],
        "observed_epoch": {"scope": "personal", "scopes": {}},
    }
    safe = sanitize_untrusted_payload(payload)

    assert "\x1b" not in str(safe)
    assert safe["result"] == {"message": "safe forged"}
    assert safe["scopes"][0]["snapshot"]["snippet"] == "red\ntext"  # type: ignore[index]


def test_read_contract_rejects_inconsistent_complete_outcomes() -> None:
    payload: dict[str, object] = {
        "schema": "neocortex.read-api/v1",
        "kind": "neocortex_scoped_status",
        "operation": "status",
        "request_id": "fixture-request-2",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "coverage": "complete",
        "status": "ok",
        "exit_code": 4,
        "error": None,
        "result": {"scopes": []},
        "scopes": [],
        "observed_epoch": {"scope": "personal", "scopes": {}},
    }
    with pytest.raises(ReadContractError, match="status/coverage"):
        validate_read_payload(payload, ReadOperation.STATUS, scope="personal")
