"""Typed, fixed-root read API for published curation-plan pages.

The adapter deliberately accepts no filesystem or state path.  It resolves the
canonical application state, delegates all plan/cursor semantics to
``build_curation_plan_page`` and adds the authority/trust envelope shared by
SDK and MCP consumers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, Required, TYPE_CHECKING, TypedDict, cast
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text
from neocortex.runtime.config.app_paths import default_state_directory

if TYPE_CHECKING:
    from neocortex.curation.preview import CurationPlanPage


CURATION_PLAN_API_SCHEMA: Literal["neocortex.curation-plan/v1"] = (
    "neocortex.curation-plan/v1"
)
CURATION_SNAPSHOT_API_SCHEMA: Literal["neocortex.curation-snapshot/v1"] = (
    "neocortex.curation-snapshot/v1"
)
MAX_CURATION_PLAN_PAGE = 100
MAX_CURATION_CURSOR_BYTES = 2_048
MAX_CURATION_PATH_CHARS = 4_096
# A page may contain up to 100 items.  Keep evidence bounded independently at
# a smaller per-item budget, so a single corpus-derived object cannot consume a
# generic envelope budget or make the complete MCP response unbounded.
MAX_CURATION_EVIDENCE_NODES = 4_096

CurationCoverage = Literal["complete", "partial", "unavailable"]
CurationErrorCode = Literal[
    "invalid_request",
    "invalid_cursor",
    "snapshot_changed",
    "schema_incompatible",
    "corrupt",
    "unavailable",
    "partial",
]


class CurationEffectsPayload(TypedDict):
    """Effects this public operation is capable of producing."""

    state: Required[Literal["none"]]
    corpus: Required[Literal["none"]]
    external: Required[Literal["none"]]


class CurationTrustPayload(TypedDict):
    """Machine-readable authority boundary for corpus-derived fields."""

    content_class: Required[Literal["untrusted_corpus_evidence"]]
    instruction_authority: Required[Literal[False]]
    tools_authorized: Required[Literal[False]]
    actions_authorized: Required[Literal[False]]


class CurationSnapshotPayload(TypedDict):
    """Published owner snapshot to which the page and cursor are bound."""

    schema: Required[Literal["neocortex.curation-snapshot/v1"]]
    snapshot_id: Required[str | None]
    coverage: Required[CurationCoverage]
    root: Required[str | None]
    scan_id: Required[int | None]
    missing_owners: Required[list[str]]


class CurationPlanItemPayload(TypedDict):
    """One advisory curation item; corpus fields remain untrusted evidence."""

    item_id: Required[str]
    kind: Required[str]
    status: Required[str]
    action: Required[str]
    source_path: Required[str]
    destination_path: Required[str | None]
    reason: Required[str]
    evidence: Required[dict[str, Any]]


class CurationPlanPagePayload(TypedDict):
    """One bounded keyset page from the immutable plan stream."""

    limit: Required[int]
    cursor: Required[str | None]
    next_cursor: Required[str | None]
    complete: Required[bool]
    plan_digest: Required[str | None]
    items_total: Required[int]
    items: Required[list[CurationPlanItemPayload]]
    inventory_files: NotRequired[int]
    duplicate_groups: NotRequired[int]
    duplicate_members: NotRequired[int]
    reclaimable_bytes: NotRequired[int]
    organization_plans: NotRequired[int]
    empty_files: NotRequired[int]


class CurationErrorPayload(TypedDict):
    """Typed abstention for unavailable or incompatible published state."""

    code: Required[CurationErrorCode]
    message: Required[str]
    retryable: Required[bool]


class CurationPlanOutput(TypedDict):
    """Stable direct response shared by Python and MCP clients."""

    schema: Required[Literal["neocortex.curation-plan/v1"]]
    kind: Required[Literal["neocortex_curation_plan"]]
    operation: Required[Literal["curation-plan"]]
    request_id: Required[str]
    read_only: Required[Literal[True]]
    effects: Required[CurationEffectsPayload]
    trust: Required[CurationTrustPayload]
    coverage: Required[CurationCoverage]
    snapshot: Required[CurationSnapshotPayload]
    page: Required[CurationPlanPagePayload]
    error: Required[CurationErrorPayload | None]


_EFFECTS: CurationEffectsPayload = {
    "state": "none",
    "corpus": "none",
    "external": "none",
}
_TRUST: CurationTrustPayload = {
    "content_class": "untrusted_corpus_evidence",
    "instruction_authority": False,
    "tools_authorized": False,
    "actions_authorized": False,
}
_VALID_COVERAGE = frozenset({"complete", "partial", "unavailable"})


def _request_id(value: str | None) -> str:
    if value is None:
        return f"curation-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("request_id is invalid")
    return value


def _limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CURATION_PLAN_PAGE
    ):
        raise ValueError(
            f"curation plan limit must be between 1 and {MAX_CURATION_PLAN_PAGE}"
        )
    return value


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_CURATION_CURSOR_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("curation plan cursor is invalid")
    return value


def _safe_required_text(value: object, *, label: str, limit: int = 128_000) -> str:
    """Validate and sanitize one producer-owned string without coercion."""

    if not isinstance(value, str):
        raise TypeError(f"curation plan {label} must be text")
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _safe_optional_text(
    value: object,
    *,
    label: str,
    limit: int = 128_000,
) -> str | None:
    if value is None:
        return None
    return _safe_required_text(value, label=label, limit=limit)


def _safe_nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"curation plan {label} must be a non-negative integer")
    return value


def _safe_optional_nonnegative_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _safe_nonnegative_int(value, label=label)


def _safe_text_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"curation plan {label} must be a list")
    return [
        _safe_required_text(item, label=f"{label} item", limit=4_096)
        for item in value
    ]


def _safe_evidence(value: object) -> dict[str, Any]:
    """Sanitize evidence with a fresh budget so it cannot consume page budget.

    ``sanitize_untrusted_payload`` intentionally has a shared node budget for a
    generic envelope.  Curation pages have a stricter structural contract: a
    large evidence object may be bounded internally, but it must never turn
    into a truncation sentinel in ``page.items`` or omit sibling items.
    """

    sanitized = sanitize_untrusted_payload(
        value,
        budget=[MAX_CURATION_EVIDENCE_NODES],
    )
    if not isinstance(sanitized, dict) or any(
        not isinstance(key, str) for key in sanitized
    ):
        raise TypeError("curation plan item evidence sanitization is invalid")
    return cast(dict[str, Any], sanitized)


def _sanitized_item_payload(value: object) -> CurationPlanItemPayload:
    payload = _item_payload(value)
    item_id = _safe_required_text(payload["item_id"], label="item_id", limit=4_096)
    if not item_id:
        raise ValueError("curation plan item_id must be non-empty")
    return {
        "item_id": item_id,
        "kind": _safe_required_text(payload["kind"], label="kind", limit=256),
        "status": _safe_required_text(payload["status"], label="status", limit=256),
        "action": _safe_required_text(payload["action"], label="action", limit=256),
        "source_path": _safe_required_text(
            payload["source_path"],
            label="source_path",
            limit=MAX_CURATION_PATH_CHARS,
        ),
        "destination_path": _safe_optional_text(
            payload["destination_path"],
            label="destination_path",
            limit=MAX_CURATION_PATH_CHARS,
        ),
        "reason": _safe_required_text(payload["reason"], label="reason", limit=4_096),
        "evidence": _safe_evidence(payload["evidence"]),
    }


def _exception_text(exc: BaseException) -> str:
    try:
        return sanitize_untrusted_text(exc, limit=800)
    except Exception:
        return type(exc).__name__


def _exception_type_names(exc: BaseException) -> frozenset[str]:
    names: set[str] = set()
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        names.add(type(current).__name__.casefold())
        current = current.__cause__ or current.__context__
    return frozenset(names)


def _looks_like_snapshot_change(message: str) -> bool:
    changed = (
        "changed",
        "change",
        "mismatch",
        "not current",
        "disappeared",
        "unstable",
    )
    return (
        ("snapshot" in message and any(token in message for token in changed))
        or "state changed" in message
        or "item count changed" in message
    )


def _looks_like_invalid_cursor(message: str) -> bool:
    return "cursor" in message and any(
        token in message
        for token in ("invalid", "canonical", "not present", "missing", "key")
    )


def _looks_like_schema_issue(message: str, type_names: frozenset[str]) -> bool:
    return (
        any("schema" in name or "contract" in name for name in type_names)
        or any(
            marker in message
            for marker in (
                "schema",
                "contract",
                "required column",
                "no such table",
                "no such column",
                "unsupported version",
                "incompatible",
            )
        )
    )


def _looks_like_corrupt_sqlite(message: str) -> bool:
    return any(
        marker in message
        for marker in (
            "corrupt",
            "corruption",
            "malformed",
            "not a database",
            "database disk image",
            "file is encrypted",
            "integrity check",
        )
    )


def _error_code(exc: BaseException) -> tuple[CurationErrorCode, bool]:
    """Map known read failures to stable wire codes, fail-closed."""

    message = _exception_text(exc).casefold()
    if _looks_like_snapshot_change(message):
        return "snapshot_changed", True
    if _looks_like_invalid_cursor(message):
        return "invalid_cursor", False
    if isinstance(exc, (ImportError, OSError)):
        return "unavailable", False

    type_names = _exception_type_names(exc)
    if _looks_like_schema_issue(message, type_names):
        return "schema_incompatible", False
    if isinstance(exc, sqlite3.Error):
        if _looks_like_corrupt_sqlite(message):
            return "corrupt", False
        # Busy, locked, I/O and unavailable SQLite owners are not evidence of
        # corruption, so preserve the conservative unavailable outcome.
        return "unavailable", False
    if isinstance(exc, (TypeError, ValueError)):
        # These are producer/contract failures at this boundary, not data that
        # callers may safely interpret as a valid page.
        if message.startswith("curation plan limit") or "request_id" in message:
            return "invalid_request", False
        return "schema_incompatible", False
    return "unavailable", False


def _plan_contract() -> tuple[type[BaseException], Any]:
    """Load the curation producer only when this operation is requested."""

    from neocortex.curation.preview import CurationStateError, build_curation_plan_page

    return CurationStateError, build_curation_plan_page


def _coverage(value: object) -> CurationCoverage:
    if not isinstance(value, str) or value not in _VALID_COVERAGE:
        raise ValueError("curation plan returned invalid coverage")
    return cast(CurationCoverage, value)


def _item_payload(value: object) -> CurationPlanItemPayload:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("curation plan item lacks its public mapping")
    payload = to_dict()
    if not isinstance(payload, Mapping):
        raise TypeError("curation plan item mapping is invalid")
    required = {
        "item_id",
        "kind",
        "status",
        "action",
        "source_path",
        "destination_path",
        "reason",
        "evidence",
    }
    if set(payload) != required:
        raise TypeError("curation plan item mapping has an incompatible shape")
    for key in required - {"destination_path", "evidence"}:
        if not isinstance(payload[key], str):
            raise TypeError(f"curation plan item {key} must be text")
    destination = payload["destination_path"]
    if destination is not None and not isinstance(destination, str):
        raise TypeError("curation plan item destination_path must be text or null")
    evidence = payload["evidence"]
    if not isinstance(evidence, Mapping) or any(not isinstance(key, str) for key in evidence):
        raise TypeError("curation plan item evidence must be an object with text keys")
    return {
        "item_id": cast(str, payload["item_id"]),
        "kind": cast(str, payload["kind"]),
        "status": cast(str, payload["status"]),
        "action": cast(str, payload["action"]),
        "source_path": cast(str, payload["source_path"]),
        "destination_path": destination,
        "reason": cast(str, payload["reason"]),
        "evidence": dict(evidence),
    }


def _success_payload(
    page: CurationPlanPage,
    *,
    request_id: str,
) -> CurationPlanOutput:
    coverage = _coverage(page.coverage)
    page_limit = _safe_nonnegative_int(page.limit, label="limit")
    if not 1 <= page_limit <= MAX_CURATION_PLAN_PAGE:
        raise ValueError("curation plan page limit is outside its public bound")
    if not isinstance(page.items, (list, tuple)):
        raise TypeError("curation plan page items must be a list")
    raw_items = tuple(page.items)
    if len(raw_items) > page_limit:
        raise ValueError("curation plan page items exceed its limit")
    items = [_sanitized_item_payload(item) for item in raw_items]
    if len(items) != len(raw_items):
        raise ValueError("curation plan page item cardinality changed")
    item_ids = [item["item_id"] for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("curation plan page contains duplicate item identifiers")
    page_cursor = _safe_optional_text(page.cursor, label="cursor", limit=MAX_CURATION_CURSOR_BYTES)
    next_cursor = _safe_optional_text(
        page.next_cursor,
        label="next_cursor",
        limit=MAX_CURATION_CURSOR_BYTES,
    )
    snapshot_id = _safe_required_text(
        page.snapshot_id,
        label="snapshot_id",
        limit=4_096,
    )
    plan_digest = _safe_required_text(page.plan_digest, label="plan_digest", limit=4_096)
    root = _safe_optional_text(
        page.root,
        label="root",
        limit=MAX_CURATION_PATH_CHARS,
    )
    missing_owners = _safe_text_list(page.missing_owners, label="missing_owners")
    items_total = _safe_nonnegative_int(page.items_total, label="items_total")
    if items_total < len(items):
        raise ValueError("curation plan total is smaller than its page")
    if coverage == "complete":
        if page_cursor is None and next_cursor is None and items_total != len(items):
            raise ValueError(
                "complete curation page without next_cursor is missing items"
            )
        if page_cursor is None and next_cursor is not None and items_total <= len(items):
            raise ValueError(
                "complete curation page has next_cursor without remaining items"
            )
    result: CurationPlanOutput = {
        "schema": CURATION_PLAN_API_SCHEMA,
        "kind": "neocortex_curation_plan",
        "operation": "curation-plan",
        "request_id": request_id,
        "read_only": True,
        "effects": _EFFECTS.copy(),
        "trust": _TRUST.copy(),
        "coverage": coverage,
        "snapshot": {
            "schema": CURATION_SNAPSHOT_API_SCHEMA,
            "snapshot_id": snapshot_id,
            "coverage": coverage,
            "root": root,
            "scan_id": _safe_optional_nonnegative_int(page.scan_id, label="scan_id"),
            "missing_owners": missing_owners,
        },
        "page": {
            "limit": page_limit,
            "cursor": page_cursor,
            "next_cursor": next_cursor,
            "complete": next_cursor is None and coverage == "complete",
            "plan_digest": plan_digest,
            "items_total": items_total,
            "items": items,
            "inventory_files": _safe_nonnegative_int(
                page.inventory_files,
                label="inventory_files",
            ),
            "duplicate_groups": _safe_nonnegative_int(
                page.duplicate_groups,
                label="duplicate_groups",
            ),
            "duplicate_members": _safe_nonnegative_int(
                page.duplicate_members,
                label="duplicate_members",
            ),
            "reclaimable_bytes": _safe_nonnegative_int(
                page.reclaimable_bytes,
                label="reclaimable_bytes",
            ),
            "organization_plans": _safe_nonnegative_int(
                page.organization_plans,
                label="organization_plans",
            ),
            "empty_files": _safe_nonnegative_int(page.empty_files, label="empty_files"),
        },
        "error": (
            None
            if coverage == "complete"
            else {
                "code": "partial" if coverage == "partial" else "unavailable",
                "message": (
                    "published curation state has partial coverage"
                    if coverage == "partial"
                    else "published curation state is unavailable"
                ),
                "retryable": coverage == "partial",
            }
        ),
    }
    # Do not run the generic envelope sanitizer here.  It has one shared node
    # budget, so a large evidence object can append a truncation sentinel to
    # ``page.items`` and violate the strict MCP output shape.  Every evidence
    # object was sanitized independently above, while envelope fields are
    # validated or sanitized at their fixed structural boundary.
    return result


def _error_payload(
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    message: object,
    code: CurationErrorCode = "unavailable",
    retryable: bool = False,
) -> CurationPlanOutput:
    bounded_message = sanitize_untrusted_text(message, limit=800)
    result: CurationPlanOutput = {
        "schema": CURATION_PLAN_API_SCHEMA,
        "kind": "neocortex_curation_plan",
        "operation": "curation-plan",
        "request_id": request_id,
        "read_only": True,
        "effects": _EFFECTS.copy(),
        "trust": _TRUST.copy(),
        "coverage": "unavailable",
        "snapshot": {
            "schema": CURATION_SNAPSHOT_API_SCHEMA,
            "snapshot_id": None,
            "coverage": "unavailable",
            "root": None,
            "scan_id": None,
            "missing_owners": [],
        },
        "page": {
            "limit": limit,
            "cursor": cursor,
            "next_cursor": None,
            "complete": False,
            "plan_digest": None,
            "items_total": 0,
            "items": [],
        },
        "error": {
            "code": code,
            "message": bounded_message or _default_error_message(code),
            "retryable": retryable,
        },
    }
    return result


def _default_error_message(code: CurationErrorCode) -> str:
    return {
        "invalid_request": "curation request is invalid",
        "invalid_cursor": "curation cursor is invalid",
        "snapshot_changed": "published curation snapshot changed",
        "schema_incompatible": "published curation state schema is incompatible",
        "corrupt": "published curation state is corrupt",
        "unavailable": "published curation state is unavailable",
        "partial": "published curation state has partial coverage",
    }[code]


def curation_plan_payload(
    *,
    limit: int = 50,
    cursor: str | None = None,
    request_id: str | None = None,
) -> CurationPlanOutput:
    """Return one bounded page from canonical published state without mutation."""

    try:
        bounded_limit = _limit(limit)
        normalized_cursor = _cursor(cursor)
        normalized_request_id = _request_id(request_id)
    except (TypeError, ValueError) as exc:
        # Keep the direct Python/MCP contract typed even when a caller bypasses
        # the MCP argument model.  Valid sibling inputs remain useful in the
        # response, while malformed values are never echoed into the envelope.
        try:
            fallback_limit = _limit(limit)
        except (TypeError, ValueError):
            fallback_limit = 50
        try:
            fallback_cursor = _cursor(cursor)
        except (TypeError, ValueError):
            fallback_cursor = None
        try:
            fallback_request_id = _request_id(request_id)
        except (TypeError, ValueError):
            fallback_request_id = _request_id(None)
        code, retryable = _error_code(exc)
        return _error_payload(
            request_id=fallback_request_id,
            limit=fallback_limit,
            cursor=fallback_cursor,
            message=exc,
            code=code,
            retryable=retryable,
        )

    try:
        # Keep the lazy producer import inside the failure boundary.  A
        # partially installed/minimal runtime must return a typed unavailable
        # response rather than leaking ImportError through MCP.
        _state_error, builder = _plan_contract()
        page = builder(
            default_state_directory(),
            limit=bounded_limit,
            cursor=normalized_cursor,
        )
        return _success_payload(page, request_id=normalized_request_id)
    except Exception as exc:
        # The producer contract is intentionally lazy and its state exception
        # type is returned dynamically, so classify the complete read boundary
        # rather than allowing a state/SQLite/contract failure to escape.
        code, retryable = _error_code(exc)
        return _error_payload(
            request_id=normalized_request_id,
            limit=bounded_limit,
            cursor=normalized_cursor,
            message=exc,
            code=code,
            retryable=retryable,
        )


__all__ = (
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_SNAPSHOT_API_SCHEMA",
    "MAX_CURATION_CURSOR_BYTES",
    "MAX_CURATION_EVIDENCE_NODES",
    "MAX_CURATION_PATH_CHARS",
    "MAX_CURATION_PLAN_PAGE",
    "CurationCoverage",
    "CurationEffectsPayload",
    "CurationErrorCode",
    "CurationErrorPayload",
    "CurationPlanItemPayload",
    "CurationPlanOutput",
    "CurationPlanPagePayload",
    "CurationSnapshotPayload",
    "CurationTrustPayload",
    "curation_plan_payload",
)
