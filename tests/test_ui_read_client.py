"""Contracts for the desktop bridge over the shared read-only API."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from neocortex.interface.read import (
    MAX_PRESENTATION_CHARACTERS,
    ReadClientError,
    ReadRequest,
    SharedReadClient,
    present_read_payload,
)


TEST_CAPABILITIES = ("base", 'ui')
pytestmark = pytest.mark.capability("base", 'ui')


def _payload(
    operation: str,
    *,
    scope: str = "all",
    scopes: list[dict[str, object]] | None = None,
    exit_code: int = 0,
) -> dict[str, object]:
    schema, kind = {
        "status": ("neocortex.read-api/v1", "neocortex_scoped_status"),
        "search": ("neocortex.read-api/v1", "neocortex_scoped_search"),
        "ask": ("neocortex.read-api/v1", "neocortex_scoped_context"),
        "review": ("neocortex.value-review/v1", "neocortex_scoped_value_review"),
    }[operation]
    payload: dict[str, object] = {
        "schema": schema,
        "kind": kind,
        "read_only": True,
        "scope_requested": scope,
        "exit_code": exit_code,
        "scopes": scopes or [],
    }
    if operation in {"search", "ask"}:
        payload["query"] = "transformador U5"
    if operation == "review":
        payload.update(
            operation="value-preview",
            advisory_only=True,
            mutation_authorized=False,
        )
    return payload


@pytest.mark.parametrize(
    ("read_request", "match"),
    [
        (ReadRequest("search", query=" "), "Escribe una consulta"),
        (ReadRequest("ask", query="x", limit=0), "limit must be between"),
        (ReadRequest("status", scope="/tmp/state"), "scope must be"),
        (ReadRequest("review", limit=True), "limit must be an integer"),
    ],
)
def test_read_request_rejects_blank_unbounded_and_path_like_inputs(
    read_request: ReadRequest,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        read_request.validated()


def test_shared_client_routes_all_operations_without_state_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.api.read_api as read_api
    import neocortex.api.cli.value_review as value_adapter

    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def fake(name: str) -> Callable[..., dict[str, object]]:
        def execute(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((name, args, kwargs))
            return _payload(name, scope=str(args[-1] if name != "status" else args[0]))

        return execute

    monkeypatch.setattr(read_api, "status_payload", fake("status"))
    monkeypatch.setattr(read_api, "search_payload", fake("search"))
    monkeypatch.setattr(read_api, "context_payload", fake("ask"))
    monkeypatch.setattr(value_adapter, "value_review_payload", fake("review"))
    client = SharedReadClient()

    client.execute(ReadRequest("status", scope="personal"))
    client.execute(ReadRequest("search", scope="framework", query="  interruptor  ", limit=7))
    client.execute(ReadRequest("ask", scope="all", query="aceite", limit=4))
    client.execute(ReadRequest("review", scope="personal", limit=12))

    assert [name for name, _args, _kwargs in calls] == [
        "status",
        "search",
        "ask",
        "review",
    ]
    assert calls[0][1] == ("personal",)
    assert calls[1][1] == ("interruptor", "framework")
    assert calls[1][2] == {"limit": 7, "mode": "evidence"}
    assert calls[2][2] == {
        "limit": 4,
        "max_characters": 12_000,
        "mode": "evidence",
        "response_version": 1,
    }
    assert calls[3][1] == ("personal",)
    assert calls[3][2] == {"limit": 12}
    assert not any(
        "state" in str(value).casefold() for _name, args, _kwargs in calls for value in args
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(read_only=False),
        lambda value: value.update(schema="neocortex.read-api/v99"),
        lambda value: value.update(scope_requested="framework"),
        lambda value: value.update(exit_code=True),
        lambda value: value.update(scopes={}),
    ],
)
def test_shared_client_fails_closed_on_incompatible_contracts(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    import neocortex.api.read_api as read_api

    payload = _payload("status")
    mutate(payload)
    monkeypatch.setattr(read_api, "status_payload", lambda _scope: payload)

    with pytest.raises(ReadClientError):
        SharedReadClient().execute(ReadRequest("status"))


def test_review_contract_must_remain_advisory_and_non_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.api.cli.value_review as value_adapter

    payload = _payload("review", scope="personal")
    payload["mutation_authorized"] = True
    monkeypatch.setattr(value_adapter, "value_review_payload", lambda *_args, **_kwargs: payload)

    with pytest.raises(ReadClientError, match="consultivo"):
        SharedReadClient().execute(ReadRequest("review", scope="personal"))


def test_shared_client_keeps_missing_fixture_state_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import neocortex.api.read_api as read_api
    import neocortex.api.cli.value_review as value_adapter

    missing = tmp_path / "published-state-that-does-not-exist"
    binding = read_api.ScopeBinding(read_api.ReadScope.PERSONAL, missing)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: (binding,))
    monkeypatch.setattr(value_adapter, "scope_bindings", lambda _scope: (binding,))

    status = SharedReadClient().execute(ReadRequest("status", scope="personal"))
    review = SharedReadClient().execute(ReadRequest("review", scope="personal"))

    assert status["read_only"] is True
    assert review["mutation_authorized"] is False
    assert not missing.exists()


def test_human_presentations_explain_status_search_ask_and_review() -> None:
    resource = {
        "resource": {"current_path": "/Corpus/Transformadores/Pruebas U5.pdf"},
        "evidence": {
            "evidence_id": "evidence:U5:page-4",
            "page": 4,
            "snippet": "La resistencia de aislamiento fue verificada antes del llenado.",
        },
        "reasons": ["fts_phrase_match"],
    }
    status = _payload(
        "status",
        scopes=[
            {
                "scope": "personal",
                "status": "ready",
                "snapshot": {
                    "snapshot_id": "personal-published-17",
                    "owners": [
                        {"owner": "pdf", "state": "available"},
                        {"owner": "audio", "state": "absent"},
                    ],
                    "active_models": ["text-embedding"],
                },
            }
        ],
    )
    search = _payload(
        "search",
        scopes=[
            {
                "scope": "personal",
                "result": {
                    "complete": True,
                    "result_window_full": False,
                    "hits": [resource],
                    "warnings": [],
                },
            }
        ],
    )
    ask = _payload(
        "ask",
        scopes=[
            {
                "scope": "personal",
                "context": {
                    "completeness": "complete",
                    "selected_hits": [resource],
                    "citation_ids": [{"citation_id": "K1"}],
                },
            }
        ],
    )
    review = _payload(
        "review",
        scope="personal",
        scopes=[
            {
                "scope": "personal",
                "status": "ready",
                "report": {
                    "matched_count": 1,
                    "items": [
                        {
                            "state": "review_low_value",
                            "path": "/Corpus/Temporal/copia.txt",
                            "size_bytes": 2048,
                            "reasons": ["old_repeated_extracted_text_in_disposable_path"],
                            "uncertainties": ["usage_history_unavailable"],
                        }
                    ],
                },
            }
        ],
    )

    status_view = present_read_payload(ReadRequest("status"), status)
    search_view = present_read_payload(ReadRequest("search", query="transformador U5"), search)
    ask_view = present_read_payload(ReadRequest("ask", query="transformador U5"), ask)
    review_view = present_read_payload(ReadRequest("review", scope="personal"), review)

    assert status_view.state == "completed"
    assert "1 fuente disponible" in status_view.body
    assert "Personal y Framework se ordenan por separado" in search_view.body
    assert "Pruebas U5.pdf" in search_view.body
    assert "página 4" in search_view.body
    assert "[K1]" in ask_view.body
    assert "no inventa una respuesta" in ask_view.body
    assert "0 acciones aplicadas" in review_view.summary
    assert "no autoriza mover, archivar o borrar" in review_view.body
    assert "texto extraído repetido y antiguo" in review_view.body
    assert "historial de uso no disponible" in review_view.body
    assert all(
        len(view.body) <= MAX_PRESENTATION_CHARACTERS + 100
        for view in (status_view, search_view, ask_view, review_view)
    )
