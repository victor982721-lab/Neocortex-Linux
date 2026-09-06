"""Scoped, paginated visibility of current generic-text coverage and errors."""

from __future__ import annotations

from pathlib import Path

from neocortex.capabilities.formats.diagnostic_queries import (
    DiagnosticPage,
    decode_cursor,
    encode_cursor,
    selector,
    state_snapshot_id,
    validate_limit,
    verify_snapshot,
)

from .text_state import _validate_reader, text_database


def list_text_errors(
    path: Path,
    limit: int = 20,
    *,
    file_key: str | None = None,
    path_scope: str | None = None,
    path_fragment: str | None = None,
    error_type: str | None = None,
    retryable: bool | None = None,
    cursor: str | None = None,
) -> DiagnosticPage:
    validate_limit(limit)
    clauses, params, scope = selector(
        file_key=file_key, path_scope=path_scope, path_fragment=path_fragment
    )
    clauses.append("d.status='error'")
    scope["owner"] = "text_errors"
    if error_type is not None:
        if (
            not isinstance(error_type, str)
            or not error_type
            or len(error_type) > 256
            or "\x00" in error_type
        ):
            raise ValueError("invalid error_type")
        clauses.append("d.error_type=?")
        params.append(error_type)
        scope["error_type"] = error_type
    if retryable is not None:
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be boolean")
        clauses.append("d.retryable=?")
        params.append(int(retryable))
        scope["retryable"] = retryable
    snapshot = state_snapshot_id(path)
    after = decode_cursor(cursor, snapshot_id=snapshot, scope=scope)
    if not path.is_file():
        return DiagnosticPage((), None, scope, snapshot, 0)
    where = " AND ".join(clauses)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM documents d WHERE {where}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT d.file_key,d.path,d.processing_signature,d.status,d.content_kind,
            d.media_type,d.error_type,substr(d.error_message,1,2000) AS error_message,
            d.retryable,d.updated_ns FROM documents d WHERE {where} AND d.file_key>?
            ORDER BY d.file_key LIMIT ?""",
            (*params, after, limit + 1),
        ).fetchall()
    verify_snapshot(path, snapshot)
    items = tuple(
        {
            **dict(row),
            "owner": "text",
            "record_id": str(row["file_key"]),
            "evidence_kind": "observation",
            "retryable": bool(row["retryable"]),
        }
        for row in rows[:limit]
    )
    next_cursor = (
        encode_cursor(str(rows[limit - 1]["file_key"]), snapshot_id=snapshot, scope=scope)
        if len(rows) > limit
        else None
    )
    return DiagnosticPage(items, next_cursor, scope, snapshot, count)


def read_text_coverage(path: Path, *, path_scope: str | None = None) -> dict[str, object]:
    clauses, params, scope = selector(path_scope=path_scope)
    where = " AND ".join(clauses) or "1"
    snapshot = state_snapshot_id(path)
    result: dict[str, object] = {
        "available": path.is_file(),
        "owner": "text",
        "scope": scope,
        "snapshot_id": snapshot,
        "coverage_scope": "persisted_text_documents",
        "candidate_scope": "unknown_inventory_not_owned_by_text",
    }
    if not path.is_file():
        return result
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            f"""SELECT COUNT(*) AS documents,
            COALESCE(SUM(status='complete'),0) AS complete,
            COALESCE(SUM(status='error'),0) AS errors,
            COALESCE(SUM(status='error' AND retryable=1),0) AS retryable_errors,
            COALESCE(SUM(status='complete' AND text_chars=0),0) AS complete_without_text,
            COALESCE(SUM(text_truncated=1),0) AS truncated,
            COALESCE(SUM(text_chars),0) AS text_chars
            FROM documents d WHERE {where}""",
            params,
        ).fetchone()
    verify_snapshot(path, snapshot)
    return {**result, **dict(row)}
