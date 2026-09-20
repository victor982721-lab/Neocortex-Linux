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

import binascii
import hashlib
import json
import base64
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from .knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty,
    AssetProblemScope,
)
from .knowledge_read_budget import KnowledgeReadBudget


OPERATIONAL_QUERY_SCHEMA = "neocortex.knowledge-operational-query/v1"
SEMANTIC_ITEM_DIAGNOSTIC_SCHEMA = "neocortex.semantic-item-diagnostic/v1"
OperationalStatus = Literal[
    "ok", "empty", "partial", "unavailable", "blocked", "error", "snapshot_changed"
]


class OperationalIntent(StrEnum):
    """Recognised operational question families."""

    PDF_PROTECTED = "pdf_protected"
    PDF_ERROR = "pdf_error"
    OFFICE_ERROR = "office_error"
    CORPUS_ERROR = "corpus_error"
    CURATION_DISPOSAL = "curation_disposal"
    UNKNOWN = "unknown"


class OperationalOwner(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TEXT = "text"
    OFFICE = "office"
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"
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
    scope: str = "personal"
    response_version: int = 1
    diagnostic_owner: str | tuple[str, ...] | None = None
    budget: KnowledgeReadBudget | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 4096:
            raise ValueError("query must be non-empty and at most 4096 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.query):
            raise ValueError("query contains control characters")
        for label, value in (
            ("state_directory", self.state_directory),
            ("source_root", self.source_root),
        ):
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
            or len(self.cursor) > 65536
            or any(ord(char) < 32 or ord(char) == 127 for char in self.cursor)
        ):
            raise ValueError("cursor must be bounded, non-empty text")
        if self.scope not in {"personal", "framework", "all"}:
            raise ValueError("scope must be personal, framework or all")
        if isinstance(self.response_version, bool) or self.response_version not in {1, 2}:
            raise ValueError("response_version must be 1 or 2")
        if self.diagnostic_owner is not None and not isinstance(
            self.diagnostic_owner, (str, tuple)
        ):
            raise TypeError("diagnostic_owner must be a v2 owner, tuple or None")
        if self.budget is not None and not isinstance(self.budget, KnowledgeReadBudget):
            raise TypeError("budget must be a KnowledgeReadBudget")


@dataclass(frozen=True, slots=True)
class OperationalFederatedCursor:
    """Canonical continuation token for a multi-owner operational page."""

    query: str
    scope: str
    state_directory: str
    source_root: str
    intent: OperationalIntent
    limit: int
    owner_cursors: tuple[tuple[str, str | None], ...]
    owner_snapshots: tuple[tuple[str, str | None], ...]

    def _payload(self) -> dict[str, Any]:
        return {
            "v": 1,
            "query": self.query,
            "scope": self.scope,
            "state_directory": self.state_directory,
            "source_root": self.source_root,
            "intent": self.intent.value,
            "limit": self.limit,
            "owner_cursors": dict(self.owner_cursors),
            "owner_snapshots": dict(self.owner_snapshots),
        }

    def to_token(self) -> str:
        payload = self._payload()
        envelope = {"payload": payload, "digest": _digest(payload)}
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def from_token(cls, token: str) -> "OperationalFederatedCursor":
        if not isinstance(token, str) or not 1 <= len(token) <= 65536:
            raise ValueError("federated cursor token must be bounded non-empty text")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "digest"}:
                raise ValueError("federated cursor envelope is invalid")
            payload = envelope["payload"]
            if not isinstance(payload, dict) or envelope["digest"] != _digest(payload):
                raise ValueError("federated cursor digest mismatch")
            if payload.get("v") != 1:
                raise ValueError("federated cursor version is unsupported")
            if (
                not isinstance(payload.get("query"), str)
                or not payload["query"].strip()
                or len(payload["query"]) > 4096
                or payload.get("scope") not in {"personal", "framework", "all"}
                or not isinstance(payload.get("state_directory"), str)
                or not isinstance(payload.get("source_root"), str)
                or type(payload.get("limit")) is not int
                or not 1 <= payload["limit"] <= 1000
            ):
                raise ValueError("federated cursor binding fields are invalid")
            cursors = payload.get("owner_cursors")
            snapshots = payload.get("owner_snapshots")
            if not isinstance(cursors, dict) or not isinstance(snapshots, dict):
                raise ValueError("federated cursor owner maps are invalid")
            if set(cursors) != set(snapshots) or not cursors:
                raise ValueError("federated cursor owner maps do not match")
            if any(not isinstance(key, str) or not key for key in cursors):
                raise ValueError("federated cursor owner names are invalid")
            if any(
                value is not None and (not isinstance(value, str) or not value)
                for value in cursors.values()
            ):
                raise ValueError("federated cursor owner continuation is invalid")
            if any(
                value is not None and (not isinstance(value, str) or not value)
                for value in snapshots.values()
            ):
                raise ValueError("federated cursor owner snapshot is invalid")
            result = cls(
                query=payload["query"],
                scope=payload["scope"],
                state_directory=payload["state_directory"],
                source_root=payload["source_root"],
                intent=OperationalIntent(payload["intent"]),
                limit=payload["limit"],
                owner_cursors=tuple(sorted(cursors.items())),
                owner_snapshots=tuple(sorted(snapshots.items())),
            )
            if result.to_token() != token:
                raise ValueError("federated cursor token is not canonical")
            return result
        except (
            binascii.Error,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise ValueError("invalid federated cursor token") from exc


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
    metrics: Mapping[str, Any] = field(default_factory=dict, compare=False)

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
            "metrics": dict(self.metrics),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fold(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def detect_operational_intent(query: str) -> OperationalIntent:
    """Classify only explicit operational vocabulary; unknown is fail-closed."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    text = _fold(query)
    protected = any(
        token in text for token in ("proteg", "password", "contrasena", "encrypted", "cifrad")
    )
    error = any(
        token in text
        for token in ("error", "fallo", "problema", "diagnostic", "no procesa", "failed")
    )
    pdf = "pdf" in text
    office = any(token in text for token in ("ppt", "pptx", "powerpoint", "presentacion", "office"))
    disposal = any(
        token in text
        for token in ("elimin", "borr", "papelera", "disposal", "deletion", "duplicad")
    )
    if pdf and protected:
        return OperationalIntent.PDF_PROTECTED
    if disposal:
        return OperationalIntent.CURATION_DISPOSAL
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


def _record_component(value: object, *, maximum: int = 512) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    return _digest(text)


def _diagnostic_record_id(owner: str, item: Mapping[str, Any], code: str) -> str:
    """Build a stable bounded identity for one persisted diagnostic row."""

    key = item.get("file_key") or item.get("container_key") or item.get("path") or "record"
    prefix = f"{owner}:{_record_component(key)}"
    issue_id = item.get("issue_id")
    if issue_id is not None:
        return _record_component(f"{prefix}:issue:{issue_id}", maximum=2048)
    page_number = item.get("page_number")
    member_chain = item.get("member_chain") or item.get("member_path")
    suffix = f":{_record_component(code)}"
    if page_number is not None:
        suffix += f":page:{_record_component(page_number)}"
    if member_chain:
        suffix += f":member:{_record_component(member_chain)}"
    return _record_component(prefix + suffix, maximum=2048)


def _error_result(
    request: OperationalQueryRequest,
    intent: OperationalIntent,
    owner: OperationalOwner,
    status: OperationalStatus,
    code: str,
    message: str,
) -> OperationalQueryResult:
    return OperationalQueryResult(
        query=request.query.strip(),
        intent=intent,
        owner=owner,
        status=status,
        facts=(),
        snapshot_id=None,
        next_cursor=None,
        coverage={"status": "unknown", "owner_snapshot_consistent": False, "persisted_only": True},
        error={"code": code, "message": message[:1000]},
    )


def _validated_diagnostic_item_id(item_id: object) -> str:
    if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 512:
        raise ValueError("item_id must be bounded non-empty text")
    if any(ord(char) < 32 or ord(char) == 127 for char in item_id):
        raise ValueError("item_id contains control characters")
    return item_id.strip()


def _trace_target(
    target: Mapping[str, Any] | None,
    *,
    fields: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if target is None:
        return {"status": "not_observed", "reason": "target_diagnostics_absent"}
    selected_fields = fields or (
        "item_id",
        "stage",
        "observed_in_published_scope",
        "source_kind",
        "source_status",
        "published_revision_id",
        "current_revision_id",
        "source_revision_is_current",
        "source_processing_signature",
        "generation_id",
        "model_signature",
        "ref_id",
        "entity_id",
        "raw_score",
        "candidate_rank",
        "within_candidate_window",
        "observed_rank",
        "raw_rank",
        "rank_is_global",
        "rank_granularity",
        "rank_budget_exhausted",
        "fused_result_rank",
        "present_in_fused_results",
        "raw_rank_basis",
        "section_kind",
        "section_id",
        "start_char",
        "end_char",
        "published_chunk_fingerprint_verified",
        "backend",
        "pipeline",
        "calibration_contract_conflict",
        "snippet",
    )
    result = {name: _bounded_projection(target[name]) for name in selected_fields if name in target}
    result["status"] = "observed"
    if fields is None:
        variant_fields: tuple[str, ...] = ("query_variant_diagnostics",)
    else:
        variant_fields = ()
    for name in variant_fields:
        if name in target:
            result[name] = _bounded_projection(target[name])
    return result


def semantic_item_diagnostic(result: object, item_id: str) -> dict[str, Any]:
    """Project existing semantic target diagnostics into one ordered item trace.

    The semantic service already performs the scan, thresholding, fusion and
    resolution.  This helper only joins its published in-memory projections;
    it does not rerun search, open SQLite, call a model, or infer authority.
    """

    selected_id = _validated_diagnostic_item_id(item_id)
    query = getattr(result, "query", None)
    rankings = getattr(result, "rankings", None)
    lexical_rankings = getattr(result, "lexical_rankings", None)
    fused = getattr(result, "fused", None)
    if (
        not isinstance(query, str)
        or not isinstance(rankings, tuple)
        or not isinstance(lexical_rankings, tuple)
        or not isinstance(fused, tuple)
    ):
        raise TypeError("result must be a SemanticSearchResult-compatible value")

    targets: list[dict[str, Any]] = []
    ranking_names: list[str] = []
    for ranking in rankings:
        name = str(getattr(ranking, "name", "semantic"))
        ranking_names.append(name)
        provenance = getattr(ranking, "provenance", {})
        raw_targets = (
            provenance.get("target_diagnostics") if isinstance(provenance, Mapping) else None
        )
        if not isinstance(raw_targets, list):
            continue
        for target in raw_targets:
            if isinstance(target, Mapping) and target.get("item_id") == selected_id:
                targets.append(
                    {
                        "ranking": name,
                        **_trace_target(target),
                        "candidate_selection": _bounded_projection(
                            provenance.get("candidate_selection", {})
                        ),
                    }
                )

    # A target can be absent from semantic rankings when it was only retained
    # by a lexical channel; preserve that fact instead of fabricating stages.
    target = targets[0] if targets else None
    lexical_observed = False
    for lexical_ranking in lexical_rankings:
        for value in getattr(lexical_ranking, "hits", ()):
            lexical_hit = getattr(value, "hit", value)
            if getattr(lexical_hit, "item_id", None) == selected_id:
                lexical_observed = True
                break
        if lexical_observed:
            break
    if target is None and lexical_observed:
        # Lexical-only results do not expose the semantic funnel stages. Keep
        # that limitation explicit rather than manufacturing publication or
        # threshold facts from a different ranking contract.
        target = {
            "item_id": selected_id,
            "stage": "lexical_only",
            "observed_in_published_scope": None,
            "ranking": "lexical",
        }
    contributions: list[dict[str, Any]] = []
    presentation: dict[str, Any] = {"status": "not_present", "reason": "item_not_in_fused_results"}
    for rank, value in enumerate(fused, 1):
        fused_hit = getattr(value, "fused", None)
        if getattr(fused_hit, "item_id", None) != selected_id:
            continue
        evidence_values = getattr(fused_hit, "evidence", ())
        for evidence in evidence_values if isinstance(evidence_values, tuple) else ():
            contributions.append(
                {
                    "ranking": getattr(evidence, "ranking", None),
                    "rank": getattr(evidence, "rank", None),
                    "raw_score": getattr(evidence, "raw_score", None),
                    "contribution": getattr(evidence, "contribution", None),
                    "entity_id": getattr(evidence, "entity_id", None),
                    "ref_id": getattr(evidence, "ref_id", None),
                    "generation_id": getattr(evidence, "generation_id", None),
                }
            )
        presentation = {
            "status": "present",
            "fused_rank": rank,
            "path": _bounded_projection(getattr(value, "path", None)),
            "source_kind": _bounded_projection(getattr(value, "source_kind", None)),
            "source_identity": _bounded_projection(getattr(value, "source_identity", None)),
            "snippet": _bounded_projection(getattr(value, "snippet", None)),
        }
        break

    status = (
        "observed" if target is not None or presentation["status"] == "present" else "not_observed"
    )
    eligibility_target = _trace_target(
        target,
        fields=(
            "item_id",
            "stage",
            "observed_in_published_scope",
            "source_kind",
            "source_status",
            "published_revision_id",
            "current_revision_id",
            "source_revision_is_current",
            "generation_id",
            "model_signature",
            "ref_id",
            "entity_id",
            "backend",
            "pipeline",
        ),
    )
    publication_target = _trace_target(
        target,
        fields=(
            "item_id",
            "stage",
            "source_kind",
            "source_status",
            "published_revision_id",
            "current_revision_id",
            "source_revision_is_current",
            "source_processing_signature",
            "generation_id",
            "model_signature",
            "ref_id",
            "entity_id",
            "published_chunk_fingerprint_verified",
            "backend",
            "pipeline",
        ),
    )
    window_target = _trace_target(
        target,
        fields=(
            "item_id",
            "stage",
            "raw_score",
            "candidate_rank",
            "within_candidate_window",
            "observed_rank",
            "raw_rank",
            "rank_is_global",
            "rank_granularity",
            "rank_budget_exhausted",
            "raw_rank_basis",
            "fused_result_rank",
            "present_in_fused_results",
            "ref_id",
            "entity_id",
        ),
    )
    resolution_target = _trace_target(
        target,
        fields=(
            "item_id",
            "stage",
            "source_kind",
            "source_status",
            "published_revision_id",
            "current_revision_id",
            "source_revision_is_current",
            "ref_id",
            "entity_id",
            "section_kind",
            "section_id",
            "start_char",
            "end_char",
            "snippet",
        ),
    )
    threshold_target = _trace_target(
        target,
        fields=(
            "item_id",
            "stage",
            "raw_score",
            "source_score_floor",
            "above_score_floor",
            "threshold_evaluation",
            "calibration_contract_conflict",
            "raw_rank",
            "candidate_rank",
            "within_candidate_window",
            "raw_rank_basis",
            "ref_id",
            "entity_id",
            "source_kind",
        ),
    )
    if target is not None and "query_variant_diagnostics" in target:
        threshold_target["query_variant_diagnostics"] = _bounded_projection(
            target["query_variant_diagnostics"]
        )
    resolved = bool(
        target is not None
        and (
            target.get("snippet") is not None
            or target.get("entity_id") is not None
            or target.get("ref_id") is not None
        )
    ) or bool(
        presentation["status"] == "present"
        and (presentation.get("path") or presentation.get("source_identity"))
    )
    return {
        "schema": SEMANTIC_ITEM_DIAGNOSTIC_SCHEMA,
        "query": query,
        "item_id": selected_id,
        "status": status,
        "read_only": True,
        "advisory_only": True,
        "mutation_authorized": False,
        "stages": {
            "eligibility": eligibility_target,
            "publication": publication_target,
            "window": window_target,
            "threshold": {
                "status": "observed" if target is not None else "not_observed",
                "rankings": ranking_names,
                "candidate_selection": target.get("candidate_selection", {})
                if target is not None
                else {},
                "stage": target.get("stage") if target is not None else None,
                "target": threshold_target,
            },
            "fusion": {
                "status": "present" if contributions else "not_present",
                "contributions": contributions[:20],
                "target": window_target,
            },
            "resolution": {
                "status": "resolved" if resolved else "not_observed",
                "target": resolution_target,
            },
            "presentation": presentation,
        },
    }


class KnowledgeOperationalQueryService:
    """Dispatch operational questions to existing bounded owner readers."""

    def query(self, request: OperationalQueryRequest) -> OperationalQueryResult:
        if not isinstance(request, OperationalQueryRequest):
            raise TypeError("request must be an OperationalQueryRequest")
        intent = detect_operational_intent(request.query)
        if intent is OperationalIntent.UNKNOWN:
            return _error_result(
                request,
                intent,
                OperationalOwner.NONE,
                "empty",
                "unsupported_operational_intent",
                "no supported diagnostic intent",
            )
        try:
            if request.response_version == 2 and intent in {
                OperationalIntent.PDF_ERROR,
                OperationalIntent.OFFICE_ERROR,
                OperationalIntent.CORPUS_ERROR,
            }:
                selected_owner = request.diagnostic_owner
                if selected_owner is None:
                    selected_owner = {
                        OperationalIntent.PDF_ERROR: "pdf",
                    }.get(intent, "all")
                return self.content_diagnostics(request, owner=selected_owner)
            if intent is OperationalIntent.PDF_ERROR:
                return self._format_diagnostics(request, intent, "pdf")
            if intent is OperationalIntent.PDF_PROTECTED:
                return self._review_candidates(
                    request, intent, route_name="pdf", recommendation="keep_protected"
                )
            if intent is OperationalIntent.OFFICE_ERROR:
                return self._federated_query(
                    request,
                    intent,
                    ("office", "text"),
                )
            if intent is OperationalIntent.CORPUS_ERROR:
                return self._federated_query(
                    request,
                    intent,
                    ("pdf", "text", "office"),
                )
            return self._review_candidates(request, intent, recommendation="deletion_candidate")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _error_result(
                request,
                intent,
                OperationalOwner.FRAMEWORK,
                "blocked",
                "owner_read_failed",
                str(exc),
            )

    def content_diagnostics(
        self,
        request: OperationalQueryRequest,
        *,
        owner: str | tuple[str, ...] | None = "all",
        budget: KnowledgeReadBudget | None = None,
    ) -> OperationalQueryResult:
        """Project the additive content-diagnostics/v2 page as typed facts."""

        if not isinstance(request, OperationalQueryRequest):
            raise TypeError("request must be an OperationalQueryRequest")
        if budget is not None and not isinstance(budget, KnowledgeReadBudget):
            raise TypeError("budget must be a KnowledgeReadBudget")
        from neocortex.api.content_diagnostics_api import content_diagnostics_v2_payload

        selected_budget = budget if budget is not None else request.budget
        payload = content_diagnostics_v2_payload(
            owner,
            request.state_directory,
            request.source_root,
            request.limit,
            cursor=request.cursor,
            budget=selected_budget,
        )
        raw_owner = payload.get("owner")
        try:
            result_owner = (
                OperationalOwner.FEDERATED
                if raw_owner == "all" or isinstance(raw_owner, list)
                else OperationalOwner(str(raw_owner))
            )
        except ValueError:
            result_owner = OperationalOwner.FEDERATED
        status = str(payload.get("status", "error"))
        allowed_statuses = {"ok", "empty", "partial", "unavailable", "blocked", "error", "snapshot_changed"}
        if status not in allowed_statuses:
            status = "error"
        raw_items = payload.get("items", [])
        items = raw_items if isinstance(raw_items, list) else []
        snapshot = payload.get("snapshot_id")
        snapshot_id = snapshot if isinstance(snapshot, str) else None
        facts: list[OperationalFact] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            code = item.get("error_type") or item.get("status") or "diagnostic_record"
            if not isinstance(code, str) or not code:
                continue
            item_owner = item.get("owner") or (raw_owner if isinstance(raw_owner, str) else "federated")
            if not isinstance(item_owner, str) or not item_owner:
                item_owner = "federated"
            record_id = item.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                record_id = _diagnostic_record_id(item_owner, item, code)
            facts.append(
                OperationalFact(
                    scope=AssetProblemScope.PROCESSING,
                    code=code,
                    certainty=AssetDiagnosticCertainty.OBSERVED,
                    owner=item_owner,
                    record_id=record_id,
                    snapshot_id=snapshot_id or "unavailable",
                    provenance={
                        "operation": payload.get("operation"),
                        "requested_root": payload.get("requested_root"),
                        "record": _bounded_projection(item),
                        "projection_digest": _digest(item),
                    },
                )
            )
        error_value = payload.get("error")
        error_map = error_value if isinstance(error_value, Mapping) else None
        if status == "snapshot_changed":
            result_status: OperationalStatus = "snapshot_changed"
        elif status in {"unavailable", "blocked", "error"}:
            result_status = cast(OperationalStatus, status)
        elif status == "partial":
            result_status = "partial"
        else:
            result_status = "ok" if facts or payload.get("next_cursor") else "empty"
        next_cursor = payload.get("next_cursor")
        coverage_value = payload.get("coverage")
        coverage_map: Mapping[str, Any] = (
            coverage_value if isinstance(coverage_value, Mapping) else {"status": "unknown"}
        )
        metrics_value = payload.get("metrics")
        metrics_map: Mapping[str, Any] = (
            metrics_value if isinstance(metrics_value, Mapping) else {}
        )
        return OperationalQueryResult(
            query=request.query.strip(),
            intent=detect_operational_intent(request.query),
            owner=result_owner,
            status=result_status,
            facts=tuple(facts),
            snapshot_id=snapshot_id,
            next_cursor=next_cursor if isinstance(next_cursor, str) else None,
            coverage=coverage_map,
            error=error_map,
            metrics=metrics_map,
        )

    # Keep a descriptive alias for SDK and adapter callers that use the
    # operation name instead of the service method name.
    query_content_diagnostics = content_diagnostics

    def _format_diagnostics(
        self,
        request: OperationalQueryRequest,
        intent: OperationalIntent,
        owner: str,
        *,
        path_fragment: str | None = None,
    ) -> OperationalQueryResult:
        from neocortex.api.content_diagnostics_api import content_diagnostics_payload

        payload = content_diagnostics_payload(
            owner,
            request.state_directory,
            request.source_root,
            request.limit,
            cursor=request.cursor,
            path_fragment=path_fragment,
        )
        status = str(payload.get("status", "error"))
        if status != "ok":
            error = payload.get("error")
            error_map = error if isinstance(error, Mapping) else {}
            return _error_result(
                request,
                intent,
                OperationalOwner(owner),
                "unavailable" if status == "unavailable" else "blocked",
                str(error_map.get("kind", "owner_state_unavailable")),
                str(error_map.get("message", "owner state unavailable")),
            )
        scope = AssetProblemScope.PROCESSING
        raw_items = payload.get("items", [])
        items = raw_items if isinstance(raw_items, list) else []
        facts = tuple(
            OperationalFact(
                scope=scope,
                code=str(item.get("error_type") or item.get("reason_code") or "diagnostic_record"),
                certainty=AssetDiagnosticCertainty.OBSERVED,
                owner=owner,
                record_id=_diagnostic_record_id(
                    owner, item, str(item.get("error_type") or item.get("reason_code"))
                ),
                snapshot_id=str(payload.get("snapshot_id")),
                provenance={
                    "operation": payload.get("operation"),
                    "requested_root": payload.get("requested_root"),
                    "reason_field": payload.get("reason_field"),
                    "record": _bounded_projection(item),
                    "projection_digest": _digest(item),
                },
            )
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get("error_type") or item.get("reason_code"), str)
            and bool((item.get("error_type") or item.get("reason_code")))
        )
        next_cursor = payload.get("next_cursor")
        next_cursor_value = next_cursor if isinstance(next_cursor, str) else None
        coverage_value = payload.get("coverage")
        coverage_map: Mapping[str, object] = (
            coverage_value if isinstance(coverage_value, Mapping) else {}
        )
        return OperationalQueryResult(
            request.query.strip(),
            intent,
            OperationalOwner(owner),
            "ok" if facts or next_cursor_value is not None else "empty",
            facts,
            str(payload.get("snapshot_id")),
            next_cursor_value,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": bool(coverage_map.get("snapshot_consistent")),
                "requested_root": payload.get("requested_root"),
                "path_fragment": path_fragment,
                "matched_count": payload.get("matched_count"),
                "query_page_complete": next_cursor is None,
            },
        )

    def _review_candidates(
        self,
        request: OperationalQueryRequest,
        intent: OperationalIntent,
        *,
        route_name: str | None = None,
        recommendation: Literal["retry", "keep_protected", "manual_review", "deletion_candidate"]
        | None = None,
    ) -> OperationalQueryResult:
        from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
        from neocortex.workflow.findings_query import list_findings_page

        database = KnowledgeStatePaths.from_directory(request.state_directory).framework
        page = list_findings_page(
            database,
            limit=request.limit,
            route_name=route_name,
            recommendation=recommendation,
            after=request.cursor,
        )
        owner = OperationalOwner.FRAMEWORK
        if page.availability != "ready":
            state: OperationalStatus = "unavailable" if page.availability == "absent" else "blocked"
            return _error_result(
                request,
                intent,
                owner,
                state,
                page.reason_code or "review_owner_unavailable",
                page.reason_code or "review owner unavailable",
            )
        page_items = page.to_dict().get("items", [])
        items = page_items if isinstance(page_items, list) else []
        facts = tuple(
            OperationalFact(
                scope=AssetProblemScope.POLICY
                if intent is OperationalIntent.CURATION_DISPOSAL
                else AssetProblemScope.PROCESSING,
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
            for item in items
            if isinstance(item, Mapping)
        )
        next_cursor = page.next_cursor.to_token() if page.next_cursor is not None else None
        return OperationalQueryResult(
            request.query.strip(),
            intent,
            owner,
            "ok" if facts or next_cursor is not None else "empty",
            facts,
            str(page.snapshot_id),
            next_cursor,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": True,
                "total_matching": page.total_matching,
                "query_page_complete": next_cursor is None,
                "recommendations_are_advisory": True,
            },
        )

    def _owner_result(
        self,
        request: OperationalQueryRequest,
        intent: OperationalIntent,
        owner: str,
    ) -> OperationalQueryResult:
        if owner == "pdf":
            return self._format_diagnostics(request, OperationalIntent.PDF_ERROR, owner)
        if owner == "text":
            if intent is OperationalIntent.OFFICE_ERROR:
                result = self._format_diagnostics(
                    request, OperationalIntent.PDF_ERROR, owner, path_fragment="ppt"
                )
            else:
                result = self._format_diagnostics(request, OperationalIntent.PDF_ERROR, owner)
            if intent is OperationalIntent.OFFICE_ERROR:
                result = self._presentation_text_result(result)
            return result
        if owner == "office":
            return self._review_candidates(
                request,
                OperationalIntent.OFFICE_ERROR,
                route_name="office",
                recommendation="manual_review",
            )
        raise ValueError(f"unsupported operational owner: {owner}")

    @staticmethod
    def _presentation_text_result(result: OperationalQueryResult) -> OperationalQueryResult:
        if result.status not in {"ok", "empty"}:
            return result
        selected: list[OperationalFact] = []
        for fact in result.facts:
            record = fact.provenance.get("record")
            path = record.get("path") if isinstance(record, Mapping) else None
            if isinstance(path, str) and path.casefold().endswith((".ppt", ".pptx")):
                selected.append(fact)
        status: OperationalStatus = "ok" if selected or result.next_cursor is not None else "empty"
        return replace(result, facts=tuple(selected), status=status)

    def _federated_query(
        self,
        request: OperationalQueryRequest,
        intent: OperationalIntent,
        owners: tuple[str, ...],
    ) -> OperationalQueryResult:
        """Read one page per owner and continue it with one cursor per owner."""

        binding = OperationalFederatedCursor(
            request.query.strip(),
            request.scope,
            str(request.state_directory),
            str(request.source_root),
            intent,
            request.limit,
            tuple((owner, None) for owner in owners),
            tuple((owner, None) for owner in owners),
        )
        if request.cursor is not None:
            try:
                cursor = OperationalFederatedCursor.from_token(request.cursor)
            except ValueError as exc:
                # Preserve the pre-federation diagnostic contract for a raw
                # owner cursor, while rejecting a token that looks federated
                # but fails its envelope/digest validation.
                try:
                    raw_cursor = base64.b64decode(
                        request.cursor + "=" * (-len(request.cursor) % 4),
                        altchars=b"-_",
                        validate=True,
                    )
                    json.loads(raw_cursor)
                    cursor_code = "invalid_federated_cursor"
                except (
                    binascii.Error,
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                ):
                    cursor_code = (
                        "invalid_federated_cursor"
                        if request.cursor.startswith("eyJkaWdlc3Qi")
                        else "federated_cursor_requires_owner_scope"
                    )
                return _error_result(
                    request,
                    intent,
                    OperationalOwner.FEDERATED,
                    "blocked",
                    cursor_code,
                    str(exc)
                    if cursor_code == "invalid_federated_cursor"
                    else "continue with the federated cursor returned by the first page",
                )
            if (
                cursor.query != binding.query
                or cursor.scope != binding.scope
                or cursor.state_directory != binding.state_directory
                or cursor.source_root != binding.source_root
                or cursor.intent is not intent
                or cursor.limit != binding.limit
                or {owner for owner, _ in cursor.owner_cursors} != set(owners)
                or {owner for owner, _ in cursor.owner_snapshots} != set(owners)
            ):
                return _error_result(
                    request,
                    intent,
                    OperationalOwner.FEDERATED,
                    "blocked",
                    "federated_cursor_binding_mismatch",
                    "federated cursor does not belong to this query, scope, roots, limit or intent",
                )
            binding = cursor

        prior_cursors = dict(binding.owner_cursors)
        prior_snapshots = dict(binding.owner_snapshots)
        facts: list[OperationalFact] = []
        owner_coverage: dict[str, Any] = {}
        current_snapshots: dict[str, str | None] = {}
        next_cursors: dict[str, str | None] = {}
        failed: list[str] = []
        changed: list[str] = []
        for owner in owners:
            owner_request = replace(request, cursor=prior_cursors[owner])
            result = self._owner_result(owner_request, intent, owner)
            current_snapshots[owner] = result.snapshot_id
            # Exhausted owners are still read once to validate their snapshot,
            # but their first page must never be reintroduced as a continuation
            # cursor when the owner reader naturally returns a fresh cursor.
            next_cursors[owner] = (
                None
                if request.cursor is not None and prior_cursors[owner] is None
                else result.next_cursor
            )
            owner_coverage[owner] = {
                "status": result.status,
                "snapshot_id": result.snapshot_id,
                "cursor_before": prior_cursors[owner],
                "next_cursor": result.next_cursor,
                "fact_count": len(result.facts),
            }
            if request.cursor is not None and result.snapshot_id != prior_snapshots[owner]:
                changed.append(owner)
            if result.status not in {"ok", "empty"}:
                failed.append(owner)
            # An owner that was exhausted on the preceding page is still read
            # for snapshot validation, but its first page must not be repeated.
            if request.cursor is None or prior_cursors[owner] is not None:
                facts.extend(result.facts)

        if changed:
            return OperationalQueryResult(
                request.query.strip(),
                intent,
                OperationalOwner.FEDERATED,
                "snapshot_changed",
                (),
                _digest(current_snapshots),
                None,
                {
                    "status": "snapshot_changed",
                    "persisted_only": True,
                    "owner_snapshot_consistent": False,
                    "changed_owners": changed,
                    "owners": owner_coverage,
                },
                {
                    "code": "snapshot_changed",
                    "message": "one or more owner snapshots changed before continuation",
                },
            )

        next_token = None
        if not failed and any(value is not None for value in next_cursors.values()):
            next_token = OperationalFederatedCursor(
                binding.query,
                binding.scope,
                binding.state_directory,
                binding.source_root,
                intent,
                binding.limit,
                tuple((owner, next_cursors[owner]) for owner in owners),
                tuple((owner, current_snapshots[owner]) for owner in owners),
            ).to_token()
        status: OperationalStatus = (
            "partial" if failed else ("ok" if facts or next_token is not None else "empty")
        )
        return OperationalQueryResult(
            request.query.strip(),
            intent,
            OperationalOwner.FEDERATED,
            status,
            tuple(facts),
            _digest(current_snapshots),
            next_token,
            {
                "status": "observed",
                "persisted_only": True,
                "owner_snapshot_consistent": not failed,
                "owners": owner_coverage,
                "incomplete_owners": failed,
                "query_page_complete": next_token is None,
                "recommendations_are_advisory": True,
            },
            {"code": "owner_pages_incomplete", "message": "one or more owner pages are unavailable"}
            if failed
            else None,
        )

    def _corpus_errors(
        self,
        request: OperationalQueryRequest,
        intent: OperationalIntent,
    ) -> OperationalQueryResult:
        """Compatibility wrapper retained for callers of the old seam."""

        return self._federated_query(request, intent, ("pdf", "text", "office"))


__all__ = [
    "OPERATIONAL_QUERY_SCHEMA",
    "SEMANTIC_ITEM_DIAGNOSTIC_SCHEMA",
    "KnowledgeOperationalQueryService",
    "OperationalFact",
    "OperationalFederatedCursor",
    "OperationalIntent",
    "OperationalOwner",
    "OperationalQueryRequest",
    "OperationalQueryResult",
    "detect_operational_intent",
    "semantic_item_diagnostic",
]
