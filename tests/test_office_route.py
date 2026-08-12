from __future__ import annotations

import os
import sqlite3
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

import _04_Nucleo_Operativo.office_route as office_route_module
import _04_Nucleo_Operativo.office_state as office_state_module
from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.cli_config import framework_config_from_args
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from _04_Nucleo_Operativo.document_catalog import (
    document_catalog_database,
    update_document_catalog,
)
from _04_Nucleo_Operativo.document_organization import (
    apply_document_organization,
    plan_document_organization,
)
from _04_Nucleo_Operativo.office_route import (
    OFFICE_MIME_FORMATS,
    ODT_MIME,
    PPTX_MIME,
    XLSX_MIME,
    OfficeRoute,
    OfficeRouteConfig,
    search_office_state,
)
from _04_Nucleo_Operativo.office_state import (
    OFFICE_SCHEMA_VERSION,
    initialize_office_state,
    office_database,
)
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from neocortex.platform_policy import sqlite_path_collation
from tests.internal_paths_test_support import disjoint_internal_paths_policy


# region [01] Minimal safe office fixtures and framework-state double


class FakeFrameworkRouteState:
    def __init__(self, candidates: dict[str, tuple[FileSnapshot, ...]]):
        self.candidates = candidates
        self.reviews: list[Any] = []
        self.resolutions: list[Any] = []

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        values = self.candidates.get(mime, ())
        eligible = (
            values
            if max_file_bytes is None
            else tuple(item for item in values if item.size <= max_file_bytes)
        )
        return len(values), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        yield from self.candidates.get(mime, ())

    def store_review_candidates(self, _run_id: int, candidates) -> None:
        self.reviews.extend(candidates)

    def resolve_review_candidates(
        self,
        _run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        note: str,
    ) -> None:
        self.resolutions.append((route_name, snapshot.path, note))

    def reconcile_review_candidates(
        self,
        run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        note: str,
        *,
        evaluated_reason_codes,
        active_reason_codes,
    ) -> None:
        self.resolutions.append(
            (
                run_id,
                route_name,
                snapshot.path,
                note,
                frozenset(evaluated_reason_codes),
                frozenset(active_reason_codes),
            )
        )

    def reconcile_review_candidates_batch(
        self,
        run_id: int,
        route_name: str,
        reconciliations,
    ) -> None:
        for reconciliation in reconciliations:
            self.resolutions.append(
                (
                    run_id,
                    route_name,
                    reconciliation.snapshot.path,
                    reconciliation.resolution_note,
                    frozenset(reconciliation.evaluated_reason_codes),
                    frozenset(reconciliation.active_reason_codes),
                )
            )


def _core(title: str, author: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
</cp:coreProperties>"""


def _write_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", _core("Orden de compra CHINT", "CHINT Electric"))
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="urn:test"><sheets>
            <sheet name="Materiales transformador"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="urn:test"><si><t>Requisición de materiales</t></si>
            <si><t>Interruptor de potencia y transformador</t></si></sst>""",
        )


def _write_typed_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", _core("Mediciones U2", "ANDRITZ"))
        archive.writestr(
            "xl/workbook.xml",
            """<workbook
             xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <workbookPr date1904="0"/>
              <sheets><sheet name="Mediciones U2" sheetId="9" r:id="rId7"/></sheets>
              <definedNames><definedName name="Entrada">'Mediciones U2'!$C$3</definedName></definedNames>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId7"
               Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
               Target="worksheets/measurements.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <si><r><t>Interruptor</t></r><r><t xml:space="preserve"> principal</t></r></si>
            </sst>""",
        )
        archive.writestr(
            "xl/styles.xml",
            """<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>
              <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>
            </styleSheet>""",
        )
        archive.writestr(
            "xl/worksheets/measurements.xml",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="s"><v>0</v></c></row>
                <row r="2"><c r="B2" t="inlineStr"><is><r><t>Tratamiento</t></r><r><t xml:space="preserve"> de aceite</t></r></is></c></row>
                <row r="3"><c r="C3" t="n"><v>1234.50</v></c></row>
                <row r="4"><c r="D4" s="1"><v>45292</v></c></row>
                <row r="5"><c r="E5" t="b"><v>1</v></c></row>
                <row r="6"><c r="F6" t="e"><v>#DIV/0!</v></c></row>
                <row r="7"><c r="G7" t="str"><f>CONCAT(&quot;O&quot;,&quot;K&quot;)</f><v>OK</v></c></row>
                <row r="8"><c r="H8"><f>SUM(C3,1)</f><v>1235.5</v></c></row>
                <row r="9"><c r="I9" t="d"><v>2026-08-09T17:49:00Z</v></c></row>
                <row r="10"><c r="J10"/></row>
                <row r="11"><c r="K11"><f>NOW()</f></c></row>
              </sheetData>
            </worksheet>""",
        )


def _write_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "docProps/core.xml",
            _core("Curso CFE de transformadores", "Comisión Federal de Electricidad"),
        )
        archive.writestr("ppt/presentation.xml", "<presentation/>")
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Material didáctico
            de capacitación en pruebas eléctricas</a:t></p:sld>""",
        )


def _write_odt(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "content.xml",
            """<office:document xmlns:office="urn:office" xmlns:text="urn:text">
            <text:h>Procedimiento SERINTRA</text:h>
            <text:p>Instructivo de mantenimiento de subestaciones</text:p>
            </office:document>""",
        )


def _route_for(state_path: Path, source_paths: dict[str, Path]):
    candidates: dict[str, tuple[FileSnapshot, ...]] = {
        mime: (snapshot_path(source_paths[mime]),) for mime in source_paths
    }
    framework = FakeFrameworkRouteState(candidates)
    route = OfficeRoute(
        OfficeRouteConfig(
            state_path=state_path,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )
    return route, framework


def _index_key_collations(
    connection: sqlite3.Connection,
    index_name: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[4]).upper()
        for row in connection.execute(f'PRAGMA index_xinfo("{index_name}")')
        if bool(row[5])
    )


def _create_populated_office_v2_state(path: Path) -> None:
    body = "Medición de relación de transformación"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in (
            office_state_module._OFFICE_V1_SCHEMA_DDL + office_state_module._OFFICE_V2_SCHEMA_DDL
        ):
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','2')")
        connection.execute(
            """INSERT INTO documents(
            file_key,format,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,author,subject,text_zlib,text_chars,text_xxh3_128,part_count,
            last_seen_run_id,updated_ns)
            VALUES('legacy-office','xlsx','/Corpus/Case.xlsx',10,20,30,'office-v2',
            'complete','Mediciones','ANDRITZ','U2',?,?,?,1,7,99)""",
            (zlib.compress(body.encode("utf-8")), len(body), "legacy-office-text"),
        )
        connection.execute(
            """INSERT INTO office_inventory(
            file_key,format,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
            VALUES('legacy-office','xlsx','/Corpus/Case.xlsx',10,20,30,7)"""
        )
        connection.execute(
            """INSERT INTO xlsx_cells(
            file_key,workbook,sheet,sheet_ordinal,cell_reference,cell_type,value,
            raw_value,formula,cached_value,style_index,number_format)
            VALUES('legacy-office','/Corpus/Case.xlsx','U2',1,'C3','number',
            '1234.5','1234.5','SUM(C1:C2)','1234.5',1,'0.0')"""
        )
        connection.execute(
            """INSERT INTO document_fts(
            rowid,file_key,format,path,title,author,body)
            VALUES(73,'legacy-office','xlsx','/Corpus/Case.xlsx',
            'Mediciones','ANDRITZ',?)""",
            (body,),
        )
        connection.commit()


# endregion [01]


# region [02] Incremental extraction, classification and corrupt review


def test_office_v2_path_collation_migration_preserves_populated_state_and_reopens(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "office-v2.sqlite3"
    _create_populated_office_v2_state(state_path)

    initialize_office_state(state_path)

    with office_database(state_path, readonly=True) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == str(OFFICE_SCHEMA_VERSION)
        assert tuple(connection.execute("SELECT path,text_chars FROM documents").fetchone()) == (
            "/Corpus/Case.xlsx",
            len("Medición de relación de transformación"),
        )
        assert connection.execute("SELECT COUNT(*) FROM office_inventory").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT workbook,sheet,cell_reference,value FROM xlsx_cells"
            ).fetchone()
        ) == ("/Corpus/Case.xlsx", "U2", "C3", "1234.5")
        assert tuple(
            connection.execute("SELECT rowid,file_key,path FROM document_fts").fetchone()
        ) == (73, "legacy-office", "/Corpus/Case.xlsx")
        assert _index_key_collations(connection, "office_documents_path_idx") == (
            sqlite_path_collation(),
        )
        assert _index_key_collations(connection, "office_inventory_path_idx") == (
            sqlite_path_collation(),
        )
        assert _index_key_collations(connection, "xlsx_cells_location_idx")[0] == (
            sqlite_path_collation()
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert search_office_state(state_path, "transformación", 5)[0]["file_key"] == ("legacy-office")

    migrated = state_path.read_bytes()
    initialize_office_state(state_path)
    assert state_path.read_bytes() == migrated

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(OFFICE_SCHEMA_VERSION + 1),),
        )
    future = state_path.read_bytes()
    with pytest.raises(RuntimeError, match="newer than supported"):
        initialize_office_state(state_path)
    assert state_path.read_bytes() == future


def test_failed_office_v2_migration_rolls_back_rows_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "office-v2.sqlite3"
    _create_populated_office_v2_state(state_path)

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM xlsx_cells")
        raise RuntimeError("forced Office migration failure")

    monkeypatch.setattr(
        office_state_module,
        "_migrate_office_v2_path_collation",
        fail_after_write,
    )

    with pytest.raises(RuntimeError, match="forced Office migration failure"):
        initialize_office_state(state_path)

    with sqlite3.connect(state_path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "2"
        )
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM xlsx_cells").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.skipif(sqlite_path_collation() != "BINARY", reason="POSIX path identity contract")
@pytest.mark.parametrize("reverse", (False, True))
def test_linux_case_distinct_office_paths_survive_both_processing_orders(
    tmp_path: Path,
    reverse: bool,
) -> None:
    sources = (tmp_path / "Case.xlsx", tmp_path / "case.xlsx")
    for source in sources:
        _write_typed_xlsx(source)
    snapshots = tuple(snapshot_path(source) for source in sources)
    ordered = tuple(reversed(snapshots)) if reverse else snapshots
    framework = FakeFrameworkRouteState({XLSX_MIME: ordered})
    state_path = tmp_path / "office.sqlite3"
    route = OfficeRoute(
        OfficeRouteConfig(
            state_path=state_path,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )

    summary = route.run()

    assert summary.extracted == 2
    with office_database(state_path, readonly=True) as connection:
        assert [
            str(row[0])
            for row in connection.execute("SELECT path FROM documents ORDER BY path COLLATE BINARY")
        ] == [str(sources[0]), str(sources[1])]
        assert connection.execute("SELECT COUNT(*) FROM office_inventory").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM xlsx_cells").fetchone()[0] == 20
        assert (
            connection.execute("SELECT COUNT(DISTINCT workbook) FROM xlsx_cells").fetchone()[0] == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("reverse", (False, True))
def test_office_windows_path_identity_schema_remains_nocase(reverse: bool) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in office_state_module._office_v1_schema_ddl(
            sqlite_path_collation(platform_name="nt")
        ) + office_state_module._office_xlsx_schema_ddl(sqlite_path_collation(platform_name="nt")):
            connection.execute(statement)
        assert _index_key_collations(connection, "office_documents_path_idx") == ("NOCASE",)
        assert _index_key_collations(connection, "office_inventory_path_idx") == ("NOCASE",)
        assert _index_key_collations(connection, "xlsx_cells_location_idx")[0] == ("NOCASE")
        paths = ("C:/Corpus/Case.xlsx", "C:/Corpus/case.xlsx")
        ordered = tuple(reversed(paths)) if reverse else paths
        connection.execute(
            """INSERT INTO office_inventory(
            file_key,format,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
            VALUES('first','xlsx',?,1,2,3,4)""",
            (ordered[0],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                """INSERT INTO office_inventory(
                file_key,format,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
                VALUES('second','xlsx',?,1,2,3,4)""",
                (ordered[1],),
            )
    finally:
        connection.close()


def test_office_route_extracts_caches_and_classifies_all_supported_formats(
    tmp_path: Path,
) -> None:
    xlsx = tmp_path / "Compra CHINT.xlsx"
    pptx = tmp_path / "Curso CFE.pptx"
    odt = tmp_path / "Procedimiento SERINTRA.odt"
    _write_xlsx(xlsx)
    _write_pptx(pptx)
    _write_odt(odt)
    office_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(
        office_path,
        {XLSX_MIME: xlsx, PPTX_MIME: pptx, ODT_MIME: odt},
    )

    first = route.run()
    second = route.run()

    assert first.extracted == 3
    assert first.errors == 0
    assert second.cache_hits == 3
    assert len(framework.resolutions) == 6
    assert all("office_corrupt_container" in item[4] for item in framework.resolutions)
    assert all(item[5] == frozenset() for item in framework.resolutions)
    with office_database(office_path, readonly=True) as connection:
        rows = connection.execute(
            "SELECT format,status,text_chars FROM documents ORDER BY format"
        ).fetchall()
    assert [(row["format"], row["status"]) for row in rows] == [
        ("odt", "complete"),
        ("pptx", "complete"),
        ("xlsx", "complete"),
    ]
    assert all(int(row["text_chars"]) > 0 for row in rows)

    summaries = update_document_catalog(tmp_path)

    assert {summary.source_kind for summary in summaries} == {
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "odt",
        "audio",
        "text",
    }
    with document_catalog_database(tmp_path / "document_catalog.sqlite3", readonly=True) as catalog:
        classified = catalog.execute(
            """SELECT source_kind,primary_kind,primary_organization
            FROM documents ORDER BY source_kind"""
        ).fetchall()
    assert [(row["source_kind"], row["primary_kind"]) for row in classified] == [
        ("odt", "procedimiento"),
        ("pptx", "curso_capacitacion"),
        ("xlsx", "compra_requisicion"),
    ]
    assert {row["primary_organization"] for row in classified} == {
        "SERINTRA",
        "CFE",
        "CHINT",
    }


def test_xlsx_cells_preserve_location_types_values_and_formula_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Mediciones transformador.xlsx"
    _write_typed_xlsx(source)
    state_path = tmp_path / "office.sqlite3"
    route, _framework = _route_for(state_path, {XLSX_MIME: source})

    first = route.run()
    cached = route.run()

    assert first.extracted == 1
    assert first.errors == 0
    assert cached.cache_hits == 1
    with office_database(state_path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT workbook,sheet,sheet_ordinal,cell_reference,cell_type,value,
            raw_value,formula,cached_value,style_index,number_format
            FROM xlsx_cells ORDER BY sheet_ordinal,cell_reference"""
        ).fetchall()
        body = zlib.decompress(
            connection.execute("SELECT text_zlib FROM documents").fetchone()[0]
        ).decode("utf-8")
        fts_hits = connection.execute(
            "SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH 'C3'"
        ).fetchone()[0]

    assert len(rows) == 10
    by_reference = {row["cell_reference"]: row for row in rows}
    assert set(by_reference) == {
        "A1",
        "B2",
        "C3",
        "D4",
        "E5",
        "F6",
        "G7",
        "H8",
        "I9",
        "K11",
    }
    assert all(row["workbook"] == str(source) for row in rows)
    assert all((row["sheet"], row["sheet_ordinal"]) == ("Mediciones U2", 1) for row in rows)
    assert (by_reference["A1"]["cell_type"], by_reference["A1"]["value"]) == (
        "shared_string",
        "Interruptor principal",
    )
    assert by_reference["A1"]["raw_value"] == "0"
    assert (by_reference["B2"]["cell_type"], by_reference["B2"]["value"]) == (
        "inline_string",
        "Tratamiento de aceite",
    )
    assert (by_reference["C3"]["cell_type"], by_reference["C3"]["value"]) == (
        "number",
        "1234.50",
    )
    assert (
        by_reference["D4"]["cell_type"],
        by_reference["D4"]["value"],
        by_reference["D4"]["raw_value"],
        by_reference["D4"]["style_index"],
        by_reference["D4"]["number_format"],
    ) == ("date", "2024-01-01", "45292", 1, "yyyy-mm-dd")
    assert (by_reference["E5"]["cell_type"], by_reference["E5"]["value"]) == (
        "boolean",
        "true",
    )
    assert (by_reference["F6"]["cell_type"], by_reference["F6"]["value"]) == (
        "error",
        "#DIV/0!",
    )
    assert (
        by_reference["G7"]["value"],
        by_reference["G7"]["formula"],
        by_reference["G7"]["cached_value"],
    ) == ("OK", 'CONCAT("O","K")', "OK")
    assert (
        by_reference["H8"]["value"],
        by_reference["H8"]["formula"],
        by_reference["H8"]["cached_value"],
    ) == ("1235.5", "SUM(C3,1)", "1235.5")
    assert (by_reference["I9"]["cell_type"], by_reference["I9"]["value"]) == (
        "date",
        "2026-08-09T17:49:00Z",
    )
    assert (
        by_reference["K11"]["value"],
        by_reference["K11"]["formula"],
        by_reference["K11"]["cached_value"],
    ) == ("", "NOW()", None)
    assert "Interruptor" in body
    assert "'Mediciones U2'!$C$3" in body
    assert '"sheet":"Mediciones U2","a1":"C3","type":"number"' in body
    assert fts_hits == 1


def test_xlsx_non_empty_cell_limit_fails_closed_without_partial_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demasiadas-celdas.xlsx"
    _write_typed_xlsx(source)
    state_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(state_path, {XLSX_MIME: source})
    monkeypatch.setattr(office_route_module, "MAX_XLSX_CELLS", 1)

    summary = route.run()

    assert summary.errors == 1
    assert summary.extracted == 0
    assert framework.reviews[0].reason_code == "office_xlsx_cell_limit"
    assert framework.reviews[0].recommendation == "manual_review"
    with office_database(state_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM xlsx_cells").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM documents").fetchone()[0] == "error"


def test_office_v1_migration_is_additive_and_preserves_existing_text(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "office-v1.sqlite3"
    with sqlite3.connect(state_path) as connection:
        for statement in office_state_module._OFFICE_V1_SCHEMA_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
        body = "contenido XLSX heredado"
        connection.execute(
            """INSERT INTO documents(
            file_key,format,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            text_zlib,text_chars,text_xxh3_128,part_count,last_seen_run_id,updated_ns)
            VALUES('legacy','xlsx','C:/Corpus/legacy.xlsx',10,20,30,'legacy-v1',
            'complete',?,?,?,1,1,1)""",
            (
                zlib.compress(body.encode("utf-8")),
                len(body),
                "legacy-fingerprint",
            ),
        )
        connection.execute(
            """INSERT INTO document_fts(file_key,format,path,title,author,body)
            VALUES('legacy','xlsx','C:/Corpus/legacy.xlsx','','',?)""",
            (body,),
        )
        connection.commit()

    initialize_office_state(state_path)

    with office_database(state_path, readonly=True) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == str(OFFICE_SCHEMA_VERSION)
        assert (
            zlib.decompress(
                connection.execute("SELECT text_zlib FROM documents").fetchone()[0]
            ).decode("utf-8")
            == body
        )
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM xlsx_cells").fetchone()[0] == 0


def test_corrupt_office_container_is_cached_as_deletion_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dañado.xlsx"
    source.write_bytes(b"not a zip container")
    route, framework = _route_for(
        tmp_path / "office.sqlite3",
        {XLSX_MIME: source},
    )

    summary = route.run()
    cached = route.run()

    assert summary.errors == 1
    assert summary.deletion_candidates == 1
    assert cached.processed == 1
    assert cached.cache_hits == 1
    assert cached.cached_errors == 1
    assert cached.errors == 0
    assert len(framework.reviews) == 2
    assert framework.reviews[0].recommendation == "deletion_candidate"
    connection = sqlite3.connect(tmp_path / "office.sqlite3")
    try:
        row = connection.execute(
            "SELECT status,error_type,review_disposition FROM documents"
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "error",
        "office_corrupt_container",
        "deletion_candidate",
    )


def test_corrupt_deflate_stream_isolated_to_one_office_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "flujo-deflate-corrupto.xlsx"
    _write_xlsx(source)
    route, framework = _route_for(
        tmp_path / "office.sqlite3",
        {XLSX_MIME: source},
    )

    def raise_zlib_error(*_args, **_kwargs):
        raise zlib.error("invalid distance too far back")

    monkeypatch.setattr(
        office_route_module,
        "_extract_xlsx_shared_strings",
        raise_zlib_error,
    )

    summary = route.run()

    assert summary.processed == 1
    assert summary.errors == 1
    assert summary.deletion_candidates == 1
    assert framework.reviews[0].reason_code == "office_corrupt_container"
    assert framework.reviews[0].recommendation == "deletion_candidate"


# endregion [02]


# region [03] Organization move and office-cache synchronization


@pytest.mark.skipif(os.name != "nt", reason="document mutation backend is Windows-only")
def test_organized_xlsx_move_updates_office_cache_and_fts(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    source = corpus / "Compra CHINT.xlsx"
    _write_xlsx(source)
    office_path = state / "office.sqlite3"
    route, _framework = _route_for(office_path, {XLSX_MIME: source})
    assert route.run().extracted == 1
    update_document_catalog(state)
    catalog_path = state / "document_catalog.sqlite3"
    destination_root = tmp_path / "organizados"
    assert plan_document_organization(catalog_path, destination_root).planned == 1

    summary = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=CorpusMutationGuard(
            CorpusAccessPolicy.capture("normal", corpus),
            disjoint_internal_paths_policy(tmp_path),
        ),
        max_actions=1,
    )
    destination = (
        destination_root
        / "Empresas"
        / "CHINT"
        / "Gestion_y_administracion"
        / "Comercial_y_contratos"
        / source.name
    )

    assert summary.applied == 1
    assert summary.cache_synced == 1
    assert destination.is_file()
    with office_database(office_path, readonly=True) as connection:
        assert connection.execute("SELECT path FROM documents").fetchone()[0] == str(destination)
        assert connection.execute("SELECT path FROM office_inventory").fetchone()[0] == str(
            destination
        )
        assert connection.execute("SELECT path FROM document_fts").fetchone()[0] == str(destination)


def test_office_mime_contract_covers_indexed_formats() -> None:
    assert set(OFFICE_MIME_FORMATS) == {XLSX_MIME, PPTX_MIME, ODT_MIME}


def test_office_cli_configuration_is_explicit_and_validated() -> None:
    args = build_parser().parse_args(
        [
            "--route",
            "office",
            "--office-max-count",
            "25",
            "--office-max-text-chars",
            "500000",
            "--office-memory-budget-mb",
            "384",
            "--office-min-free-memory-mb",
            "0",
            "--office-min-free-commit-mb",
            "0",
        ]
    )
    validate_arguments(args)

    config = framework_config_from_args(args)

    assert config.office_max_documents == 25
    assert config.office_max_text_chars == 500_000
    assert config.office_memory_budget_bytes == 384 * 1024 * 1024


# endregion [03]
