"""Typed, bounded XLSX workbook and cell extraction."""

from __future__ import annotations

import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path, PurePosixPath
from typing import Mapping

from .extraction_support import (
    CancellationCheckpoint,
    _ReadBudget,
    _TextAccumulator,
    _attribute_by_local_name,
    _bounded_member,
    _extract_part_text,
    _local_name,
)
from .models import MAX_XLSX_SHARED_STRINGS, OfficeExtractionError, XlsxCell
from neocortex.capabilities.formats.xml_safety import safe_xml_iterparse


SharedStringsExtractor = Callable[..., tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _XlsxSheet:
    name: str
    ordinal: int
    relationship_id: str | None


@dataclass(frozen=True, slots=True)
class _XlsxStyle:
    number_format: str | None
    is_date: bool


_XLSX_A1 = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z")
_XLSX_BUILTIN_DATE_FORMATS: Mapping[int, str] = {
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    27: "yyyy-mm-dd",
    28: "yyyy-mm-dd",
    29: "yyyy-mm-dd",
    30: "m/d/yy",
    31: "yyyy-mm-dd",
    32: "h:mm:ss",
    33: "h:mm:ss",
    34: "yyyy-mm-dd",
    35: "yyyy-mm-dd",
    36: "yyyy-mm-dd",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    50: "yyyy-mm-dd",
    51: "yyyy-mm-dd",
    52: "yyyy-mm-dd",
    53: "yyyy-mm-dd",
    54: "yyyy-mm-dd",
    55: "yyyy-mm-dd",
    56: "yyyy-mm-dd",
    57: "yyyy-mm-dd",
    58: "yyyy-mm-dd",
}


def _extract_xlsx(
    archive: zipfile.ZipFile,
    *,
    path: Path,
    names: Mapping[str, zipfile.ZipInfo],
    selected: tuple[zipfile.ZipInfo, ...],
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
    max_cells: int,
    shared_strings_extractor: SharedStringsExtractor,
) -> tuple[XlsxCell, ...]:
    workbook_info = names["xl/workbook.xml"]
    sheets, date_1904 = _extract_xlsx_workbook(
        archive,
        workbook_info,
        accumulator=accumulator,
        budget=budget,
        cancellation=cancellation,
    )
    relationship_targets = _extract_xlsx_relationships(
        archive,
        names.get("xl/_rels/workbook.xml.rels"),
        budget=budget,
        cancellation=cancellation,
    )
    styles = _extract_xlsx_styles(
        archive,
        names.get("xl/styles.xml"),
        budget=budget,
        cancellation=cancellation,
    )
    shared_strings = shared_strings_extractor(
        archive,
        names.get("xl/sharedstrings.xml"),
        accumulator=accumulator,
        budget=budget,
        cancellation=cancellation,
    )

    worksheet_parts, worksheet_infos = _xlsx_worksheet_parts(
        sheets,
        relationship_targets,
        selected,
    )
    cells: list[XlsxCell] = []
    _extract_xlsx_worksheets(
        archive,
        path=path,
        worksheet_parts=worksheet_parts,
        shared_strings=shared_strings,
        styles=styles,
        date_1904=date_1904,
        cells=cells,
        accumulator=accumulator,
        budget=budget,
        cancellation=cancellation,
        max_cells=max_cells,
    )
    _extract_remaining_xlsx_parts(
        archive,
        selected=selected,
        worksheet_infos=worksheet_infos,
        accumulator=accumulator,
        budget=budget,
        cancellation=cancellation,
    )
    return tuple(cells)


def _xlsx_sheet_targets(
    sheets: tuple[_XlsxSheet, ...],
    relationship_targets: Mapping[str, str],
) -> dict[str, _XlsxSheet]:
    by_part: dict[str, _XlsxSheet] = {}
    for sheet in sheets:
        if sheet.relationship_id is None:
            continue
        target = relationship_targets.get(sheet.relationship_id)
        if target is None:
            continue
        if target in by_part:
            raise OfficeExtractionError(
                "office_corrupt_container",
                f"multiple workbook sheets target {target}",
                recommendation="deletion_candidate",
                retryable=False,
            )
        by_part[target] = sheet
    return by_part


def _xlsx_worksheet_parts(
    sheets: tuple[_XlsxSheet, ...],
    relationship_targets: Mapping[str, str],
    selected: tuple[zipfile.ZipInfo, ...],
) -> tuple[tuple[tuple[_XlsxSheet, zipfile.ZipInfo], ...], tuple[zipfile.ZipInfo, ...]]:
    by_part = _xlsx_sheet_targets(sheets, relationship_targets)
    worksheet_infos = _xlsx_worksheet_infos(selected)
    assigned_ordinals = _assigned_sheet_ordinals(worksheet_infos, by_part)
    worksheet_parts = _build_worksheet_parts(
        worksheet_infos,
        sheets=sheets,
        by_part=by_part,
        assigned_ordinals=assigned_ordinals,
    )
    return worksheet_parts, worksheet_infos


def _xlsx_worksheet_infos(
    selected: tuple[zipfile.ZipInfo, ...],
) -> tuple[zipfile.ZipInfo, ...]:
    return tuple(
        sorted(
            (info for info in selected if info.filename.casefold().startswith("xl/worksheets/")),
            key=lambda info: info.filename.casefold(),
        )
    )


def _assigned_sheet_ordinals(
    worksheet_infos: tuple[zipfile.ZipInfo, ...],
    by_part: Mapping[str, _XlsxSheet],
) -> set[int]:
    assigned: set[int] = set()
    for info in worksheet_infos:
        mapped_sheet = by_part.get(info.filename.casefold())
        if mapped_sheet is not None:
            assigned.add(mapped_sheet.ordinal)
    return assigned


def _build_worksheet_parts(
    worksheet_infos: tuple[zipfile.ZipInfo, ...],
    *,
    sheets: tuple[_XlsxSheet, ...],
    by_part: Mapping[str, _XlsxSheet],
    assigned_ordinals: set[int],
) -> tuple[tuple[_XlsxSheet, zipfile.ZipInfo], ...]:
    fallbacks = iter(sheet for sheet in sheets if sheet.ordinal not in assigned_ordinals)
    worksheet_parts: list[tuple[_XlsxSheet, zipfile.ZipInfo]] = []
    synthetic_ordinal = max((sheet.ordinal for sheet in sheets), default=0)
    for info in worksheet_infos:
        selected_sheet: _XlsxSheet | None = by_part.get(info.filename.casefold())
        if selected_sheet is None:
            selected_sheet = next(fallbacks, None)
        if selected_sheet is None:
            synthetic_ordinal += 1
            selected_sheet = _XlsxSheet(
                PurePosixPath(info.filename).stem,
                synthetic_ordinal,
                None,
            )
        worksheet_parts.append((selected_sheet, info))
    worksheet_parts.sort(key=lambda item: (item[0].ordinal, item[1].filename.casefold()))
    return tuple(worksheet_parts)


def _extract_xlsx_worksheets(
    archive: zipfile.ZipFile,
    *,
    path: Path,
    worksheet_parts: tuple[tuple[_XlsxSheet, zipfile.ZipInfo], ...],
    shared_strings: tuple[str, ...],
    styles: tuple[_XlsxStyle, ...],
    date_1904: bool,
    cells: list[XlsxCell],
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
    max_cells: int,
) -> None:
    for sheet, info in worksheet_parts:
        cancellation.checkpoint()
        _extract_xlsx_worksheet(
            archive,
            info,
            workbook=str(path),
            sheet=sheet,
            shared_strings=shared_strings,
            styles=styles,
            date_1904=date_1904,
            cells=cells,
            accumulator=accumulator,
            budget=budget,
            cancellation=cancellation,
            max_cells=max_cells,
        )


def _extract_remaining_xlsx_parts(
    archive: zipfile.ZipFile,
    *,
    selected: tuple[zipfile.ZipInfo, ...],
    worksheet_infos: tuple[zipfile.ZipInfo, ...],
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> None:
    specialized = {
        "xl/workbook.xml",
        "xl/sharedstrings.xml",
        *(info.filename.casefold() for info in worksheet_infos),
    }
    for info in selected:
        if info.filename.casefold() in specialized:
            continue
        cancellation.checkpoint()
        _extract_part_text(
            archive,
            info,
            format_name="xlsx",
            accumulator=accumulator,
            budget=budget,
        )


def _extract_xlsx_workbook(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> tuple[tuple[_XlsxSheet, ...], bool]:
    source = _bounded_member(archive, info, budget)
    sheets: list[_XlsxSheet] = []
    date_1904 = False
    events = 0
    try:
        for _event, element in safe_xml_iterparse(source, events=("end",)):
            local = _local_name(element.tag)
            if local == "workbookPr":
                raw = element.attrib.get("date1904", "").casefold()
                date_1904 = raw in {"1", "true"}
            elif local == "sheet":
                name = element.attrib.get("name") or f"Sheet{len(sheets) + 1}"
                sheets.append(
                    _XlsxSheet(
                        name=name,
                        ordinal=len(sheets) + 1,
                        relationship_id=_attribute_by_local_name(element.attrib, "id"),
                    )
                )
                accumulator.add(name)
            elif local in {"t", "f", "definedName"}:
                accumulator.add(element.text)
            element.clear()
            events += 1
            if events % 1024 == 0:
                cancellation.checkpoint()
    finally:
        source.close()
    return tuple(sheets), date_1904


def _normalize_xlsx_relationship_target(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        normalized = normalized.lstrip("/")
    else:
        normalized = posixpath.join("xl", normalized)
    normalized = posixpath.normpath(normalized)
    if not normalized.startswith("xl/worksheets/") or normalized.startswith("../"):
        raise OfficeExtractionError(
            "office_corrupt_container",
            f"unsafe XLSX worksheet relationship target: {target}",
            recommendation="deletion_candidate",
            retryable=False,
        )
    return normalized.casefold()


def _extract_xlsx_relationships(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    *,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> dict[str, str]:
    if info is None:
        return {}
    source = _bounded_member(archive, info, budget)
    targets: dict[str, str] = {}
    count = 0
    try:
        for _event, element in safe_xml_iterparse(source, events=("end",)):
            if _local_name(element.tag) == "Relationship":
                relationship_type = element.attrib.get("Type", "")
                if relationship_type.rsplit("/", 1)[-1] == "worksheet":
                    relationship_id = element.attrib.get("Id")
                    target = element.attrib.get("Target")
                    if not relationship_id or not target or relationship_id in targets:
                        raise OfficeExtractionError(
                            "office_corrupt_container",
                            "invalid or duplicate XLSX worksheet relationship",
                            recommendation="deletion_candidate",
                            retryable=False,
                        )
                    targets[relationship_id] = _normalize_xlsx_relationship_target(target)
            element.clear()
            count += 1
            if count % 1024 == 0:
                cancellation.checkpoint()
    finally:
        source.close()
    return targets


def _looks_like_xlsx_date_format(format_code: str) -> bool:
    candidate = re.sub(r'"(?:[^"]|"")*"', "", format_code)
    candidate = re.sub(r"\\.", "", candidate)
    candidate = re.sub(r"[_*].", "", candidate)

    def replace_bracket(match: re.Match[str]) -> str:
        content = match.group(1).casefold()
        return content if re.fullmatch(r"[hms]+", content) else ""

    candidate = re.sub(r"\[([^]]*)\]", replace_bracket, candidate).casefold()
    return re.search(r"[ymdhs]", candidate) is not None


def _extract_xlsx_styles(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    *,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> tuple[_XlsxStyle, ...]:
    if info is None:
        return ()
    source = _bounded_member(archive, info, budget)
    custom_formats: dict[int, str] = {}
    styles: list[_XlsxStyle] = []
    inside_cell_xfs = False
    try:
        for count, (event, element) in enumerate(
            safe_xml_iterparse(source, events=("start", "end")),
            start=1,
        ):
            inside_cell_xfs = _consume_xlsx_style_event(
                event,
                element,
                inside_cell_xfs=inside_cell_xfs,
                custom_formats=custom_formats,
                styles=styles,
            )
            if event == "end" and count % 1024 == 0:
                cancellation.checkpoint()
    finally:
        source.close()
    return tuple(styles)


def _consume_xlsx_style_event(
    event: str,
    element: ET.Element,
    *,
    inside_cell_xfs: bool,
    custom_formats: dict[int, str],
    styles: list[_XlsxStyle],
) -> bool:
    local = _local_name(element.tag)
    if event == "start":
        return inside_cell_xfs or local == "cellXfs"
    if local == "numFmt":
        _record_xlsx_number_format(element, custom_formats)
    elif local == "xf" and inside_cell_xfs:
        styles.append(_xlsx_style_from_element(element, custom_formats))
    elif local == "cellXfs":
        inside_cell_xfs = False
    element.clear()
    return inside_cell_xfs


def _record_xlsx_number_format(
    element: ET.Element,
    custom_formats: dict[int, str],
) -> None:
    try:
        number_format_id = int(element.attrib["numFmtId"])
    except (KeyError, ValueError):
        return
    custom_formats[number_format_id] = element.attrib.get("formatCode", "")


def _xlsx_style_from_element(
    element: ET.Element,
    custom_formats: Mapping[int, str],
) -> _XlsxStyle:
    try:
        number_format_id = int(element.attrib.get("numFmtId", "0"))
    except ValueError:
        number_format_id = 0
    number_format = custom_formats.get(number_format_id)
    if number_format is None:
        number_format = _XLSX_BUILTIN_DATE_FORMATS.get(number_format_id)
    is_date = number_format_id in _XLSX_BUILTIN_DATE_FORMATS or (
        number_format is not None and _looks_like_xlsx_date_format(number_format)
    )
    return _XlsxStyle(number_format=number_format, is_date=is_date)


def _extract_xlsx_shared_strings(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    *,
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> tuple[str, ...]:
    if info is None:
        return ()
    source = _bounded_member(archive, info, budget)
    values: list[str] = []
    root: ET.Element | None = None
    try:
        for event, element in safe_xml_iterparse(source, events=("start", "end")):
            if root is None and event == "start":
                root = element
            if event != "end":
                continue
            appended = _consume_shared_string_element(element, accumulator, values)
            if appended and len(values) % 1024 == 0:
                cancellation.checkpoint()
                if root is not None:
                    root.clear()
    finally:
        source.close()
    return tuple(values)


def _consume_shared_string_element(
    element: ET.Element,
    accumulator: _TextAccumulator,
    values: list[str],
) -> bool:
    local = _local_name(element.tag)
    if local == "t":
        accumulator.add(element.text)
        return False
    if local != "si":
        return False
    if len(values) >= MAX_XLSX_SHARED_STRINGS:
        raise OfficeExtractionError(
            "office_shared_string_limit",
            f"XLSX exceeds {MAX_XLSX_SHARED_STRINGS} shared strings",
            recommendation="manual_review",
            retryable=False,
        )
    values.append(
        "".join(child.text or "" for child in element.iter() if _local_name(child.tag) == "t")
    )
    element.clear()
    return True


def _normalize_xlsx_cell_reference(value: str) -> str:
    match = _XLSX_A1.fullmatch(value)
    if match is None:
        raise OfficeExtractionError(
            "office_invalid_cell_reference",
            f"invalid XLSX cell reference: {value!r}",
            recommendation="manual_review",
            retryable=False,
        )
    column_text, row_text = match.groups()
    column = 0
    for character in column_text.upper():
        column = column * 26 + ord(character) - ord("A") + 1
    row = int(row_text)
    if column > 16_384 or row > 1_048_576:
        raise OfficeExtractionError(
            "office_invalid_cell_reference",
            f"XLSX cell reference is outside the worksheet grid: {value!r}",
            recommendation="manual_review",
            retryable=False,
        )
    return f"{column_text.upper()}{row}"


def _xlsx_style(
    element: ET.Element,
    styles: tuple[_XlsxStyle, ...],
) -> tuple[int | None, _XlsxStyle | None]:
    raw = element.attrib.get("s")
    if raw is None:
        return None, None
    try:
        index = int(raw)
    except ValueError:
        return None, None
    if index < 0:
        return None, None
    return index, styles[index] if index < len(styles) else None


def _excel_serial_to_iso(value: str, *, date_1904: bool) -> str:
    try:
        serial = Decimal(value)
    except InvalidOperation:
        return value
    if not serial.is_finite():
        return value
    microseconds_per_day = Decimal(86_400_000_000)
    try:
        leap_day = _excel_leap_day_iso(serial, date_1904, microseconds_per_day)
        if leap_day is not None:
            return leap_day
        converted = _excel_serial_datetime(serial, date_1904, microseconds_per_day)
    except (OverflowError, ValueError):
        return value
    if converted.time() == datetime.min.time():
        return converted.date().isoformat()
    return converted.isoformat()


def _excel_leap_day_iso(
    serial: Decimal,
    date_1904: bool,
    microseconds_per_day: Decimal,
) -> str | None:
    if date_1904 or not Decimal(60) <= serial < Decimal(61):
        return None
    fraction = serial - Decimal(60)
    micros = int((fraction * microseconds_per_day).to_integral_value(rounding=ROUND_HALF_EVEN))
    if micros == 0:
        return "1900-02-29"
    time_value = (datetime(1900, 3, 1) + timedelta(microseconds=micros)).time()
    return f"1900-02-29T{time_value.isoformat()}"


def _excel_serial_datetime(
    serial: Decimal,
    date_1904: bool,
    microseconds_per_day: Decimal,
) -> datetime:
    adjusted = serial if date_1904 or serial < 60 else serial - 1
    base = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 31)
    micros = int((adjusted * microseconds_per_day).to_integral_value(rounding=ROUND_HALF_EVEN))
    return base + timedelta(microseconds=micros)


def _xlsx_cell_from_element(
    element: ET.Element,
    *,
    workbook: str,
    sheet: _XlsxSheet,
    shared_strings: tuple[str, ...],
    styles: tuple[_XlsxStyle, ...],
    date_1904: bool,
) -> XlsxCell | None:
    formula, raw_value, inline_value = _xlsx_cell_payload(element)
    if formula is None and raw_value in {None, ""} and inline_value in {None, ""}:
        return None
    reference = _required_xlsx_cell_reference(element)
    style_index, style = _xlsx_style(element, styles)
    declared_type = element.attrib.get("t")
    cell_type, value, preserved_raw = _xlsx_typed_cell_value(
        declared_type,
        raw_value=raw_value,
        inline_value=inline_value,
        shared_strings=shared_strings,
        style=style,
        date_1904=date_1904,
        cell_label=f"{sheet.name}!{reference}",
    )
    return XlsxCell(
        workbook=workbook,
        sheet=sheet.name,
        sheet_ordinal=sheet.ordinal,
        cell_reference=reference,
        cell_type=cell_type,
        value=value,
        raw_value=preserved_raw,
        formula=formula,
        cached_value=preserved_raw if formula is not None else None,
        style_index=style_index,
        number_format=None if style is None else style.number_format,
    )


def _xlsx_cell_payload(element: ET.Element) -> tuple[str | None, str | None, str | None]:
    children = {_local_name(child.tag): child for child in element}
    formula_element = children.get("f")
    value_element = children.get("v")
    inline_element = children.get("is")
    formula = None if formula_element is None else formula_element.text or ""
    raw_value = None if value_element is None else value_element.text or ""
    inline_value = None if inline_element is None else _xlsx_inline_text(inline_element)
    return formula, raw_value, inline_value


def _xlsx_inline_text(element: ET.Element) -> str:
    return "".join(child.text or "" for child in element.iter() if _local_name(child.tag) == "t")


def _required_xlsx_cell_reference(element: ET.Element) -> str:
    reference = element.attrib.get("r")
    if reference:
        return _normalize_xlsx_cell_reference(reference)
    raise OfficeExtractionError(
        "office_invalid_cell_reference",
        "non-empty XLSX cell lacks an A1 reference",
        recommendation="manual_review",
        retryable=False,
    )


def _xlsx_typed_cell_value(
    declared_type: str | None,
    *,
    raw_value: str | None,
    inline_value: str | None,
    shared_strings: tuple[str, ...],
    style: _XlsxStyle | None,
    date_1904: bool,
    cell_label: str,
) -> tuple[str, str, str | None]:
    special = _xlsx_special_cell_value(
        declared_type,
        raw_value=raw_value,
        inline_value=inline_value,
        shared_strings=shared_strings,
        cell_label=cell_label,
    )
    if special is not None:
        return special
    simple = _xlsx_simple_cell_value(declared_type, raw_value)
    if simple is not None:
        return simple
    if declared_type in {None, "n"}:
        return _xlsx_numeric_value(raw_value, style, date_1904)
    assert declared_type is not None
    return _xlsx_unknown_cell_value(declared_type, raw_value, inline_value)


__all__ = (
    "SharedStringsExtractor",
    "_extract_xlsx",
    "_extract_xlsx_shared_strings",
)


def _xlsx_special_cell_value(
    declared_type: str | None,
    *,
    raw_value: str | None,
    inline_value: str | None,
    shared_strings: tuple[str, ...],
    cell_label: str,
) -> tuple[str, str, str | None] | None:
    if declared_type == "s":
        value = _xlsx_shared_value(raw_value, shared_strings, cell_label)
        return "shared_string", value, raw_value
    if declared_type == "inlineStr":
        return "inline_string", inline_value or "", inline_value
    if declared_type == "b":
        value = {"0": "false", "1": "true"}.get(raw_value or "", raw_value or "")
        return "boolean", value, raw_value
    return None


def _xlsx_simple_cell_value(
    declared_type: str | None,
    raw_value: str | None,
) -> tuple[str, str, str | None] | None:
    cell_type = {"e": "error", "d": "date", "str": "string"}.get(declared_type or "")
    if cell_type is None:
        return None
    return cell_type, raw_value or "", raw_value


def _xlsx_unknown_cell_value(
    declared_type: str,
    raw_value: str | None,
    inline_value: str | None,
) -> tuple[str, str, str | None]:
    if raw_value is not None:
        return f"unknown:{declared_type}", raw_value, raw_value
    return f"unknown:{declared_type}", inline_value or "", inline_value


def _xlsx_shared_value(
    raw_value: str | None,
    shared_strings: tuple[str, ...],
    cell_label: str,
) -> str:
    try:
        return shared_strings[int(raw_value or "")]
    except (ValueError, IndexError):
        raise OfficeExtractionError(
            "office_invalid_shared_string",
            f"{cell_label} references an unavailable shared string",
            recommendation="manual_review",
            retryable=False,
        ) from None


def _xlsx_numeric_value(
    raw_value: str | None,
    style: _XlsxStyle | None,
    date_1904: bool,
) -> tuple[str, str, str | None]:
    if style is not None and style.is_date and raw_value not in {None, ""}:
        assert raw_value is not None
        return "date", _excel_serial_to_iso(raw_value, date_1904=date_1904), raw_value
    return "number", raw_value or "", raw_value


def _xlsx_cell_projection(cell: XlsxCell) -> str:
    workbook_name = cell.workbook.replace("\\", "/").rsplit("/", 1)[-1]
    return "XLSX_CELL " + json.dumps(
        {
            "workbook": workbook_name,
            "sheet": cell.sheet,
            "a1": cell.cell_reference,
            "type": cell.cell_type,
            "value": cell.value,
            "formula": cell.formula,
            "cached_value": cell.cached_value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_xlsx_worksheet(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    workbook: str,
    sheet: _XlsxSheet,
    shared_strings: tuple[str, ...],
    styles: tuple[_XlsxStyle, ...],
    date_1904: bool,
    cells: list[XlsxCell],
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
    max_cells: int,
) -> None:
    source = _bounded_member(archive, info, budget)
    references: set[str] = set()
    try:
        for _event, element in safe_xml_iterparse(source, events=("end",)):
            local = _local_name(element.tag)
            if local == "c":
                cell = _xlsx_cell_from_element(
                    element,
                    workbook=workbook,
                    sheet=sheet,
                    shared_strings=shared_strings,
                    styles=styles,
                    date_1904=date_1904,
                )
                if cell is not None:
                    _append_xlsx_cell(
                        cell,
                        sheet=sheet,
                        references=references,
                        cells=cells,
                        accumulator=accumulator,
                        max_cells=max_cells,
                    )
                    if len(cells) % 1024 == 0:
                        cancellation.checkpoint()
                element.clear()
            elif local == "row":
                element.clear()
    finally:
        source.close()


def _append_xlsx_cell(
    cell: XlsxCell,
    *,
    sheet: _XlsxSheet,
    references: set[str],
    cells: list[XlsxCell],
    accumulator: _TextAccumulator,
    max_cells: int,
) -> None:
    if cell.cell_reference in references:
        raise OfficeExtractionError(
            "office_duplicate_cell_reference",
            f"duplicate XLSX cell {sheet.name}!{cell.cell_reference}",
            recommendation="manual_review",
            retryable=False,
        )
    if len(cells) >= max_cells:
        raise OfficeExtractionError(
            "office_xlsx_cell_limit",
            f"XLSX exceeds {max_cells} non-empty cells",
            recommendation="manual_review",
            retryable=False,
        )
    references.add(cell.cell_reference)
    cells.append(cell)
    accumulator.add(_xlsx_cell_projection(cell))
