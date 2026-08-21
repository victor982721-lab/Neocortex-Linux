"""Validated bridge over NeoCortex's shared read-only APIs."""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from .models import ReadClientError, ReadRequest

_EXPECTED_CONTRACTS = {
    "status": ("neocortex.read-api/v1", "neocortex_scoped_status"),
    "search": ("neocortex.read-api/v1", "neocortex_scoped_search"),
    "ask": ("neocortex.read-api/v1", "neocortex_scoped_context"),
    "review": ("neocortex.value-review/v1", "neocortex_scoped_value_review"),
}


class SharedReadClient:
    """Call the canonical local facade without accepting caller-owned paths."""

    def execute(self, request: ReadRequest) -> dict[str, object]:
        selected = request.validated()
        raw_payload = self._execute_selected(selected)
        return _validated_payload(selected, raw_payload)

    @staticmethod
    def _execute_selected(request: ReadRequest) -> object:
        if request.operation == "review":
            adapter = importlib.import_module("neocortex.value_cli_adapter")
            return adapter.value_review_payload(request.scope, limit=request.limit)

        read_api = importlib.import_module("neocortex.read_api")
        if request.operation == "status":
            return read_api.status_payload(request.scope)
        if request.operation == "search":
            return read_api.search_payload(
                request.query,
                request.scope,
                limit=request.limit,
                mode="evidence",
            )
        return read_api.context_payload(
            request.query,
            request.scope,
            limit=request.limit,
            max_characters=12_000,
            mode="evidence",
        )


def _validated_payload(request: ReadRequest, value: object) -> dict[str, object]:
    payload = _mapping_payload(value)
    _validate_identity(request, payload)
    _validate_envelope(request, payload)
    return payload


def _mapping_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReadClientError("El read API devolvió una respuesta no estructurada.")
    return dict(value)


def _validate_identity(request: ReadRequest, payload: Mapping[str, object]) -> None:
    expected_schema, expected_kind = _EXPECTED_CONTRACTS[request.operation]
    if payload.get("schema") != expected_schema or payload.get("kind") != expected_kind:
        raise ReadClientError("El read API devolvió un contrato incompatible.")
    if payload.get("read_only") is not True:
        raise ReadClientError("El read API no confirmó el modo de solo lectura.")
    if payload.get("scope_requested") != request.scope:
        raise ReadClientError("El read API respondió para un scope distinto al solicitado.")


def _validate_envelope(request: ReadRequest, payload: Mapping[str, object]) -> None:
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ReadClientError("El read API no devolvió un código de salida válido.")
    if not isinstance(payload.get("scopes"), list):
        raise ReadClientError("El read API no devolvió scopes consultables.")
    if request.operation == "review":
        _validate_review(payload)


def _validate_review(payload: Mapping[str, object]) -> None:
    if (
        payload.get("operation") != "value-preview"
        or payload.get("advisory_only") is not True
        or payload.get("mutation_authorized") is not False
    ):
        raise ReadClientError("La revisión no confirmó su carácter consultivo.")


__all__ = ["SharedReadClient"]
