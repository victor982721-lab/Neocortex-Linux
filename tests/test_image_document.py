from __future__ import annotations

import inspect
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from _04_Nucleo_Operativo.bounded_subprocess import SubprocessOutputLimitError
from _04_Nucleo_Operativo.image_document import (
    DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES,
    DOCUMENT_OCR_MEMORY_BYTES,
    DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES,
    DOCUMENT_OCR_TSV_MAX_BYTES,
    DocumentTextEvidence,
    DocumentVerifierRuntime,
    verify_document_text,
)


# region [01] Bounded OCR fixtures


RUNTIME = DocumentVerifierRuntime(
    enabled=True,
    lang="spa+eng",
    timeout_seconds=12.0,
    tesseract_cmd="tesseract-test",
    tessdata_dir=None,
    signature="test-document-verifier",
    provenance="test-tesseract",
)


def _tsv_result(words: list[str]) -> subprocess.CompletedProcess[bytes]:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )
    rows = [header]
    for index, word in enumerate(words, start=1):
        rows.append(
            f"5\t1\t1\t1\t{((index - 1) // 8) + 1}\t{index}\t1\t1\t20\t10\t92.5\t{word}"
        )
    return subprocess.CompletedProcess(
        args=["tesseract-test"],
        returncode=0,
        stdout=("\n".join(rows) + "\n").encode("utf-8"),
        stderr=b"",
    )


def _detailed_tsv_result(
    rows: list[tuple[str, str, int, int, int, int, int]],
) -> subprocess.CompletedProcess[bytes]:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )
    encoded_rows = [header]
    for index, (word, confidence, block, paragraph, line, width, height) in enumerate(
        rows,
        start=1,
    ):
        encoded_rows.append(
            f"5\t1\t{block}\t{paragraph}\t{line}\t{index}\t1\t1\t{width}\t"
            f"{height}\t{confidence}\t{word}"
        )
    return subprocess.CompletedProcess(
        args=["tesseract-test"],
        returncode=0,
        stdout=("\n".join(encoded_rows) + "\n").encode("utf-8"),
        stderr=b"",
    )


def _image(root: Path) -> Path:
    path = root / "subestación_ñ.png"
    with Image.new("RGB", (640, 480), "white") as image:
        image.save(path)
    return path


# endregion [01]


# region [02] Unicode and retention limit


class ImageDocumentTextTests(unittest.TestCase):
    def test_public_signature_and_disabled_result_are_frozen(self) -> None:
        self.assertEqual(
            str(inspect.signature(verify_document_text)),
            "(path: 'Path', runtime: 'DocumentVerifierRuntime', memory_gate=None) "
            "-> 'DocumentTextEvidence'",
        )
        disabled = replace(
            RUNTIME,
            enabled=False,
            unavailable_reason="disabled_by_configuration",
        )

        evidence = verify_document_text(Path("missing.png"), disabled, object())

        self.assertEqual(
            evidence,
            DocumentTextEvidence(
                attempted=False,
                available=False,
                error_type="VerifierUnavailable",
                error_message="disabled_by_configuration",
            ),
        )

    def test_phase_order_keeps_decode_and_subprocess_inside_memory_admission(
        self,
    ) -> None:
        events: list[object] = []

        class RecordingGate:
            def admit(self, estimated_bytes: int) -> AbstractContextManager[None]:
                events.append(("admit", estimated_bytes))

                @contextmanager
                def admitted() -> Iterator[None]:
                    events.append("gate_enter")
                    try:
                        yield
                    finally:
                        events.append("gate_exit")

                return admitted()

        @contextmanager
        def decode_scope(*, allow_truncated: bool) -> Iterator[None]:
            events.append(("decode_enter", allow_truncated))
            try:
                yield
            finally:
                events.append("decode_exit")

        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            events.append("subprocess")
            return _tsv_result(["factura"])

        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with (
                patch(
                    "_04_Nucleo_Operativo.image_document.pillow_decode_scope",
                    new=decode_scope,
                ),
                patch(
                    "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                    side_effect=run,
                ),
            ):
                evidence = verify_document_text(path, RUNTIME, RecordingGate())

        self.assertTrue(evidence.available)
        self.assertEqual(
            events,
            [
                ("admit", DOCUMENT_OCR_MEMORY_BYTES),
                "gate_enter",
                ("decode_enter", False),
                "decode_exit",
                "subprocess",
                "gate_exit",
            ],
        )

    def test_admission_cancellation_propagates_with_exact_identity(self) -> None:
        failure = RuntimeError("cancel admission")

        class CancelledGate:
            def admit(self, _estimated_bytes: int) -> AbstractContextManager[None]:
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            verify_document_text(Path("unused.png"), RUNTIME, CancelledGate())

        self.assertIs(caught.exception, failure)

    def test_base_exception_from_subprocess_propagates_after_releasing_gate(
        self,
    ) -> None:
        released = False
        failure = KeyboardInterrupt()

        class RecordingGate:
            @contextmanager
            def admit(self, _estimated_bytes: int) -> Iterator[None]:
                nonlocal released
                try:
                    yield
                finally:
                    released = True

        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                side_effect=failure,
            ):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    verify_document_text(path, RUNTIME, RecordingGate())

        self.assertIs(caught.exception, failure)
        self.assertTrue(released)

    def test_subprocess_contract_and_tessdata_order_are_exact(self) -> None:
        runtime = replace(RUNTIME, tessdata_dir="C:/tess data")
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                return_value=_tsv_result(["factura"]),
            ) as run:
                evidence = verify_document_text(path, runtime)

        self.assertTrue(evidence.available)
        self.assertEqual(
            run.call_args.args[0],
            [
                "tesseract-test",
                "stdin",
                "stdout",
                "-l",
                "spa+eng",
                "--psm",
                "11",
                "--tessdata-dir",
                "C:/tess data",
                "tsv",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 12.0)
        self.assertEqual(
            run.call_args.kwargs["stdout_limit_bytes"],
            DOCUMENT_OCR_TSV_MAX_BYTES,
        )
        self.assertEqual(
            run.call_args.kwargs["stderr_limit_bytes"],
            DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES,
        )

    def test_tsv_aggregation_is_complete_and_rejects_low_confidence_rows(
        self,
    ) -> None:
        result = _detailed_tsv_result(
            [
                ("Factura", "90", 1, 1, 1, 40, 10),
                ("transformador", "70", 1, 1, 1, 60, 10),
                ("mantenimiento", "50", 1, 1, 2, 70, 10),
                ("EPP", "30", 1, 1, 2, 20, 10),
                ("ignorado", "29.9", 1, 1, 3, 100, 10),
                ("invalido", "not-a-number", 1, 1, 4, 100, 10),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                return_value=result,
            ):
                evidence = verify_document_text(path, RUNTIME)

        self.assertEqual(evidence.recognized_text, "Factura transformador mantenimiento EPP")
        self.assertEqual(evidence.word_count, 4)
        self.assertEqual(evidence.line_count, 2)
        self.assertEqual(evidence.character_count, 36)
        self.assertEqual(evidence.mean_confidence, 60.0)
        self.assertEqual(evidence.text_coverage, 0.00618)
        self.assertEqual(evidence.document_terms, ("factura",))
        self.assertEqual(evidence.industrial_entities, ("transformador",))
        self.assertEqual(evidence.industrial_activities, ("mantenimiento",))
        self.assertEqual(evidence.industrial_safety_conditions, ("epp",))

    def test_nonzero_exit_becomes_bounded_unavailable_evidence(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["tesseract-test"],
            returncode=7,
            stdout=b"",
            stderr=b"diagnostic from tesseract",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                return_value=failed,
            ):
                evidence = verify_document_text(path, RUNTIME)

        self.assertTrue(evidence.attempted)
        self.assertFalse(evidence.available)
        self.assertEqual(evidence.provenance, "test-tesseract")
        self.assertEqual(evidence.error_type, "RuntimeError")
        self.assertEqual(evidence.error_message, "diagnostic from tesseract")

    def test_preserves_unicode_words_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            words = ["Subestación", "eléctrica", "número", "tres"]

            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                return_value=_tsv_result(words),
            ):
                evidence = verify_document_text(path, RUNTIME)

            self.assertTrue(evidence.available)
            self.assertEqual(evidence.recognized_text, " ".join(words))
            self.assertFalse(evidence.recognized_text_truncated)
            self.assertEqual(evidence.word_count, len(words))
            self.assertEqual(
                evidence.character_count,
                sum(len(word) for word in words),
            )

    def test_retained_text_is_a_whole_word_bounded_utf8_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            word = "á" * 32
            words = [word] * 400

            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                return_value=_tsv_result(words),
            ):
                evidence = verify_document_text(path, RUNTIME)

            retained = evidence.recognized_text.encode("utf-8")
            self.assertLessEqual(
                len(retained),
                DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES,
            )
            self.assertTrue(evidence.recognized_text_truncated)
            self.assertEqual(evidence.word_count, len(words))
            self.assertTrue(evidence.recognized_text)
            self.assertTrue(
                all(value == word for value in evidence.recognized_text.split(" "))
            )

    def test_output_overflow_becomes_bounded_unavailable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _image(Path(temporary))
            with patch(
                "_04_Nucleo_Operativo.image_document.run_bounded_capture",
                side_effect=SubprocessOutputLimitError("stdout", 1024),
            ):
                evidence = verify_document_text(path, RUNTIME)

            self.assertTrue(evidence.attempted)
            self.assertFalse(evidence.available)
            self.assertEqual(evidence.error_type, "SubprocessOutputLimitError")
            self.assertIn("1024 bytes", evidence.error_message or "")


# endregion [02]


if __name__ == "__main__":
    unittest.main()
