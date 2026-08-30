# region [00] Contexto del módulo
# Módulo: tests/test_retry_policy.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import unittest

from neocortex.runtime.control.retry_policy import (
    PDF_MAX_AUTOMATIC_RETRIES,
    automatic_retry_due,
    classify_pdf_failure,
    is_ocr_scale_retryable_failure,
    is_retryable_pdf_document_error,
    retry_delay_seconds,
)
# endregion [01]

# region [02] Implementación


class RetryPolicyTests(unittest.TestCase):
    def test_automatic_retry_honors_backoff_and_hard_limit(self) -> None:
        self.assertTrue(automatic_retry_due(0, None, now_ns=100))
        self.assertFalse(automatic_retry_due(1, 101, now_ns=100))
        self.assertTrue(automatic_retry_due(1, 100, now_ns=100))
        self.assertFalse(
            automatic_retry_due(PDF_MAX_AUTOMATIC_RETRIES, None, now_ns=100)
        )
        self.assertEqual(retry_delay_seconds(1), 3600)
        self.assertEqual(retry_delay_seconds(20), 24 * 3600)

    def test_pdf_document_taxonomy_excludes_structural_corruption(self) -> None:
        self.assertTrue(
            is_retryable_pdf_document_error(
                "PdfChildProcessError",
                "extractor exited with code 1",
            )
        )
        self.assertTrue(
            is_retryable_pdf_document_error(
                "OperationalError",
                "database is locked",
            )
        )
        self.assertFalse(
            is_retryable_pdf_document_error(
                "PSEOF",
                "PSEOF: Unexpected EOF",
            )
        )
        self.assertTrue(
            is_retryable_pdf_document_error(
                "PdfDocumentTimeout",
                "PDF extraction exceeded 600 seconds",
            )
        )
        self.assertTrue(
            is_retryable_pdf_document_error(
                "PdfChildReportedError",
                "PSEOF: Unexpected EOF",
            )
        )

    def test_pdf_diagnostic_preserves_phase_exit_and_disposition(self) -> None:
        child = classify_pdf_failure(
            "PdfChildExitError",
            "extractor exited",
            phase="ocr",
            exit_code=1,
        )
        self.assertEqual(child.phase, "ocr")
        self.assertEqual(child.exit_code, 1)
        self.assertTrue(child.retryable)
        self.assertEqual(child.recommendation, "retry")

        protected = classify_pdf_failure(
            "EncryptedPdf", "password required", phase="open"
        )
        self.assertEqual(protected.recommendation, "keep_protected")

        recovered = classify_pdf_failure(
            "PSEOF",
            "Unexpected EOF",
            phase="structural_recovery",
            recovered=True,
        )
        self.assertFalse(recovered.retryable)
        self.assertEqual(recovered.recommendation, "manual_review")

        unrecoverable = classify_pdf_failure(
            "PdfStructuralRecoveryFailed",
            "Unexpected EOF; qpdf failed; pdfminer PSEOF",
            phase="pdfminer_recovery",
        )
        self.assertFalse(unrecoverable.retryable)
        self.assertEqual(
            unrecoverable.reason_code,
            "pdf_unrecoverable_structural_damage",
        )
        self.assertEqual(unrecoverable.recommendation, "deletion_candidate")

    def test_ocr_memory_taxonomy_is_narrow(self) -> None:
        self.assertTrue(is_ocr_scale_retryable_failure(MemoryError()))
        self.assertTrue(
            is_ocr_scale_retryable_failure(RuntimeError("pixdata_malloc fail"))
        )
        self.assertTrue(
            is_ocr_scale_retryable_failure(RuntimeError("Tesseract process timeout"))
        )
        self.assertFalse(
            is_ocr_scale_retryable_failure(RuntimeError("invalid PDF object"))
        )


if __name__ == "__main__":
    unittest.main()
# endregion [02]
