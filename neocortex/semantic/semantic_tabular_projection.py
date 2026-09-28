"""Bounded dense navigation for identifier-heavy tables; full FTS is retained.

This is not an exclusion or a deletion policy. It projects small, explicitly
labelled metadata summaries only when rows consistently describe paths/IDs,
and leaves ordinary narrative/business tables unchanged. Exact values remain
available in the source-owned text/FTS, not silently dropped from the corpus.
"""

from __future__ import annotations

import csv
import io
import itertools
import re
from dataclasses import replace
from collections.abc import Callable

from .semantic_models import TextSection


TABULAR_METADATA_POLICY = "identifier-table-navigation-v2-content-shape"
_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/]|file://)")
_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:")
_SAMPLE_ROWS = 64
_MIN_ROWS = 32
_MAX_COLUMNS = 64
_MAX_VALUES = 3
_MAX_VALUE_CHARS = 120


def _path_cell(value: str) -> bool:
    return bool(_PATH.match(value.strip()))


def _metadata_row(row: list[str]) -> bool:
    if not row or len(row) > _MAX_COLUMNS or not any(_path_cell(cell) for cell in row):
        return False
    # A path alongside real narrative does not justify reducing that narrative.
    return not any(
        not _path_cell(cell) and (len(cell) > 400 or len(cell.split()) > 24)
        for cell in row
    )


def metadata_table_section(
    text: str, content_kind: str, *, checkpoint: Callable[[], None],
) -> TextSection | None:
    """Return an explicitly non-exhaustive navigation summary, or abstain.

    Bounds apply to retained values, not the original FTS representation. The
    complete row stream is checked before reducing it; a late narrative row
    prevents the transformation rather than silently discarding its content.
    """
    if content_kind in {"txt", "text", "plain"}:
        # Identify conservatively calls many real CSV/TSV files text/plain.
        # Infer only through the same complete, strict row-shape proof; never
        # trust a suffix or turn a narrative TXT into a reduced representation.
        candidates = [
            section for kind in ("csv", "tsv")
            if (section := metadata_table_section(text, kind, checkpoint=checkpoint)) is not None
        ]
        if len(candidates) != 1:
            return None
        section = candidates[0]
        return replace(section, provenance={
            **section.provenance,
            "declared_content_kind": content_kind,
            "delimited_format_inferred": True,
        })
    if content_kind not in {"csv", "tsv"}:
        return None
    checkpoint()
    reader = csv.reader(io.StringIO(text), delimiter="\t" if content_kind == "tsv" else ",")
    try:
        sample = list(itertools.islice(reader, _SAMPLE_ROWS))
        if len(sample) < _MIN_ROWS:
            return None
        width = len(sample[0])
        if not 2 <= width <= _MAX_COLUMNS:
            return None
        first_is_header = not _metadata_row(sample[0]) and all(
            re.fullmatch(r"[A-Za-z_][\w .-]{0,63}", cell) for cell in sample[0]
        )
        data_sample = sample[1:] if first_is_header else sample
        if any(len(row) != width or not _metadata_row(row) for row in data_sample):
            return None
        columns = sample[0] if first_is_header else [f"campo_{n + 1}" for n in range(width)]
        values: list[list[str]] = [[] for _ in range(width)]
        omitted = [0] * width
        row_count = 0
        session_log_rows = 0
        for row in itertools.chain(data_sample, reader):
            if row_count % _SAMPLE_ROWS == 0:
                checkpoint()
            if len(row) != width or not _metadata_row(row):
                return None
            row_count += 1
            session_log_rows += int(
                any(_DATE.match(cell) for cell in row)
                and any(_UUID.fullmatch(cell) for cell in row)
                and any(_path_cell(cell) and cell.endswith(".jsonl") for cell in row)
            )
            for index, cell in enumerate(row):
                value = cell.strip()
                if not value or value in values[index]:
                    continue
                if len(values[index]) < _MAX_VALUES:
                    # Keep both ends of paths: the tail often carries the useful
                    # filename/directory while the exact path remains in FTS.
                    if len(value) > _MAX_VALUE_CHARS:
                        value = value[:39] + " … " + value[-78:]
                    values[index].append(value)
                else:
                    omitted[index] += 1
        checkpoint()
    except csv.Error:
        return None
    session_shape = session_log_rows == row_count and row_count > 0
    lines = [
        "Resumen de navegación de una tabla de metadatos de archivos y rutas.",
        "File paths and metadata table; full original rows remain available in lexical search.",
        f"Formato: {content_kind.upper()}. Registros: {row_count}. Columnas: {width}.",
        "Muestras no exhaustivas; no sustituyen los registros originales.",
    ]
    if session_shape:
        lines.append("Estructura inferida: registros de sesiones y directorio de trabajo; session logs and working directories.")
    for index, name in enumerate(columns):
        lines.append(f"{name}: " + " | ".join(values[index]))
    return TextSection(
        section_kind="text_metadata_navigation",
        section_id=TABULAR_METADATA_POLICY,
        text="\n".join(lines),
        provenance={
            "policy_signature": TABULAR_METADATA_POLICY,
            "basis": "parsed_identifier_heavy_table",
            "advisory_only": True,
            "representation": "navigation_summary_not_full_rows",
            "coverage": "summary_with_complete_source_lexical_body",
            "source_rows": row_count,
            "source_columns": width,
            "source_header_present": first_is_header,
            "max_sample_values_per_column": _MAX_VALUES,
            "omitted_sample_values": sum(omitted),
            "session_shape_inferred": session_shape,
            "original_rows_unchanged": True,
        },
    )
