"""Regressions for Office extraction ordering and XLSX projections."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.office.extraction import extract_office_document
from neocortex.capabilities.formats.office.route import (
    ODT_MIME,
    OfficeRoute,
    OfficeRouteConfig,
    PPTX_MIME,
    search_office_state,
)
from neocortex.capabilities.formats.office.state import office_database
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from tests.test_office_route import FakeFrameworkRouteState, _write_pptx


@pytest.fixture(autouse=True)
def private_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any framework-side paths private to this regression file."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _write_odt(path: Path) -> None:
    content = """<?xml version="1.0" encoding="UTF-8"?>
    <office:document-content
      xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
      xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
      <office:body><office:text>
        <text:p>Antes <text:span>centro</text:span> después
          <text:a>enlace <text:span>anidado</text:span> final</text:a>
          <text:note><text:note-body><text:p>nota interna tail</text:p>
          </text:note-body></text:note> cola</text:p>
        <text:h>Segundo <text:span>título</text:span> tail</text:h>
      </office:text></office:body>
    </office:document-content>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content)


def _write_xlsx(path: Path, *, shared: bool) -> None:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    if shared:
        cell_a1 = '<c r="A1" t="s"><v>0</v></c>'
        cell_b2 = '<c r="B2" t="s"><v>0</v></c>'
        shared_part = f'<sst xmlns="{main}"><si><r><t>VISIBLE</t></r><r><t> Ω</t></r></si><si><t>ORPHAN</t></si></sst>'
        content_type = '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        shared_relationship = f'<Relationship Id="rId2" Type="{relationships}/sharedStrings" Target="sharedStrings.xml"/>'
    else:
        cell_a1 = '<c r="A1" t="inlineStr"><is><r><t>VISIBLE</t></r><r><t> Ω</t></r></is></c>'
        cell_b2 = '<c r="B2" t="inlineStr"><is><t>VISIBLE Ω</t></is></c>'
        shared_part = content_type = shared_relationship = ""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f"<Types xmlns=\"{main}\"><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>{content_type}</Types>",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook xmlns=\"{main}\" xmlns:r=\"{relationships}\"><sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f"<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"{relationships}/worksheet\" Target=\"worksheets/sheet1.xml\"/>{shared_relationship}</Relationships>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet xmlns=\"{main}\"><sheetData><row r=\"1\">{cell_a1}</row><row r=\"2\">{cell_b2}<c r=\"C2\"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>",
        )
        if shared:
            archive.writestr("xl/sharedStrings.xml", shared_part)


def _office_route(state: Path, source: Path, mime: str, run_id: int) -> OfficeRoute:
    return OfficeRoute(
        OfficeRouteConfig(state, min_free_memory_bytes=0, min_free_commit_bytes=0),
        FakeFrameworkRouteState({mime: (snapshot_path(source),)}),  # type: ignore[arg-type]
        run_id,
        cancellation=CancellationToken(),
    )


def test_odt_spans_links_and_tails_keep_document_order_and_replay(tmp_path: Path) -> None:
    source = tmp_path / "mixed.odt"
    state = tmp_path / "state" / "office.sqlite3"
    _write_odt(source)

    direct = extract_office_document(
        source,
        "odt",
        max_text_chars=10_000,
        cancellation=CancellationToken(),
    )
    assert direct.text == (
        "Antes centro después enlace anidado final nota interna tail cola\n"
        "Segundo título tail"
    )

    route = _office_route(state, source, ODT_MIME, 1)
    first = route.run()
    assert (first.extracted, first.errors) == (1, 0)
    with office_database(state, readonly=True) as connection:
        body = connection.execute("SELECT text_zlib FROM documents").fetchone()[0]
    import zlib

    assert zlib.decompress(body).decode("utf-8") == direct.text

    replay = _office_route(state, source, ODT_MIME, 2).run()
    assert (replay.cache_hits, replay.extracted, replay.errors) == (1, 0, 0)
    assert search_office_state(state, "después")[0]["path"] == str(source)


def test_xlsx_shared_strings_are_dictionary_only_and_cells_remain_typed(tmp_path: Path) -> None:
    shared = tmp_path / "shared.xlsx"
    inline = tmp_path / "inline.xlsx"
    _write_xlsx(shared, shared=True)
    _write_xlsx(inline, shared=False)

    shared_result = extract_office_document(
        shared,
        "xlsx",
        max_text_chars=10_000,
        cancellation=CancellationToken(),
    )
    inline_result = extract_office_document(
        inline,
        "xlsx",
        max_text_chars=10_000,
        cancellation=CancellationToken(),
    )

    # Two visible cells legitimately produce two projections in either
    # encoding; the shared dictionary must not add a third occurrence.
    assert shared_result.text.count("VISIBLE Ω") == 2
    assert inline_result.text.count("VISIBLE Ω") == 2
    assert shared_result.text.count("XLSX_SHARED_STRING_ORPHAN ORPHAN") == 1
    assert [
        (cell.cell_reference, cell.cell_type, cell.value, cell.formula, cell.cached_value)
        for cell in shared_result.xlsx_cells
    ] == [
        ("A1", "shared_string", "VISIBLE Ω", None, None),
        ("B2", "shared_string", "VISIBLE Ω", None, None),
        ("C2", "number", "2", "1+1", "2"),
    ]


def test_office_extraction_signatures_invalidate_only_changed_formats(tmp_path: Path) -> None:
    config = OfficeRouteConfig(tmp_path / "state" / "office.sqlite3")
    assert config.processing_signature_for("pptx") == config.processing_signature
    assert config.processing_signature_for("odt") != config.processing_signature
    assert config.processing_signature_for("xlsx") != config.processing_signature

    pptx = tmp_path / "stable.pptx"
    pptx_state = tmp_path / "pptx.sqlite3"
    _write_pptx(pptx)
    first_pptx = _office_route(pptx_state, pptx, PPTX_MIME, 1).run()
    replay_pptx = _office_route(pptx_state, pptx, PPTX_MIME, 2).run()
    assert (first_pptx.extracted, replay_pptx.cache_hits, replay_pptx.extracted) == (1, 1, 0)

    odt = tmp_path / "changed.odt"
    odt_state = tmp_path / "odt.sqlite3"
    _write_odt(odt)
    first_odt = _office_route(odt_state, odt, ODT_MIME, 1).run()
    assert first_odt.extracted == 1
    # Simulate a pre-contract row: ODT used the old route-wide signature.
    with office_database(odt_state) as connection:
        connection.execute(
            "UPDATE documents SET processing_signature=?",
            (config.processing_signature,),
        )
        connection.commit()
    replay_odt = _office_route(odt_state, odt, ODT_MIME, 2).run()
    assert (replay_odt.cache_hits, replay_odt.extracted) == (0, 1)


def test_odt_completed_empty_blocks_are_unlinked_not_retained_until_eof(tmp_path: Path, monkeypatch) -> None:
    from neocortex.capabilities.formats.office import extraction_support as support
    path = tmp_path / "many.odt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", ODT_MIME)
        archive.writestr("content.xml", '<root xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">' + '<text:p/>' * 20000 + '</root>')
    original = support.safe_xml_iterparse
    peak_children = 0
    def observe(*args, **kwargs):
        nonlocal peak_children
        root = None
        for event, node in original(*args, **kwargs):
            if root is None:
                root = node
            yield event, node
            peak_children = max(peak_children, len(root))
    monkeypatch.setattr(support, 'safe_xml_iterparse', observe)
    extract_office_document(path, 'odt', max_text_chars=100, cancellation=CancellationToken())
    # iterparse may read ahead a bounded input block; it must not retain all N.
    assert peak_children < 5000
