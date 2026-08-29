"""Bounded, explainable page-layout mapping for native and scanned PDFs."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import re
from typing import Any

import xxhash


# region [01] Layout signature constants

LAYOUT_VERSION = 1
SIMHASH_BITS = 64
MAX_BLOCKS_PER_PAGE = 512
MAX_DRAWINGS_PER_PAGE = 256
VISUAL_COLUMNS = 16
VISUAL_ROWS = 20
VISUAL_SCALE = 0.25
FONT_RE = re.compile(r"[^a-z0-9]+")

# endregion [01]


# region [02] Signature helpers


def add_feature(counters: list[int], feature: str, weight: int = 1) -> None:
    value = xxhash.xxh3_64_intdigest(feature.encode("utf-8"))
    for bit in range(SIMHASH_BITS):
        counters[bit] += weight if value & (1 << bit) else -weight


def add_signature(counters: list[int], signature: str, weight: int = 1) -> None:
    value = int(signature, 16)
    for bit in range(SIMHASH_BITS):
        counters[bit] += weight if value & (1 << bit) else -weight


def finish_signature(counters: list[int]) -> str:
    value = 0
    for bit, count in enumerate(counters):
        if count >= 0:
            value |= 1 << bit
    return f"{value:016x}"


def signature_similarity(left: str, right: str) -> float:
    return 1.0 - ((int(left, 16) ^ int(right, 16)).bit_count() / SIMHASH_BITS)


# endregion [02]


# region [03] Geometry extraction


def _normalized_bbox(rect: Any, width: float, height: float) -> list[int]:
    values = list(rect)
    if len(values) < 4:
        return [0, 0, 0, 0]
    x0, y0, x1, y1 = (float(value) for value in values[:4])
    return [
        max(0, min(1000, round(x0 * 1000 / width))),
        max(0, min(1000, round(y0 * 1000 / height))),
        max(0, min(1000, round(x1 * 1000 / width))),
        max(0, min(1000, round(y1 * 1000 / height))),
    ]


def _bbox_feature(kind: str, bbox: list[int]) -> str:
    quantized = tuple(min(31, max(0, value * 32 // 1001)) for value in bbox)
    return f"{kind}:{quantized[0]}:{quantized[1]}:{quantized[2]}:{quantized[3]}"


def _font_name(value: str) -> str:
    normalized = FONT_RE.sub("-", value.casefold()).strip("-")
    return normalized[:48] or "unknown"


def _geometry(
    page: Any, width: float, height: float
) -> tuple[list[dict], list[dict], str, dict]:
    counters = [0] * SIMHASH_BITS
    add_feature(counters, "geometry-layout-v1", 2)
    blocks: list[dict] = []
    drawings: list[dict] = []
    text_blocks = image_blocks = span_count = 0
    truncated_blocks = truncated_drawings = 0
    payload = page.get_text("dict")
    for raw in payload.get("blocks", ()):
        kind = "text" if int(raw.get("type", 0)) == 0 else "image"
        if kind == "text":
            text_blocks += 1
        else:
            image_blocks += 1
        if len(blocks) >= MAX_BLOCKS_PER_PAGE:
            truncated_blocks += 1
            continue
        bbox = _normalized_bbox(raw.get("bbox", (0, 0, 0, 0)), width, height)
        entry: dict[str, Any] = {"kind": kind, "bbox": bbox}
        add_feature(counters, _bbox_feature(kind, bbox), 3 if bbox[1] < 250 else 2)
        if kind == "text":
            fonts: set[str] = set()
            sizes: set[int] = set()
            flags: set[int] = set()
            line_count = 0
            characters = 0
            for line in raw.get("lines", ()):
                line_count += 1
                for span in line.get("spans", ()):
                    span_count += 1
                    text = str(span.get("text", ""))
                    characters += len(text)
                    font = _font_name(str(span.get("font", "")))
                    size = max(1, min(96, round(float(span.get("size", 0)))))
                    flag = int(span.get("flags", 0)) & 31
                    fonts.add(font)
                    sizes.add(size)
                    flags.add(flag)
            entry.update(
                {
                    "lines": min(line_count, 999),
                    "characters": min(characters, 999_999),
                    "fonts": sorted(fonts)[:12],
                    "sizes": sorted(sizes)[:12],
                    "flags": sorted(flags)[:12],
                }
            )
            for font in entry["fonts"]:
                add_feature(counters, f"font:{font}")
            for size in entry["sizes"]:
                add_feature(counters, f"size:{min(size // 2, 32)}")
        blocks.append(entry)

    try:
        raw_drawings = page.get_drawings()
    except Exception:
        raw_drawings = ()
    for raw in raw_drawings:
        if len(drawings) >= MAX_DRAWINGS_PER_PAGE:
            truncated_drawings += 1
            continue
        bbox = _normalized_bbox(raw.get("rect", (0, 0, 0, 0)), width, height)
        item_count = min(len(raw.get("items", ())), 999)
        entry = {"bbox": bbox, "items": item_count}
        drawings.append(entry)
        add_feature(counters, _bbox_feature("drawing", bbox))
        add_feature(counters, f"drawing-items:{min(item_count, 16)}")
    source_kind = (
        "native_text" if text_blocks else "image_only" if image_blocks else "empty"
    )
    counts = {
        "text_blocks": text_blocks,
        "image_blocks": image_blocks,
        "spans": span_count,
        "drawings": len(raw_drawings),
        "truncated_blocks": truncated_blocks,
        "truncated_drawings": truncated_drawings,
    }
    return (
        blocks,
        drawings,
        finish_signature(counters),
        {
            "source_kind": source_kind,
            **counts,
        },
    )


# endregion [03]


# region [04] Low-resolution visual geometry


def _visual_grid(page: Any) -> tuple[list[int], str, str, str, str | None]:
    try:
        import fitz  # type: ignore[import-untyped]

        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(VISUAL_SCALE, VISUAL_SCALE),
            colorspace=fitz.csGRAY,
            alpha=False,
        )
        width = int(pixmap.width)
        height = int(pixmap.height)
        stride = int(pixmap.stride)
        samples = memoryview(pixmap.samples)
        grid: list[int] = []
        combined = [0] * SIMHASH_BITS
        header = [0] * SIMHASH_BITS
        footer = [0] * SIMHASH_BITS
        visual = [0] * SIMHASH_BITS
        for counters, seed in (
            (combined, "combined-visual-v1"),
            (header, "header-visual-v1"),
            (footer, "footer-visual-v1"),
            (visual, "page-visual-v1"),
        ):
            add_feature(counters, seed, 2)
        for row in range(VISUAL_ROWS):
            y0 = row * height // VISUAL_ROWS
            y1 = max(y0 + 1, (row + 1) * height // VISUAL_ROWS)
            for column in range(VISUAL_COLUMNS):
                x0 = column * width // VISUAL_COLUMNS
                x1 = max(x0 + 1, (column + 1) * width // VISUAL_COLUMNS)
                darkness = pixels = 0
                for y in range(y0, y1):
                    offset = y * stride
                    for x in range(x0, x1):
                        darkness += 255 - int(samples[offset + x])
                        pixels += 1
                level = min(7, round((darkness / max(1, pixels)) / 32))
                grid.append(level)
                if level <= 0:
                    continue
                feature = f"cell:{column}:{row}:{level}"
                add_feature(visual, feature, level)
                add_feature(combined, feature, level)
                if row < 5:
                    add_feature(header, feature, level)
                    add_feature(combined, f"header:{feature}", level * 2)
                if row >= 17:
                    add_feature(footer, feature, level)
                    add_feature(combined, f"footer:{feature}", level)
        return (
            grid,
            finish_signature(combined),
            finish_signature(header),
            finish_signature(footer),
            None,
        )
    except Exception as exc:
        empty = [0] * SIMHASH_BITS
        add_feature(empty, "visual-unavailable-v1", 2)
        signature = finish_signature(empty)
        return [], signature, signature, signature, f"{type(exc).__name__}: {exc}"[:300]


# endregion [04]


# region [05] Public page mapper


def map_page_layout(page: Any) -> dict:
    """Return a bounded map without retaining page text or raster artifacts."""

    rect = page.rect
    width = max(1.0, float(rect.width))
    height = max(1.0, float(rect.height))
    blocks, drawings, geometry_signature, counts = _geometry(page, width, height)
    grid, visual_signature, header_signature, footer_signature, visual_error = (
        _visual_grid(page)
    )
    combined = [0] * SIMHASH_BITS
    add_signature(combined, geometry_signature, 3)
    add_signature(combined, visual_signature, 2)
    add_signature(combined, header_signature, 2)
    add_signature(combined, footer_signature)
    return {
        "algorithm_version": LAYOUT_VERSION,
        "width": round(width, 2),
        "height": round(height, 2),
        "rotation": int(page.rotation),
        "source_kind": counts.pop("source_kind"),
        "blocks": blocks,
        "drawings": drawings,
        "visual_grid": grid,
        "header_ink": sum(grid[: VISUAL_COLUMNS * 5]),
        "footer_ink": sum(grid[VISUAL_COLUMNS * 17 :]),
        "counts": counts,
        "geometry_simhash64": geometry_signature,
        "visual_simhash64": visual_signature,
        "header_simhash64": header_signature,
        "footer_simhash64": footer_signature,
        "layout_simhash64": finish_signature(combined),
        "visual_error": visual_error,
    }


# endregion [05]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_layout")
