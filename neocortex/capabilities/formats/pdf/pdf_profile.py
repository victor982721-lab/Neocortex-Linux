"""Pure PDF page-profile primitives shared by local and isolated workers."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

from typing import Any

from .pdf_layout import map_page_layout


# region [01] Page profile extraction


def profile_page(page: Any) -> dict:
    rect = page.rect
    layout = map_page_layout(page)
    fonts = sorted(
        {font for block in layout["blocks"] for font in block.get("fonts", ())}
    )
    counts = layout["counts"]
    return {
        "width": round(float(rect.width), 2),
        "height": round(float(rect.height), 2),
        "rotation": int(page.rotation),
        "font_names": fonts,
        "font_count": len(fonts),
        "image_count": int(counts["image_blocks"]),
        "drawing_count": int(counts["drawings"]),
        "text_block_count": int(counts["text_blocks"]),
        "layout": layout,
    }


# endregion [01]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_profile")
