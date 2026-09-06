"""Published PDF coverage, separate from historical extraction attempts."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Mapping

from neocortex.capabilities.formats.diagnostic_queries import (
    DiagnosticPage,
    decode_cursor,
    encode_cursor,
    selector,
    state_snapshot_id,
    validate_limit,
    verify_snapshot,
)
from neocortex.persistence.sqlite_schema_contract import read_metadata_schema_version

from .pdf_schema import PDF_SCHEMA_VERSION, validate_pdf_schema
from .pdf_state import pdf_database


def _validate_reader(connection: sqlite3.Connection) -> None:
    version = read_metadata_schema_version(connection, label="PDF")
    if version != PDF_SCHEMA_VERSION:
        raise ValueError(f"PDF diagnostics require schema {PDF_SCHEMA_VERSION}; found {version}")
    validate_pdf_schema(connection)


def _metadata(raw: object) -> tuple[dict[str, object], bool]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def project_pdf_diagnostic(row: Mapping[str, object]) -> dict[str, object]:
    """Project a published row plus SQL page counters; never reuse past gaps."""

    status = str(row["status"])
    metadata, metadata_valid = _metadata(row.get("metadata_json"))
    recovery = metadata.get("neocortex_recovery")
    recovered = isinstance(recovery, dict)
    history: list[dict[str, object]] = []
    if isinstance(recovery, dict):
        primary_error = str(recovery.get("primary_error") or "")[:2_000]
        attempt: dict[str, object] = {
            "stage": "primary",
            "status": "failed",
            "error": primary_error,
            "coverage_scope": "historical_attempt_only",
        }
        for key, pattern in (
            ("consecutive_errors", r"(\d+) consecutive page extraction failures"),
            ("last_attempted_page", r"last[_ ]attempted[_ ]page[=: ]+(\d+)"),
            ("skipped_pages", r"(?:skipped[_ ]pages[=: ]+|skipped[=: ]+)(\d+)"),
        ):
            match = re.search(pattern, primary_error, re.IGNORECASE)
            attempt[key] = int(match.group(1)) if match else None
        structured = recovery.get("primary_attempt")
        if not isinstance(structured, dict) and "{" in primary_error:
            try:
                structured = json.loads(
                    primary_error[primary_error.index("{") : primary_error.rindex("}") + 1]
                )
            except (TypeError, ValueError):
                structured = None
        if isinstance(structured, dict):
            for key in ("consecutive_errors", "last_attempted_page", "skipped_pages"):
                value = structured.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    attempt[key] = value
        history.append(attempt)
    page_count = row.get("page_count")
    total = int(page_count) if isinstance(page_count, int) and page_count >= 0 else None
    published = int(row.get("published_pages") or 0)
    declared = int(row.get("completed_pages") or 0)
    page_errors = int(row.get("page_errors_count") or 0)
    observed_errors = int(row.get("current_page_errors") or 0)
    attempt_errors = int(row.get("attempt_page_error_records") or observed_errors)
    page_min, page_max = row.get("first_published_page"), row.get("last_published_page")
    bounds_valid = published == 0 or (
        isinstance(page_min, int)
        and isinstance(page_max, int)
        and page_min >= 0
        and total is not None
        and page_max < total
    )
    all_pages = bool(
        total and published == total and bounds_valid and page_min == 0 and page_max == total - 1
    )
    complete = (
        status == "done"
        and all_pages
        and declared == published
        and not row.get("is_partial")
        and observed_errors == 0
    )
    if status == "protected":
        coverage_status = "protected"
    elif status == "error":
        coverage_status = "failed"
    elif complete:
        coverage_status = "complete"
    elif status in {"done", "partial"}:
        coverage_status = "partial" if total is not None and bounds_valid else "unknown"
    else:
        coverage_status = "pending"
    # Missing pages describe the *published final generation*, never pages
    # skipped during an earlier primary attempt, even when recovery is done.
    missing = (
        max(0, total - published)
        if total is not None and bounds_valid and status in {"done", "partial"}
        else None
    )
    reasons = []
    if declared != published:
        reasons.append("declared_vs_published_page_count_mismatch")
    if not bounds_valid:
        reasons.append("published_page_bounds_invalid")
    if not metadata_valid:
        reasons.append("metadata_invalid")
    if attempt_errors > observed_errors:
        reasons.append("historical_page_errors_superseded_by_published_pages")
        history.append(
            {
                "stage": "prior_page_attempts",
                "status": "superseded",
                "record_count": attempt_errors - observed_errors,
                "coverage_scope": "historical_attempt_only",
            }
        )
    if page_errors != observed_errors:
        reasons.append("stored_page_error_counter_differs_from_uncovered_pages")
    if recovered and not complete:
        reasons.append("recovery_does_not_prove_complete_extraction")
    return {
        "owner": "pdf",
        "record_id": str(row["file_key"]),
        "file_key": str(row["file_key"]),
        "path": str(row["path"]),
        "processing_signature": str(row.get("processing_signature") or ""),
        "status": status,
        "recovered": recovered,
        "recovery_engine": str(recovery.get("engine") or "unknown")
        if isinstance(recovery, dict)
        else None,
        "final_coverage": {
            "status": coverage_status,
            "complete": complete,
            "total_pages": total,
            "published_pages": published,
            "declared_completed_pages": declared,
            "pages_without_text": int(row.get("pages_without_text") or 0),
            "pages_with_text": int(row.get("pages_with_text") or 0),
            "missing_pages": missing,
            "page_errors": observed_errors,
            "stored_page_errors_count": page_errors,
            "current_page_error_records": observed_errors,
            "requested_start_one_based": row.get("page_start"),
            "requested_end_one_based_inclusive": row.get("page_end"),
            "reasons": reasons,
        },
        "historical_attempts": history,
        "error_type": row.get("error_type"),
        "error_message": str(row.get("error_message") or "")[:2_000] or None,
        "recommendation": "keep_protected" if status == "protected" else None,
        "corruption_status": "not_inferred_from_extraction_failure",
        "evidence_kind": "observation",
        "mutates_source": False,
    }


_PAGE_COLUMNS = """
    (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key) AS published_pages,
    (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key AND p.text_chars=0) AS pages_without_text,
    (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key AND p.text_chars>0) AS pages_with_text,
    (SELECT MIN(page_number) FROM pages p WHERE p.file_key=d.file_key) AS first_published_page,
    (SELECT MAX(page_number) FROM pages p WHERE p.file_key=d.file_key) AS last_published_page,
    (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key AND e.processing_signature=d.processing_signature AND NOT EXISTS(SELECT 1 FROM pages p WHERE p.file_key=e.file_key AND p.page_number=e.page_number)) AS current_page_errors,
    (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key AND e.processing_signature=d.processing_signature) AS attempt_page_error_records
"""


def list_pdf_diagnostics(
    path: Path,
    limit: int = 20,
    *,
    file_key: str | None = None,
    path_scope: str | None = None,
    path_fragment: str | None = None,
    status: str | None = None,
    error_type: str | None = None,
    cursor: str | None = None,
) -> DiagnosticPage:
    validate_limit(limit)
    clauses, params, scope = selector(
        file_key=file_key, path_scope=path_scope, path_fragment=path_fragment
    )
    scope["owner"] = "pdf"
    if status is not None:
        if not isinstance(status, str) or status not in {
            "done",
            "partial",
            "error",
            "protected",
            "pending",
            "processing",
        }:
            raise ValueError("invalid PDF status")
        clauses.append("d.status=?")
        params.append(status)
        scope["status"] = status
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
    snapshot = state_snapshot_id(path)
    after = decode_cursor(cursor, snapshot_id=snapshot, scope=scope)
    if not path.is_file():
        return DiagnosticPage((), None, scope, snapshot, 0)
    where = " AND ".join(clauses) or "1"
    with pdf_database(path, readonly=True) as connection:
        _validate_reader(connection)
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM documents d WHERE {where}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"SELECT d.*, {_PAGE_COLUMNS} FROM documents d WHERE {where} AND d.file_key>? ORDER BY d.file_key LIMIT ?",
            (*params, after, limit + 1),
        ).fetchall()
    verify_snapshot(path, snapshot)
    next_cursor = (
        encode_cursor(str(rows[limit - 1]["file_key"]), snapshot_id=snapshot, scope=scope)
        if len(rows) > limit
        else None
    )
    return DiagnosticPage(
        tuple(project_pdf_diagnostic(dict(row)) for row in rows[:limit]),
        next_cursor,
        scope,
        snapshot,
        count,
    )


def read_pdf_coverage(path: Path, *, path_scope: str | None = None) -> dict[str, object]:
    clauses, params, scope = selector(path_scope=path_scope)
    where = " AND ".join(clauses) or "1"
    snapshot = state_snapshot_id(path)
    result: dict[str, object] = {
        "available": path.is_file(),
        "owner": "pdf",
        "scope": scope,
        "snapshot_id": snapshot,
        "candidate_scope": "persisted_pdf_inventory",
        "coverage_scope": "published_pdf_documents",
        "categories_overlap": ["recovered", "pages_without_text"],
    }
    if not path.is_file():
        return result
    counts = dict.fromkeys(
        (
            "documents",
            "extractions_complete",
            "protected",
            "recovered",
            "partial",
            "failed",
            "pending",
            "unknown",
            "pages_without_text",
            "pages_with_text",
        ),
        0,
    )
    with pdf_database(path, readonly=True) as connection:
        _validate_reader(connection)
        result["candidates"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM pdf_inventory d WHERE {where}", params
            ).fetchone()[0]
        )
        result["inventory_without_document"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM pdf_inventory d WHERE {where} AND NOT EXISTS(SELECT 1 FROM documents p WHERE p.file_key=d.file_key)",
                params,
            ).fetchone()[0]
        )
        result["documents_not_in_inventory"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM documents d WHERE {where} AND NOT EXISTS(SELECT 1 FROM pdf_inventory p WHERE p.file_key=d.file_key)",
                params,
            ).fetchone()[0]
        )
        for row in connection.execute(
            f"SELECT d.*, {_PAGE_COLUMNS} FROM documents d WHERE {where}", params
        ):
            item = project_pdf_diagnostic(dict(row))
            coverage = item["final_coverage"]
            assert isinstance(coverage, dict)
            counts["documents"] += 1
            category = str(coverage["status"])
            counts["extractions_complete" if category == "complete" else category] += 1
            counts["recovered"] += int(bool(item["recovered"]))
            counts["pages_without_text"] += int(coverage["pages_without_text"])
            counts["pages_with_text"] += int(coverage["pages_with_text"])
    verify_snapshot(path, snapshot)
    return {**result, **counts}
