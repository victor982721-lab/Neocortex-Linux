"""Office replay validates once inside the transaction that repairs its index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from neocortex.capabilities.formats.office import route as route_module
from neocortex.capabilities.formats.office import state as state_module
from neocortex.deduplication import snapshot_path
from tests.test_office_xlsx_cache_projection import _route, _write_whitespace_xlsx


def _cached_xlsx(tmp_path: Path):
    source = tmp_path / "source.xlsx"
    _write_whitespace_xlsx(source)
    snapshot = snapshot_path(source)
    database = tmp_path / "office.sqlite3"
    assert _route(database, snapshot, 1).run().extracted == 1
    return source, snapshot, database


def test_replay_decodes_once_and_validates_cells_in_writer_transaction(tmp_path: Path, monkeypatch) -> None:
    _source, snapshot, database = _cached_xlsx(tmp_path)
    original_decode = state_module._decode_cached_text
    original_cells = state_module._cached_xlsx_cells_are_valid
    decodes = []
    validations = []

    def decode(*args, **kwargs):
        decodes.append(1)
        return original_decode(*args, **kwargs)

    def cells(connection, key, text):
        assert connection.in_transaction
        validations.append(key)
        return original_cells(connection, key, text)

    monkeypatch.setattr(state_module, "_decode_cached_text", decode)
    monkeypatch.setattr(state_module, "_cached_xlsx_cells_are_valid", cells)
    replay = _route(database, snapshot, 2).run()
    assert (replay.cache_hits, replay.extracted, replay.errors) == (1, 0, 0)
    assert len(decodes) == len(validations) == 1
    assert route_module.search_office_state(database, "transformador")


@pytest.mark.parametrize("mutation", (
    "UPDATE documents SET text_zlib=X'00'",
    "UPDATE documents SET processing_signature='changed'",
    "UPDATE documents SET birthtime_ns=birthtime_ns+1",
    "UPDATE documents SET status='error'",
    "UPDATE documents SET format='pptx'",
    "DELETE FROM documents",
    "DELETE FROM xlsx_cells",
    "UPDATE xlsx_cells SET value='forged' WHERE cell_reference='A1'",
))
def test_replay_revalidates_drift_after_metadata_probe(tmp_path: Path, monkeypatch, mutation: str) -> None:
    _source, snapshot, database = _cached_xlsx(tmp_path)
    original_refresh = route_module._refresh_cached_path
    refreshes = []

    def refresh(connection, *args, **kwargs):
        connection.execute(mutation)
        result = original_refresh(connection, *args, **kwargs)
        refreshes.append(result)
        return result

    monkeypatch.setattr(route_module, "_refresh_cached_path", refresh)
    replay = _route(database, snapshot, 2).run()
    assert refreshes == [None]
    assert (replay.cache_hits, replay.extracted, replay.errors) == (0, 1, 0)
    assert route_module.search_office_state(database, "transformador")


def test_typed_cell_mismatch_stops_before_materializing_remaining_rows() -> None:
    observed = []
    expected = {
        "workbook": "source.xlsx", "sheet": "Sheet", "a1": "A1", "type": "string",
        "value": "expected", "formula": None, "cached_value": None,
    }
    row = {
        "workbook": "/fixture/source.xlsx", "sheet": "Sheet", "cell_reference": "A1",
        "cell_type": "string", "value": "forged", "formula": None, "cached_value": None,
    }

    class Rows:
        def __iter__(self):
            for index in range(1000):
                observed.append(index)
                yield row

        def fetchall(self):
            return list(self)

    class Connection:
        def execute(self, _sql, _parameters):
            return Rows()

    assert not state_module._cached_xlsx_cells_are_valid(
        cast(sqlite3.Connection, Connection()), "fixture", "XLSX_CELL " + json.dumps(expected),
    )
    assert observed == [0]
