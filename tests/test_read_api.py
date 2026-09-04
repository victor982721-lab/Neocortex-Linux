from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli.cli_knowledge import KnowledgeExitCode
from neocortex.code.code_contracts import CodeSearchHit
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeCompleteness,
    OwnerAvailability,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthState,
)
from neocortex.api import read_api


@dataclass
class _FakeSnapshot:
    marker: str
    state: OwnerAvailability = OwnerAvailability.AVAILABLE
    consistency: SnapshotConsistency = SnapshotConsistency.STABLE

    @property
    def owners(self):
        return (SimpleNamespace(state=self.state),)

    def to_dict(self) -> dict[str, object]:
        return {"kind": "knowledge_snapshot", "snapshot_id": self.marker, "owners": []}


class _FakeService:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def status(self, **_kwargs):
        return _FakeSnapshot(self.marker)

    def search(self, _query, **_kwargs):
        return SimpleNamespace(
            complete=True,
            to_dict=lambda: {
                "kind": "knowledge_search_result",
                "complete": True,
                "hits": [{"rank": 1, "scope_marker": self.marker}],
            },
        )

    def context(self, _query, **_kwargs):
        payload = {
            "kind": "context_bundle",
            "completeness": "complete",
            "snapshot": {"snapshot_id": self.marker},
            "citation_ids": [{"citation_id": "K1", "evidence_id": "e:1"}],
            "selected_hits": [{"evidence": {"evidence_id": "e:1"}}],
        }
        return SimpleNamespace(
            completeness=KnowledgeCompleteness.COMPLETE,
            to_dict=lambda: payload,
        )


def _bindings(tmp_path: Path) -> tuple[read_api.ScopeBinding, ...]:
    return (read_api.ScopeBinding(read_api.ReadScope.PERSONAL, tmp_path / "personal"),)


def test_scope_bindings_are_fixed_ordered_and_reject_arbitrary_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "default_state_directory", lambda: tmp_path / "personal")
    assert [item.scope for item in read_api.scope_bindings("all")] == [
        read_api.ReadScope.PERSONAL,
    ]
    with pytest.raises(ValueError, match="personal, framework or all"):
        read_api.scope_bindings(str(tmp_path / "attacker-controlled"))


def test_status_search_and_context_keep_scopes_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bindings = _bindings(tmp_path)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)
    monkeypatch.setattr(
        read_api,
        "_service",
        lambda binding: _FakeService(binding.scope.value),
    )
    monkeypatch.setattr(
        read_api,
        "knowledge_search_exit_code",
        lambda _result: KnowledgeExitCode.SUCCESS,
    )
    monkeypatch.setattr(
        read_api,
        "knowledge_context_exit_code",
        lambda _result: KnowledgeExitCode.SUCCESS,
    )

    status = read_api.status_payload("all")
    search = read_api.search_payload("transformador", "all", limit=3)
    context = read_api.context_payload("transformador", "all", limit=2)

    assert status["federation_policy"] == read_api.FEDERATION_POLICY
    assert search["limit_per_scope"] == 3
    assert context["limit_per_scope"] == 2
    assert [entry["scope"] for entry in search["scopes"]] == ["personal"]
    assert [entry["result"]["hits"][0]["scope_marker"] for entry in search["scopes"]] == ["personal"]
    assert search["exit_code"] == 0


def test_evidence_resolves_only_selected_context_citations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: _bindings(tmp_path))
    monkeypatch.setattr(
        read_api,
        "context_payload",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "snapshot": {"snapshot_id": "stable"},
                        "citation_ids": [{"citation_id": "K1", "evidence_id": "evidence:1"}],
                        "selected_hits": [
                            {
                                "resource": {"resource_id": "resource:1"},
                                "evidence": {"evidence_id": "evidence:1"},
                            }
                        ],
                    },
                }
            ],
        },
    )

    found = read_api.evidence_payload("breaker", "K1", "personal")
    missing = read_api.evidence_payload("breaker", "K9", "personal")

    assert found["found"] is True
    assert found["evidence_id"] == "evidence:1"
    assert found["expected_snapshot_id"] is None
    assert found["matches"][0]["hit"]["evidence"]["evidence_id"] == "evidence:1"
    assert missing["found"] is False
    assert missing["exit_code"] == int(KnowledgeExitCode.NO_RESULTS)


def test_evidence_id_is_stable_and_citation_is_only_a_presentation_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: _bindings(tmp_path))
    monkeypatch.setattr(
        read_api,
        "context_payload",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "snapshot": {"snapshot_id": "stable"},
                        "citation_ids": [
                            {"citation_id": "K1", "evidence_id": "evidence:1"},
                            {"citation_id": "K2", "evidence_id": "evidence:2"},
                        ],
                        "selected_hits": [
                            {"evidence": {"evidence_id": "evidence:1"}},
                            {"evidence": {"evidence_id": "evidence:2"}},
                        ],
                    },
                }
            ],
        },
    )

    payload = read_api.evidence_payload(
        "breaker",
        "K1",
        "personal",
        evidence_id="evidence:2",
        expected_snapshot_id="stable",
    )

    assert payload["found"] is True
    assert payload["citation_id"] == "K1"
    assert payload["evidence_id"] == "evidence:2"
    assert payload["expected_snapshot_id"] == "stable"
    assert payload["matches"][0]["citation"]["citation_id"] == "K2"
    assert payload["matches"][0]["hit"]["evidence"]["evidence_id"] == "evidence:2"


def test_evidence_expected_snapshot_drift_fails_without_reassigning_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: _bindings(tmp_path))

    def context(*_args, **_kwargs) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "snapshot": {"snapshot_id": "new-snapshot"},
                        "citation_ids": [
                            {"citation_id": "K1", "evidence_id": "evidence:new"}
                        ],
                        "selected_hits": [
                            {"evidence": {"evidence_id": "evidence:new"}}
                        ],
                    },
                }
            ],
        }

    monkeypatch.setattr(read_api, "context_payload", context)

    payload = read_api.evidence_payload(
        "breaker",
        "K1",
        "personal",
        evidence_id="evidence:old",
        expected_snapshot_id="old-snapshot",
    )

    assert calls == 1
    assert payload["found"] is False
    assert payload["matches"] == []
    assert payload["evidence_id"] == "evidence:old"
    assert payload["expected_snapshot_id"] == "old-snapshot"
    assert payload["exit_code"] == int(KnowledgeExitCode.SNAPSHOT_CHANGED)
    assert payload["status"] == "snapshot_changed"
    assert payload["coverage"] == "blocked"
    assert payload["error"]["code"] == "snapshot_changed"


def test_evidence_expected_snapshot_abstains_when_context_has_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: _bindings(tmp_path))
    monkeypatch.setattr(
        read_api,
        "context_payload",
        lambda *_args, **_kwargs: {
            "exit_code": int(KnowledgeExitCode.NO_RESULTS),
            "scopes": [{"scope": "personal", "status": "no_results", "context": {}}],
        },
    )

    payload = read_api.evidence_payload(
        "breaker",
        "K1",
        "personal",
        expected_snapshot_id="expected-snapshot",
    )

    assert payload["found"] is False
    assert payload["exit_code"] == int(KnowledgeExitCode.SNAPSHOT_CHANGED)
    assert payload["status"] == "snapshot_changed"
    assert payload["error"]["code"] == "snapshot_changed"


def test_ambiguous_citation_alias_never_selects_an_arbitrary_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: _bindings(tmp_path))
    monkeypatch.setattr(
        read_api,
        "context_payload",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "snapshot": {"snapshot_id": "stable"},
                        "citation_ids": [
                            {"citation_id": "K1", "evidence_id": "evidence:1"},
                            {"citation_id": "K1", "evidence_id": "evidence:2"},
                        ],
                        "selected_hits": [
                            {"evidence": {"evidence_id": "evidence:1"}},
                            {"evidence": {"evidence_id": "evidence:2"}},
                        ],
                    },
                }
            ],
        },
    )

    payload = read_api.evidence_payload("breaker", "K1", "personal")

    assert payload["found"] is False
    assert payload["matches"] == []
    assert payload["evidence_id"] is None
    assert payload["exit_code"] == int(KnowledgeExitCode.PARTIAL)
    assert payload["status"] == "partial"
    assert payload["error"] == {
        "code": "ambiguous_citation",
        "message": "citation alias identifies multiple evidence records; provide evidence_id",
        "retryable": False,
    }


def test_code_search_is_bounded_labelled_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binding = read_api.ScopeBinding(read_api.ReadScope.PERSONAL, tmp_path / "personal")
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: (binding,))
    monkeypatch.setattr(
        read_api,
        "search_code",
        lambda path, query: (
            CodeSearchHit(
                path="neocortex/interface/entrypoint.py",
                project="Neocortex",
                language="python",
                artifact_kind="source",
                symbol="entrypoint",
                signature="def entrypoint(...)",
                start_line=1,
                end_line=4,
                snippet="def entrypoint",
                score=0.5,
                match_types=("symbol",),
                evidence=("symbol:function",),
                version_id=1,
                observed_size=100,
                observed_mtime_ns=2,
                analysis_status="complete",
            ),
        ),
    )

    payload = read_api.code_search_payload("entrypoint", limit=4)

    assert payload["read_only"] is True
    assert payload["limit_per_scope"] == 4
    assert payload["scopes"][0]["hits"][0]["symbol"] == "entrypoint"


def test_lineage_uses_only_fixed_scope_roots_and_keeps_results_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bindings = _bindings(tmp_path)
    observed: list[tuple[Path, str]] = []
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)

    def inspect(state_directory: Path, identifier: str) -> dict[str, object]:
        observed.append((state_directory, identifier))
        return {
            "status": "ready",
            "exit_code": 0,
            "read_only": True,
            "marker": state_directory.name,
        }

    monkeypatch.setattr(read_api, "inspect_derivation_lineage", inspect)

    payload = read_api.lineage_payload(" revision:text:one ", "all")

    assert payload["read_only"] is True
    assert payload["federation_policy"] == read_api.FEDERATION_POLICY
    assert payload["identifier"] == "revision:text:one"
    assert observed == [(tmp_path / "personal", "revision:text:one")]
    assert [entry["lineage"]["marker"] for entry in payload["scopes"]] == ["personal"]
    assert payload["exit_code"] == int(KnowledgeExitCode.SUCCESS)


def test_asset_health_uses_stable_identity_and_fixed_scopes_without_creating_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from neocortex.knowledge import knowledge_asset_health

    bindings = _bindings(tmp_path)
    observed: list[tuple[Path, str]] = []
    resource_id = "resource:file:1:2:-1"
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)

    def inspect(paths, query):
        observed.append((paths.inventory.parent, query.resource_id))
        return SimpleNamespace(
            resource_id=query.resource_id,
            health=KnowledgeAssetHealthState.HEALTHY,
            completeness=KnowledgeAssetHealthCompleteness.COMPLETE,
            reason_code="causal_trace_aligned",
            to_dict=lambda: {
                "schema": "neocortex.knowledge-asset-health/v1",
                "resource_id": query.resource_id,
                "health": "healthy",
                "completeness": "complete",
                "reason_code": "causal_trace_aligned",
                "read_only": True,
                "advisory_only": True,
                "mutation_authorized": False,
            },
        )

    monkeypatch.setattr(knowledge_asset_health, "inspect_knowledge_asset_health", inspect)

    payload = read_api.asset_health_payload(resource_id, "all")

    assert payload["read_only"] is True
    assert payload["resource_id"] == resource_id
    assert observed == [(tmp_path / "personal", resource_id)]
    assert [item["status"] for item in payload["scopes"]] == ["healthy"]
    assert not (tmp_path / "personal").exists()


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: read_api.search_payload(" "), "query cannot be blank"),
        (lambda: read_api.search_payload("x", limit=0), "limit must be between"),
        (
            lambda: read_api.context_payload("x", max_characters=1_000_001),
            "max_characters must be between",
        ),
        (
            lambda: read_api.code_search_payload("x", modes=("unknown",)),
            "unsupported",
        ),
        (lambda: read_api.lineage_payload(" "), "identifier cannot be blank"),
        (
            lambda: read_api.asset_health_payload("/arbitrary/path"),
            "resource:file identity scheme",
        ),
    ],
)
def test_public_read_api_rejects_unbounded_or_ambiguous_inputs(call, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        call()


def test_missing_state_status_does_not_create_any_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-state"
    binding = read_api.ScopeBinding(read_api.ReadScope.PERSONAL, missing)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: (binding,))

    payload = read_api.status_payload("personal")

    assert payload["read_only"] is True
    assert payload["coverage"] == "empty"
    assert payload["status"] == "empty"
    assert payload["exit_code"] == int(KnowledgeExitCode.NO_RESULTS)
    assert not missing.exists()
