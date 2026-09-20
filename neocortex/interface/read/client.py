"""Validated bridge over NeoCortex's shared read-only APIs."""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from neocortex.api.read_contract import (
    MAX_SANITIZED_PAYLOAD_NODES,
    ReadContractError,
    ReadOperation,
    expected_read_outcome,
    normalize_read_payload,
    sanitize_untrusted_payload,
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
def _sanitize_nested(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    """Remove terminal controls from corpus-derived result data.

    Contract metadata is validated before this function runs, so only result,
    scope details, errors and epoch diagnostics are sanitized.  A node/depth
    budget keeps an untrusted producer from turning the desktop bridge into an
    unbounded renderer while preserving the legacy JSON shape.
    """

    # Keep this private compatibility wrapper for callers/tests that used the
    # old helper name, but make the implementation shared with the MCP and
    # human CLI boundaries so JSON keys and unknown values are safe too.
    return sanitize_untrusted_payload(value, depth=depth, budget=budget)


def _sanitize_result_sections(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a safe copy while leaving strict request echoes byte-for-byte."""

    result = dict(payload)
    budget = [MAX_SANITIZED_PAYLOAD_NODES]
    for key in (
        "result",
        "scopes",
        "error",
        "observed_epoch",
        "coverage",
        "budget",
        "sources",
        "citations",
        "entities",
        "relations",
        "contradictions",
        "graph_budget",
        "telemetry",
        "read_budget",
    ):
        if key in result:
            result[key] = _sanitize_nested(result[key], budget=budget)
    return result


def _validate_outcome_consistency(payload: Mapping[str, object]) -> None:
    """Reject a complete envelope whose status lies about its exit code."""

    code = payload.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int):
        return
    try:
        coverage, status = expected_read_outcome(code)
    except (KeyError, ValueError):
        return
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
            response_version=request.response_version,
        )


def _validated_payload(request: ReadRequest, value: object) -> dict[str, object]:
    try:
        operation = (
            ReadOperation.CONTEXT
            if request.operation == "ask"
            else ReadOperation(request.operation)
        )
        compact_v2 = (
            isinstance(value, Mapping)
            and value.get("schema") in {
                "neocortex.context-response/v2",
                "neocortex.evidence-response/v2",
            }
        )
        complete_v1 = (
            isinstance(value, Mapping)
            and _COMPLETE_ENVELOPE_FIELDS.issubset(value)
        )
        legacy_payload = not (compact_v2 or complete_v1)
        if request.operation == "ask":
            expected_schema = (
                "neocortex.context-response/v2"
                if request.response_version == 2
                else "neocortex.read-api/v1"
            )
            if isinstance(value, Mapping) and value.get("schema") != expected_schema:
                raise ReadClientError(
                    "La versión de respuesta no coincide con la versión solicitada."
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
                }
                else None
            ),
            strict_echo=not legacy_payload,
        )
        if complete_v1:
            _validate_outcome_consistency(validated)
        return _sanitize_result_sections(validated)
    except (ReadContractError, TypeError, ValueError) as exc:
        raise ReadClientError(str(exc)) from exc


__all__ = ["SharedReadClient"]
