"""Root-scoped, read-only envelopes over the existing format diagnostics.

The selected root is always supplied by the caller, never discovered from the
latest run. Counts describe persisted owner evidence, not a fresh corpus scan.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

CONTENT_DIAGNOSTICS_SCHEMA = "neocortex.content-diagnostics/v1"
CONTENT_DIAGNOSTIC_OWNERS = ("pdf", "text", "archive")
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
) -> dict[str, object]:
    """Read one bounded page without creating, migrating or repairing owners.

    ``reason`` is an exact extractor error_type for PDF/Text and reason_code
    for Archive. Root coverage is explicitly separate from filtered matches.
    Missing owners never imply zero errors or complete extraction.
    """

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
        kind = "invalid_cursor" if "cursor" in message.lower() else "state_changed" if "changed" in message.lower() else "invalid_request"
        return _failed(payload, kind, message)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return _failed(payload, "owner_state_unavailable", str(exc), status="blocked")
    except (TypeError, AttributeError) as exc:
        return _failed(payload, "adapter_contract_error", str(exc))


__all__ = [
    "CONTENT_DIAGNOSTICS_SCHEMA", "CONTENT_DIAGNOSTIC_OWNERS",
    "content_diagnostics_error_payload", "content_diagnostics_payload", "validate_content_diagnostics_request",
]
