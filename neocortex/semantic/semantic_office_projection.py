"""Bounded dense projections for the semantic view of XLSX cell caches.

The Office owner keeps the canonical ``XLSX_CELL`` representation (and its
FTS row) unchanged.  Semantic indexing may use this module as a lossy,
explicitly labelled projection: cell locators remain in the projected text,
the workbook name is not repeated for every cell, and a formula is never
turned into a value when its cached result is absent.

This module deliberately does not read an XLSX file or an owner database.  It
only transforms an already bounded ``TextSection`` supplied by the source
adapter, so the owner remains the authority for extraction and validation.
"""

from __future__ import annotations

import json
import io
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass

from .semantic_models import TextSection


XLSX_CELL_PREFIX = "XLSX_CELL "
XLSX_DENSE_SECTION_KIND = "xlsx_cell_projection"
XLSX_AUXILIARY_SECTION_KIND = "xlsx_projection_auxiliary"
XLSX_PROJECTION_POLICY = "semantic-xlsx-cell-dense-v1"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class XlsxProjectionStats:
    """Bounded accounting for one source section's dense projection."""

    cell_records: int = 0
    projected_cells: int = 0
    omitted_formula_without_cached_value: int = 0
    auxiliary_records: int = 0
    malformed_cell_records: int = 0

    @property
    def complete_for_dense_values(self) -> bool:
        """Whether every parsed cell had a value usable by the dense view."""

        return self.omitted_formula_without_cached_value == 0


@dataclass(frozen=True, slots=True)
class _ParsedXlsxCell:
    sheet: str
    cell_reference: str
    cell_type: str
    value: str
    formula: str | None
    cached_value: str | None


def _normalized_text(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value)).strip()


def _scalar_text(value: object, *, field_name: str, allow_none: bool) -> str | None:
    if value is None:
        if allow_none:
            return None
        return ""
    if isinstance(value, bool):
        # Keep an explicit JSON boolean; do not let False disappear through a
        # truthiness fallback.
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return _normalized_text(value)
    raise ValueError(f"XLSX cell {field_name} must be a scalar or null")


def _parse_cell(line: str) -> _ParsedXlsxCell:
    raw = json.loads(line[len(XLSX_CELL_PREFIX) :])
    if not isinstance(raw, Mapping):
        raise ValueError("XLSX cell projection must be an object")
    required = ("sheet", "a1", "type", "value", "formula", "cached_value")
    if any(name not in raw for name in required):
        raise ValueError("XLSX cell projection is missing a required field")
    sheet = _normalized_text(raw["sheet"])
    cell_reference = _normalized_text(raw["a1"])
    cell_type = _normalized_text(raw["type"])
    if not sheet or not cell_reference or not cell_type:
        raise ValueError("XLSX cell locator fields cannot be blank")
    value = _scalar_text(raw["value"], field_name="value", allow_none=False)
    if value is None:  # pragma: no cover - defensive; allow_none is false
        raise ValueError("XLSX cell value cannot be null")
    return _ParsedXlsxCell(
        sheet=sheet,
        cell_reference=cell_reference,
        cell_type=cell_type,
        value=_normalized_text(value),
        formula=_scalar_text(raw["formula"], field_name="formula", allow_none=True),
        cached_value=_scalar_text(raw["cached_value"], field_name="cached_value", allow_none=True),
    )


def _projected_cell_text(cell: _ParsedXlsxCell) -> str | None:
    """Return one locator/value line, or ``None`` for an uncached formula."""

    if cell.formula is not None and cell.formula != "" and (
        cell.cached_value is None or cell.cached_value == ""
    ):
        # The raw source remains searchable and retains the formula.  The
        # dense view must not invent a result from ``value`` in this case.
        return None
    value = cell.cached_value if cell.formula else cell.value
    if value is None or value == "":
        value = "[empty]"
    formula_marker = (
        " formula-cached"
        if cell.formula is not None and cell.formula != ""
        else ""
    )
    return f"{cell.sheet}!{cell.cell_reference} [{cell.cell_type}{formula_marker}] | {value}"


def _projection_provenance(
    source: TextSection,
    stats: XlsxProjectionStats,
    *,
    channel: str,
) -> dict[str, object]:
    provenance: dict[str, object] = {
        "policy_signature": XLSX_PROJECTION_POLICY,
        "projection": "dense_cell_values",
        "channel": channel,
        "source_section_kind": source.section_kind,
        "source_section_id": source.section_id,
        "cell_records": stats.cell_records,
        "projected_cells": stats.projected_cells,
        "omitted_formula_without_cached_value": stats.omitted_formula_without_cached_value,
        "auxiliary_records": stats.auxiliary_records,
        "malformed_cell_records": stats.malformed_cell_records,
        "workbook_name_in_text": False,
        "source_retained_in_owner": True,
    }
    adapter = source.provenance.get("adapter")
    if isinstance(adapter, str) and adapter:
        provenance["source_adapter"] = adapter
    return provenance


def project_xlsx_section(
    source: TextSection, *, checkpoint: Callable[[], None] | None = None,
) -> tuple[TextSection, ...]:
    """Project one XLSX source section without changing the source owner.

    Non-XLSX sections are returned unchanged.  For an XLSX document, parsed
    cells become a compact locator/value channel and all non-cell records are
    retained in a separately labelled auxiliary channel.  The two channels
    share bounded provenance counters, including an explicit count of formula
    cells omitted because no cached value was available.
    """

    if source.section_kind != "xlsx_document":
        return (source,)

    dense_lines: list[str] = []
    auxiliary_lines: list[str] = []
    cell_records = projected_cells = omitted = auxiliary = malformed = 0
    for ordinal, raw_line in enumerate(io.StringIO(source.text)):
        if checkpoint is not None and ordinal % 64 == 0:
            checkpoint()
        line = raw_line.rstrip("\r\n")
        if not line.startswith(XLSX_CELL_PREFIX):
            if line:
                auxiliary_lines.append(line)
                auxiliary += 1
            continue
        cell_records += 1
        try:
            cell = _parse_cell(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            # A malformed owner record is not silently discarded from the
            # semantic projection.  Keep its original line in the auxiliary
            # channel and let the owner/cache validation remain authoritative.
            auxiliary_lines.append(line)
            auxiliary += 1
            malformed += 1
            continue
        projected = _projected_cell_text(cell)
        if projected is None:
            omitted += 1
            continue
        dense_lines.append(projected)
        projected_cells += 1

    if checkpoint is not None:
        checkpoint()
    if not cell_records:
        # A legacy/unknown body has not acquired a typed cell-value meaning.
        return (source,)
    stats = XlsxProjectionStats(
        cell_records=cell_records,
        projected_cells=projected_cells,
        omitted_formula_without_cached_value=omitted,
        auxiliary_records=auxiliary,
        malformed_cell_records=malformed,
    )
    result: list[TextSection] = []
    if dense_lines:
        result.append(
            TextSection(
                XLSX_DENSE_SECTION_KIND,
                f"{source.section_id}:dense",
                "\n".join(dense_lines),
                _projection_provenance(source, stats, channel="dense"),
            )
        )
    if auxiliary_lines:
        result.append(
            TextSection(
                XLSX_AUXILIARY_SECTION_KIND,
                f"{source.section_id}:auxiliary",
                "\n".join(auxiliary_lines),
                _projection_provenance(source, stats, channel="auxiliary"),
            )
        )
    if not result:
        # Retain the item and its omission counters, without inventing a
        # computed value or admitting raw formulas as dense content.
        result.append(TextSection(
            XLSX_DENSE_SECTION_KIND,
            f"{source.section_id}:dense",
            "",
            _projection_provenance(source, stats, channel="dense"),
        ))
    return tuple(result)


def project_xlsx_sections(sections: Iterable[TextSection]) -> Iterator[TextSection]:
    """Yield projected sections while preserving source section order."""

    for section in sections:
        yield from project_xlsx_section(section)


__all__ = [
    "XLSX_AUXILIARY_SECTION_KIND",
    "XLSX_CELL_PREFIX",
    "XLSX_DENSE_SECTION_KIND",
    "XLSX_PROJECTION_POLICY",
    "XlsxProjectionStats",
    "project_xlsx_section",
    "project_xlsx_sections",
]
