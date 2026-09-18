"""Root-scoped, read-only envelopes over the existing format diagnostics.

The selected root is always supplied by the caller, never discovered from the
latest run. Counts describe persisted owner evidence, not a fresh corpus scan.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from neocortex.knowledge.knowledge_read_budget import (
    KnowledgeReadBudget,
    KnowledgeReadBudgetExceeded,
)

CONTENT_DIAGNOSTICS_SCHEMA = "neocortex.content-diagnostics/v1"
CONTENT_DIAGNOSTIC_OWNERS = ("pdf", "text", "archive")
CONTENT_DIAGNOSTICS_V2_SCHEMA = "neocortex.content-diagnostics/v2"
CONTENT_DIAGNOSTIC_V2_OWNERS = (
    "pdf",
    "docx",
    "office",
    "archive",
    "text",
    "audio",
    "video",
    "image",
    "code",
)
_OPERATIONS = {"pdf": "pdf-diagnostics", "text": "text-errors", "archive": "archive-issues"}
_REASON_FIELDS = {"pdf": "error_type", "text": "error_type", "archive": "reason_code"}


def _optional_string(value: str | None, label: str, maximum: int) -> None:
    if value is not None and (
        not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value
    ):
        raise ValueError(f"{label} must be non-empty, NUL-free and at most {maximum} characters")


def validate_content_diagnostics_request(
    owner: str,
    source_root: Path | str,
    limit: int,
    *,
    cursor: str | None = None,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
) -> str:
    """Validate before any owner access and return the explicit lexical root."""

    if owner not in CONTENT_DIAGNOSTIC_OWNERS:
        raise ValueError("owner must be pdf, text or archive")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    if not isinstance(source_root, (str, Path)):
        raise ValueError("source_root is required")
    root = os.fspath(source_root)
    _optional_string(root, "source_root", 4_096)
    if not Path(root).is_absolute():
        raise ValueError("source_root must be an absolute Linux path")
    if any(part in {".", ".."} for part in root.split("/")):
        raise ValueError("source_root must not contain traversal components")
    _optional_string(cursor, "cursor", 8_192)
    _optional_string(file_key, "file_key", 2_048)
    _optional_string(path_fragment, "path_fragment", 2_048)
    _optional_string(reason, "reason", 256)
    return os.path.normpath(root)


def _failed(payload: dict[str, object], kind: str, message: str, *, status: str = "error") -> dict[str, object]:
    payload.update(
        status=status,
        error={"kind": kind, "message": message[:1_000]},
        items=[], count=0, matched_count=None, truncated=None, next_cursor=None,
        coverage={"status": "unknown", "persisted_only": True, "snapshot_consistent": False},
    )
    return payload


def _base_payload(
    owner: str,
    source_root: Path | str | None,
    limit: int = 20,
    *,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "schema": CONTENT_DIAGNOSTICS_SCHEMA,
        "owner": owner,
        "operation": _OPERATIONS.get(owner) if isinstance(owner, str) else None,
        "status": "error",
        "read_only": True,
        "requested_root": None if source_root is None else str(source_root),
        "owner_path": None,
        "filters": {"file_key": file_key, "path_fragment": path_fragment, "reason": reason},
        "reason_field": _REASON_FIELDS.get(owner) if isinstance(owner, str) else None,
        "limit": limit,
        "snapshot_id": None,
        "error": None,
    }


def content_diagnostics_error_payload(
    owner: str,
    source_root: Path | str | None = None,
    *,
    kind: str,
    message: str,
    status: str = "error",
    limit: int = 20,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    """Share the failure envelope with adapters whose configuration is unavailable."""

    if status not in {"error", "blocked", "unavailable"}:
        raise ValueError("diagnostic failure status must be error, blocked or unavailable")
    return _failed(
        _base_payload(owner, source_root, limit, file_key=file_key, path_fragment=path_fragment, reason=reason),
        kind, message, status=status,
    )


def content_diagnostics_payload(
    owner: str,
    state_directory: Path | str,
    source_root: Path | str,
    limit: int = 20,
    *,
    cursor: str | None = None,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
    response_version: int = 1,
    budget: KnowledgeReadBudget | None = None,
    filters: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Read one bounded page without creating, migrating or repairing owners.

    ``reason`` is an exact extractor error_type for PDF/Text and reason_code
    for Archive. Root coverage is explicitly separate from filtered matches.
    Missing owners never imply zero errors or complete extraction.
    """

    if response_version == 2:
        return content_diagnostics_v2_payload(
            owner,
            state_directory,
            source_root,
            limit,
            cursor=cursor,
            file_key=file_key,
            path_fragment=path_fragment,
            reason=reason,
            filters=filters,
            budget=budget,
        )
    if isinstance(response_version, bool) or response_version != 1:
        return content_diagnostics_error_payload(
            owner,
            source_root,
            kind="invalid_request",
            message="response_version must be 1 or 2",
            limit=limit,
            file_key=file_key,
            path_fragment=path_fragment,
            reason=reason,
        )
    if budget is not None:
        return content_diagnostics_error_payload(
            owner,
            source_root,
            kind="invalid_request",
            message="budget requires response_version=2",
            limit=limit,
            file_key=file_key,
            path_fragment=path_fragment,
            reason=reason,
        )
    payload = _base_payload(
        owner, source_root, limit, file_key=file_key, path_fragment=path_fragment, reason=reason,
    )
    try:
        root = validate_content_diagnostics_request(
            owner, source_root, limit, cursor=cursor, file_key=file_key,
            path_fragment=path_fragment, reason=reason,
        )
        if not isinstance(state_directory, (str, Path)) or not os.fspath(state_directory):
            raise ValueError("state_directory is required")
        path = Path(state_directory).expanduser().absolute() / f"{owner}.sqlite3"
        payload.update(requested_root=root, owner_path=str(path))
    except (TypeError, ValueError, OSError) as exc:
        return _failed(payload, "invalid_request", str(exc))

    from neocortex.capabilities.formats.diagnostic_queries import state_snapshot_id, verify_snapshot
    from neocortex.persistence.sqlite_immutable import (
        ImmutableSQLiteUnavailable,
        SQLiteSnapshotBudgetExceeded,
    )

    try:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return _failed(payload, "owner_missing", "format owner is absent", status="unavailable")
        if not stat.S_ISREG(metadata.st_mode):
            return _failed(payload, "owner_unsafe", "format owner is not a regular file", status="blocked")
        before = state_snapshot_id(path)
        source_coverage: dict[str, object] | None = None
        if owner == "pdf":
            from neocortex.capabilities.formats.pdf.pdf_diagnostics import list_pdf_diagnostics, read_pdf_coverage

            page = list_pdf_diagnostics(
                path, limit, path_scope=root, file_key=file_key,
                path_fragment=path_fragment, error_type=reason, cursor=cursor,
            )
            items = list(page.items)
            matched: int | None = page.matched_count
            next_cursor = page.next_cursor
            snapshot = page.snapshot_id
            source_coverage = read_pdf_coverage(path, path_scope=root)
        elif owner == "text":
            from neocortex.capabilities.formats.text.text_diagnostics import list_text_errors, read_text_coverage

            page = list_text_errors(
                path, limit, path_scope=root, file_key=file_key,
                path_fragment=path_fragment, error_type=reason, cursor=cursor,
            )
            items = list(page.items)
            matched = page.matched_count
            next_cursor = page.next_cursor
            snapshot = page.snapshot_id
            source_coverage = read_text_coverage(path, path_scope=root)
        else:
            from neocortex.capabilities.formats.archive.state import list_archive_issues

            archive_page = list_archive_issues(
                path, limit, path_scope=root, container_key=file_key,
                container_fragment=path_fragment, reason_code=reason, cursor=cursor,
            )
            items = [item.to_dict() for item in archive_page.items]
            matched = None  # The Archive producer does not expose a population count.
            next_cursor = archive_page.next_cursor
            revision = archive_page.scope.get("owner_revision")
            if not isinstance(revision, str):
                raise TypeError("Archive diagnostic producer omitted its owner revision")
            snapshot = revision
        verify_snapshot(path, before)
        if source_coverage is not None and (
            source_coverage.get("available") is not True
            or snapshot != before
            or source_coverage.get("snapshot_id") != snapshot
        ):
            raise ValueError("format state changed between diagnostics and root coverage")
        payload.update(
            status="ok", items=items, count=len(items), matched_count=matched,
            truncated=next_cursor is not None, next_cursor=next_cursor,
            snapshot_id=snapshot,
            coverage={
                "status": "observed",
                "persisted_only": True,
                "snapshot_consistent": True,
                "query_page_complete": next_cursor is None,
                "scope": "exact_requested_root_and_filters",
                "source_root": root,
                "root_summary_scope": "requested_root_without_query_filters",
                "root_summary": source_coverage,
            },
        )
        return payload
    except SQLiteSnapshotBudgetExceeded as exc:
        return _failed(payload, "cancelled" if exc.reason == "cancelled" else "budget_exhausted", str(exc), status="blocked")
    except ImmutableSQLiteUnavailable as exc:
        return _failed(payload, "owner_unsafe", str(exc), status="blocked")
    except ValueError as exc:
        message = str(exc)
        lowered = message.lower()
        if "cursor" in lowered:
            return _failed(payload, "invalid_cursor", message)
        if "changed" in lowered:
            return _failed(payload, "state_changed", message)
        # The PDF reader reports an incompatible schema as ValueError, unlike
        # Text and Archive. That is persisted-owner state, not bad caller input.
        if owner == "pdf" and "pdf diagnostics require schema " in lowered:
            return _failed(payload, "owner_state_unavailable", message, status="blocked")
        return _failed(payload, "invalid_request", message)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return _failed(payload, "owner_state_unavailable", str(exc), status="blocked")
    except (TypeError, AttributeError) as exc:
        return _failed(payload, "adapter_contract_error", str(exc))


# region [03] Additive content-diagnostics/v2 federation


class ContentDiagnosticOwnerState(StrEnum):
    """Typed state of one persisted content owner in a v2 read."""

    READY = "ready"
    MISSING = "missing"
    PARTIAL = "partial"
    FUTURE = "future"
    CORRUPT = "corrupt"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ContentDiagnosticsCursor:
    """Signed continuation bound to every input that can change a page."""

    state_directory: str
    source_root: str
    owners: tuple[str, ...]
    filters: tuple[tuple[str, str | None], ...]
    limit: int
    snapshots: tuple[tuple[str, str | None], ...]
    after: tuple[tuple[str, str | None], ...]
    version: int = 2

    def __post_init__(self) -> None:
        if self.version != 2:
            raise ValueError("content diagnostics cursor version is unsupported")
        if not isinstance(self.state_directory, str) or not self.state_directory:
            raise ValueError("content diagnostics cursor state directory is invalid")
        if not isinstance(self.source_root, str) or not self.source_root:
            raise ValueError("content diagnostics cursor source root is invalid")
        if not self.owners or tuple(self.owners) != tuple(
            owner for owner in CONTENT_DIAGNOSTIC_V2_OWNERS if owner in self.owners
        ):
            raise ValueError("content diagnostics cursor owners are not canonical")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 1_000:
            raise ValueError("content diagnostics cursor limit is invalid")
        owner_set = set(self.owners)
        for name, values in (("snapshots", self.snapshots), ("after", self.after)):
            if tuple(owner for owner, _ in values) != self.owners:
                raise ValueError(f"content diagnostics cursor {name} are not canonical")
            if any(owner not in owner_set for owner, _ in values):
                raise ValueError(f"content diagnostics cursor {name} contain unknown owners")
            if any(value is not None and (not isinstance(value, str) or not value) for _, value in values):
                raise ValueError(f"content diagnostics cursor {name} contain invalid values")

    def _payload(self) -> dict[str, object]:
        return {
            "v": self.version,
            "state_directory": self.state_directory,
            "source_root": self.source_root,
            "owners": list(self.owners),
            "filters": dict(self.filters),
            "limit": self.limit,
            "snapshots": dict(self.snapshots),
            "after": dict(self.after),
        }

    def to_token(self) -> str:
        payload = self._payload()
        envelope = {"payload": payload, "digest": _v2_digest(payload)}
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def from_token(cls, token: str) -> "ContentDiagnosticsCursor":
        if not isinstance(token, str) or not token or len(token) > 8_192:
            raise ValueError("invalid content diagnostics cursor")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "digest"}:
                raise ValueError("cursor envelope is invalid")
            payload = envelope["payload"]
            if not isinstance(payload, dict) or envelope["digest"] != _v2_digest(payload):
                raise ValueError("cursor digest mismatch")
            if payload.get("v") != 2:
                raise ValueError("cursor version is unsupported")
            owners = payload.get("owners")
            filters = payload.get("filters")
            snapshots = payload.get("snapshots")
            after = payload.get("after")
            if not isinstance(owners, list) or any(not isinstance(item, str) for item in owners):
                raise ValueError("cursor owners are invalid")
            if not isinstance(filters, dict) or not isinstance(snapshots, dict) or not isinstance(after, dict):
                raise ValueError("cursor bindings are invalid")
            result = cls(
                state_directory=payload["state_directory"],
                source_root=payload["source_root"],
                owners=tuple(owners),
                filters=tuple(sorted(filters.items())),
                limit=payload["limit"],
                snapshots=tuple((owner, snapshots[owner]) for owner in owners),
                after=tuple((owner, after[owner]) for owner in owners),
            )
            if result.to_token() != token:
                raise ValueError("cursor is not canonical")
            return result
        except (binascii.Error, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid content diagnostics cursor") from exc


@dataclass(slots=True)
class _V2OwnerRead:
    owner: str
    state: ContentDiagnosticOwnerState
    status: str
    snapshot_id: str | None
    items: list[dict[str, object]]
    matched_count: int | None
    next_after: str | None
    error: dict[str, str] | None
    rows_examined: int = 0
    rows_returned: int = 0
    temporary_bytes: int = 0
    phases_ns: dict[str, int] | None = None


class _V2OwnerStateError(RuntimeError):
    def __init__(self, state: ContentDiagnosticOwnerState, code: str, message: str) -> None:
        self.state = state
        self.code = code
        super().__init__(message)


_V2_EXPECTED_SCHEMAS = {
    "pdf": 13,
    "docx": 6,
    "office": 3,
    "archive": 2,
    "text": 2,
    "audio": 2,
    "video": 2,
    "image": 6,
    "code": 8,
}

_V2_REQUIRED_TABLES = {
    "pdf": ("documents", "page_errors"),
    "docx": ("documents", "document_diagnostics"),
    "office": ("documents",),
    "archive": ("containers", "archive_issues"),
    "text": ("documents",),
    "audio": ("documents",),
    "video": ("documents", "frames"),
    "image": ("images",),
    "code": ("files", "file_versions", "diagnostics"),
}

_V2_REQUIRED_COLUMNS = {
    "pdf": {"documents": ("file_key", "path", "status"), "page_errors": ("file_key", "page_number", "error_type")},
    "docx": {"documents": ("file_key", "path", "status"), "document_diagnostics": ("file_key", "ordinal", "code")},
    "office": {"documents": ("file_key", "path", "status")},
    "archive": {"containers": ("container_key", "path", "status"), "archive_issues": ("issue_id", "container_key", "reason_code")},
    "text": {"documents": ("file_key", "path", "status")},
    "audio": {"documents": ("file_key", "path", "status")},
    "video": {"documents": ("file_key", "path", "status"), "frames": ("file_key", "frame_index", "timestamp_ms")},
    "image": {"images": ("file_key", "path", "status")},
    "code": {"files": ("file_id", "current_path", "status"), "file_versions": ("version_id", "file_id", "analysis_status"), "diagnostics": ("diagnostic_id", "version_id", "code")},
}


def _v2_digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _v2_text(value: object, *, maximum: int = 2_048) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:maximum]


def _v2_owner_tuple(owner: str | Sequence[str] | None) -> tuple[str, ...]:
    if owner is None or owner == "all":
        return CONTENT_DIAGNOSTIC_V2_OWNERS
    if isinstance(owner, str):
        selected: tuple[str, ...] = (owner,)
    elif isinstance(owner, (list, tuple)):
        selected = tuple(owner)
    else:
        raise ValueError("owner must be a v2 owner, all, or a sequence of owners")
    if not selected or any(item not in CONTENT_DIAGNOSTIC_V2_OWNERS for item in selected):
        raise ValueError("owner contains an unsupported v2 diagnostic owner")
    if len(set(selected)) != len(selected):
        raise ValueError("owner cannot contain duplicates")
    return tuple(item for item in CONTENT_DIAGNOSTIC_V2_OWNERS if item in selected)


def _v2_root(value: Path | str, *, label: str) -> str:
    if not isinstance(value, (Path, str)):
        raise ValueError(f"{label} is required")
    selected = os.fspath(value)
    if not selected or len(selected) > 4_096 or "\x00" in selected:
        raise ValueError(f"{label} must be non-empty, NUL-free and bounded")
    if not Path(selected).is_absolute() or any(part in {".", ".."} for part in selected.split("/")):
        raise ValueError(f"{label} must be an absolute Linux path without traversal")
    return os.path.normpath(selected)


def _v2_filters(
    *,
    file_key: str | None,
    path_fragment: str | None,
    reason: str | None,
    status: str | None,
    filters: Mapping[str, object] | None,
) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "file_key": file_key,
        "path_fragment": path_fragment,
        "reason": reason,
        "status": status,
    }
    if filters is not None:
        if not isinstance(filters, Mapping):
            raise ValueError("filters must be a mapping")
        unknown = set(filters) - set(values)
        if unknown:
            raise ValueError("filters contain unsupported keys")
        for key, value in filters.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"filter {key} must be text or None")
            if values[key] is not None and values[key] != value:
                raise ValueError(f"filter {key} was supplied twice")
            values[key] = value
    for key, value in values.items():
        if value is not None and (not value or len(value) > (256 if key in {"reason", "status"} else 2_048) or "\x00" in value):
            raise ValueError(f"filter {key} is invalid")
    return values


def validate_content_diagnostics_v2_request(
    owner: str | Sequence[str] | None,
    source_root: Path | str,
    limit: int,
    *,
    state_directory: Path | str | None = None,
    cursor: str | None = None,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
    status: str | None = None,
    filters: Mapping[str, object] | None = None,
) -> tuple[tuple[str, ...], str, str | None, dict[str, str | None]]:
    """Validate v2 inputs before resolving or opening any owner."""

    owners = _v2_owner_tuple(owner)
    root = _v2_root(source_root, label="source_root")
    state_root = None if state_directory is None else _v2_root(state_directory, label="state_directory")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8_192):
        raise ValueError("cursor must be bounded non-empty text")
    selected_filters = _v2_filters(
        file_key=file_key,
        path_fragment=path_fragment,
        reason=reason,
        status=status,
        filters=filters,
    )
    return owners, root, state_root, selected_filters


def _v2_connection_tables(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables: dict[str, set[str]] = {}
    for row in rows:
        table = str(row[0])
        columns = {
            str(item[1])
            for item in connection.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")')
        }
        tables[table] = columns
    return tables


def _v2_validate_connection(connection: sqlite3.Connection, owner: str) -> None:
    tables = _v2_connection_tables(connection)
    for table in _V2_REQUIRED_TABLES[owner]:
        if table not in tables:
            raise _V2OwnerStateError(
                ContentDiagnosticOwnerState.UNAVAILABLE,
                "required_table_missing",
                f"{owner} required diagnostic table is absent: {table}",
            )
        if any(column not in tables[table] for column in _V2_REQUIRED_COLUMNS[owner][table]):
            raise _V2OwnerStateError(
                ContentDiagnosticOwnerState.CORRUPT,
                "required_column_missing",
                f"{owner} diagnostic table shape is incomplete: {table}",
            )
    if "metadata" not in tables:
        raise _V2OwnerStateError(
            ContentDiagnosticOwnerState.UNAVAILABLE,
            "schema_version_missing",
            f"{owner} metadata schema version is absent",
        )
    row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    try:
        version = int(row[0]) if row is not None else None
    except (TypeError, ValueError):
        version = None
    expected = _V2_EXPECTED_SCHEMAS[owner]
    if version is None:
        raise _V2OwnerStateError(
            ContentDiagnosticOwnerState.UNAVAILABLE,
            "schema_version_missing",
            f"{owner} metadata schema version is absent",
        )
    if version > expected:
        raise _V2OwnerStateError(
            ContentDiagnosticOwnerState.FUTURE,
            "future_schema",
            f"{owner} schema {version} is newer than supported schema {expected}",
        )
    if version != expected:
        raise _V2OwnerStateError(
            ContentDiagnosticOwnerState.UNAVAILABLE,
            "incompatible_schema",
            f"{owner} schema {version} is incompatible with supported schema {expected}",
        )


def _v2_scope_sql(filters: Mapping[str, str | None], root: str) -> tuple[str, list[object]]:
    normalized = root.rstrip("/") or "/"
    prefix = "/" if normalized == "/" else normalized + "/"
    clauses = ["(path=? COLLATE BINARY OR substr(path,1,?)=? COLLATE BINARY)"]
    params: list[object] = [normalized, len(prefix), prefix]
    if filters.get("file_key") is not None:
        clauses.append("file_key=?")
        params.append(filters["file_key"])
    if filters.get("path_fragment") is not None:
        clauses.append("instr(path,?)>0")
        params.append(filters["path_fragment"])
    if filters.get("reason") is not None:
        clauses.append("code=?")
        params.append(filters["reason"])
    if filters.get("status") is not None:
        clauses.append("status=?")
        params.append(filters["status"])
    return " AND ".join(clauses), params


def _v2_base_query(owner: str) -> str:
    """Return a normalized diagnostic projection; it never reads corpus bytes."""

    if owner == "pdf":
        return """
            SELECT 'pdf:document:' || d.file_key AS record_id, d.file_key AS file_key,
                   d.path AS path, COALESCE(d.error_type,d.status) AS code,
                   d.error_message AS message, d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.status IN ('error','failed','partial','processing')
            UNION ALL
            SELECT 'pdf:page:' || p.file_key || ':' || p.page_number AS record_id,
                   p.file_key AS file_key, d.path AS path, p.error_type AS code,
                   p.error_message AS message, d.status AS status, p.updated_ns AS updated_ns,
                   'page' AS locator_kind, CAST(p.page_number AS TEXT) AS locator_value
            FROM page_errors p JOIN documents d ON d.file_key=p.file_key
        """
    if owner == "docx":
        return """
            SELECT 'docx:document:' || d.file_key AS record_id, d.file_key AS file_key,
                   d.path AS path, COALESCE(d.error_type,d.failure_code,d.status) AS code,
                   d.error_message AS message, d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.failure_code IS NOT NULL
                  OR d.status IN ('error','failed','partial','processing')
            UNION ALL
            SELECT 'docx:diagnostic:' || dd.file_key || ':' || dd.ordinal,
                   dd.file_key, d.path, dd.code, dd.message, d.status, d.updated_ns,
                   'part' AS locator_kind, dd.part_name
            FROM document_diagnostics dd JOIN documents d ON d.file_key=dd.file_key
        """
    if owner == "office":
        return """
            SELECT 'office:document:' || d.file_key AS record_id,
                   d.file_key AS file_key, d.path AS path,
                   COALESCE(d.error_type,d.status) AS code, d.error_message AS message,
                   d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.status IN ('error','failed','partial','processing')
        """
    if owner == "archive":
        return """
            SELECT 'archive:container:' || c.container_key AS record_id,
                   c.container_key AS file_key, c.path AS path,
                   COALESCE(c.error_type,c.status) AS code, c.error_message AS message,
                   c.status AS status, c.updated_ns AS updated_ns,
                   'container' AS locator_kind, NULL AS locator_value
            FROM containers c
            WHERE c.error_type IS NOT NULL OR c.status IN ('error','failed','partial','processing')
            UNION ALL
            SELECT 'archive:issue:' || i.container_key || ':' || i.issue_id AS record_id,
                   i.container_key AS file_key, c.path AS path, i.reason_code AS code,
                   i.detail AS message, c.status AS status, i.created_ns AS updated_ns,
                   'member' AS locator_kind, i.member_chain AS locator_value
            FROM archive_issues i JOIN containers c ON c.container_key=i.container_key
        """
    if owner == "text":
        return """
            SELECT 'text:document:' || d.file_key AS record_id,
                   d.file_key AS file_key, d.path AS path,
                   COALESCE(d.error_type,d.status) AS code, d.error_message AS message,
                   d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.status IN ('error','failed','partial','processing')
        """
    if owner == "audio":
        return """
            SELECT 'audio:document:' || d.file_key AS record_id,
                   d.file_key AS file_key, d.path AS path,
                   COALESCE(d.error_type,d.status) AS code, d.error_message AS message,
                   d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.status IN ('error','failed','partial','processing')
        """
    if owner == "video":
        return """
            SELECT 'video:document:' || d.file_key AS record_id,
                   d.file_key AS file_key, d.path AS path,
                   COALESCE(d.error_type,d.status) AS code, d.error_message AS message,
                   d.status AS status, d.updated_ns AS updated_ns,
                   'document' AS locator_kind, NULL AS locator_value
            FROM documents d
            WHERE d.error_type IS NOT NULL OR d.status IN ('error','failed','partial','processing')
            UNION ALL
            SELECT 'video:frame:' || f.file_key || ':' || f.frame_index AS record_id,
                   f.file_key AS file_key, d.path AS path,
                   COALESCE(f.ocr_error_type,'frame_diagnostic') AS code,
                   f.ocr_error_message AS message, d.status AS status, d.updated_ns AS updated_ns,
                   'frame' AS locator_kind, CAST(f.timestamp_ms AS TEXT) AS locator_value
            FROM frames f JOIN documents d ON d.file_key=f.file_key
            WHERE f.ocr_error_type IS NOT NULL
        """
    if owner == "image":
        return """
            SELECT 'image:document:' || i.file_key AS record_id,
                   i.file_key AS file_key, i.path AS path,
                   COALESCE(i.error_type,i.error_phase,i.status) AS code,
                   i.error_message AS message, i.status AS status,
                   i.updated_ns AS updated_ns, 'document' AS locator_kind,
                   NULL AS locator_value
            FROM images i
            WHERE i.error_type IS NOT NULL OR i.error_phase IS NOT NULL
                  OR i.status IN ('error','failed','partial','processing')
        """
    if owner == "code":
        return """
            SELECT 'code:diagnostic:' || d.diagnostic_id AS record_id,
                   CAST(f.file_id AS TEXT) AS file_key, f.current_path AS path,
                   d.code AS code, d.message AS message, v.analysis_status AS status,
                   v.valid_from_ns AS updated_ns, 'line' AS locator_kind,
                   CAST(d.start_line AS TEXT) AS locator_value
            FROM diagnostics d
            JOIN file_versions v ON v.version_id=d.version_id
            JOIN files f ON f.file_id=v.file_id
            WHERE f.status='current' AND f.current_version_id=v.version_id
        """
    raise ValueError(f"unsupported v2 owner: {owner}")


def _v2_query_owner(
    connection: sqlite3.Connection,
    owner: str,
    root: str,
    filters: Mapping[str, str | None],
    after: str | None,
    limit: int,
    budget: KnowledgeReadBudget | None,
) -> tuple[list[dict[str, object]], int, str | None, int]:
    base = _v2_base_query(owner)
    scope, params = _v2_scope_sql(filters, root)
    wrapped = f"SELECT * FROM ({base}) AS records WHERE {scope}"
    count_row = connection.execute(f"SELECT COUNT(*) FROM ({wrapped})", params).fetchone()
    matched_count = int(count_row[0]) if count_row is not None else 0
    after_params: list[object] = []
    after_sql = ""
    if after is not None:
        after_sql = " AND record_id>?"
        after_params.append(after)
    remaining_row = connection.execute(
        f"SELECT COUNT(*) FROM ({wrapped}) AS remaining WHERE 1=1{after_sql}",
        [*params, *after_params],
    ).fetchone()
    remaining_count = int(remaining_row[0]) if remaining_row is not None else 0
    page_limit = limit
    if budget is not None:
        budget.checkpoint()
        if budget.rows_remaining is not None:
            if budget.rows_remaining <= 0:
                raise KnowledgeReadBudgetExceeded("rows_exhausted")
            page_limit = min(page_limit, budget.rows_remaining)
    rows = connection.execute(
        f"{wrapped}{after_sql} ORDER BY record_id COLLATE BINARY LIMIT ?",
        [*params, *after_params, page_limit],
    ).fetchall()
    if budget is not None:
        budget.checkpoint(rows=len(rows))
    items: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {
            "owner": owner,
            "record_id": _v2_text(row["record_id"], maximum=2_048),
            "file_key": _v2_text(row["file_key"], maximum=2_048),
            "path": _v2_text(row["path"], maximum=4_096),
            "error_type": _v2_text(row["code"], maximum=256),
            "error_message": _v2_text(row["message"], maximum=2_048),
            "status": _v2_text(row["status"], maximum=128),
            "locator_kind": _v2_text(row["locator_kind"], maximum=128),
            "locator": _v2_text(row["locator_value"], maximum=2_048),
        }
        if owner == "archive":
            item["container_key"] = item["file_key"]
            item["container_path"] = item["path"]
            if item["locator_kind"] == "member":
                item["member_chain"] = item["locator"]
        elif owner == "pdf" and item["locator_kind"] == "page":
            try:
                item["page_number"] = int(str(item["locator"]))
            except (TypeError, ValueError):
                item["page_number"] = item["locator"]
        elif owner == "docx" and item["locator_kind"] == "part":
            item["part_name"] = item["locator"]
        elif owner == "video" and item["locator_kind"] == "frame":
            try:
                item["timestamp_ms"] = int(str(item["locator"]))
            except (TypeError, ValueError):
                item["timestamp_ms"] = item["locator"]
        elif owner == "code" and item["locator_kind"] == "line":
            try:
                item["start_line"] = int(str(item["locator"]))
            except (TypeError, ValueError):
                item["start_line"] = item["locator"]
        items.append(item)
    next_after = None
    if remaining_count > len(items) and items:
        value = items[-1].get("record_id")
        next_after = value if isinstance(value, str) else None
    return items, matched_count, next_after, len(rows)


def _v2_owner_failure(
    owner: str,
    *,
    state: ContentDiagnosticOwnerState,
    snapshot_id: str | None,
    code: str,
    message: str,
) -> _V2OwnerRead:
    return _V2OwnerRead(
        owner=owner,
        state=state,
        status="unavailable" if state in {ContentDiagnosticOwnerState.MISSING, ContentDiagnosticOwnerState.UNAVAILABLE, ContentDiagnosticOwnerState.FUTURE} else state.value,
        snapshot_id=snapshot_id,
        items=[],
        matched_count=None,
        next_after=None,
        error={"kind": code, "message": message[:1_000]},
    )


def _v2_read_owner(
    owner: str,
    state_root: Path,
    root: str,
    filters: Mapping[str, str | None],
    after: str | None,
    limit: int,
    budget: KnowledgeReadBudget | None,
) -> _V2OwnerRead:
    started = time.perf_counter_ns()
    path = state_root / f"{owner}.sqlite3"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.MISSING,
            snapshot_id=None,
            code="owner_missing",
            message="format owner is absent",
        )
    except OSError as exc:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.UNAVAILABLE,
            snapshot_id=None,
            code="owner_unavailable",
            message=str(exc),
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.BLOCKED,
            snapshot_id=None,
            code="owner_unsafe",
            message="format owner is not a regular file",
        )
    from neocortex.capabilities.formats.diagnostic_queries import state_snapshot_id
    from neocortex.persistence.sqlite_immutable import (
        ImmutableSQLiteUnavailable,
        SQLiteReadSession,
        SQLiteSnapshotBudget,
        SQLiteSnapshotBudgetExceeded,
        preferred_sqlite_read_mode,
    )

    try:
        fence_started_ns = time.perf_counter_ns()
        before = state_snapshot_id(path)
        fence_before_ns = max(0, time.perf_counter_ns() - fence_started_ns)
        sqlite_budget = None
        if budget is not None and budget.max_temporary_bytes is not None:
            remaining = budget.temporary_bytes_remaining
            if remaining is None or remaining <= 0:
                raise KnowledgeReadBudgetExceeded("temporary_bytes_exhausted")
            sqlite_budget = SQLiteSnapshotBudget(
                max_temporary_bytes=remaining,
                cancellation_check=budget.cancellation_check,
            )
        session_started_ns = time.perf_counter_ns()
        session = SQLiteReadSession(
            path,
            mode=preferred_sqlite_read_mode(path),
            budget=sqlite_budget,
            cancellation_check=(budget.cancellation_check if budget is not None else None),
        )
        with session as connection:
            _v2_validate_connection(connection, owner)
            items, matched, next_after, rows_examined = _v2_query_owner(
                connection, owner, root, filters, after, limit, budget
            )
        temporary_bytes = int(session.metrics.temporary_bytes)
        snapshot_ns = int(session.metrics.prepare_time_ns)
        owner_elapsed_ns = max(0, time.perf_counter_ns() - session_started_ns)
        if budget is not None and temporary_bytes:
            budget.checkpoint(temporary_bytes=temporary_bytes)
        if state_snapshot_id(path) != before:
            raise ValueError("owner state changed during v2 diagnostic read")
        elapsed = max(0, time.perf_counter_ns() - started)
        return _V2OwnerRead(
            owner=owner,
            state=ContentDiagnosticOwnerState.READY,
            status="partial" if next_after is not None else ("ok" if items else "empty"),
            snapshot_id=before,
            items=items,
            matched_count=matched,
            next_after=next_after,
            error=None,
            rows_examined=rows_examined,
            rows_returned=len(items),
            temporary_bytes=temporary_bytes,
            phases_ns={
                "fences": fence_before_ns,
                "snapshot": snapshot_ns,
                "owner_read": max(0, owner_elapsed_ns - snapshot_ns),
                "total": elapsed,
            },
        )
    except _V2OwnerStateError as exc:
        return _v2_owner_failure(
            owner, state=exc.state, snapshot_id=locals().get("before"), code=exc.code, message=str(exc)
        )
    except KnowledgeReadBudgetExceeded:
        raise
    except SQLiteSnapshotBudgetExceeded as exc:
        if exc.reason == "cancelled":
            raise KnowledgeReadBudgetExceeded("cancelled") from exc
        raise KnowledgeReadBudgetExceeded("temporary_bytes_exhausted") from exc
    except ImmutableSQLiteUnavailable as exc:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.BLOCKED,
            snapshot_id=locals().get("before"),
            code="owner_read_blocked",
            message=str(exc),
        )
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.CORRUPT,
            snapshot_id=locals().get("before"),
            code="owner_corrupt",
            message=str(exc),
        )
    except ValueError as exc:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.PARTIAL,
            snapshot_id=locals().get("before"),
            code="snapshot_changed" if "changed" in str(exc).lower() else "owner_state_unavailable",
            message=str(exc),
        )
    except (OSError, RuntimeError, TypeError, KeyError) as exc:
        return _v2_owner_failure(
            owner,
            state=ContentDiagnosticOwnerState.UNAVAILABLE,
            snapshot_id=locals().get("before"),
            code="owner_state_unavailable",
            message=str(exc),
        )


def _v2_base_payload(
    owner: str | Sequence[str] | None,
    source_root: Path | str | None,
    limit: int,
    *,
    state_directory: Path | str | None,
    filters: Mapping[str, str | None],
) -> dict[str, object]:
    selected_owners: object
    try:
        selected_owners = list(_v2_owner_tuple(owner))
    except (TypeError, ValueError):
        selected_owners = owner
    return {
        "schema": CONTENT_DIAGNOSTICS_V2_SCHEMA,
        "response_version": 2,
        "operation": "content-diagnostics",
        "owner": owner if isinstance(owner, str) else "all",
        "owners": selected_owners,
        "read_only": True,
        "advisory_only": True,
        "mutation_authorized": False,
        "requested_root": None if source_root is None else str(source_root),
        "state_directory": None if state_directory is None else str(state_directory),
        "filters": dict(filters),
        "limit": limit,
        "items": [],
        "count": 0,
        "matched_count": None,
        "truncated": None,
        "next_cursor": None,
        "snapshot_id": None,
        "snapshots": {},
        "owner_states": {},
        "coverage": {"status": "unknown", "persisted_only": True, "snapshot_consistent": False},
        "metrics": {},
        "error": None,
    }


def content_diagnostics_v2_error_payload(
    owner: str | Sequence[str] | None = "all",
    source_root: Path | str | None = None,
    *,
    state_directory: Path | str | None = None,
    kind: str,
    message: str,
    status: str = "error",
    limit: int = 20,
    filters: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    selected = filters or {"file_key": None, "path_fragment": None, "reason": None, "status": None}
    payload = _v2_base_payload(owner, source_root, limit, state_directory=state_directory, filters=selected)
    payload.update(
        status=status,
        error={"kind": str(kind)[:128], "message": str(message)[:1_000]},
        coverage={"status": "unknown", "persisted_only": True, "snapshot_consistent": False},
    )
    return payload


def content_diagnostics_v2_payload(
    owner: str | Sequence[str] | None = "all",
    state_directory: Path | str | None = None,
    source_root: Path | str | None = None,
    limit: int = 20,
    *,
    cursor: str | None = None,
    file_key: str | None = None,
    path_fragment: str | None = None,
    reason: str | None = None,
    status: str | None = None,
    filters: Mapping[str, object] | None = None,
    budget: KnowledgeReadBudget | None = None,
) -> dict[str, object]:
    """Read one bounded federated page from all requested format owners.

    This is additive to :func:`content_diagnostics_payload` (v1).  It observes
    only persisted owner state, reports each missing/future/corrupt owner, and
    never scans source files or creates/migrates a state database.
    """

    try:
        raw_filters = _v2_filters(
            file_key=file_key,
            path_fragment=path_fragment,
            reason=reason,
            status=status,
            filters=filters,
        )
    except (TypeError, ValueError) as exc:
        raw_filters = {
            "file_key": file_key,
            "path_fragment": path_fragment,
            "reason": reason,
            "status": status,
        }
        return content_diagnostics_v2_error_payload(
            owner,
            source_root,
            state_directory=state_directory,
            kind="invalid_request",
            message=str(exc),
            limit=limit,
            filters=raw_filters,
        )
    payload = _v2_base_payload(
        owner,
        source_root,
        limit,
        state_directory=state_directory,
        filters=raw_filters,
    )
    started_ns = time.perf_counter_ns()
    try:
        if source_root is None:
            raise ValueError("source_root is required")
        owners, root, state_root, selected_filters = validate_content_diagnostics_v2_request(
            owner,
            source_root,
            limit,
            state_directory=state_directory,
            cursor=cursor,
            file_key=file_key,
            path_fragment=path_fragment,
            reason=reason,
            status=status,
            filters=filters,
        )
        if state_root is None:
            raise ValueError("state_directory is required")
        if budget is not None and not isinstance(budget, KnowledgeReadBudget):
            raise TypeError("budget must be a KnowledgeReadBudget")
    except (TypeError, ValueError, OSError) as exc:
        return content_diagnostics_v2_error_payload(
            owner,
            source_root,
            state_directory=state_directory,
            kind="invalid_request",
            message=str(exc),
            limit=limit,
            filters=raw_filters,
        )

    canonical_filters = tuple(sorted(selected_filters.items()))
    previous: ContentDiagnosticsCursor | None = None
    if cursor is not None:
        try:
            previous = ContentDiagnosticsCursor.from_token(cursor)
        except ValueError as exc:
            return content_diagnostics_v2_error_payload(
                owner,
                root,
                state_directory=state_root,
                kind="invalid_cursor",
                message=str(exc),
                limit=limit,
                filters=selected_filters,
            )
        if (
            previous.state_directory != str(state_root)
            or previous.source_root != root
            or previous.owners != owners
            or previous.filters != canonical_filters
            or previous.limit != limit
        ):
            return content_diagnostics_v2_error_payload(
                owner,
                root,
                state_directory=state_root,
                kind="cursor_binding_mismatch",
                message="cursor does not belong to this root, owner set, filters or limit",
                limit=limit,
                filters=selected_filters,
            )

    previous_snapshots = dict(previous.snapshots) if previous is not None else dict.fromkeys(owners)
    previous_after = dict(previous.after) if previous is not None else dict.fromkeys(owners)
    owner_results: list[_V2OwnerRead] = []
    all_items: list[dict[str, object]] = []
    changed: list[str] = []
    budget_error: KnowledgeReadBudgetExceeded | None = None
    for selected_owner in owners:
        try:
            result = _v2_read_owner(
                selected_owner,
                Path(state_root),
                root,
                selected_filters,
                previous_after[selected_owner],
                limit,
                budget,
            )
        except KnowledgeReadBudgetExceeded as exc:
            budget_error = exc
            break
        owner_results.append(result)
        if previous is not None and result.snapshot_id != previous_snapshots[selected_owner]:
            changed.append(selected_owner)
        if result.error is not None and result.error.get("kind") == "snapshot_changed":
            changed.append(selected_owner)
        if previous is None or previous_after[selected_owner] is not None:
            all_items.extend(result.items)

    if budget_error is not None:
        owner_results_by_name = {item.owner: item for item in owner_results}
        for selected_owner in owners:
            if selected_owner not in owner_results_by_name:
                owner_results.append(
                    _v2_owner_failure(
                        selected_owner,
                        state=ContentDiagnosticOwnerState.PARTIAL,
                        snapshot_id=None,
                        code=budget_error.reason,
                        message=str(budget_error),
                    )
                )
        changed = []

    changed = sorted(set(changed))

    if changed:
        changed_metrics = {
            "elapsed_ns": max(0, time.perf_counter_ns() - started_ns),
            "owners_requested": len(owners),
            "owners_read": len(owner_results),
            "rows_examined": sum(item.rows_examined for item in owner_results),
            "rows_returned": 0,
            "vectors_scanned": 0,
            "temporary_bytes": sum(item.temporary_bytes for item in owner_results),
            "phase_ns": {
                "snapshot": sum((item.phases_ns or {}).get("snapshot", 0) for item in owner_results),
                "fences": sum((item.phases_ns or {}).get("fences", 0) for item in owner_results),
                "owner_read": sum(
                    (item.phases_ns or {}).get("owner_read", 0) for item in owner_results
                ),
                "ranking": 0,
                "models": 0,
                "hydration": 0,
                "packing": 0,
                "serialization": 0,
            },
            "budget": None if budget is None else budget.to_dict(),
        }
        payload.update(
            status="snapshot_changed",
            owners=list(owners),
            requested_root=root,
            state_directory=str(state_root),
            snapshots={item.owner: item.snapshot_id for item in owner_results},
            owner_states={
                item.owner: {
                    "state": item.state.value,
                    "status": item.status,
                    "snapshot_id": item.snapshot_id,
                    "error": item.error,
                }
                for item in owner_results
            },
            coverage={
                "status": "snapshot_changed",
                "persisted_only": True,
                "snapshot_consistent": False,
                "changed_owners": sorted(changed),
                "requested_root": root,
            },
            error={
                "kind": "snapshot_changed",
                "message": "one or more owner snapshots changed before continuation",
            },
            metrics=changed_metrics,
        )
        return payload

    owner_by_name = {item.owner: item for item in owner_results}
    # Owners after a budget stop are represented as partial, never as a clean
    # missing owner or an empty successful query.
    for selected_owner in owners:
        if selected_owner not in owner_by_name:
            owner_by_name[selected_owner] = _v2_owner_failure(
                selected_owner,
                state=ContentDiagnosticOwnerState.PARTIAL,
                snapshot_id=None,
                code=budget_error.reason if budget_error is not None else "owner_not_read",
                message=str(budget_error) if budget_error is not None else "owner was not read",
            )
    owner_results = [owner_by_name[name] for name in owners]
    next_after = {
        item.owner: (None if previous is not None and previous_after[item.owner] is None else item.next_after)
        for item in owner_results
    }
    next_token = None
    if budget_error is None and any(value is not None for value in next_after.values()):
        next_token = ContentDiagnosticsCursor(
            state_directory=str(state_root),
            source_root=root,
            owners=owners,
            filters=canonical_filters,
            limit=limit,
            snapshots=tuple((name, owner_by_name[name].snapshot_id) for name in owners),
            after=tuple((name, next_after[name]) for name in owners),
        ).to_token()
    known_counts = [item.matched_count for item in owner_results]
    matched_count: int | None = (
        sum(value for value in known_counts if value is not None)
        if all(value is not None for value in known_counts)
        else None
    )
    missing = [item.owner for item in owner_results if item.state is ContentDiagnosticOwnerState.MISSING]
    unavailable = [
        item.owner
        for item in owner_results
        if item.state in {
            ContentDiagnosticOwnerState.UNAVAILABLE,
            ContentDiagnosticOwnerState.FUTURE,
            ContentDiagnosticOwnerState.CORRUPT,
            ContentDiagnosticOwnerState.BLOCKED,
        }
    ]
    partial = [
        item.owner
        for item in owner_results
        if item.state is ContentDiagnosticOwnerState.PARTIAL or item.next_after is not None
    ]
    has_items = bool(all_items)
    if budget_error is not None:
        result_status = "blocked"
    elif unavailable and has_items:
        result_status = "partial"
    elif unavailable or missing:
        result_status = "partial" if any(item.state is ContentDiagnosticOwnerState.READY for item in owner_results) else "unavailable"
    elif partial or next_token is not None:
        result_status = "partial"
    else:
        result_status = "ok" if has_items else "empty"
    snapshots = {item.owner: item.snapshot_id for item in owner_results}
    metrics = {
        "elapsed_ns": max(0, time.perf_counter_ns() - started_ns),
        "owners_requested": len(owners),
        "owners_read": sum(item.state is ContentDiagnosticOwnerState.READY for item in owner_results),
        "owners_missing": len(missing),
        "owners_unavailable": len(unavailable),
        "rows_examined": sum(item.rows_examined for item in owner_results),
        "rows_returned": sum(item.rows_returned for item in owner_results),
        "vectors_scanned": 0,
        "temporary_bytes": sum(item.temporary_bytes for item in owner_results),
        "phase_ns": {
            "snapshot": sum(
                (item.phases_ns or {}).get("snapshot", 0) for item in owner_results
            ),
            "fences": sum(
                (item.phases_ns or {}).get("fences", 0) for item in owner_results
            ),
            "owner_read": sum(
                (item.phases_ns or {}).get("owner_read", 0) for item in owner_results
            ),
            "ranking": 0,
            "models": 0,
            "hydration": 0,
            "packing": 0,
            "serialization": 0,
        },
        "budget": None if budget is None else budget.to_dict(),
    }
    payload.update(
        status=result_status,
        owners=list(owners),
        requested_root=root,
        state_directory=str(state_root),
        items=all_items,
        count=len(all_items),
        matched_count=matched_count,
        truncated=next_token is not None or budget_error is not None,
        next_cursor=next_token,
        snapshot_id=_v2_digest(snapshots),
        snapshots=snapshots,
        owner_states={
            item.owner: {
                "state": item.state.value,
                "status": item.status,
                "snapshot_id": item.snapshot_id,
                "matched_count": item.matched_count,
                "truncated": item.next_after is not None,
                "error": item.error,
            }
            for item in owner_results
        },
        coverage={
            "status": "complete" if result_status in {"ok", "empty"} else "partial",
            "persisted_only": True,
            "snapshot_consistent": True,
            "requested_root": root,
            "scope": "exact_requested_root_and_filters",
            "missing_owners": missing,
            "unavailable_owners": unavailable,
            "partial_owners": partial,
            "query_page_complete": next_token is None and budget_error is None,
        },
        metrics=metrics,
        error=(
            None
            if budget_error is None and not unavailable and not missing
            else {
                "kind": budget_error.reason if budget_error is not None else "owner_pages_incomplete",
                "message": str(budget_error)
                if budget_error is not None
                else "one or more owner states are not complete",
            }
        ),
    )
    serialization_started_ns = time.perf_counter_ns()
    json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    phase_metrics = metrics["phase_ns"]
    assert isinstance(phase_metrics, dict)
    phase_metrics["serialization"] = max(
        0, time.perf_counter_ns() - serialization_started_ns
    )
    metrics["elapsed_ns"] = max(0, time.perf_counter_ns() - started_ns)
    return payload


def content_diagnostics_payload_v2(*args: Any, **kwargs: Any) -> dict[str, object]:
    """Compatibility alias using the suffix style of older API helpers."""

    return content_diagnostics_v2_payload(*args, **kwargs)


# endregion [03]


__all__ = [
    "CONTENT_DIAGNOSTICS_SCHEMA",
    "CONTENT_DIAGNOSTICS_V2_SCHEMA",
    "CONTENT_DIAGNOSTIC_OWNERS",
    "CONTENT_DIAGNOSTIC_V2_OWNERS",
    "ContentDiagnosticOwnerState",
    "ContentDiagnosticsCursor",
    "KnowledgeReadBudget",
    "KnowledgeReadBudgetExceeded",
    "content_diagnostics_error_payload",
    "content_diagnostics_payload",
    "content_diagnostics_payload_v2",
    "content_diagnostics_v2_error_payload",
    "content_diagnostics_v2_payload",
    "validate_content_diagnostics_request",
    "validate_content_diagnostics_v2_request",
]
