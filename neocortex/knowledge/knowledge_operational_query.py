"""Bounded, read-only operational questions over existing owner evidence.

This module is intentionally separate from the citation/context compiler.  It
recognises a small set of operational question families and dispatches to the
owner readers that already publish those facts.  It never opens the corpus,
creates state, infers permissions, or turns a recommendation into an action.

The first implementation is deliberately conservative: one request selects one
owner view so that the returned cursor and snapshot always have one unambiguous
owner binding.  Callers can issue another request for a different owner when a
question spans formats.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from .knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty,
    AssetProblemScope,
)


OPERATIONAL_QUERY_SCHEMA = "neocortex.knowledge-operational-query/v1"
OperationalStatus = Literal["ok", "empty", "partial", "unavailable", "blocked", "error"]


class OperationalIntent(StrEnum):
    """Recognised operational question families."""

    PDF_PROTECTED = "pdf_protected"
    PDF_ERROR = "pdf_error"
    OFFICE_ERROR = "office_error"
    ARCHIVE_ISSUE = "archive_issue"
    CORPUS_ERROR = "corpus_error"
    CURATION_DISPOSAL = "curation_disposal"
    UNKNOWN = "unknown"


class OperationalOwner(StrEnum):
    PDF = "pdf"
    TEXT = "text"
    OFFICE = "office"
    ARCHIVE = "archive"
    FRAMEWORK = "framework"
    FEDERATED = "federated"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class OperationalQueryRequest:
    """Validated request bound to one state root and one corpus root."""

    query: str
    state_directory: Path
    source_root: Path
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 4096:
            raise ValueError("query must be non-empty and at most 4096 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.query):
            raise ValueError("query contains control characters")
        for label, value in (("state_directory", self.state_directory), ("source_root", self.source_root)):
            if not isinstance(value, Path):
                raise TypeError(f"{label} must be a Path")
            if not value.is_absolute():
                raise ValueError(f"{label} must be an absolute Linux path")
            if any(part in {".", ".."} for part in value.parts):
                raise ValueError(f"{label} must not contain traversal components")
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or not self.cursor.strip()
            or len(self.cursor) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in self.cursor)
        ):
            raise ValueError("cursor must be bounded, non-empty text")


@dataclass(frozen=True, slots=True)
class OperationalFact:
    """One typed owner fact with a bounded provenance projection."""

    scope: AssetProblemScope
    code: str
    certainty: AssetDiagnosticCertainty
    owner: str
    record_id: str
    snapshot_id: str
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AssetProblemScope):
            raise TypeError("scope must be AssetProblemScope")
        if not isinstance(self.certainty, AssetDiagnosticCertainty):
            raise TypeError("certainty must be AssetDiagnosticCertainty")
        for label, value, maximum in (
            ("code", self.code, 128),
            ("owner", self.owner, 64),
            ("record_id", self.record_id, 2048),
            ("snapshot_id", self.snapshot_id, 256),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{label} must be bounded non-empty text")
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "code": self.code,
            "certainty": self.certainty.value,
            "owner": self.owner,
            "record_id": self.record_id,
            "snapshot_id": self.snapshot_id,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class OperationalQueryResult:
    """Read-only result; recommendations never imply authorization."""

    query: str
    intent: OperationalIntent
    owner: OperationalOwner
    status: OperationalStatus
    facts: tuple[OperationalFact, ...]
    snapshot_id: str | None
    next_cursor: str | None
    coverage: Mapping[str, Any]
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.facts, tuple) or len(self.facts) > 1000:
            raise ValueError("facts must be a bounded tuple")
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise TypeError("next_cursor must be text or None")
        if self.error is not None and not isinstance(self.error, Mapping):
            raise TypeError("error must be a mapping or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OPERATIONAL_QUERY_SCHEMA,
            "query": self.query,
            "intent": self.intent.value,
            "owner": self.owner.value,
            "status": self.status,
            "read_only": True,
            "advisory_only": True,
            "mutation_authorized": False,
            "facts": [fact.to_dict() for fact in self.facts],
            "snapshot_id": self.snapshot_id,
            "next_cursor": self.next_cursor,
            "coverage": dict(self.coverage),
            "error": None if self.error is None else dict(self.error),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def detect_operational_intent(query: str) -> OperationalIntent:
    """Classify only explicit operational vocabulary; unknown is fail-closed."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    text = _fold(query)
    protected = any(token in text for token in ("proteg", "password", "contrasena", "encrypted", "cifrad"))
    error = any(token in text for token in ("error", "fallo", "problema", "diagnostic", "no procesa", "failed"))
    pdf = "pdf" in text
    office = any(token in text for token in ("ppt", "pptx", "powerpoint", "presentacion", "office"))
    archive = any(token in text for token in ("zip", "archivo comprim", "archive"))
    disposal = any(token in text for token in ("elimin", "borr", "papelera", "disposal", "deletion", "duplicad"))
    if pdf and protected:
        return OperationalIntent.PDF_PROTECTED
    if disposal:
        return OperationalIntent.CURATION_DISPOSAL
    if archive:
        return OperationalIntent.ARCHIVE_ISSUE
    if office and error:
        return OperationalIntent.OFFICE_ERROR
    if pdf and error:
        return OperationalIntent.PDF_ERROR
    if error:
        return OperationalIntent.CORPUS_ERROR
    return OperationalIntent.UNKNOWN


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bounded_projection(value: object, *, depth: int = 0) -> object:
    if depth > 3:
        return "[bounded]"
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _bounded_projection(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_projection(item, depth=depth + 1) for item in list(value)[:64]]
    return str(value)[:2048]


def _error_result(request: OperationalQueryRequest, intent: OperationalIntent, owner: OperationalOwner,
                  status: OperationalStatus, code: str, message: str) -> OperationalQueryResult:
    return OperationalQueryResult(
        query=request.query.strip(), intent=intent, owner=owner, status=status, facts=(),
        snapshot_id=None, next_cursor=None,
        coverage={"status": "unknown", "owner_snapshot_consistent": False, "persisted_only": True},
        error={"code": code, "message": message[:1000]},
    )


class KnowledgeOperationalQueryService:
    """Dispatch operational questions to existing bounded owner readers."""

    def query(self, request: OperationalQueryRequest) -> OperationalQueryResult:
        if not isinstance(request, OperationalQueryRequest):
            raise TypeError("request must be an OperationalQueryRequest")
        intent = detect_operational_intent(request.query)
        if intent is OperationalIntent.UNKNOWN:
            return _error_result(request, intent, OperationalOwner.NONE, "empty", "unsupported_operational_intent", "no supported diagnostic intent")
        try:
            if intent is OperationalIntent.PDF_ERROR:
                return self._format_diagnostics(request, intent, "pdf")
            if intent is OperationalIntent.ARCHIVE_ISSUE:
                return self._format_diagnostics(request, intent, "archive")
            if intent is OperationalIntent.PDF_PROTECTED:
                return self._review_candidates(request, intent, route_name="pdf", recommendation="keep_protected")
            if intent is OperationalIntent.OFFICE_ERROR:
                return self._review_candidates(request, intent, route_name="office", recommendation="manual_review")
            if intent is OperationalIntent.CORPUS_ERROR:
                return self._corpus_errors(request, intent)
            return self._review_candidates(request, intent, recommendation="deletion_candidate")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _error_result(request, intent, OperationalOwner.FRAMEWORK, "blocked", "owner_read_failed", str(exc))

    def _format_diagnostics(self, request: OperationalQueryRequest, intent: OperationalIntent, owner: str) -> OperationalQueryResult:
        from neocortex.api.content_diagnostics_api import content_diagnostics_payload

        payload = content_diagnostics_payload(
            owner, request.state_directory, request.source_root, request.limit, cursor=request.cursor,
        )
        status = str(payload.get("status", "error"))
        if status != "ok":
            return _error_result(
                request, intent, OperationalOwner(owner), "unavailable" if status == "unavailable" else "blocked",
                str((payload.get("error") or {}).get("kind", "owner_state_unavailable")),
                str((payload.get("error") or {}).get("message", "owner state unavailable")),
            )
        scope = AssetProblemScope.PROCESSING
        facts = tuple(
            OperationalFact(
                scope=scope,
                code=str(item.get("error_type") or item.get("reason_code") or "diagnostic_record"),
                certainty=AssetDiagnosticCertainty.OBSERVED,
                owner=owner,
                record_id=str(item.get("file_key") or item.get("container_key") or "diagnostic-record"),
                snapshot_id=str(payload.get("snapshot_id")),
                provenance={
                    "operation": payload.get("operation"),
                    "requested_root": payload.get("requested_root"),
                    "reason_field": payload.get("reason_field"),
                    "record": _bounded_projection(item),
                    "projection_digest": _digest(item),
                },
            )
            for item in payload.get("items", []) if isinstance(item, Mapping)
        )
        next_cursor = payload.get("next_cursor")
        return OperationalQueryResult(
            request.query.strip(), intent, OperationalOwner(owner), "ok" if facts else "empty", facts,
            str(payload.get("snapshot_id")), next_cursor if isinstance(next_cursor, str) else None,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": bool((payload.get("coverage") or {}).get("snapshot_consistent")),
                "requested_root": payload.get("requested_root"),
                "matched_count": payload.get("matched_count"),
                "query_page_complete": next_cursor is None,
            },
        )

    def _review_candidates(self, request: OperationalQueryRequest, intent: OperationalIntent, *,
                           route_name: str | None = None, recommendation: str | None = None) -> OperationalQueryResult:
        from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
        from neocortex.workflow.review.review_candidate_query import list_review_candidates_page

        database = KnowledgeStatePaths.from_directory(request.state_directory).framework
        page = list_review_candidates_page(
            database, limit=request.limit, route_name=route_name, recommendation=recommendation, after=request.cursor,
        )
        owner = OperationalOwner.FRAMEWORK
        if page.availability != "ready":
            state = "unavailable" if page.availability == "absent" else "blocked"
            return _error_result(request, intent, owner, state, page.reason_code or "review_owner_unavailable", page.reason_code or "review owner unavailable")
        facts = tuple(
            OperationalFact(
                scope=AssetProblemScope.POLICY if intent is OperationalIntent.CURATION_DISPOSAL else AssetProblemScope.PROCESSING,
                code=str(item.get("reason_code") or "review_candidate"),
                certainty=AssetDiagnosticCertainty.OBSERVED,
                owner="framework",
                record_id=f"review-candidate:{item.get('route_name', 'unknown')}:{item.get('volume_id', '')}:{item.get('file_id', '')}:{item.get('reason_code', '')}",
                snapshot_id=str(page.snapshot_id),
                provenance={
                    "operation": "review-candidates",
                    "route_name": item.get("route_name"),
                    "recommendation": item.get("recommendation"),
                    "record": _bounded_projection(item),
                    "projection_digest": _digest(item),
                    "mutation_authorized": False,
                },
            )
            for item in page.to_dict().get("items", []) if isinstance(item, Mapping)
        )
        next_cursor = page.next_cursor.to_token() if page.next_cursor is not None else None
        return OperationalQueryResult(
            request.query.strip(), intent, owner, "ok" if facts else "empty", facts,
            str(page.snapshot_id), next_cursor,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": True,
                "total_matching": page.total_matching,
                "query_page_complete": next_cursor is None,
                "recommendations_are_advisory": True,
            },
        )

    def _corpus_errors(
        self, request: OperationalQueryRequest, intent: OperationalIntent,
    ) -> OperationalQueryResult:
        """Combine bounded diagnostic owner pages without mixing cursors."""

        # A cursor is owned by one diagnostic page. Refuse to pretend a token
        # for one owner can continue a federated page from another owner.
        if request.cursor is not None:
            return _error_result(
                request,
                intent,
                OperationalOwner.FEDERATED,
                "blocked",
                "federated_cursor_requires_owner_scope",
                "continue each diagnostic owner with its own cursor",
            )

        owner_results = (
            ("pdf", self._format_diagnostics(request, OperationalIntent.PDF_ERROR, "pdf")),
            ("text", self._format_diagnostics(request, OperationalIntent.PDF_ERROR, "text")),
            ("archive", self._format_diagnostics(request, OperationalIntent.ARCHIVE_ISSUE, "archive")),
            ("office", self._review_candidates(request, OperationalIntent.OFFICE_ERROR, recommendation="manual_review")),
        )
        facts: list[OperationalFact] = []
        owner_coverage: dict[str, Any] = {}
        snapshots: dict[str, str | None] = {}
        incomplete: list[str] = []
        for owner, result in owner_results:
            facts.extend(result.facts)
            snapshots[owner] = result.snapshot_id
            owner_coverage[owner] = {
                "status": result.status,
                "snapshot_id": result.snapshot_id,
                "next_cursor": result.next_cursor,
                "fact_count": len(result.facts),
            }
            if result.status not in {"ok", "empty"} or result.next_cursor is not None:
                incomplete.append(owner)
        status = "partial" if incomplete else "ok" if facts else "empty"
        snapshot_id = _digest(snapshots) if snapshots else None
        error = (
            {
                "code": "owner_pages_incomplete",
                "message": "some diagnostic owner pages require a separate continuation",
            }
            if incomplete else None
        )
        return OperationalQueryResult(
            request.query.strip(), intent, OperationalOwner.FEDERATED, status,
            tuple(facts[: request.limit * len(owner_results)]), snapshot_id, None,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": all(
                    value is not None for value in snapshots.values()
                ),
                "owners": owner_coverage,
                "incomplete_owners": incomplete,
                "recommendations_are_advisory": True,
            },
            error,
        )


__all__ = [
    "KnowledgeOperationalQueryService",
    "OPERATIONAL_QUERY_SCHEMA",
    "OperationalFact",
    "OperationalIntent",
    "OperationalOwner",
    "OperationalQueryRequest",
    "OperationalQueryResult",
    "detect_operational_intent",
]
