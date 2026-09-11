"""XLSX cache projections use canonical text without changing typed values."""

from __future__ import annotations

import sqlite3
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

import neocortex.capabilities.formats.office.route as office_route_module
from neocortex.capabilities.formats.office.route import (
    OfficeRoute,
    OfficeRouteConfig,
    XLSX_MIME,
    search_office_state,
)
from neocortex.capabilities.formats.office.state import office_database
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.orchestration.replay_metrics import route_replay_metrics


TEST_CAPABILITIES = ("base",)

_CELL_A1 = "  Línea\t\nde   prueba Ω  "
_CELL_B2 = "Café β — transformador"


class _Framework:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        _max_file_bytes: int | None,
        _route_name: str,
        _selection,
    ) -> tuple[int, int]:
        return (1, 1) if mime == XLSX_MIME else (0, 0)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection,
    ):
        if mime == XLSX_MIME:
            yield self.snapshot

    def store_review_candidates(self, _run_id: int, _candidates) -> None:
        return None

    def reconcile_review_candidates_batch(self, _run_id: int, _route_name: str, _items) -> None:
        return None


def _write_whitespace_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheets><sheet name="Hoja  Ω"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row><c r="A1" t="inlineStr"><is><t>{_CELL_A1}</t></is></c></row>
                <row><c r="B2" t="inlineStr"><is><t>{_CELL_B2}</t></is></c></row>
              </sheetData>
            </worksheet>""",
        )


def _route(path: Path, snapshot: FileSnapshot, run_id: int) -> OfficeRoute:
    return OfficeRoute(
        OfficeRouteConfig(
            state_path=path,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        _Framework(snapshot),  # type: ignore[arg-type]
        run_id,
        cancellation=CancellationToken(),
    )


def test_xlsx_cache_projection_normalizes_whitespace_and_preserves_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "espacios.xlsx"
    _write_whitespace_xlsx(source)
    snapshot = snapshot_path(source)
    database = tmp_path / "office.sqlite3"
    first = _route(database, snapshot, 1).run()

    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        side_effect=AssertionError("replay must not re-extract a valid XLSX cache"),
    ) as extractor:
        replay = _route(database, snapshot, 2).run()
    extractor.assert_not_called()

    assert (first.extracted, replay.extracted, replay.cache_hits) == (1, 0, 1)
    assert route_replay_metrics("office", replay)["new_work"] == 0
    with office_database(database, readonly=True) as connection:
        rows = connection.execute(
            """SELECT cell_reference,sheet,value FROM xlsx_cells
            ORDER BY cell_reference"""
        ).fetchall()
        body = zlib.decompress(
            connection.execute("SELECT text_zlib FROM documents").fetchone()[0]
        ).decode("utf-8")
    assert [(row["cell_reference"], row["sheet"], row["value"]) for row in rows] == [
        ("A1", "Hoja  Ω", _CELL_A1),
        ("B2", "Hoja  Ω", _CELL_B2),
    ]
    assert "XLSX_CELL" in body
    assert "Línea" in body
    assert "Café β" in body
    assert search_office_state(database, "transformador")


def test_xlsx_cache_derived_layers_repair_or_invalidate_without_stale_hits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "derivados.xlsx"
    _write_whitespace_xlsx(source)
    snapshot = snapshot_path(source)
    database = tmp_path / "office.sqlite3"
    _route(database, snapshot, 1).run()

    with office_database(database) as connection:
        connection.execute("DELETE FROM document_fts")
        connection.commit()
    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        side_effect=AssertionError("FTS repair must use durable text"),
    ) as extractor:
        repaired = _route(database, snapshot, 2).run()
    extractor.assert_not_called()
    assert (repaired.cache_hits, repaired.extracted) == (1, 0)
    assert search_office_state(database, "transformador")

    with office_database(database) as connection:
        connection.execute("UPDATE xlsx_cells SET value='forged' WHERE cell_reference='A1'")
        connection.commit()
    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        wraps=office_route_module.extract_office_document,
    ) as extractor:
        invalidated = _route(database, snapshot, 3).run()
    assert extractor.call_count == 1
    assert (invalidated.cache_hits, invalidated.extracted) == (0, 1)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM xlsx_cells WHERE cell_reference='A1'"
        ).fetchone()[0] == _CELL_A1
