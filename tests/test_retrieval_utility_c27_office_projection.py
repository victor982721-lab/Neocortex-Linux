"""Synthetic C27 checks for the bounded XLSX semantic projection."""

from __future__ import annotations

import json

import pytest

from neocortex.semantic.semantic_models import TextSection
from neocortex.semantic.semantic_office_projection import (
    XLSX_AUXILIARY_SECTION_KIND,
    XLSX_DENSE_SECTION_KIND,
    project_xlsx_section,
)
from neocortex.semantic.semantic_quality import assess_semantic_text


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _cell(
    sheet: str,
    a1: str,
    value: object,
    *,
    cell_type: str = "string",
    formula: object = None,
    cached_value: object = None,
) -> str:
    return "XLSX_CELL " + json.dumps(
        {
            "workbook": "private-workbook.xlsx",
            "sheet": sheet,
            "a1": a1,
            "type": cell_type,
            "value": value,
            "formula": formula,
            "cached_value": cached_value,
        },
        separators=(",", ":"),
    )


def test_projection_keeps_locator_values_and_scalar_zero_false() -> None:
    source = TextSection(
        "xlsx_document",
        "body",
        "\n".join(
            (
                _cell("Hoja 1", "A1", 0, cell_type="number"),
                _cell("Hoja 1", "B1", 0.0, cell_type="number"),
                _cell("Hoja 1", "C1", False, cell_type="boolean"),
                _cell(
                    "Hoja 1",
                    "D1",
                    "formula raw",
                    cell_type="number",
                    formula="A1+B1",
                    cached_value=0.0,
                ),
                _cell(
                    "Hoja 1",
                    "E1",
                    "not a result",
                    cell_type="number",
                    formula="A1+C1",
                    cached_value=None,
                ),
            )
        ),
        {"adapter": "synthetic-office"},
    )

    projected = project_xlsx_section(source)
    assert len(projected) == 1
    dense = projected[0]
    assert dense.section_kind == XLSX_DENSE_SECTION_KIND
    assert "Hoja 1!A1 [number] | 0" in dense.text
    assert "Hoja 1!B1 [number] | 0.0" in dense.text
    assert "Hoja 1!C1 [boolean] | false" in dense.text
    assert "Hoja 1!D1 [number formula-cached] | 0.0" in dense.text
    assert "Hoja 1!E1" not in dense.text
    assert dense.provenance["omitted_formula_without_cached_value"] == 1
    assert dense.provenance["workbook_name_in_text"] is False


def test_projection_preserves_unknown_records_in_auxiliary_channel() -> None:
    malformed = "XLSX_CELL {not-json}"
    source = TextSection(
        "xlsx_document",
        "body",
        "\n".join((_cell("Hoja", "A1", "dato"), "XLSX_SHARED_STRING_ORPHAN texto", malformed)),
    )

    projected = project_xlsx_section(source)
    assert [section.section_kind for section in projected] == [
        XLSX_DENSE_SECTION_KIND,
        XLSX_AUXILIARY_SECTION_KIND,
    ]
    auxiliary = projected[1]
    assert "XLSX_SHARED_STRING_ORPHAN texto" in auxiliary.text
    assert malformed in auxiliary.text
    assert auxiliary.provenance["malformed_cell_records"] == 1
    assert auxiliary.provenance["auxiliary_records"] == 2


def test_projection_locators_do_not_trigger_formula_dump_gate() -> None:
    source = TextSection(
        "xlsx_document",
        "body",
        "\n".join(_cell("Hoja", f"A{index}", f"valor-{index}") for index in range(1, 14)),
    )
    dense = project_xlsx_section(source)[0]

    assessment = assess_semantic_text(
        dense.text,
        section_kind=dense.section_kind,
        source_kind="xlsx",
    )
    assert assessment.eligible
    assert assessment.reason == "eligible"


def test_projection_is_noop_for_non_xlsx_sections() -> None:
    source = TextSection("pdf_page", "1", "-U9 X1 400 kV")
    assert project_xlsx_section(source) == (source,)


def test_projection_preserves_legacy_body_and_checks_mid_stream() -> None:
    import pytest
    from neocortex.semantic.semantic_models import TextSection

    legacy = TextSection("xlsx_document", "body", "A1 =SUM(B1:C1)", {})
    assert project_xlsx_section(legacy) == (legacy,)
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("projection deadline")
    body = "\n".join("legacy line" for _ in range(200))
    with pytest.raises(RuntimeError, match="projection deadline"):
        project_xlsx_section(TextSection("xlsx_document", "body", body, {}), checkpoint=stop)
    assert calls == 2


def test_all_uncached_formulas_preserve_empty_item_and_omission_provenance() -> None:
    import json
    from neocortex.semantic.semantic_models import TextSection
    text = "XLSX_CELL " + json.dumps({
        "sheet": "Sheet", "a1": "A1", "type": "formula", "value": None,
        "formula": "SUM(B1:C1)", "cached_value": None,
    })
    sections = project_xlsx_section(TextSection("xlsx_document", "body", text, {}))
    assert len(sections) == 1
    assert sections[0].text == ""
    assert sections[0].provenance["omitted_formula_without_cached_value"] == 1
    assert sections[0].provenance["cell_records"] == 1
