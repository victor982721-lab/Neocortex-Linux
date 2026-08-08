"""Characterization matrix for persisted PDF cache-status policy."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.pdf_route_cache import PdfRouteCacheMixin
from _04_Nucleo_Operativo.pdf_route_models import CacheDecision, PdfRouteConfig
from _04_Nucleo_Operativo.retry_policy import (
    classify_pdf_failure,
    is_retryable_pdf_document_error,
)
from _04_Nucleo_Operativo.route_filters import CandidateSelection


def _route(
    state_path: Path,
    *,
    apply_actions: bool = False,
    retry_errors: bool = False,
    selection: CandidateSelection | None = None,
) -> PdfRouteCacheMixin:
    route = PdfRouteCacheMixin()
    route.config = PdfRouteConfig(
        state_path,
        apply_actions=apply_actions,
        ocr_mode="never",
        workers=1,
        retry_errors=retry_errors,
        selection=CandidateSelection() if selection is None else selection,
    )
    return route


def _row(status: str, **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "status": status,
        "error_type": None,
        "error_message": None,
        "metadata_json": None,
        "latest_page_error_type": None,
        "latest_page_error_message": None,
        "persisted_page_error_count": 0,
        "completed_pages": 0,
        "page_count": 0,
        "transient_retry_count": 0,
        "next_retry_ns": None,
        "has_retryable_page_error": 0,
    }
    row.update(updates)
    return row


def test_cached_status_decision_signature_and_done_precedence(tmp_path: Path) -> None:
    assert str(inspect.signature(PdfRouteCacheMixin._cached_status_decision)) == (
        "(self, row, prior_status: 'str', retry_pages: 'int') -> 'CacheDecision'"
    )
    route = _route(
        tmp_path / "pdf.sqlite3",
        apply_actions=True,
        retry_errors=True,
        selection=CandidateSelection(statuses=("processing",)),
    )
    row = _row(
        "done",
        error_type="HistoricWarning",
        error_message="bounded evidence",
        metadata_json='{"fixture":true}',
        latest_page_error_type="PageWarning",
        latest_page_error_message="page evidence",
    )

    assert route._cached_status_decision(row, "done", 4) == CacheDecision(
        True,
        "done",
        4,
        error_type="HistoricWarning",
        error_message="bounded evidence",
        metadata_json='{"fixture":true}',
        page_error_type="PageWarning",
        page_error_message="page evidence",
    )


def test_explicit_retry_precedence_avoids_later_policy_reads(tmp_path: Path) -> None:
    retry_all = _route(tmp_path / "retry.sqlite3", retry_errors=True)
    assert retry_all._cached_status_decision(
        {"status": "error"},
        "error",
        7,
    ) == CacheDecision(False, "error", 7)

    selected = _route(
        tmp_path / "selected.sqlite3",
        selection=CandidateSelection(statuses=("processing",)),
    )
    assert selected._cached_status_decision(
        {"status": "processing"},
        "processing",
        0,
    ) == CacheDecision(False, "processing", 0)


@pytest.mark.parametrize(
    ("row", "apply_actions"),
    (
        (
            _row(
                "error",
                error_type="PdfStructuralRecoveryFailed",
                error_message="all engines failed",
            ),
            True,
        ),
        (
            _row(
                "partial",
                error_type="PdfPageSequenceAborted",
                persisted_page_error_count=1,
            ),
            False,
        ),
        (
            _row(
                "partial",
                error_type="",
                persisted_page_error_count=32,
            ),
            False,
        ),
        (
            _row(
                "partial",
                error_type="PdfDocumentTimeout",
                error_message="[durable-progress:2->4] worker deadline exceeded",
                transient_retry_count=3,
            ),
            False,
        ),
        (
            _row(
                "error",
                error_type="PdfDocumentTimeout",
                error_message="legacy worker deadline exceeded",
                completed_pages=2,
                page_count=5,
                transient_retry_count=3,
            ),
            False,
        ),
    ),
)
def test_structural_and_progress_policies_force_one_revalidation(
    tmp_path: Path,
    row: dict[str, object],
    apply_actions: bool,
) -> None:
    route = _route(tmp_path / "pdf.sqlite3", apply_actions=apply_actions)

    assert route._cached_status_decision(
        row,
        str(row["status"]),
        5,
    ) == CacheDecision(False, str(row["status"]), 5)


@pytest.mark.parametrize(
    "row",
    (
        _row(
            "partial",
            error_type="PdfSyntaxError",
            error_message="invalid xref",
            has_retryable_page_error=1,
        ),
        _row(
            "partial",
            error_type="PdfResourceError",
            error_message="memory headroom unavailable",
        ),
        _row(
            "error",
            error_type="PdfResourceError",
            error_message="memory headroom unavailable",
        ),
    ),
)
def test_due_automatic_retries_are_cache_misses(
    tmp_path: Path,
    row: dict[str, object],
) -> None:
    route = _route(tmp_path / "pdf.sqlite3")

    assert route._cached_status_decision(
        row,
        str(row["status"]),
        3,
    ) == CacheDecision(False, str(row["status"]), 3)


@pytest.mark.parametrize(
    "row",
    (
        _row(
            "error",
            error_type="PdfResourceError",
            error_message="retry budget exhausted",
            transient_retry_count=3,
        ),
        _row(
            "partial",
            error_type="PdfResourceError",
            error_message="backoff is still active",
            next_retry_ns=9_000_000_000_000_000_000,
        ),
        _row(
            "protected",
            error_type="EncryptedPdf",
            error_message="password required",
        ),
    ),
)
def test_suppressed_retries_remain_hits_with_diagnostic_evidence(
    tmp_path: Path,
    row: dict[str, object],
) -> None:
    route = _route(tmp_path / "pdf.sqlite3")
    status = str(row["status"])

    assert route._cached_status_decision(row, status, 2) == CacheDecision(
        True,
        status,
        2,
        error_type=str(row["error_type"]),
        error_message=str(row["error_message"]),
    )


def test_unknown_and_processing_statuses_are_not_cache_hits(tmp_path: Path) -> None:
    route = _route(tmp_path / "pdf.sqlite3")

    for status in ("processing", "unknown"):
        assert route._cached_status_decision(
            _row(status),
            status,
            0,
        ) == CacheDecision(False, status, 0)


@pytest.mark.parametrize(
    ("error_type", "error_message", "expected_retryable"),
    (
        ("PdfDocumentTimeout", "worker deadline exceeded", True),
        ("OperationalError", "database is locked", True),
        ("EncryptedPdf", "password required", False),
        ("PdfSyntaxError", "invalid xref", False),
    ),
)
def test_cache_retry_guard_matches_labeled_failure_policy(
    error_type: str,
    error_message: str,
    expected_retryable: bool,
) -> None:
    diagnostic = classify_pdf_failure(error_type, error_message)

    assert diagnostic.retryable is expected_retryable
    assert (
        is_retryable_pdf_document_error(error_type, error_message)
        is expected_retryable
    )
