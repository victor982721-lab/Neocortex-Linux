"""Structured image failure policy shared by workers and persistence."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import UnidentifiedImageError

from .decode import (
    RecoveredImageContentError,
    is_recoverable_decode_error,
)
from .png import probe_png_structure


# region [01] Queue-facing failure contract


ErrorDisposition = Literal["retry", "manual_review", "deletion_candidate"]
ERROR_POLICY_VERSION = "image-error-policy-v1"


@dataclass(frozen=True, slots=True)
class ImageFailure:
    error_type: str
    message: str
    phase: str
    retryable: bool
    disposition: ErrorDisposition
    provenance: str = ERROR_POLICY_VERSION


def _safe_message(exc: BaseException) -> str:
    return str(exc).encode("utf-8", "replace").decode("utf-8")[:2000]


def worker_supervision_failure(message: str) -> ImageFailure:
    return ImageFailure(
        "ImageWorkerError",
        message[:2000],
        "worker_supervision",
        True,
        "retry",
    )


# endregion [01]


# region [02] Deterministic exception classification


def classify_image_failure(
    exc: BaseException,
    *,
    phase_hint: str | None = None,
) -> ImageFailure:
    """Preserve remote failures and classify local failures conservatively."""

    carried = getattr(exc, "failure", None)
    if isinstance(carried, ImageFailure):
        return carried

    error_type = type(exc).__name__
    message = _safe_message(exc)
    lowered = message.casefold()

    if isinstance(exc, RecoveredImageContentError):
        return ImageFailure(
            error_type,
            message,
            "decode",
            False,
            "deletion_candidate",
        )
    if isinstance(exc, UnidentifiedImageError) or is_recoverable_decode_error(exc):
        return ImageFailure(
            error_type,
            message,
            "decode",
            False,
            "manual_review",
        )
    if error_type in {"DecompressionBombError", "SyntaxError"}:
        return ImageFailure(
            error_type,
            message,
            "decode",
            False,
            "manual_review",
        )
    if error_type in {"MemoryBudgetExceeded", "MemoryHeadroomTimeout"}:
        return ImageFailure(
            error_type,
            message,
            "resource_admission",
            True,
            "retry",
        )
    if isinstance(exc, TimeoutError):
        return ImageFailure(
            error_type,
            message,
            phase_hint or "worker_timeout",
            True,
            "retry",
        )
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return ImageFailure(
            error_type,
            message,
            phase_hint or "input_access",
            True,
            "retry",
        )
    if "metadata changed" in lowered:
        return ImageFailure(
            error_type,
            message,
            "source_validation",
            True,
            "retry",
        )
    if isinstance(exc, OSError):
        return ImageFailure(
            error_type,
            message,
            phase_hint or "decode",
            False,
            "manual_review",
        )
    return ImageFailure(
        error_type,
        message,
        phase_hint or "analysis",
        False,
        "manual_review",
    )


def refine_image_failure(path: Path, failure: ImageFailure) -> ImageFailure:
    """Elevate only independently proven PNG corruption to deletion review."""

    if failure.error_type != "UnidentifiedImageError":
        return failure
    try:
        probe = probe_png_structure(path)
    except OSError:
        return failure
    if not probe.deterministic_corruption:
        return failure
    return ImageFailure(
        error_type="PngStructureCorrupt",
        message=(
            f"PNG structural validation failed: {probe.reason_code}; "
            f"bytes_checked={probe.bytes_checked}; chunks_checked={probe.chunks_checked}"
        ),
        phase="container_integrity",
        retryable=False,
        disposition="deletion_candidate",
        provenance=probe.provenance,
    )


# endregion [02]
