"""Memory-bounded pixel and metadata feature extraction."""

from __future__ import annotations
import math
from contextlib import nullcontext
from pathlib import Path
from typing import Iterator, TypeAlias

from .decode import (
    RECOVERED_DECODE_PROVENANCE,
    STRICT_DECODE_PROVENANCE,
    RecoveredImageContentError,
    is_recoverable_decode_error,
    pillow_decode_scope,
    recovered_content_is_meaningful,
)
from .models import Features
from .policy import MIB, SAMPLE_SIDE
from neocortex.runtime.control.memory_runtime import MemoryResourceLimits, WeightedMemoryGate

try:
    from PIL import Image, ImageFilter, ImageOps, ImageStat
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Falta Pillow. Instálalo con: python -m pip install Pillow") from exc


# region [01] Memory admission

ImageResourceLimits: TypeAlias = MemoryResourceLimits
ImageMemoryGate: TypeAlias = WeightedMemoryGate
_EDGE_BINARY_LUT = (0,) * 48 + (1,) * (256 - 48)


def estimated_image_memory_bytes(
    width: int,
    height: int,
    file_size: int = 0,
) -> int:
    """Estimate compressed input, decoded pixels and bounded sample workspace."""

    decoded = max(1, width) * max(1, height) * 8
    sample_workspace = 96 * MIB
    return max(0, file_size) + decoded + sample_workspace


# endregion [01]


# region [02] Pixel primitives


def entropy(gray: Image.Image) -> float:
    histogram = gray.histogram()
    total = sum(histogram)
    if not total:
        return 0.0
    return -sum((count / total) * math.log2(count / total) for count in histogram if count)


def projection_features(edges: Image.Image) -> tuple[float, float, float]:
    """Measure long lines and text-like bands using edge-map projections."""

    with edges.point(_EDGE_BINARY_LUT) as binary:
        width, height = binary.size
        pixels = binary.tobytes()
    column_counts = [0] * width
    long_horizontal = text_bands = 0
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        count = sum(row)
        density = count / width
        long_horizontal += density >= 0.42
        text_bands += 0.018 <= density <= 0.32
        for x, value in enumerate(row):
            column_counts[x] += value
    long_vertical = sum((count / height) >= 0.42 for count in column_counts)
    return (
        long_horizontal / max(1, height),
        long_vertical / max(1, width),
        text_bands / max(1, height),
    )


def exif_number(exif: object, tag: int) -> float | None:
    try:
        value = exif.get(tag)  # type: ignore[attr-defined]
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def is_skin_tone(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return (
        red > 80
        and green > 30
        and blue > 15
        and red > green
        and red > blue
        and max(pixel) - min(pixel) > 18
        and abs(red - green) > 12
    )


def _triples(data: bytes) -> Iterator[tuple[int, int, int]]:
    for offset in range(0, len(data), 3):
        yield data[offset], data[offset + 1], data[offset + 2]


# endregion [02]


# region [03] Bounded feature extraction


def _extract_admitted_features(
    source: Image.Image,
    path: Path,
    width: int,
    height: int,
    fmt: str,
    frames: int,
    exif: object,
    decode_quality: str,
    decode_provenance: str,
) -> Features:
    """Decode a reduced first-frame sample while the caller holds admission."""

    has_camera_exif = bool(
        exif and any(tag in exif for tag in (271, 272, 306, 36867, 37386))  # type: ignore[operator]
    )
    flash_value = int(exif_number(exif, 37385) or 0)
    flash_fired = bool(flash_value & 1)
    iso = exif_number(exif, 34855)
    exposure_time = exif_number(exif, 33434)
    focal_length_35mm = exif_number(exif, 41989)
    orientation = int(exif_number(exif, 274) or 1)
    effective_width, effective_height = (
        (height, width) if orientation in {5, 6, 7, 8} else (width, height)
    )

    source.draft("RGB", (SAMPLE_SIDE, SAMPLE_SIDE))
    source.thumbnail(
        (SAMPLE_SIDE, SAMPLE_SIDE),
        Image.Resampling.BILINEAR,
        reducing_gap=2.0,
    )
    oriented = ImageOps.exif_transpose(source)
    try:
        has_transparency = "A" in oriented.getbands() or "transparency" in oriented.info
        rgba = oriented.convert("RGBA")
    finally:
        if oriented is not source:
            oriented.close()

    with rgba:
        with rgba.getchannel("A") as alpha:
            alpha_histogram = alpha.histogram()
            alpha_fraction = sum(alpha_histogram[:250]) / max(1, sum(alpha_histogram))
            with Image.new("RGB", rgba.size, "white") as rgb:
                with rgba.convert("RGB") as opaque:
                    rgb.paste(opaque, mask=alpha)

                pixels = rgb.tobytes()
                total = max(1, len(pixels) // 3)
                white = light = dark = neutral = greenish = warm = skin = 0
                color_range_sum = brightness_sum = 0.0
                for red, green, blue in _triples(pixels):
                    low = min(red, green, blue)
                    high = max(red, green, blue)
                    value = (red + green + blue) / 3
                    white += low >= 238
                    light += value >= 205
                    dark += value <= 80
                    neutral += high - low <= 24
                    color_range_sum += high - low
                    brightness_sum += value
                    greenish += green > red * 1.08 and green > blue * 1.05 and green >= 55
                    warm += red > blue * 1.15 and red > green * 1.03 and red >= 70
                    skin += is_skin_tone((red, green, blue))

                with rgb.crop((0, 0, rgb.width, max(1, rgb.height // 3))) as top:
                    top_data = top.tobytes()
                    top_total = max(1, len(top_data) // 3)
                    top_blue_fraction = (
                        sum(
                            blue > red * 1.08 and blue > green * 1.03 and blue >= 75
                            for red, green, blue in _triples(top_data)
                        )
                        / top_total
                    )

                margin_x = rgb.width // 5
                margin_y = rgb.height // 5
                with rgb.crop(
                    (margin_x, margin_y, rgb.width - margin_x, rgb.height - margin_y)
                ) as center:
                    center_data = center.tobytes()
                    center_total = max(1, len(center_data) // 3)
                    central_skin_fraction = (
                        sum(is_skin_tone(pixel) for pixel in _triples(center_data)) / center_total
                    )

                with rgb.convert("L") as gray:
                    with gray.filter(ImageFilter.FIND_EDGES) as edges:
                        edge_histogram = edges.histogram()
                        edge_fraction = sum(edge_histogram[48:]) / max(1, sum(edge_histogram))
                        horizontal, vertical, text_bands = projection_features(edges)
                        edge_strength = ImageStat.Stat(edges).mean[0] / 255
                    brightness_std = ImageStat.Stat(gray).stddev[0] / 255
                    entropy_value = entropy(gray)

                with rgb.quantize(colors=64) as quantized:
                    quantized_colors = len(quantized.getcolors(maxcolors=256) or [])

                border = max(1, min(rgb.size) // 24)
                boxes = (
                    (0, 0, rgb.width, border),
                    (0, rgb.height - border, rgb.width, rgb.height),
                    (0, 0, border, rgb.height),
                    (rgb.width - border, 0, rgb.width, rgb.height),
                )
                border_white_count = border_total = 0
                for box in boxes:
                    with rgb.crop(box) as strip:
                        strip_data = strip.tobytes()
                    for red, green, blue in _triples(strip_data):
                        border_total += 1
                        border_white_count += min(red, green, blue) >= 238

    return Features(
        width=effective_width,
        height=effective_height,
        file_size=path.stat().st_size,
        format=fmt,
        frames=frames,
        has_transparency=has_transparency,
        alpha_fraction=alpha_fraction,
        has_camera_exif=has_camera_exif,
        white_fraction=white / total,
        light_fraction=light / total,
        dark_fraction=dark / total,
        neutral_fraction=neutral / total,
        colorfulness=color_range_sum / (255 * total),
        brightness_mean=brightness_sum / (255 * total),
        brightness_std=brightness_std,
        entropy=entropy_value,
        edge_strength=edge_strength,
        edge_fraction=edge_fraction,
        quantized_colors=quantized_colors,
        border_white_fraction=border_white_count / max(1, border_total),
        long_horizontal_lines=horizontal,
        long_vertical_lines=vertical,
        text_band_fraction=text_bands,
        top_blue_fraction=top_blue_fraction,
        green_fraction=greenish / total,
        warm_fraction=warm / total,
        skin_fraction=skin / total,
        central_skin_fraction=central_skin_fraction,
        flash_fired=flash_fired,
        iso=iso,
        exposure_time=exposure_time,
        focal_length_35mm=focal_length_35mm,
        decode_quality=decode_quality,
        decode_provenance=decode_provenance,
    )


def _extract_features_once(
    path: Path,
    memory_gate: ImageMemoryGate | None,
    *,
    allow_truncated: bool,
    decode_quality: str,
    decode_provenance: str,
) -> Features:
    file_size = path.stat().st_size
    with pillow_decode_scope(allow_truncated=allow_truncated):
        with Image.open(path) as source:
            width, height = source.size
            if width <= 0 or height <= 0:
                raise ValueError("dimensiones inválidas")
            fmt = (source.format or path.suffix.lstrip(".")).upper()
            admission = (
                memory_gate.admit(estimated_image_memory_bytes(width, height, file_size))
                if memory_gate is not None
                else nullcontext()
            )
            with admission:
                frames = int(getattr(source, "n_frames", 1))
                exif = source.getexif()
                return _extract_admitted_features(
                    source,
                    path,
                    width,
                    height,
                    fmt,
                    frames,
                    exif,
                    decode_quality,
                    decode_provenance,
                )


def extract_features(
    path: Path,
    memory_gate: ImageMemoryGate | None = None,
) -> Features:
    """Decode strictly, then retry only a recognized incomplete stream."""

    try:
        return _extract_features_once(
            path,
            memory_gate,
            allow_truncated=False,
            decode_quality="strict",
            decode_provenance=STRICT_DECODE_PROVENANCE,
        )
    except OSError as exc:
        if not is_recoverable_decode_error(exc):
            raise
        recovered = _extract_features_once(
            path,
            memory_gate,
            allow_truncated=True,
            decode_quality="recovered_truncated",
            decode_provenance=RECOVERED_DECODE_PROVENANCE,
        )
        if not recovered_content_is_meaningful(recovered):
            raise RecoveredImageContentError(
                "tolerant decode yielded uniform or contentless pixels"
            ) from exc
        return recovered


# endregion [03]
