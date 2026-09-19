"""Streaming OOXML text and layout evidence extraction."""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any, IO

from neocortex.foundation.hash_compat import xxhash

from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.capabilities.formats.xml_safety import safe_xml_iterparse
from .models import ALGORITHM_VERSION


# region [01] OOXML limits and namespaces

MAX_LAYOUT_VALUES = 64
TEXT_PIECE_BATCH = 8192
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TextBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.consumed = 0

    def consume(self, count: int) -> None:
        self.consumed += max(0, count)
        if self.consumed > self.limit:
            raise ValueError(f"DOCX text exceeds {self.limit} characters")


# endregion [01]


# region [02] Streaming text and layout


def normalized_text_digest(text: str) -> str:
    """Digest bounded normalized text using the route character cap."""

    import unicodedata

    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return xxhash.xxh3_128_hexdigest(normalized.encode("utf-8"))


def _collect_element_text(
    element: ET.Element,
    pieces: list[str],
    budget: TextBudget,
) -> None:
    """Append the text contribution of one completed WordprocessingML node."""

    tag = element.tag
    if tag == f"{W}t" and element.text:
        budget.consume(len(element.text))
        pieces.append(element.text)
    elif tag == f"{W}tab":
        budget.consume(1)
        pieces.append("\t")
    elif tag in {f"{W}br", f"{W}cr"}:
        budget.consume(1)
        pieces.append("\n")
    elif tag == f"{W}p":
        budget.consume(1)
        pieces.append("\n")


def _record_paragraph_layout(element: ET.Element, layout: dict[str, Any]) -> None:
    layout["paragraphs"] = int(layout["paragraphs"]) + 1
    style = element.find(f"./{W}pPr/{W}pStyle")
    align = element.find(f"./{W}pPr/{W}jc")
    if style is not None:
        value = style.get(f"{W}val")
        if value:
            layout["styles"][value] += 1
    if align is not None:
        value = align.get(f"{W}val")
        if value:
            layout["alignments"][value] += 1


def _section_layout(element: ET.Element) -> dict[str, str | None]:
    page = element.find(f"./{W}pgSz")
    margin = element.find(f"./{W}pgMar")
    columns = element.find(f"./{W}cols")
    return {
        "width": page.get(f"{W}w") if page is not None else None,
        "height": page.get(f"{W}h") if page is not None else None,
        "orientation": page.get(f"{W}orient") if page is not None else None,
        "top": margin.get(f"{W}top") if margin is not None else None,
        "right": margin.get(f"{W}right") if margin is not None else None,
        "bottom": margin.get(f"{W}bottom") if margin is not None else None,
        "left": margin.get(f"{W}left") if margin is not None else None,
        "columns": columns.get(f"{W}num", "1") if columns is not None else "1",
    }


def _record_element_layout(element: ET.Element, layout: dict[str, Any]) -> None:
    tag = element.tag
    if tag == f"{W}p":
        _record_paragraph_layout(element, layout)
    elif tag == f"{W}tbl":
        layout["tables"] = int(layout["tables"]) + 1
    elif tag == f"{W}sectPr":
        layout["sections"].append(_section_layout(element))


def _flush_text_pieces(pieces: list[str], output: io.StringIO) -> None:
    output.write("".join(pieces))
    pieces.clear()


def xml_text_and_layout(
    source: IO[bytes],
    *,
    collect_layout: bool,
    budget: TextBudget,
    cancellation: CancellationToken | None = None,
) -> tuple[str, dict[str, Any]]:
    pieces: list[str] = []
    output = io.StringIO()
    layout: dict[str, Any] = {
        "paragraphs": 0,
        "tables": 0,
        "sections": [],
        "styles": Counter(),
        "alignments": Counter(),
    }
    parents: list[ET.Element] = []
    retained_children: list[int] = []
    for event, element in safe_xml_iterparse(source, events=("start", "end")):
        if event == "start":
            parents.append(element)
            retained_children.append(0)
            continue
        if cancellation is not None:
            cancellation.checkpoint()
        tag = element.tag
        _collect_element_text(element, pieces, budget)
        if collect_layout:
            _record_element_layout(element, layout)
        discard = tag in {f"{W}p", f"{W}tbl", f"{W}sectPr"}
        if discard:
            element.clear()
        if len(parents) > 1:
            if discard:
                # Track the child index: remove(element) would repeatedly
                # search any earlier, retained extension/property siblings.
                del parents[-2][retained_children[-2]]
            else:
                retained_children[-2] += 1
        if budget.consumed > budget.limit:
            raise ValueError(f"DOCX text exceeds {budget.limit} characters")
        if len(pieces) >= TEXT_PIECE_BATCH:
            _flush_text_pieces(pieces, output)
        parents.pop()
        retained_children.pop()
    if pieces:
        _flush_text_pieces(pieces, output)
    text = output.getvalue()
    output.close()
    return text, layout


# endregion [02]


# region [03] Layout classification


def _page_class(sections: list[dict]) -> str:
    if not sections:
        return "unspecified"
    section = sections[0]
    try:
        width, height = int(section["width"]), int(section["height"])
    except (KeyError, TypeError, ValueError):
        return "unspecified"
    portrait = height >= width
    short, long = sorted((width, height))
    if abs(short - 11906) <= 250 and abs(long - 16838) <= 250:
        paper = "a4"
    elif abs(short - 12240) <= 250 and abs(long - 15840) <= 250:
        paper = "letter"
    elif abs(short - 12240) <= 250 and abs(long - 20160) <= 250:
        paper = "legal"
    else:
        paper = "custom"
    return f"{paper}_{'portrait' if portrait else 'landscape'}"


def layout_result(
    layout: dict[str, Any],
    header_signatures: list[str],
    footer_signatures: list[str],
    images: int,
) -> tuple[str, str, str]:
    sections = layout["sections"]
    paragraphs = int(layout["paragraphs"])
    tables = int(layout["tables"])
    page_class = _page_class(sections)
    if tables >= max(2, paragraphs // 4):
        structure = "table_heavy"
    elif header_signatures or footer_signatures:
        structure = "letterhead"
    elif paragraphs >= 40:
        structure = "report"
    elif images >= 3:
        structure = "illustrated"
    else:
        structure = "simple"
    layout_class = f"{page_class}:{structure}"
    evidence = {
        "page_class": page_class,
        "structure": structure,
        "sections": sections[:MAX_LAYOUT_VALUES],
        "styles": sorted(layout["styles"].items())[:MAX_LAYOUT_VALUES],
        "alignments": sorted(layout["alignments"].items())[:MAX_LAYOUT_VALUES],
        "paragraph_count": paragraphs,
        "table_count": tables,
        "image_count": images,
        "header_signatures": header_signatures,
        "footer_signatures": footer_signatures,
        "algorithm": ALGORITHM_VERSION,
    }
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = xxhash.xxh3_128_hexdigest(encoded.encode("utf-8"))
    return layout_class, signature, encoded


# endregion [03]


for _historical_symbol in (
    TextBudget,
    normalized_text_digest,
    _collect_element_text,
    _record_paragraph_layout,
    _section_layout,
    _record_element_layout,
    _flush_text_pieces,
    xml_text_and_layout,
    _page_class,
    layout_result,
):
    _historical_symbol.__module__ = "neocortex.capabilities.formats.docx.layout"
del _historical_symbol
