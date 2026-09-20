"""Stable request and presentation models for desktop read-only work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ReadOperation = Literal["status", "search", "ask"]
ReadVisualState = Literal["completed", "warning", "failed"]

MAX_QUERY_CHARACTERS = 4_096
MAX_RESULTS_PER_SCOPE = 100
MAX_PRESENTATION_CHARACTERS = 128_000
MAX_PRESENTATION_ROWS = 200

_OPERATIONS = frozenset({"status", "search", "ask"})
_SCOPES = frozenset({"personal", "framework", "all"})


class ReadClientError(RuntimeError):
    """The shared read API could not provide a safe display contract."""


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """One bounded desktop request over a fixed published-state scope.

    Context reads default to the compact v2 contract.  The v1 context envelope
    remains available only by setting ``response_version=1`` explicitly.
    """

    operation: ReadOperation
    scope: str = "all"
    query: str = ""
    limit: int = 10
    response_version: int = 2

    def validated(self) -> ReadRequest:
        self._validate_choices()
        self._validate_limit()
        self._validate_response_version()
        query = self.query.strip()
        self._validate_query(query)
        return ReadRequest(
            operation=self.operation,
            scope=self.scope,
            query=query,
            limit=self.limit,
            response_version=self.response_version,
        )

    def _validate_choices(self) -> None:
        if self.operation not in _OPERATIONS:
            raise ValueError("operation must be status, search or ask")
        if self.scope not in _SCOPES:
            raise ValueError("scope must be personal, framework or all")

    def _validate_limit(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= MAX_RESULTS_PER_SCOPE:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS_PER_SCOPE} per scope")

    def _validate_response_version(self) -> None:
        if (
            isinstance(self.response_version, bool)
            or not isinstance(self.response_version, int)
            or self.response_version not in {1, 2}
        ):
            raise ValueError("response_version must be 1 or 2")

    def _validate_query(self, query: str) -> None:
        if self.operation in {"search", "ask"} and not query:
            raise ValueError("Escribe una consulta antes de continuar.")
        if len(query) > MAX_QUERY_CHARACTERS:
            raise ValueError(f"La consulta no puede exceder {MAX_QUERY_CHARACTERS} caracteres.")


@dataclass(frozen=True, slots=True)
class ReadPresentation:
    """Plain-text view model safe to expose in a selectable Qt widget."""

    title: str
    summary: str
    body: str
    state: ReadVisualState


class ReadClient(Protocol):
    def execute(self, request: ReadRequest) -> dict[str, object]: ...


__all__ = [
    "MAX_PRESENTATION_CHARACTERS",
    "MAX_PRESENTATION_ROWS",
    "MAX_QUERY_CHARACTERS",
    "MAX_RESULTS_PER_SCOPE",
    "ReadClient",
    "ReadClientError",
    "ReadOperation",
    "ReadPresentation",
    "ReadRequest",
    "ReadVisualState",
]
