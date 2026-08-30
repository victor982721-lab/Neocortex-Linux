"""Shared, bounded retry classification and scheduling policy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


# region [01] Stable retry bounds

PDF_MAX_AUTOMATIC_RETRIES = 3
TRANSIENT_RETRY_BASE_SECONDS = 60 * 60


def retry_delay_seconds(attempt: int) -> int:
    """Return a capped exponential delay for a one-based retry attempt."""

    return TRANSIENT_RETRY_BASE_SECONDS * min(24, 2 ** max(0, attempt - 1))


def automatic_retry_due(
    attempt_count: int,
    next_retry_ns: int | None,
    *,
    now_ns: int | None = None,
    max_attempts: int = PDF_MAX_AUTOMATIC_RETRIES,
) -> bool:
    """Admit a retry only while its durable budget and backoff allow it."""

    if attempt_count >= max_attempts:
        return False
    current_ns = time.time_ns() if now_ns is None else now_ns
    return next_retry_ns is None or next_retry_ns <= current_ns


# endregion [01]


# region [02] PDF error taxonomy

PdfReviewRecommendation = Literal[
    "retry",
    "keep_protected",
    "manual_review",
    "deletion_candidate",
]


@dataclass(frozen=True, slots=True)
class PdfFailureDiagnostic:
    """Stable failure evidence shared by persistence and the review queue."""

    error_type: str
    phase: str
    reason_code: str
    retryable: bool
    recommendation: PdfReviewRecommendation
    confidence: float
    exit_code: int | None = None

    def evidence(self, message: str) -> dict[str, object]:
        return {
            "error_type": self.error_type,
            "phase": self.phase,
            "message": message[:2000],
            "exit_code": self.exit_code,
            "retryable": self.retryable,
        }


_STRUCTURAL_PDF_MARKERS = (
    "unexpected eof",
    "no /root object",
    "invalid octal",
    "object is not a stream",
    "page not in document",
    "cannot find page",
    "consecutive page extraction failures",
    "xref not found",
    "invalid xref",
    "errors while decoding content stream",
)


def is_structural_pdf_failure(error_type: str, error_message: str) -> bool:
    """Return whether evidence describes malformed PDF structure/content."""

    normalized_type = error_type.casefold()
    message = error_message.casefold()
    return normalized_type in {
        "pseof",
        "pdfsyntaxerror",
        "pdfstructurederror",
    } or any(marker in message for marker in _STRUCTURAL_PDF_MARKERS)


def classify_pdf_failure(
    error_type: str,
    error_message: str,
    *,
    phase: str = "unknown",
    exit_code: int | None = None,
    recovered: bool = False,
) -> PdfFailureDiagnostic:
    """Classify one PDF failure without turning evidence into an action."""

    message = error_message.casefold()
    if error_type == "EncryptedPdf":
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_password_required",
            False,
            "keep_protected",
            1.0,
            exit_code,
        )
    if recovered:
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_structural_recovered",
            False,
            "manual_review",
            0.98,
            exit_code,
        )
    if error_type in {
        "PdfChildProcessError",
        "PdfChildExitError",
        "PdfDocumentTimeout",
        "InterruptedPdfProcessing",
        "MemoryBudgetExceeded",
        "MemoryHeadroomTimeout",
        "PdfResourceError",
        "MemoryError",
        "PermissionError",
    }:
        reason = (
            "pdf_document_timeout"
            if error_type == "PdfDocumentTimeout"
            else "pdf_interrupted_processing"
            if error_type == "InterruptedPdfProcessing"
            else "pdf_child_exit"
            if error_type in {"PdfChildProcessError", "PdfChildExitError"}
            else "pdf_resource_failure"
        )
        return PdfFailureDiagnostic(
            error_type,
            phase,
            reason,
            True,
            "retry",
            0.95,
            exit_code,
        )
    if error_type == "OperationalError" and ("locked" in message or "busy" in message):
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_state_contention",
            True,
            "retry",
            0.95,
            exit_code,
        )
    if "source metadata changed" in message:
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_source_changed",
            True,
            "retry",
            1.0,
            exit_code,
        )
    if error_type == "AttributeError" and (
        "boundedsemaphore" in message and "has no attribute 'get'" in message
    ):
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_legacy_ocr_control_bug",
            True,
            "retry",
            1.0,
            exit_code,
        )
    if error_type == "LegacyOcrControlError":
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_legacy_ocr_control_bug",
            True,
            "retry",
            1.0,
            exit_code,
        )
    if error_type == "PdfChildReportedError" and message.startswith(
        ("memoryerror:", "permissionerror:")
    ):
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_legacy_child_resource_failure",
            True,
            "retry",
            0.98,
            exit_code,
        )
    if is_ocr_scale_retryable_failure(RuntimeError(error_message)):
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_ocr_resource_failure",
            True,
            "retry",
            0.95,
            exit_code,
        )
    if error_type == "PdfChildReportedError" and is_structural_pdf_failure(
        error_type, error_message
    ):
        # Legacy rows predate the repair backend. Admit exactly the bounded
        # retry that will rewrite their stored type to the original child type.
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_legacy_structural_retry",
            True,
            "retry",
            0.98,
            exit_code,
        )
    if error_type == "PdfStructuralRecoveryFailed":
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_unrecoverable_structural_damage",
            False,
            "deletion_candidate",
            0.99,
            exit_code,
        )
    if is_structural_pdf_failure(error_type, error_message):
        return PdfFailureDiagnostic(
            error_type,
            phase,
            "pdf_structural_damage",
            False,
            "manual_review",
            0.98,
            exit_code,
        )
    return PdfFailureDiagnostic(
        error_type,
        phase,
        "pdf_unclassified_failure",
        False,
        "manual_review",
        0.6,
        exit_code,
    )


# This predicate is intentionally narrow. Limits that remain unchanged between
# runs (render/page budgets) and structurally invalid pages are not transient.
PDF_RETRYABLE_PAGE_ERROR_SQL = """(
    error_type='PermissionError'
    OR error_type='MemoryError'
    OR error_type='LegacyOcrControlError'
    OR (
        error_type='AttributeError'
        AND error_message='''BoundedSemaphore'' object has no attribute ''get'''
    )
    OR (
        error_type='RuntimeError'
        AND (
            error_message LIKE 'Tesseract process timeout%'
            OR error_message LIKE '%malloc (% failed%'
            OR error_message LIKE '%out of memory%'
        )
    )
    OR (
        error_type='TesseractError'
        AND (
            error_message LIKE '%out of memory%'
            OR error_message LIKE '%pixdata_malloc%'
            OR error_message LIKE '%3221225477%'
            OR error_message LIKE '%3221225725%'
            OR error_message LIKE '%1073741845%'
        )
    )
)"""


def is_retryable_pdf_document_error(error_type: str, error_message: str) -> bool:
    """Classify only failures that can plausibly change without file mutation."""

    return classify_pdf_failure(error_type, error_message).retryable


def is_ocr_scale_retryable_failure(exc: Exception) -> bool:
    """Recognize OCR failures for which a smaller render changes the attempt."""

    if isinstance(exc, MemoryError):
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "out of memory",
            "bad_alloc",
            "pixdata_malloc",
            "malloc (",
            "3221225477",
            "3221225725",
            "1073741845",
            "tesseract process timeout",
        )
    )


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.retry_policy")


# endregion [02]
