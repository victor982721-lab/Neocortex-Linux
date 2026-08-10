"""Bounded OCR evidence for document-like raster images.

The verifier is deliberately auxiliary: it records compact layout/semantic
signals, returns only a bounded prefix of recognized text for persistence, and
never turns an OCR failure into an image-route failure.  Tesseract runs inside
the existing isolated image worker, so the parent can cancel and contain the
complete process tree.
"""

# region [01] Imports, policy and result models

from __future__ import annotations

import csv
import io
import re
import subprocess
import unicodedata
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from PIL import Image

from .bounded_subprocess import run_bounded_capture
from .image_decode import pillow_decode_scope
from .ocr_image_preprocess import bounded_grayscale, orient_and_deskew
from .ocr_profiles import (
    OcrOrientation,
    OcrProfileName,
    contains_traditional_han,
    parse_language_spec,
    parse_osd_output,
    resolve_ocr_profile,
    route_ocr_languages,
    should_use_ocr_fallback,
)
from .image_policy import (
    DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES,
    INDUSTRIAL_ACTIVITY_HINTS,
    INDUSTRIAL_ENTITY_HINTS,
    OPERATIONAL_CONTEXT_HINTS,
    SAFETY_CONDITION_HINTS,
)
from .processing_provenance import (
    build_processing_provenance,
    resolve_tesseract_runtime,
)

DOCUMENT_OCR_VERSION = "document-text-tesseract-v3"
# Compatibility alias retained for callers which report the historical bound.
# Recognition now preserves materially more source detail and is capped by both
# dimensions and total pixels rather than blindly thumbnailing every image to 768px.
DOCUMENT_OCR_SAMPLE_SIDE = 3_200
DOCUMENT_OCR_MAX_PIXELS = 10_000_000
# Keep the OCR add-on reservation compatible with the route's documented
# 256 MiB single-worker budget (192 MiB decoder worker + 64 MiB OCR).  The
# larger sampling bound is independently capped at ten megapixels and encoded
# as grayscale before Tesseract is invoked, so increasing this fixed surcharge
# would reject otherwise bounded inputs before any decode occurs.
DOCUMENT_OCR_MEMORY_BYTES = 64 * 1024 * 1024
DOCUMENT_OCR_TSV_MAX_BYTES = 8 * 1024 * 1024
DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES = 256 * 1024
OCR_WORD_CONFIDENCE = 30.0
TOKEN_RE = re.compile(r"[a-z0-9]+")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DOCUMENT_TERMS = {
    "acta": ("acta",),
    "certificado": ("certificado", "certificate"),
    "contrato": ("contrato", "contract"),
    "estado_cuenta": ("estado de cuenta", "account statement"),
    "factura": ("factura", "invoice", "cfdi"),
    "folio": ("folio",),
    "informe": ("informe", "reporte", "report"),
    "recibo": ("recibo", "receipt"),
    "tabla": ("subtotal", "cantidad", "quantity"),
}

UI_TERMS = {
    "busqueda": ("buscar", "search", "no se encontraron resultados"),
    "configuracion": ("configuracion", "settings"),
    "descarga": ("descargar", "download"),
    "inicio_sesion": ("iniciar sesion", "sign in", "log in"),
    "instrument_controls": (
        "ramp wizard",
        "report all ramps",
        "akimi",
        "gerilim",
    ),
    "navegacion": ("home", "menu", "back"),
    "operacion_app": ("enviar dinero", "share", "compartir", "cancelar", "cancel"),
}


@dataclass(frozen=True, slots=True)
class DocumentVerifierConfig:
    mode: Literal["auto", "never"] = "auto"
    lang: str = "spa+eng"
    timeout_seconds: float = 12.0
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    profile: OcrProfileName = "configured"


@dataclass(frozen=True, slots=True)
class DocumentVerifierRuntime:
    enabled: bool
    lang: str
    timeout_seconds: float
    tesseract_cmd: str | None
    tessdata_dir: str | None
    signature: str
    provenance: str | None = None
    unavailable_reason: str | None = None
    processing_provenance_json: str | None = None
    profile: OcrProfileName = "configured"
    requested_languages: tuple[str, ...] = ()
    traineddata_hashes: tuple[tuple[str, str | None], ...] = ()
    osd_enabled: bool = False


@dataclass(frozen=True, slots=True)
class DocumentTextEvidence:
    attempted: bool
    available: bool
    word_count: int = 0
    line_count: int = 0
    character_count: int = 0
    recognized_text: str = ""
    recognized_text_truncated: bool = False
    text_coverage: float = 0.0
    mean_confidence: float = 0.0
    document_terms: tuple[str, ...] = ()
    ui_terms: tuple[str, ...] = ()
    industrial_entities: tuple[str, ...] = ()
    industrial_activities: tuple[str, ...] = ()
    industrial_operational_contexts: tuple[str, ...] = ()
    industrial_safety_conditions: tuple[str, ...] = ()
    provenance: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    ocr_profile: OcrProfileName = "configured"
    requested_languages: tuple[str, ...] = ()
    effective_languages: tuple[str, ...] = ()
    traineddata_hashes: tuple[tuple[str, str | None], ...] = ()
    orientation_degrees: int = 0
    rotation_degrees: int = 0
    orientation_confidence: float = 0.0
    detected_script: str = "unknown"
    script_confidence: float = 0.0
    osd_available: bool = False
    osd_unavailable_reason: str | None = None
    deskew_degrees: float = 0.0
    page_segmentation_mode: int = 11
    fallback_attempted: bool = False
    fallback_languages: tuple[str, ...] = ()
    fallback_reason: str | None = None
    fallback_error_type: str | None = None
    fallback_error_message: str | None = None
    recognition_attempts: int = 0

    @property
    def dense_text(self) -> bool:
        return (
            self.available
            and self.word_count >= 15
            and self.line_count >= 8
            and self.character_count >= 80
        )


# endregion [01]


# region [02] Runtime resolution


def _safe_error(exc: BaseException) -> str:
    return str(exc).encode("utf-8", "replace").decode("utf-8")[:500]


def _document_ocr_processing_provenance(
    config: DocumentVerifierConfig,
    component: dict[str, object],
):
    return build_processing_provenance(
        "image-document-ocr",
        DOCUMENT_OCR_VERSION,
        {
            "language": config.lang,
            "mode": config.mode,
            "profile": config.profile,
            "sample_max_pixels": DOCUMENT_OCR_MAX_PIXELS,
            "sample_max_side": DOCUMENT_OCR_SAMPLE_SIDE,
            "text_max_utf8_bytes": DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES,
        },
        (component,),
        compatibility_tag=DOCUMENT_OCR_VERSION,
    )


def resolve_document_verifier(
    config: DocumentVerifierConfig,
) -> DocumentVerifierRuntime:
    """Resolve executable, languages and version once in the parent process."""

    if config.mode not in {"auto", "never"}:
        raise ValueError(f"unsupported image document OCR mode: {config.mode}")
    if config.timeout_seconds <= 0:
        raise ValueError("image document OCR timeout must be positive")
    plan = resolve_ocr_profile(config.profile, config.lang)
    if config.mode == "never":
        processing = _document_ocr_processing_provenance(
            config,
            {
                "name": "tesseract",
                "kind": "native-executable",
                "status": "disabled",
            },
        )
        return DocumentVerifierRuntime(
            False,
            config.lang,
            config.timeout_seconds,
            None,
            config.tessdata_dir,
            processing.signature,
            unavailable_reason="disabled_by_configuration",
            processing_provenance_json=processing.manifest_json,
            profile=plan.profile,
            requested_languages=plan.required_languages,
            osd_enabled=plan.osd_enabled,
        )

    runtime = resolve_tesseract_runtime(
        command=config.tesseract_cmd,
        tessdata_dir=config.tessdata_dir,
        language=plan.required_language_spec,
        timeout_seconds=config.timeout_seconds,
    )
    processing = _document_ocr_processing_provenance(config, runtime.component)
    if runtime.available:
        provenance = f"tesseract-{runtime.version}|layout-keywords-v2"
        return DocumentVerifierRuntime(
            True,
            config.lang,
            config.timeout_seconds,
            runtime.command,
            runtime.tessdata_dir,
            processing.signature,
            provenance=provenance,
            processing_provenance_json=processing.manifest_json,
            profile=plan.profile,
            requested_languages=runtime.requested_languages,
            traineddata_hashes=runtime.traineddata_hashes,
            osd_enabled=plan.osd_enabled,
        )
    return DocumentVerifierRuntime(
        False,
        config.lang,
        config.timeout_seconds,
        None,
        runtime.tessdata_dir,
        processing.signature,
        unavailable_reason=runtime.unavailable_reason,
        processing_provenance_json=processing.manifest_json,
        profile=plan.profile,
        requested_languages=runtime.requested_languages or plan.required_languages,
        traineddata_hashes=runtime.traineddata_hashes,
        osd_enabled=plan.osd_enabled,
    )


# endregion [02]


# region [03] Bounded OCR and compact semantics


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(TOKEN_RE.findall(normalized))


def _semantic_hits(text: str, groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    normalized = f" {_normalized_text(text)} "
    labels = []
    for label, phrases in groups.items():
        if any(f" {_normalized_text(phrase)} " in normalized for phrase in phrases):
            labels.append(label)
    return tuple(sorted(labels))


@dataclass(frozen=True, slots=True)
class _DocumentOcrSample:
    width: int
    height: int
    payload: bytes


type _TsvRow = Mapping[str, str | list[str] | None]


def _contains_han_word(value: str) -> bool:
    return any(
        0x3400 <= ord(character) <= 0x4DBF
        or 0x4E00 <= ord(character) <= 0x9FFF
        or 0xF900 <= ord(character) <= 0xFAFF
        or 0x20000 <= ord(character) <= 0x323AF
        for character in value
    )


@dataclass(slots=True)
class _DocumentTextAccumulator:
    retained_words: list[str] = field(default_factory=list)
    retained_utf8_bytes: int = 0
    text_truncated: bool = False
    word_count: int = 0
    character_count: int = 0
    confidence_total: float = 0.0
    lines: set[tuple[int, int, int]] = field(default_factory=set)
    box_area: int = 0

    def _retain(self, word: str) -> None:
        if self.text_truncated:
            return
        word_bytes = len(word.encode("utf-8"))
        separator_bytes = 1 if self.retained_words else 0
        if (
            self.retained_utf8_bytes + separator_bytes + word_bytes
            <= DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES
        ):
            self.retained_words.append(word)
            self.retained_utf8_bytes += separator_bytes + word_bytes
            return
        self.text_truncated = True

    def observe(self, row: _TsvRow) -> None:
        word = str(row.get("text") or "").strip()
        confidence = _ocr_confidence(row)
        if (
            not word
            or confidence < 0.0
            or (confidence < OCR_WORD_CONFIDENCE and not _contains_han_word(word))
        ):
            return
        self.word_count += 1
        self.character_count += len(word)
        self.confidence_total += confidence
        self._retain(word)
        self.lines.add(
            (
                _ocr_integer(row, "block_num"),
                _ocr_integer(row, "par_num"),
                _ocr_integer(row, "line_num"),
            )
        )
        self.box_area += _ocr_integer(row, "width") * _ocr_integer(row, "height")


def _ocr_confidence(row: _TsvRow) -> float:
    try:
        return float(cast(str | int, row.get("conf") or -1))
    except (TypeError, ValueError):
        return -1.0


def _ocr_integer(row: _TsvRow, name: str) -> int:
    return int(cast(str | int, row.get(name) or 0))


def _encode_document_sample(contrasted: Image.Image) -> _DocumentOcrSample:
    try:
        width, height = contrasted.size
        with io.BytesIO() as encoded:
            contrasted.save(encoded, format="PNG")
            payload = encoded.getvalue()
        return _DocumentOcrSample(width, height, payload)
    finally:
        contrasted.close()


def _sample_document_image(path: Path) -> Image.Image:
    with pillow_decode_scope(allow_truncated=False):
        with Image.open(path) as source:
            return bounded_grayscale(
                source,
                max_side=DOCUMENT_OCR_SAMPLE_SIDE,
                max_pixels=DOCUMENT_OCR_MAX_PIXELS,
            )


def _document_ocr_command(
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
    *,
    languages: tuple[str, ...] | None = None,
    page_segmentation_mode: int | None = None,
) -> list[str]:
    effective_languages = languages or parse_language_spec(runtime.lang)
    command = [
        tesseract_cmd,
        "stdin",
        "stdout",
        "-l",
        "+".join(effective_languages),
        "--psm",
        str(11 if page_segmentation_mode is None else page_segmentation_mode),
    ]
    if runtime.tessdata_dir:
        command.extend(("--tessdata-dir", runtime.tessdata_dir))
    command.append("tsv")
    return command


def _run_document_ocr(
    sample: _DocumentOcrSample,
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
    *,
    languages: tuple[str, ...] | None = None,
    page_segmentation_mode: int | None = None,
) -> bytes:
    result = run_bounded_capture(
        _document_ocr_command(
            runtime,
            tesseract_cmd,
            languages=languages,
            page_segmentation_mode=page_segmentation_mode,
        ),
        input_bytes=sample.payload,
        timeout_seconds=runtime.timeout_seconds,
        stdout_limit_bytes=DOCUMENT_OCR_TSV_MAX_BYTES,
        stderr_limit_bytes=DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[:500]
        raise RuntimeError(detail or f"tesseract exited with code {result.returncode}")
    return result.stdout


def _document_osd_command(
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
) -> list[str]:
    command = [
        tesseract_cmd,
        "stdin",
        "stdout",
        "-l",
        "osd",
        "--psm",
        "0",
    ]
    if runtime.tessdata_dir:
        command.extend(("--tessdata-dir", runtime.tessdata_dir))
    return command


def _run_document_osd(
    sample: _DocumentOcrSample,
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
) -> OcrOrientation:
    result = run_bounded_capture(
        _document_osd_command(runtime, tesseract_cmd),
        input_bytes=sample.payload,
        timeout_seconds=runtime.timeout_seconds,
        stdout_limit_bytes=DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES,
        stderr_limit_bytes=DOCUMENT_OCR_DIAGNOSTIC_MAX_BYTES,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[:500]
        raise RuntimeError(detail or f"tesseract OSD exited with code {result.returncode}")
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    return parse_osd_output(output)


def _parse_document_tsv(payload: bytes) -> _DocumentTextAccumulator:
    accumulator = _DocumentTextAccumulator()
    decoded = payload.decode("utf-8", "replace")
    for row in csv.DictReader(
        io.StringIO(decoded),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    ):
        accumulator.observe(row)
    return accumulator


@dataclass(frozen=True, slots=True)
class _DocumentOcrAttempt:
    languages: tuple[str, ...]
    page_segmentation_mode: int
    text: _DocumentTextAccumulator

    @property
    def recognized_text(self) -> str:
        return " ".join(self.text.retained_words)

    @property
    def mean_confidence(self) -> float:
        if not self.text.word_count:
            return 0.0
        return self.text.confidence_total / self.text.word_count

    @property
    def quality_score(self) -> tuple[int, float, int, int]:
        conservative_failure = should_use_ocr_fallback(
            recognized_text=self.recognized_text,
            character_count=self.text.character_count,
            mean_confidence=self.mean_confidence,
        )
        return (
            int(not conservative_failure),
            round(self.mean_confidence, 3),
            self.text.character_count,
            self.text.word_count,
        )


@dataclass(frozen=True, slots=True)
class _DocumentOcrMetadata:
    orientation: OcrOrientation
    deskew_degrees: float
    fallback_attempted: bool
    fallback_languages: tuple[str, ...]
    fallback_reason: str | None
    fallback_error_type: str | None
    fallback_error_message: str | None
    recognition_attempts: int


def _available_document_evidence(
    sample: _DocumentOcrSample,
    attempt: _DocumentOcrAttempt,
    runtime: DocumentVerifierRuntime,
    metadata: _DocumentOcrMetadata,
) -> DocumentTextEvidence:
    text = attempt.text
    recognized = " ".join(text.retained_words)
    orientation = metadata.orientation
    return DocumentTextEvidence(
        attempted=True,
        available=True,
        word_count=text.word_count,
        line_count=len(text.lines),
        character_count=text.character_count,
        recognized_text=recognized,
        recognized_text_truncated=text.text_truncated,
        text_coverage=round(
            min(1.0, text.box_area / max(1, sample.width * sample.height)),
            5,
        ),
        mean_confidence=round(
            text.confidence_total / text.word_count if text.word_count else 0.0,
            2,
        ),
        document_terms=_semantic_hits(recognized, DOCUMENT_TERMS),
        ui_terms=_semantic_hits(recognized, UI_TERMS),
        industrial_entities=_semantic_hits(recognized, INDUSTRIAL_ENTITY_HINTS),
        industrial_activities=_semantic_hits(recognized, INDUSTRIAL_ACTIVITY_HINTS),
        industrial_operational_contexts=_semantic_hits(recognized, OPERATIONAL_CONTEXT_HINTS),
        industrial_safety_conditions=_semantic_hits(recognized, SAFETY_CONDITION_HINTS),
        provenance=runtime.provenance,
        ocr_profile=runtime.profile,
        requested_languages=(runtime.requested_languages or parse_language_spec(runtime.lang)),
        effective_languages=attempt.languages,
        traineddata_hashes=runtime.traineddata_hashes,
        orientation_degrees=orientation.orientation_degrees,
        rotation_degrees=orientation.rotate_degrees,
        orientation_confidence=orientation.orientation_confidence,
        detected_script=orientation.script,
        script_confidence=orientation.script_confidence,
        osd_available=orientation.available,
        osd_unavailable_reason=orientation.unavailable_reason,
        deskew_degrees=metadata.deskew_degrees,
        page_segmentation_mode=attempt.page_segmentation_mode,
        fallback_attempted=metadata.fallback_attempted,
        fallback_languages=metadata.fallback_languages,
        fallback_reason=metadata.fallback_reason,
        fallback_error_type=metadata.fallback_error_type,
        fallback_error_message=metadata.fallback_error_message,
        recognition_attempts=metadata.recognition_attempts,
    )


def _unavailable_document_evidence(
    runtime: DocumentVerifierRuntime,
    exc: Exception,
) -> DocumentTextEvidence:
    return DocumentTextEvidence(
        attempted=True,
        available=False,
        provenance=runtime.provenance,
        error_type=type(exc).__name__,
        error_message=_safe_error(exc),
        ocr_profile=runtime.profile,
        requested_languages=(runtime.requested_languages or parse_language_spec(runtime.lang)),
        traineddata_hashes=runtime.traineddata_hashes,
    )


def _osd_unavailable(exc: Exception) -> OcrOrientation:
    return OcrOrientation(
        unavailable_reason=f"{type(exc).__name__}: {_safe_error(exc)}",
    )


def _recognize_document_sample(
    sample: _DocumentOcrSample,
    runtime: DocumentVerifierRuntime,
    *,
    languages: tuple[str, ...],
    page_segmentation_mode: int,
) -> _DocumentOcrAttempt:
    assert runtime.tesseract_cmd is not None
    payload = _run_document_ocr(
        sample,
        runtime,
        runtime.tesseract_cmd,
        languages=languages,
        page_segmentation_mode=page_segmentation_mode,
    )
    return _DocumentOcrAttempt(
        languages,
        page_segmentation_mode,
        _parse_document_tsv(payload),
    )


def _execute_document_ocr(
    image: Image.Image,
    runtime: DocumentVerifierRuntime,
) -> tuple[_DocumentOcrSample, _DocumentOcrAttempt, _DocumentOcrMetadata]:
    """Run configured OCR unchanged or one OSD-routed bounded profile."""

    plan = resolve_ocr_profile(runtime.profile, runtime.lang)
    if not plan.osd_enabled:
        sample = _encode_document_sample(image)
        languages = parse_language_spec(runtime.lang)
        attempt = _recognize_document_sample(
            sample,
            runtime,
            languages=languages,
            page_segmentation_mode=11,
        )
        return (
            sample,
            attempt,
            _DocumentOcrMetadata(
                OcrOrientation(unavailable_reason="osd_disabled_for_configured_profile"),
                0.0,
                False,
                (),
                None,
                None,
                None,
                1,
            ),
        )

    assert runtime.tesseract_cmd is not None
    osd_sample = _encode_document_sample(image.copy())
    try:
        try:
            orientation = _run_document_osd(
                osd_sample,
                runtime,
                runtime.tesseract_cmd,
            )
        except Exception as exc:
            orientation = _osd_unavailable(exc)
        preprocessed = orient_and_deskew(
            image,
            rotation_clockwise_degrees=orientation.rotate_degrees,
        )
    finally:
        image.close()
    sample = _encode_document_sample(preprocessed.image)
    decision = route_ocr_languages(plan, orientation)
    primary = _recognize_document_sample(
        sample,
        runtime,
        languages=decision.primary_languages,
        page_segmentation_mode=6,
    )
    selected = primary
    fallback_attempted = False
    fallback_languages: tuple[str, ...] = ()
    fallback_reason: str | None = None
    fallback_error_type: str | None = None
    fallback_error_message: str | None = None
    attempts = 1
    if decision.fallback_languages is not None:
        low_quality = should_use_ocr_fallback(
            recognized_text=primary.recognized_text,
            character_count=primary.text.character_count,
            mean_confidence=primary.mean_confidence,
        )
        traditional_signal = contains_traditional_han(primary.recognized_text)
        if low_quality or traditional_signal:
            fallback_attempted = True
            fallback_languages = decision.fallback_languages
            fallback_reason = (
                "traditional_han_signal" if traditional_signal else "low_primary_quality"
            )
            attempts += 1
            try:
                fallback = _recognize_document_sample(
                    sample,
                    runtime,
                    languages=decision.fallback_languages,
                    page_segmentation_mode=6,
                )
                if fallback.quality_score > primary.quality_score or (
                    traditional_signal and fallback.quality_score == primary.quality_score
                ):
                    selected = fallback
            except Exception as exc:
                fallback_error_type = type(exc).__name__
                fallback_error_message = _safe_error(exc)
    return (
        sample,
        selected,
        _DocumentOcrMetadata(
            orientation,
            preprocessed.deskew_degrees,
            fallback_attempted,
            fallback_languages,
            fallback_reason,
            fallback_error_type,
            fallback_error_message,
            attempts,
        ),
    )


def verify_document_text(
    path: Path,
    runtime: DocumentVerifierRuntime,
    memory_gate=None,
) -> DocumentTextEvidence:
    """Return counts, labels and a whole-word UTF-8-bounded text prefix."""

    if not runtime.enabled:
        return DocumentTextEvidence(
            attempted=False,
            available=False,
            error_type="VerifierUnavailable",
            error_message=runtime.unavailable_reason,
            ocr_profile=runtime.profile,
            requested_languages=runtime.requested_languages,
            traineddata_hashes=runtime.traineddata_hashes,
        )

    admission = (
        memory_gate.admit(DOCUMENT_OCR_MEMORY_BYTES) if memory_gate is not None else nullcontext()
    )
    try:
        assert runtime.tesseract_cmd is not None
        with admission:
            image = _sample_document_image(path)
            sample, attempt, metadata = _execute_document_ocr(image, runtime)
        return _available_document_evidence(sample, attempt, runtime, metadata)
    except Exception as exc:
        return _unavailable_document_evidence(runtime, exc)


# endregion [03]
