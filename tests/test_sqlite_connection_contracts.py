# region [00] Contexto del módulo
# Módulo: tests/test_sqlite_connection_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import (
    audio_state,
    code_schema,
    document_catalog,
    docx_state,
    image_state,
    office_state,
    pdf_state,
    semantic_schema,
)
from _04_Nucleo_Operativo import framework_route_state, framework_state_writer
from _04_Nucleo_Operativo.framework_route_state import FrameworkRouteState
from _04_Nucleo_Operativo.framework_state_writer import FrameworkState
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class _ConnectionCase:
    name: str
    module: object
    initialize: Callable[[Path], None]
    open_database: Callable[..., object]


_CASES = (
    _ConnectionCase(
        "audio",
        audio_state,
        audio_state.initialize_audio_state,
        audio_state.audio_database,
    ),
    _ConnectionCase(
        "code",
        code_schema,
        code_schema.initialize_code_state,
        code_schema.code_database,
    ),
    _ConnectionCase(
        "document-catalog",
        document_catalog,
        document_catalog.initialize_document_catalog,
        document_catalog.document_catalog_database,
    ),
    _ConnectionCase(
        "docx",
        docx_state,
        docx_state.initialize_docx_state,
        docx_state.docx_database,
    ),
    _ConnectionCase(
        "image",
        image_state,
        image_state.initialize_image_state,
        image_state.image_database,
    ),
    _ConnectionCase(
        "office",
        office_state,
        office_state.initialize_office_state,
        office_state.office_database,
    ),
    _ConnectionCase(
        "pdf",
        pdf_state,
        pdf_state.initialize_pdf_state,
        pdf_state.pdf_database,
    ),
    _ConnectionCase(
        "semantic",
        semantic_schema,
        semantic_schema.initialize_semantic_state,
        semantic_schema.semantic_database,
    ),
)


@contextmanager
def _opened(
    case: _ConnectionCase,
    database: Path,
    *,
    readonly: bool,
) -> Iterator[sqlite3.Connection]:
    manager = case.open_database(database, readonly=readonly)
    with manager as connection:  # type: ignore[attr-defined]
        yield connection


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_public_sqlite_factories_enforce_connection_pragmas(
    tmp_path: Path,
    case: _ConnectionCase,
) -> None:
    database = tmp_path / f"{case.name}.sqlite3"
    case.initialize(database)

    with _opened(case, database, readonly=True) as reader:
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        expected_reader_journal = "delete" if case.name == "code" else "wal"
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == (expected_reader_journal)
        assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden_write(value INTEGER)")

    with _opened(case, database, readonly=False) as writer:
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE audit_parent(parent_id INTEGER PRIMARY KEY)")
        writer.execute(
            """CREATE TABLE audit_child(
            child_id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES audit_parent(parent_id))"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            writer.execute("INSERT INTO audit_child VALUES(1,999)")
        writer.rollback()


class _InjectedAbort(BaseException):
    pass


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_public_sqlite_factories_rollback_and_close_on_base_exception(
    tmp_path: Path,
    case: _ConnectionCase,
) -> None:
    database = tmp_path / f"{case.name}.sqlite3"
    case.initialize(database)
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE audit_probe(value INTEGER NOT NULL)")

    observed: sqlite3.Connection | None = None
    with pytest.raises(_InjectedAbort):
        with _opened(case, database, readonly=False) as writer:
            observed = writer
            writer.execute("INSERT INTO audit_probe VALUES(1)")
            raise _InjectedAbort

    assert observed is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed.execute("SELECT 1")
    with sqlite3.connect(database) as verification:
        assert verification.execute("SELECT COUNT(*) FROM audit_probe").fetchone()[0] == 0


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_public_sqlite_factories_close_if_configuration_aborts(
    tmp_path: Path,
    case: _ConnectionCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingConnection:
        row_factory: object | None = None

        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> object:
            raise _InjectedAbort

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    connection = _FailingConnection()
    monkeypatch.setattr(case.module.sqlite3, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(_InjectedAbort):
        with case.open_database(tmp_path / "state.sqlite3", readonly=False):
            pytest.fail("connection configuration unexpectedly completed")

    assert connection.closed is True


def test_framework_writer_enforces_foreign_keys(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"

    with FrameworkState(database) as state:
        connection = state._connection
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("CREATE TABLE audit_parent(parent_id INTEGER PRIMARY KEY)")
        connection.execute(
            """CREATE TABLE audit_child(
            child_id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES audit_parent(parent_id))"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("INSERT INTO audit_child VALUES(1,999)")
        connection.rollback()


def test_framework_route_connections_enforce_read_write_pragmas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    repository = FrameworkRouteState(database)

    reader = repository._connect(readonly=True)
    try:
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden_write(value INTEGER)")
    finally:
        reader.close()

    writer = repository._connect(readonly=False)
    try:
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        writer.close()


def test_framework_route_writer_never_creates_missing_state(tmp_path: Path) -> None:
    database = tmp_path / "missing-framework.sqlite3"

    with pytest.raises(sqlite3.OperationalError, match="open database"):
        FrameworkRouteState(database)._connect(readonly=False)

    assert not database.exists()


@pytest.mark.parametrize("component", ("writer", "route"))
def test_framework_connections_close_if_configuration_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    class _FailingConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> object:
            raise _InjectedAbort

        def close(self) -> None:
            self.closed = True

    connection = _FailingConnection()
    module = framework_state_writer if component == "writer" else framework_route_state
    monkeypatch.setattr(module.sqlite3, "connect", lambda *_a, **_kw: connection)

    with pytest.raises(_InjectedAbort):
        if component == "writer":
            FrameworkState(tmp_path / "framework.sqlite3")
        else:
            FrameworkRouteState(tmp_path / "framework.sqlite3")._connect(readonly=False)

    assert connection.closed is True


# endregion [02]
