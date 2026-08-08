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

from PIL import Image, ImageOps

from .bounded_subprocess import run_bounded_capture
from .image_decode import pillow_decode_scope
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

DOCUMENT_OCR_VERSION = "document-text-tesseract-v2"
DOCUMENT_OCR_SAMPLE_SIDE = 768
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
            "sample_side": DOCUMENT_OCR_SAMPLE_SIDE,
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
    requested = tuple(part for part in config.lang.split("+") if part)
    if not requested:
        raise ValueError("image document OCR language must not be empty")
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
        )

    runtime = resolve_tesseract_runtime(
        command=config.tesseract_cmd,
        tessdata_dir=config.tessdata_dir,
        language=config.lang,
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
        if not word or confidence < OCR_WORD_CONFIDENCE:
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


def _gray_document_sample(source: Image.Image) -> Image.Image:
    oriented = ImageOps.exif_transpose(source)
    try:
        return oriented.convert("L")
    finally:
        if oriented is not source:
            oriented.close()


def _contrast_document_sample(sample: Image.Image) -> Image.Image:
    try:
        sample.thumbnail(
            (DOCUMENT_OCR_SAMPLE_SIDE, DOCUMENT_OCR_SAMPLE_SIDE),
            Image.Resampling.LANCZOS,
        )
        return ImageOps.autocontrast(sample)
    finally:
        sample.close()


def _encode_document_sample(contrasted: Image.Image) -> _DocumentOcrSample:
    try:
        width, height = contrasted.size
        with io.BytesIO() as encoded:
            contrasted.save(encoded, format="PNG")
            payload = encoded.getvalue()
        return _DocumentOcrSample(width, height, payload)
    finally:
        contrasted.close()


def _sample_document_image(path: Path) -> _DocumentOcrSample:
    with pillow_decode_scope(allow_truncated=False):
        with Image.open(path) as source:
            gray = _gray_document_sample(source)
            contrasted = _contrast_document_sample(gray)
            return _encode_document_sample(contrasted)


def _document_ocr_command(
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
) -> list[str]:
    command = [
        tesseract_cmd,
        "stdin",
        "stdout",
        "-l",
        runtime.lang,
        "--psm",
        "11",
    ]
    if runtime.tessdata_dir:
        command.extend(("--tessdata-dir", runtime.tessdata_dir))
    command.append("tsv")
    return command


def _run_document_ocr(
    sample: _DocumentOcrSample,
    runtime: DocumentVerifierRuntime,
    tesseract_cmd: str,
) -> bytes:
    result = run_bounded_capture(
        _document_ocr_command(runtime, tesseract_cmd),
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


def _available_document_evidence(
    sample: _DocumentOcrSample,
    text: _DocumentTextAccumulator,
    runtime: DocumentVerifierRuntime,
) -> DocumentTextEvidence:
    recognized = " ".join(text.retained_words)
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
        industrial_operational_contexts=_semantic_hits(
            recognized, OPERATIONAL_CONTEXT_HINTS
        ),
        industrial_safety_conditions=_semantic_hits(
            recognized, SAFETY_CONDITION_HINTS
        ),
        provenance=runtime.provenance,
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
        )

    admission = (
        memory_gate.admit(DOCUMENT_OCR_MEMORY_BYTES)
        if memory_gate is not None
        else nullcontext()
    )
    try:
        assert runtime.tesseract_cmd is not None
        with admission:
            sample = _sample_document_image(path)
            tsv = _run_document_ocr(
                sample,
                runtime,
                runtime.tesseract_cmd,
            )
        text = _parse_document_tsv(tsv)
        return _available_document_evidence(sample, text, runtime)
    except Exception as exc:
        return _unavailable_document_evidence(runtime, exc)


# endregion [03]
