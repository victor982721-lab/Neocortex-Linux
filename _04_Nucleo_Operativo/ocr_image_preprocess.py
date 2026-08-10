"""Bounded Pillow preprocessing shared by image and PDF OCR."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageOps

OCR_PREPROCESS_VERSION = "ocr-orient-deskew-v1"
DESKEW_CANDIDATE_DEGREES = (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0)
DESKEW_ANALYSIS_SIDE = 512


@dataclass(frozen=True, slots=True)
class OcrPreprocessResult:
    image: Image.Image
    rotation_clockwise_degrees: int
    deskew_degrees: float


def _bounded_dimensions(
    width: int,
    height: int,
    *,
    max_side: int,
    max_pixels: int,
) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise ValueError("OCR image dimensions must be positive")
    if max_side < 1 or max_pixels < 1:
        raise ValueError("OCR image bounds must be positive")
    scale = min(
        1.0,
        max_side / max(width, height),
        math.sqrt(max_pixels / (width * height)),
    )
    return max(1, round(width * scale)), max(1, round(height * scale))


def bounded_grayscale(
    source: Image.Image,
    *,
    max_side: int,
    max_pixels: int,
) -> Image.Image:
    """Apply EXIF orientation and downscale only when an explicit cap requires it."""

    oriented = ImageOps.exif_transpose(source)
    try:
        gray = oriented.convert("L")
    finally:
        if oriented is not source:
            oriented.close()
    target = _bounded_dimensions(
        *gray.size,
        max_side=max_side,
        max_pixels=max_pixels,
    )
    if target != gray.size:
        resized = gray.resize(target, Image.Resampling.LANCZOS)
        gray.close()
        gray = resized
    contrasted = ImageOps.autocontrast(gray)
    gray.close()
    return contrasted


def _horizontal_projection_score(image: Image.Image) -> float:
    width, height = image.size
    pixels = image.tobytes()
    row_counts: list[int] = []
    for offset in range(0, len(pixels), width):
        row = pixels[offset : offset + width]
        row_counts.append(sum(value < 192 for value in row))
    mean = sum(row_counts) / max(1, height)
    return sum((value - mean) ** 2 for value in row_counts) / max(1, height)


def estimate_deskew_degrees(source: Image.Image) -> float:
    """Choose a small deterministic rotation using horizontal ink projection."""

    analysis = source.copy()
    try:
        analysis.thumbnail(
            (DESKEW_ANALYSIS_SIDE, DESKEW_ANALYSIS_SIDE),
            Image.Resampling.BILINEAR,
        )
        baseline = analysis.rotate(0.0, expand=True, fillcolor=255)
        try:
            baseline_score = _horizontal_projection_score(baseline)
        finally:
            baseline.close()
        best_degrees = 0.0
        best_score = baseline_score
        for degrees in DESKEW_CANDIDATE_DEGREES:
            if not degrees:
                continue
            candidate = analysis.rotate(degrees, expand=True, fillcolor=255)
            try:
                score = _horizontal_projection_score(candidate)
            finally:
                candidate.close()
            if score > best_score:
                best_score = score
                best_degrees = degrees
        # Avoid rotating near-blank/noisy pages for a negligible projection gain.
        minimum_gain = max(1.0, baseline_score * 0.03)
        if best_degrees and best_score - baseline_score < minimum_gain:
            return 0.0
        return best_degrees
    finally:
        analysis.close()


def orient_and_deskew(
    source: Image.Image,
    *,
    rotation_clockwise_degrees: int,
    deskew: bool = True,
) -> OcrPreprocessResult:
    """Return a caller-owned image after quarter-turn orientation and small deskew."""

    if rotation_clockwise_degrees not in {0, 90, 180, 270}:
        raise ValueError("OCR rotation must be a clockwise quarter turn")
    rotated = source.rotate(
        -rotation_clockwise_degrees,
        expand=True,
        fillcolor=255,
    )
    deskew_degrees = estimate_deskew_degrees(rotated) if deskew else 0.0
    if not deskew_degrees:
        return OcrPreprocessResult(
            rotated,
            rotation_clockwise_degrees,
            0.0,
        )
    corrected = rotated.rotate(deskew_degrees, expand=True, fillcolor=255)
    rotated.close()
    return OcrPreprocessResult(
        corrected,
        rotation_clockwise_degrees,
        deskew_degrees,
    )


__all__ = (
    "DESKEW_ANALYSIS_SIDE",
    "DESKEW_CANDIDATE_DEGREES",
    "OCR_PREPROCESS_VERSION",
    "OcrPreprocessResult",
    "bounded_grayscale",
    "estimate_deskew_degrees",
    "orient_and_deskew",
)
