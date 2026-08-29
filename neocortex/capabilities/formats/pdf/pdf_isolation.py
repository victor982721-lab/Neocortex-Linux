"""Process-isolated, streaming PDF extraction with enforceable document deadlines."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import multiprocessing
import math
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from neocortex.deduplication import FileSnapshot, stat_matches_snapshot

from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.isolated_process import (
    close_isolated_process as _close_process_handles,
    isolated_spawn_process,
    terminate_isolated_process as _terminate_process_tree,
)
from _04_Nucleo_Operativo.retry_policy import (
    classify_pdf_failure,
    is_ocr_scale_retryable_failure,
)
from .pdf_route_models import (
    PDF_PAGE_SEQUENCE_ERROR_LIMIT,
    STRUCTURAL_RECOVERY_VERSION,
)
from _04_Nucleo_Operativo.ocr_image_preprocess import orient_and_deskew
from _04_Nucleo_Operativo.ocr_profiles import (
    OcrOrientation,
    OcrProfileName,
    contains_traditional_han,
    native_text_quality,
    parse_language_spec,
    parse_osd_output,
    resolve_ocr_profile,
    route_ocr_languages,
    should_use_ocr_fallback,
)
from _04_Nucleo_Operativo.sqlite_paths import readonly_sqlite_uri


# region [01] Process protocol
# Messages remain small and page-scoped so the queue bounds cross-process memory.

ExtractionMessage = tuple
MAX_CONSECUTIVE_PAGE_ERRORS = PDF_PAGE_SEQUENCE_ERROR_LIMIT
OCR_PAGE_PROVENANCE_SCHEMA = "neocortex.ocr-page/v1"


@dataclass(frozen=True, slots=True)
class IsolatedExtractionConfig:
    ocr_mode: Literal["auto", "never", "always"]
    ocr_lang: str
    dpi: int
    min_page_chars: int
    max_page_text_chars: int
    max_render_pixels: int
    max_ocr_pages: int | None
    ocr_timeout_seconds: int
    pdfminer_fallback: bool
    max_pages: int | None
    page_start: int | None
    page_end: int | None
    fail_fast_pages: bool
    skip_before: int
    only_pages: frozenset[int]
    prior_ocr_pages: int
    tesseract_cmd: str | None
    tessdata_dir: str | None
    ocr_scale_factor: float = 1.0
    structural_recovery_reason: str | None = None
    ocr_profile: OcrProfileName = "configured"
    ocr_processing_signature: str | None = None
    ocr_traineddata_hashes: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class PdfOcrPageResult:
    text: str
    provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PdfOcrAttempt:
    languages: tuple[str, ...]
    page_segmentation_mode: int
    text: str
    character_count: int
    mean_confidence: float

    @property
    def quality_score(self) -> tuple[int, float, int]:
        conservative_failure = should_use_ocr_fallback(
            recognized_text=self.text,
            character_count=self.character_count,
            mean_confidence=self.mean_confidence,
        )
        return (
            int(not conservative_failure),
            round(self.mean_confidence, 3),
            self.character_count,
        )


class PdfDocumentTimeout(TimeoutError):
    """The isolated document process exceeded its hard wall-clock deadline."""

    def __init__(self, message: str, *, phase: str = "supervision") -> None:
        self.phase = phase
        super().__init__(message)


class PdfChildProcessError(RuntimeError):
    """The isolated extractor failed before producing a complete stream."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        phase: str = "unknown",
        memory_limit_bytes: int | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.phase = phase
        self.memory_limit_bytes = memory_limit_bytes
        super().__init__(message)


class PdfChildReportedError(RuntimeError):
    """A child completed its error protocol and reported the original cause."""

    def __init__(
        self,
        child_error_type: str,
        detail: str,
        *,
        phase: str = "unknown",
    ) -> None:
        self.child_error_type = child_error_type
        self.detail = detail
        self.phase = phase
        super().__init__(f"{child_error_type}: {detail}")


class PdfStructuralRecoveryFailed(RuntimeError):
    """All bounded structural recovery engines rejected the source."""


class PdfPageSequenceAborted(RuntimeError):
    """A malformed page tree exceeded the bounded consecutive-error limit."""


# endregion [01]


# region [02] Child extraction
# The child never opens SQLite; only the parent promotes streamed results atomically.


def _normalized_length(text: str) -> int:  # pyright: ignore[reportUnusedFunction]
    return len(" ".join(text.casefold().split()))


def _source_matches(snapshot: FileSnapshot) -> bool:
    stat = os.stat(snapshot.path, follow_symlinks=False)
    return stat_matches_snapshot(snapshot, stat)


def _ocr_scales(initial_scale: float) -> tuple[float, ...]:
    scales: list[float] = []
    for candidate in (initial_scale, max(1.0, initial_scale * 0.75), 1.0):
        if not scales or abs(scales[-1] - candidate) > 0.01:
            scales.append(candidate)
    return tuple(scales)


def _run_tesseract(pytesseract, image, config: IsolatedExtractionConfig) -> str:
    for attempt in range(2):
        try:
            return pytesseract.image_to_string(
                image,
                lang=config.ocr_lang,
                timeout=config.ocr_timeout_seconds,
                config=(f'--tessdata-dir "{config.tessdata_dir}"' if config.tessdata_dir else ""),
            )
        except PermissionError as exc:
            if attempt or getattr(exc, "winerror", None) != 32:
                raise
            time.sleep(0.25)
    raise RuntimeError("unreachable OCR retry state")


def _profile_tesseract_config(
    config: IsolatedExtractionConfig,
    *,
    page_segmentation_mode: int,
) -> str:
    values = [f"--psm {page_segmentation_mode}"]
    if config.tessdata_dir:
        values.append(f'--tessdata-dir "{config.tessdata_dir}"')
    return " ".join(values)


def _run_tesseract_osd(
    pytesseract,
    image,
    config: IsolatedExtractionConfig,
) -> OcrOrientation:
    for attempt in range(2):
        try:
            output = pytesseract.image_to_osd(
                image,
                timeout=config.ocr_timeout_seconds,
                config=_profile_tesseract_config(
                    config,
                    page_segmentation_mode=0,
                ),
            )
            return parse_osd_output(str(output))
        except PermissionError as exc:
            if attempt or getattr(exc, "winerror", None) != 32:
                raise
            time.sleep(0.25)
    raise RuntimeError("unreachable OSD retry state")


def _run_tesseract_attempt(
    pytesseract,
    image,
    config: IsolatedExtractionConfig,
    *,
    languages: tuple[str, ...],
    page_segmentation_mode: int,
) -> _PdfOcrAttempt:
    for attempt in range(2):
        try:
            data = pytesseract.image_to_data(
                image,
                lang="+".join(languages),
                timeout=config.ocr_timeout_seconds,
                config=_profile_tesseract_config(
                    config,
                    page_segmentation_mode=page_segmentation_mode,
                ),
                output_type=pytesseract.Output.DICT,
            )
            break
        except PermissionError as exc:
            if attempt or getattr(exc, "winerror", None) != 32:
                raise
            time.sleep(0.25)
    else:  # pragma: no cover - bounded loop invariant
        raise RuntimeError("unreachable OCR data retry state")
    if not isinstance(data, dict):
        raise TypeError("pytesseract OCR data result must be a mapping")
    raw_words = data.get("text", ())
    raw_confidences = data.get("conf", ())
    if not isinstance(raw_words, (list, tuple)):
        raise TypeError("pytesseract OCR text data must be a sequence")
    if not isinstance(raw_confidences, (list, tuple)):
        raw_confidences = ()
    words: list[str] = []
    confidences: list[float] = []
    for index, raw_word in enumerate(raw_words):
        word = str(raw_word or "").strip()
        if not word:
            continue
        confidence = 0.0
        if index < len(raw_confidences):
            try:
                confidence = float(raw_confidences[index])
            except (TypeError, ValueError):
                confidence = 0.0
        if confidence < 0:
            continue
        words.append(word)
        confidences.append(confidence)
    text = " ".join(words)
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return _PdfOcrAttempt(
        languages,
        page_segmentation_mode,
        text,
        sum(len(word) for word in words),
        round(mean_confidence, 3),
    )


def _unavailable_osd(exc: Exception) -> OcrOrientation:
    message = str(exc).encode("utf-8", "replace").decode("utf-8")[:500]
    return OcrOrientation(
        unavailable_reason=f"{type(exc).__name__}: {message}",
    )


def _traineddata_provenance(
    config: IsolatedExtractionConfig,
) -> list[dict[str, object]]:
    return [
        {"language": language, "xxh3_128": digest}
        for language, digest in config.ocr_traineddata_hashes
    ]


def _ocr_profile_result(
    pytesseract,
    image,
    config: IsolatedExtractionConfig,
    *,
    render_dpi: float,
) -> PdfOcrPageResult:
    plan = resolve_ocr_profile(config.ocr_profile, config.ocr_lang)
    try:
        orientation = _run_tesseract_osd(pytesseract, image, config)
    except Exception as exc:
        if is_ocr_scale_retryable_failure(exc):
            raise
        orientation = _unavailable_osd(exc)
    preprocessed = orient_and_deskew(
        image,
        rotation_clockwise_degrees=orientation.rotate_degrees,
    )
    try:
        decision = route_ocr_languages(plan, orientation)
        primary = _run_tesseract_attempt(
            pytesseract,
            preprocessed.image,
            config,
            languages=decision.primary_languages,
            page_segmentation_mode=6,
        )
        selected = primary
        fallback_attempted = False
        fallback_languages: tuple[str, ...] = ()
        fallback_reason: str | None = None
        fallback_error: dict[str, str] | None = None
        recognition_attempts = 1
        if decision.fallback_languages is not None:
            low_quality = should_use_ocr_fallback(
                recognized_text=primary.text,
                character_count=primary.character_count,
                mean_confidence=primary.mean_confidence,
            )
            traditional_signal = contains_traditional_han(primary.text)
            if low_quality or traditional_signal:
                fallback_attempted = True
                fallback_languages = decision.fallback_languages
                fallback_reason = (
                    "traditional_han_signal" if traditional_signal else "low_primary_quality"
                )
                recognition_attempts += 1
                try:
                    fallback = _run_tesseract_attempt(
                        pytesseract,
                        preprocessed.image,
                        config,
                        languages=decision.fallback_languages,
                        page_segmentation_mode=6,
                    )
                    if fallback.quality_score > primary.quality_score or (
                        traditional_signal and fallback.quality_score == primary.quality_score
                    ):
                        selected = fallback
                except Exception as exc:
                    if is_ocr_scale_retryable_failure(exc):
                        raise
                    fallback_error = {
                        "type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
        provenance: dict[str, object] = {
            "schema": OCR_PAGE_PROVENANCE_SCHEMA,
            "profile": config.ocr_profile,
            "configured_languages": list(plan.configured_languages),
            "requested_languages": list(plan.required_languages),
            "effective_languages": list(selected.languages),
            "traineddata": _traineddata_provenance(config),
            "processing_signature": config.ocr_processing_signature,
            "render_dpi": round(render_dpi, 3),
            "orientation_degrees": orientation.orientation_degrees,
            "rotation_degrees": orientation.rotate_degrees,
            "orientation_confidence": orientation.orientation_confidence,
            "detected_script": orientation.script,
            "script_confidence": orientation.script_confidence,
            "osd_available": orientation.available,
            "osd_unavailable_reason": orientation.unavailable_reason,
            "deskew_degrees": preprocessed.deskew_degrees,
            "page_segmentation_mode": selected.page_segmentation_mode,
            "mean_confidence": selected.mean_confidence,
            "fallback_attempted": fallback_attempted,
            "fallback_languages": list(fallback_languages),
            "fallback_reason": fallback_reason,
            "fallback_error": fallback_error,
            "recognition_attempts": recognition_attempts,
        }
        return PdfOcrPageResult(selected.text, provenance)
    finally:
        preprocessed.image.close()


def _ocr_page_result(
    page,
    fitz,
    config: IsolatedExtractionConfig,
    ocr_admission,
) -> PdfOcrPageResult:
    import pytesseract  # type: ignore[import-untyped]
    from PIL import Image

    if config.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd
    plan = resolve_ocr_profile(config.ocr_profile, config.ocr_lang)
    if not 0.0 < config.ocr_scale_factor <= 1.0:
        raise ValueError("OCR scale factor must be within (0, 1]")
    target_dpi = config.dpi if not plan.osd_enabled else max(300, config.dpi)
    requested_scale = max(1.0, target_dpi / 72.0 * config.ocr_scale_factor)
    base_pixels = max(1.0, float(page.rect.width) * float(page.rect.height))
    safe_scale = math.sqrt(config.max_render_pixels / base_pixels) * 0.999
    scale = min(requested_scale, safe_scale)
    if scale < 1.0:
        minimum_pixels = int(base_pixels)
        raise RuntimeError(
            f"OCR page requires {minimum_pixels} pixels even at 72 DPI; "
            f"limit={config.max_render_pixels}"
        )

    with ocr_admission:
        for current_scale in _ocr_scales(scale):
            render_pixels = int(page.rect.width * current_scale) * int(
                page.rect.height * current_scale
            )
            if render_pixels > config.max_render_pixels:
                raise RuntimeError(
                    f"OCR render would require {render_pixels} pixels; "
                    f"limit={config.max_render_pixels}"
                )
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(current_scale, current_scale),
                colorspace=fitz.csGRAY,
                alpha=False,
            )
            image = Image.frombytes(
                "L",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            del pixmap
            try:
                if not plan.osd_enabled:
                    text = _run_tesseract(pytesseract, image, config)
                    return PdfOcrPageResult(
                        text,
                        {
                            "schema": OCR_PAGE_PROVENANCE_SCHEMA,
                            "profile": config.ocr_profile,
                            "configured_languages": list(plan.configured_languages),
                            "requested_languages": list(plan.required_languages),
                            "effective_languages": list(parse_language_spec(config.ocr_lang)),
                            "traineddata": _traineddata_provenance(config),
                            "processing_signature": config.ocr_processing_signature,
                            "render_dpi": round(current_scale * 72.0, 3),
                            "orientation_degrees": 0,
                            "rotation_degrees": 0,
                            "orientation_confidence": 0.0,
                            "detected_script": "unknown",
                            "script_confidence": 0.0,
                            "osd_available": False,
                            "osd_unavailable_reason": ("osd_disabled_for_configured_profile"),
                            "deskew_degrees": 0.0,
                            "page_segmentation_mode": 3,
                            "mean_confidence": None,
                            "fallback_attempted": False,
                            "fallback_languages": [],
                            "fallback_reason": None,
                            "fallback_error": None,
                            "recognition_attempts": 1,
                        },
                    )
                return _ocr_profile_result(
                    pytesseract,
                    image,
                    config,
                    render_dpi=current_scale * 72.0,
                )
            except Exception as exc:
                if not is_ocr_scale_retryable_failure(exc) or current_scale <= 1.01:
                    raise
            finally:
                image.close()
    raise RuntimeError("unreachable adaptive OCR state")


def _ocr_page(  # pyright: ignore[reportUnusedFunction]
    page, fitz, config: IsolatedExtractionConfig, ocr_admission
) -> str:
    """Compatibility wrapper for callers that only consume recognized text."""

    return _ocr_page_result(page, fitz, config, ocr_admission).text


@contextmanager
def _remote_ocr_admission(channel, control_channel):
    """Borrow a parent-owned OCR lease without owning its semaphore token."""

    channel.put(("ocr_request",))
    response = control_channel.get()
    if response != ("ocr_granted",):
        raise RuntimeError("invalid OCR admission response")
    try:
        yield
    finally:
        channel.put(("ocr_release",))


def _page_bounds(page_count: int, config: IsolatedExtractionConfig) -> tuple[int, int]:
    start = 0 if config.page_start is None else config.page_start - 1
    end = page_count if config.page_end is None else min(page_count, config.page_end)
    if config.max_pages is not None:
        end = min(end, start + config.max_pages)
    if start >= page_count:
        raise ValueError(f"page range starts at {start + 1}, but document has {page_count} pages")
    return start, end


def _extract_with_pdfminer(
    snapshot: FileSnapshot,
    config,
    channel,
    *,
    recovery: dict[str, object],
) -> tuple[int, int, int]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    start = 0 if config.page_start is None else config.page_start - 1
    channel.put(
        (
            "header",
            0,
            start,
            -1,
            {
                "engine": "pdfminer",
                "fallback": True,
                "neocortex_recovery": recovery,
            },
        )
    )
    page_count = 0
    range_limit = config.page_end
    if config.max_pages is not None:
        range_limit = min(
            range_limit if range_limit is not None else 2**63 - 1,
            start + config.max_pages,
        )
    for page_number, layout in enumerate(extract_pages(snapshot.path)):
        if range_limit is not None and page_number >= range_limit:
            break
        page_count = page_number + 1
        if config.page_start is not None and page_number < config.page_start - 1:
            continue
        if page_number < config.skip_before or (
            config.only_pages and page_number not in config.only_pages
        ):
            continue
        try:
            text = "".join(
                element.get_text() for element in layout if isinstance(element, LTTextContainer)
            )
            if len(text) > config.max_page_text_chars:
                raise RuntimeError(
                    f"page text has {len(text)} characters; limit={config.max_page_text_chars}"
                )
            channel.put(("page", page_number, "pdfminer", text))
        except Exception as exc:
            channel.put(("page_error", page_number, type(exc).__name__, str(exc)[:2000]))
            if config.fail_fast_pages:
                raise
    if page_count == 0:
        raise RuntimeError("pdfminer produced no pages")
    start, end = _page_bounds(page_count, config)
    return page_count, start, end


def _mupdf_warning_summary(fitz) -> tuple[int, tuple[str, ...]]:
    """Drain native warnings into a small serializable summary."""

    raw = str(fitz.TOOLS.mupdf_warnings(reset=True) or "")
    lines = [
        line.strip()[:500].encode("utf-8", "backslashreplace").decode("utf-8")
        for line in raw.splitlines()
        if line.strip()
    ]
    return len(lines), tuple(lines[:20])


def _read_file_tail(path: Path, maximum_bytes: int) -> bytes:
    """Read at most ``maximum_bytes`` from the end of a diagnostics file."""

    if maximum_bytes < 1:
        return b""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - maximum_bytes), os.SEEK_SET)
        return stream.read(maximum_bytes)


@contextmanager
def _qpdf_repaired_copy(
    snapshot: FileSnapshot,
    config: IsolatedExtractionConfig,
    *,
    primary_error: str,
    fallback_error: str,
):
    """Yield a bounded temporary qpdf rewrite and always remove it afterward."""

    executable = shutil.which("qpdf")
    if executable is None:
        raise RuntimeError("qpdf recovery unavailable")
    timeout_seconds = max(30, min(300, config.ocr_timeout_seconds * 2))
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="neocortex_pdf_recovery_") as directory:
        root = Path(directory)
        output = root / "recovered.pdf"
        diagnostics = root / "qpdf.stderr"
        with diagnostics.open("wb") as stderr:
            try:
                completed = subprocess.run(
                    [executable, snapshot.path, str(output)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    check=False,
                    timeout=timeout_seconds,
                    creationflags=creation_flags,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"qpdf recovery exceeded {timeout_seconds} seconds") from exc
        sample = _read_file_tail(diagnostics, 8192).decode("utf-8", "replace")
        if completed.returncode not in {0, 2, 3} or not output.is_file():
            raise RuntimeError(
                f"qpdf recovery exited with code {completed.returncode}: {sample[-1000:]}"
            )
        output_size = output.stat().st_size
        maximum_size = max(snapshot.size * 2, snapshot.size + 64 * 1024 * 1024)
        if output_size < 5 or output_size > maximum_size:
            raise RuntimeError(
                f"qpdf recovery produced invalid size {output_size}; limit={maximum_size}"
            )
        with output.open("rb") as stream:
            if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                raise RuntimeError("qpdf recovery output has no PDF header")
        if not _source_matches(snapshot):
            raise RuntimeError("source metadata changed during qpdf recovery")
        yield (
            str(output),
            {
                "engine": "qpdf+pymupdf",
                "recovery_version": STRUCTURAL_RECOVERY_VERSION,
                "qpdf_exit_code": completed.returncode,
                "qpdf_warning_sample": sample[-2000:],
                "primary_error": primary_error[:1000],
                "fallback_error": fallback_error[:1000],
                "temporary_output_bytes": output_size,
            },
        )


class _ChildExtractionSession:
    """Stateful implementation of the child-side extraction protocol."""

    def __init__(self, snapshot, config, channel, ocr_control) -> None:
        self.snapshot = snapshot
        self.config = config
        self.channel = channel
        self.ocr_control = ocr_control
        self.document: Any = None
        self.fitz: Any = None
        self.header_sent = False
        self.warning_count = 0
        self.emitted_warning_count = 0
        self.warning_samples: list[str] = []
        self.phase = "startup"
        self.ocr_attempted = config.prior_ocr_pages

    def set_phase(self, value: str) -> None:
        self.phase = value
        self.channel.put(("phase", value))

    def drain_warnings(self) -> None:
        if self.fitz is None:
            return
        count, samples = _mupdf_warning_summary(self.fitz)
        self.warning_count += count
        for sample in samples:
            if sample not in self.warning_samples and len(self.warning_samples) < 20:
                self.warning_samples.append(sample)

    def emit_warnings(self) -> None:
        self.drain_warnings()
        delta = self.warning_count - self.emitted_warning_count
        if delta > 0:
            self.channel.put(("warnings", delta, tuple(self.warning_samples)))
            self.emitted_warning_count = self.warning_count

    def close_document(self) -> None:
        if self.document is not None:
            self.document.close()
            self.document = None

    def _initialize_engine(self) -> None:
        import fitz  # type: ignore[import-untyped]

        self.fitz = fitz
        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        fitz.TOOLS.reset_mupdf_warnings()

    def _page_text(self, page) -> tuple[str, str, dict[str, object]]:
        native_text = page.get_text("text") or ""
        quality = native_text_quality(
            native_text,
            min_characters=self.config.min_page_chars,
        )
        plan = resolve_ocr_profile(
            self.config.ocr_profile,
            self.config.ocr_lang,
        )
        source = "native"
        text = native_text
        should_ocr = self.config.ocr_mode == "always" or (
            self.config.ocr_mode == "auto" and not quality.usable
        )
        ocr_available = (
            self.config.max_ocr_pages is None or self.ocr_attempted < self.config.max_ocr_pages
        )
        provenance: dict[str, object] = {
            "schema": OCR_PAGE_PROVENANCE_SCHEMA,
            "profile": self.config.ocr_profile,
            "configured_languages": list(plan.configured_languages),
            "requested_languages": list(plan.required_languages),
            "effective_languages": [],
            "traineddata": _traineddata_provenance(self.config),
            "processing_signature": self.config.ocr_processing_signature,
            "native_text_quality": asdict(quality),
            "ocr_attempted": False,
            "ocr_selected": False,
        }
        if should_ocr and ocr_available:
            self.ocr_attempted += 1
            ocr_result = _ocr_page_result(
                page,
                self.fitz,
                self.config,
                _remote_ocr_admission(self.channel, self.ocr_control),
            )
            provenance = dict(ocr_result.provenance)
            provenance["native_text_quality"] = asdict(quality)
            provenance["ocr_attempted"] = True
            if ocr_result.text.strip() or self.config.ocr_mode == "always":
                text = ocr_result.text
                source = "ocr"
                provenance["ocr_selected"] = True
            else:
                provenance["ocr_selected"] = False
                provenance["ocr_not_selected_reason"] = "empty_ocr_result"
        elif should_ocr:
            provenance["ocr_skipped_reason"] = "max_ocr_pages_reached"
        else:
            provenance["ocr_skipped_reason"] = (
                "ocr_disabled" if self.config.ocr_mode == "never" else "native_text_usable"
            )
        return source, text, provenance

    def _emit_page_error(
        self,
        page_number: int,
        end: int,
        consecutive_errors: int,
        error: Exception,
        recovery: dict[str, object] | None,
    ) -> bool:
        self.channel.put(("page_error", page_number, type(error).__name__, str(error)[:2000]))
        if self.config.fail_fast_pages:
            raise error
        if consecutive_errors < MAX_CONSECUTIVE_PAGE_ERRORS:
            return False
        detail = (
            f"{consecutive_errors} consecutive page extraction failures; "
            f"last error {type(error).__name__}: {error}"
        )
        if self.config.structural_recovery_reason is not None or recovery is not None:
            raise PdfPageSequenceAborted(detail) from error
        self.channel.put(
            (
                "page_error_limit",
                page_number,
                consecutive_errors,
                max(0, end - page_number - 1),
                type(error).__name__,
                str(error)[:2000],
            )
        )
        return True

    def _extract_pages(
        self,
        start: int,
        end: int,
        recovery: dict[str, object] | None,
    ) -> None:
        consecutive_errors = 0
        for page_number in range(start, end):
            if page_number < self.config.skip_before or (
                self.config.only_pages and page_number not in self.config.only_pages
            ):
                continue
            try:
                page = self.document.load_page(page_number)
                source, text, provenance = self._page_text(page)
                if len(text) > self.config.max_page_text_chars:
                    raise RuntimeError(
                        f"page text has {len(text)} characters; "
                        f"limit={self.config.max_page_text_chars}"
                    )
                self.channel.put(("page", page_number, source, text, provenance))
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if self._emit_page_error(page_number, end, consecutive_errors, exc, recovery):
                    break
            finally:
                self.drain_warnings()

    def extract_with_pymupdf(
        self,
        path: str,
        *,
        recovery: dict[str, object] | None = None,
    ) -> None:
        self.set_phase("open_recovered" if recovery is not None else "open")
        self.document = self.fitz.open(path)
        self.drain_warnings()
        if self.document.needs_pass:
            protected_pages = int(self.document.page_count)
            self.close_document()
            self.emit_warnings()
            self.channel.put(("protected", protected_pages))
            return
        page_count = int(self.document.page_count)
        start, end = _page_bounds(page_count, self.config)
        metadata = dict(self.document.metadata or {})
        if recovery is not None:
            metadata["neocortex_recovery"] = recovery
            self.channel.put(("recovery", recovery))
        self.channel.put(("header", page_count, start, end, metadata))
        self.header_sent = True
        self.set_phase("page_extraction")
        self._extract_pages(start, end, recovery)
        if not _source_matches(self.snapshot):
            raise RuntimeError("source metadata changed during PDF extraction")
        self.close_document()
        self.emit_warnings()
        self.set_phase("complete")
        self.channel.put(("done", page_count, start, end))

    def _primary_failure_detail(self) -> str | None:
        if self.config.structural_recovery_reason is not None:
            return self.config.structural_recovery_reason
        try:
            self.extract_with_pymupdf(self.snapshot.path)
            return None
        except BaseException as primary_exc:
            if self.header_sent:
                raise
            self.drain_warnings()
            self.close_document()
            detail = f"{type(primary_exc).__name__}: {primary_exc}"
            diagnostic = classify_pdf_failure(
                type(primary_exc).__name__, str(primary_exc), phase=self.phase
            )
            if diagnostic.retryable:
                raise
            return detail

    def _qpdf_failure_detail(self, primary_detail: str) -> str | None:
        qpdf_error = "not attempted"
        try:
            self.set_phase("qpdf_recovery")
            with _qpdf_repaired_copy(
                self.snapshot,
                self.config,
                primary_error=primary_detail,
                fallback_error=qpdf_error,
            ) as (repaired_path, evidence):
                self.extract_with_pymupdf(repaired_path, recovery=evidence)
                return None
        except BaseException as repair_exc:
            self.drain_warnings()
            self.close_document()
            qpdf_error = f"{type(repair_exc).__name__}: {repair_exc}"
            if self.header_sent:
                if not isinstance(repair_exc, PdfPageSequenceAborted):
                    raise
                self.channel.put(("restart", "pdfminer_recovery", qpdf_error[:2000]))
                self.header_sent = False
            if not self.config.pdfminer_fallback:
                raise PdfStructuralRecoveryFailed(f"{primary_detail}; {qpdf_error}") from repair_exc
            return qpdf_error

    def _extract_with_pdfminer_recovery(
        self,
        primary_detail: str,
        qpdf_error: str,
    ) -> None:
        try:
            self.set_phase("pdfminer_recovery")
            recovery: dict[str, object] = {
                "engine": "pdfminer",
                "recovery_version": STRUCTURAL_RECOVERY_VERSION,
                "primary_error": primary_detail[:1000],
                "qpdf_error": qpdf_error[:1000],
            }
            self.channel.put(("recovery", recovery))
            page_count, start, end = _extract_with_pdfminer(
                self.snapshot,
                self.config,
                self.channel,
                recovery=recovery,
            )
            self.emit_warnings()
            self.set_phase("complete")
            self.channel.put(("done", page_count, start, end))
        except BaseException as fallback_exc:
            raise PdfStructuralRecoveryFailed(
                f"{primary_detail}; {qpdf_error}; pdfminer "
                f"{type(fallback_exc).__name__}: {fallback_exc}"
            ) from fallback_exc

    def run(self) -> None:
        if not _source_matches(self.snapshot):
            raise RuntimeError("source metadata changed before PDF extraction")
        self._initialize_engine()
        primary_detail = self._primary_failure_detail()
        if primary_detail is None:
            return
        qpdf_error = self._qpdf_failure_detail(primary_detail)
        if qpdf_error is None:
            return
        self._extract_with_pdfminer_recovery(primary_detail, qpdf_error)


def _extract_child(snapshot, config, channel, ocr_control) -> None:
    session = _ChildExtractionSession(snapshot, config, channel, ocr_control)
    try:
        session.run()
    except BaseException as exc:
        session.emit_warnings()
        diagnostic = classify_pdf_failure(type(exc).__name__, str(exc), phase=session.phase)
        channel.put(
            (
                "fatal",
                type(exc).__name__,
                str(exc)[:2000],
                session.phase,
                diagnostic.retryable,
                diagnostic.recommendation,
                diagnostic.reason_code,
            )
        )
    finally:
        session.close_document()


def _profile_child(path: str, state_path: str, file_key: str, channel) -> None:
    warning_count = 0
    warning_samples: list[str] = []

    def drain_warnings() -> None:
        nonlocal warning_count
        try:
            tool = fitz
        except (NameError, UnboundLocalError):
            return
        count, samples = _mupdf_warning_summary(tool)
        warning_count += count
        for sample in samples:
            if sample not in warning_samples and len(warning_samples) < 20:
                warning_samples.append(sample)

    def emit_warnings() -> None:
        drain_warnings()
        if warning_count:
            channel.put(("warnings", warning_count, tuple(warning_samples)))

    try:
        import fitz

        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        fitz.TOOLS.reset_mupdf_warnings()

        from .pdf_layout import LAYOUT_VERSION
        from .pdf_profile import profile_page

        with (
            fitz.open(path) as document,
            closing(
                sqlite3.connect(
                    readonly_sqlite_uri(Path(state_path)),
                    uri=True,
                    timeout=60,
                )
            ) as connection,
        ):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=60000")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("PDF profile reader could not enable foreign keys")
            rows = connection.execute(
                """SELECT p.page_number FROM pages p
                LEFT JOIN page_layouts l ON l.file_key=p.file_key
                    AND l.page_number=p.page_number AND l.algorithm_version=?
                WHERE p.file_key=? AND (p.profile_json IS NULL OR l.file_key IS NULL)
                ORDER BY p.page_number""",
                (LAYOUT_VERSION, file_key),
            )
            for (page_number,) in rows:
                number = int(page_number)
                try:
                    profile = profile_page(document.load_page(number))
                    channel.put(("page", number, profile))
                finally:
                    drain_warnings()
        emit_warnings()
        channel.put(("done",))
    except BaseException as exc:
        emit_warnings()
        channel.put(("fatal", type(exc).__name__, str(exc)[:2000]))


# endregion [02]


# region [03] Parent supervision
# Terminate only the supervised child job; never target a process by name or PID lookup.


class _ParentOcrLease:
    """Keep the semaphore token in the supervisor so child death cannot leak it."""

    def __init__(self, slots) -> None:
        self._slots = slots
        self.acquired = False

    def acquire(
        self,
        *,
        process,
        deadline: float,
        path: str,
        cancellation: CancellationToken | None,
    ) -> None:
        if self.acquired:
            raise PdfChildProcessError("PDF child requested two OCR leases")
        while True:
            if cancellation is not None:
                cancellation.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfDocumentTimeout(
                    f"PDF extraction exceeded its deadline waiting for an OCR slot: {path}"
                )
            if self._slots.acquire(timeout=min(0.1, remaining)):
                self.acquired = True
                return
            if not process.is_alive():
                raise PdfChildProcessError(
                    f"PDF extractor exited with code {process.exitcode}: {path}",
                    exit_code=process.exitcode,
                    phase="ocr_admission",
                )

    def release(self) -> None:
        if self.acquired:
            self._slots.release()
            self.acquired = False


def _next_extraction_message(
    channel,
    process,
    snapshot: FileSnapshot,
    *,
    deadline: float,
    timeout_seconds: float,
    last_phase: str,
    cancellation: CancellationToken | None,
    memory_limit_bytes: int | None,
) -> ExtractionMessage | None:
    if cancellation is not None:
        cancellation.checkpoint()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PdfDocumentTimeout(
            f"PDF extraction exceeded {timeout_seconds:g} seconds: {snapshot.path}",
            phase=last_phase,
        )
    try:
        return channel.get(timeout=min(0.2, remaining))
    except queue.Empty:
        if not process.is_alive():
            raise PdfChildProcessError(
                f"PDF extractor exited with code {process.exitcode}: {snapshot.path}",
                exit_code=process.exitcode,
                phase=last_phase,
                memory_limit_bytes=memory_limit_bytes,
            ) from None
        return None


def _handle_extraction_control(
    message: ExtractionMessage,
    *,
    process,
    control_channel,
    ocr_lease: _ParentOcrLease,
    deadline: float,
    path: str,
    cancellation: CancellationToken | None,
    last_phase: str,
) -> tuple[bool, str]:
    kind = message[0]
    if kind == "phase":
        return True, str(message[1])
    if kind == "ocr_request":
        ocr_lease.acquire(
            process=process,
            deadline=deadline,
            path=path,
            cancellation=cancellation,
        )
        control_channel.put(("ocr_granted",))
        return True, "ocr"
    if kind == "ocr_release":
        if not ocr_lease.acquired:
            raise PdfChildProcessError("PDF child released an OCR lease it did not own")
        ocr_lease.release()
        return True, last_phase
    return False, last_phase


def _await_extraction_shutdown(
    process,
    snapshot: FileSnapshot,
    *,
    cancellation: CancellationToken | None,
    last_phase: str,
    memory_limit_bytes: int | None,
) -> None:
    join_deadline = time.monotonic() + 5
    while process.is_alive() and time.monotonic() < join_deadline:
        if cancellation is not None:
            cancellation.checkpoint()
        process.join(timeout=0.1)
    if process.is_alive():
        raise PdfDocumentTimeout(
            f"PDF extractor did not exit after completion: {snapshot.path}",
            phase="shutdown",
        )
    if process.exitcode not in {0, None}:
        raise PdfChildProcessError(
            f"PDF extractor exited with code {process.exitcode}: {snapshot.path}",
            exit_code=process.exitcode,
            phase=last_phase,
            memory_limit_bytes=memory_limit_bytes,
        )


def _close_extraction_supervisor(
    process,
    ocr_lease: _ParentOcrLease,
    channel,
    control_channel,
) -> None:
    try:
        _terminate_process_tree(process)
    finally:
        try:
            ocr_lease.release()
        finally:
            for current_channel in (channel, control_channel):
                current_channel.close()
                current_channel.cancel_join_thread()
            _close_process_handles(process)


def stream_isolated_extraction(
    snapshot: FileSnapshot,
    config: IsolatedExtractionConfig,
    *,
    timeout_seconds: float,
    ocr_slots,
    cancellation: CancellationToken | None = None,
    memory_limit_bytes: int | None = None,
) -> Iterator[ExtractionMessage]:
    if cancellation is not None:
        cancellation.checkpoint()
    context = multiprocessing.get_context("spawn")
    channel = context.Queue(maxsize=2)
    control_channel = context.Queue(maxsize=1)
    process = isolated_spawn_process(
        target=_extract_child,
        args=(snapshot, config, channel, control_channel),
        daemon=False,
        memory_limit_bytes=memory_limit_bytes,
    )
    process.start()
    deadline = time.monotonic() + timeout_seconds
    complete = False
    ocr_lease = _ParentOcrLease(ocr_slots)
    last_phase = "startup"
    try:
        while not complete:
            message = _next_extraction_message(
                channel,
                process,
                snapshot,
                deadline=deadline,
                timeout_seconds=timeout_seconds,
                last_phase=last_phase,
                cancellation=cancellation,
                memory_limit_bytes=memory_limit_bytes,
            )
            if message is None:
                continue
            handled, last_phase = _handle_extraction_control(
                message,
                process=process,
                control_channel=control_channel,
                ocr_lease=ocr_lease,
                deadline=deadline,
                path=snapshot.path,
                cancellation=cancellation,
                last_phase=last_phase,
            )
            if handled:
                continue
            yield message
            complete = message[0] in {"done", "protected", "fatal"}
        _await_extraction_shutdown(
            process,
            snapshot,
            cancellation=cancellation,
            last_phase=last_phase,
            memory_limit_bytes=memory_limit_bytes,
        )
    finally:
        _close_extraction_supervisor(process, ocr_lease, channel, control_channel)


def stream_isolated_profiles(
    path: str,
    state_path: str,
    file_key: str,
    *,
    timeout_seconds: float,
    cancellation: CancellationToken | None = None,
    memory_limit_bytes: int | None = None,
) -> Iterator[ExtractionMessage]:
    if cancellation is not None:
        cancellation.checkpoint()
    context = multiprocessing.get_context("spawn")
    channel = context.Queue(maxsize=2)
    process = isolated_spawn_process(
        target=_profile_child,
        args=(path, state_path, file_key, channel),
        daemon=False,
        memory_limit_bytes=memory_limit_bytes,
    )
    process.start()
    deadline = time.monotonic() + timeout_seconds
    complete = False
    try:
        while not complete:
            if cancellation is not None:
                cancellation.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfDocumentTimeout(
                    f"PDF profiling exceeded {timeout_seconds:g} seconds: {path}"
                )
            try:
                message = channel.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if not process.is_alive():
                    raise PdfChildProcessError(
                        f"PDF profiler exited with code {process.exitcode}: {path}"
                    ) from None
                continue
            yield message
            complete = message[0] in {"done", "fatal"}
        join_deadline = time.monotonic() + 5
        while process.is_alive() and time.monotonic() < join_deadline:
            if cancellation is not None:
                cancellation.checkpoint()
            process.join(timeout=0.1)
        if process.is_alive():
            raise PdfDocumentTimeout(f"PDF profiler did not exit: {path}")
        if process.exitcode not in {0, None}:
            raise PdfChildProcessError(f"PDF profiler exited with code {process.exitcode}: {path}")
    finally:
        try:
            _terminate_process_tree(process)
        finally:
            channel.close()
            channel.cancel_join_thread()
            _close_process_handles(process)


# endregion [03]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_isolation")
