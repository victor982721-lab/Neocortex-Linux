"""Validated bridge over NeoCortex's shared read-only APIs."""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from neocortex.api.read_contract import (
    ReadContractError,
    ReadOperation,
    normalize_read_payload,
    sanitize_untrusted_text,
    validate_read_payload,
)

from .models import ReadClientError, ReadRequest


_COMPLETE_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "kind",
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
        "scopes",
    }
)
_MAX_SANITIZED_NODES = 20_000
_MAX_SANITIZED_DEPTH = 16
_MAX_SANITIZED_STRING = 128_000


def _sanitize_nested(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    """Remove terminal controls from corpus-derived result data.

    Contract metadata is validated before this function runs, so only result,
    scope details, errors and epoch diagnostics are sanitized.  A node/depth
    budget keeps an untrusted producer from turning the desktop bridge into an
    unbounded renderer while preserving the legacy JSON shape.
    """

    if budget is None:
        budget = [_MAX_SANITIZED_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_SANITIZED_DEPTH:
        return "[contenido omitido por límite]"
    if isinstance(value, str):
        return sanitize_untrusted_text(value, limit=_MAX_SANITIZED_STRING, single_line=False)
    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            safe_key: object = (
                sanitize_untrusted_text(key, limit=512)
                if isinstance(key, str)
                else key
            )
            result[safe_key] = _sanitize_nested(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, list):
        return [_sanitize_nested(item, depth=depth + 1, budget=budget) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_nested(item, depth=depth + 1, budget=budget) for item in value]
    return value


def _sanitize_result_sections(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a safe copy while leaving strict request echoes byte-for-byte."""

    result = dict(payload)
    budget = [_MAX_SANITIZED_NODES]
    for key in ("result", "scopes", "error", "observed_epoch"):
        if key in result:
            result[key] = _sanitize_nested(result[key], budget=budget)
    return result


def _validate_outcome_consistency(payload: Mapping[str, object]) -> None:
    """Reject a complete envelope whose status lies about its exit code."""

    expected = {
        0: ("complete", "ok"),
        1: ("unavailable", "error"),
        2: ("unavailable", "usage_error"),
        3: ("empty", "empty"),
        4: ("partial", "partial"),
        5: ("blocked", "snapshot_changed"),
        6: ("unavailable", "schema_incompatible"),
        7: ("unavailable", "corrupt"),
        130: ("blocked", "cancelled"),
    }
    code = payload.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int) or code not in expected:
        return
    coverage, status = expected[code]
    if payload.get("coverage") != coverage or payload.get("status") != status:
        raise ReadClientError("El read API devolvió status/coverage incompatibles con exit_code.")
    if code not in {0, 3} and payload.get("error") is None:
        raise ReadClientError("El read API no describió el error de un resultado incompleto.")


class SharedReadClient:
    """Call and validate the canonical local read facade.

    A producer that emits the complete v1 envelope is checked with strict
    request echoes and an observed publication epoch.  Older adapters remain
    accepted through the explicit compatibility path, but are still normalized
    and validated before a UI can render them.
    """

    def execute(self, request: ReadRequest) -> dict[str, object]:
        selected = request.validated()
        raw_payload = self._execute_selected(selected)
        return _validated_payload(selected, raw_payload)

    @staticmethod
    def _execute_selected(request: ReadRequest) -> object:
        if request.operation == "review":
            adapter = importlib.import_module("neocortex.api.cli.value_review")
            return adapter.value_review_payload(request.scope, limit=request.limit)

        read_api = importlib.import_module("neocortex.api.read_api")
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
    try:
        operation = (
            ReadOperation.CONTEXT
            if request.operation == "ask"
            else ReadOperation(request.operation)
        )
        legacy_payload = not (
            isinstance(value, Mapping)
            and _COMPLETE_ENVELOPE_FIELDS.issubset(value)
        )
        if not isinstance(value, Mapping):
            raise ReadClientError("El read API devolvió una respuesta no estructurada.")
        if "observed_epoch" in value and not isinstance(value.get("observed_epoch"), Mapping):
            raise ReadClientError("El read API no devolvió un observed_epoch válido.")
        payload = normalize_read_payload(value, operation)
        validated = validate_read_payload(
            payload,
            operation,
            scope=request.scope,
            # Existing adapters predate the v1 echo fields.  They still get
            # identity, shape and safety validation, while the strict echo
            # checks apply as soon as a producer emits the complete envelope.
            query=None if legacy_payload else request.query or None,
            mode=(
                "evidence"
                if not legacy_payload
                and operation in {ReadOperation.SEARCH, ReadOperation.CONTEXT}
                else None
            ),
            include_history=(
                False
                if not legacy_payload
                and operation in {ReadOperation.SEARCH, ReadOperation.CONTEXT}
                else None
            ),
            limit=(
                request.limit
                if not legacy_payload
                and operation in {
                    ReadOperation.SEARCH,
                    ReadOperation.CONTEXT,
                    ReadOperation.REVIEW,
                    ReadOperation.INSPECT_CODE,
                }
                else None
            ),
            strict_echo=not legacy_payload,
        )
        if not legacy_payload:
            _validate_outcome_consistency(validated)
        return _sanitize_result_sections(validated)
    except (ReadContractError, TypeError, ValueError) as exc:
        raise ReadClientError(str(exc)) from exc


__all__ = ["SharedReadClient"]
