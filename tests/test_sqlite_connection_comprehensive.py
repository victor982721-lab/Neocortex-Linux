from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.enumeration.path_index import repository as path_index_module
from neocortex.enumeration.path_index import schema as path_index_schema
from neocortex.enumeration.path_index.repository import SqlitePathIndex
from neocortex.deduplication.inventory import index as inventory_module
from neocortex.deduplication.inventory.index import DedupIndex
from neocortex.deduplication import schema as inventory_schema
from neocortex.documents import document_catalog
from neocortex.persistence import framework_state_writer
from neocortex.workflow.review import review_evidence
from neocortex.semantic import semantic_sources
from neocortex.documents.document_cache_sync import _synchronize_database
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.sqlite_schema_lifecycle import existing_sqlite_uri


# region [01] Existing-file URI and framework-family policy


def test_existing_sqlite_uri_encodes_special_paths_and_refuses_creation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state #owner ü.sqlite3"
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE probe(value INTEGER)")

    connection = sqlite3.connect(existing_sqlite_uri(database), uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM probe").fetchone() == (0,)
    finally:
        connection.close()

    absent = tmp_path / "absent #owner ü.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        sqlite3.connect(existing_sqlite_uri(absent), uri=True)
    assert not absent.exists()


def test_framework_existing_connections_enforce_mode_fk_wal_and_locking(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework #state ü.sqlite3"
    with FrameworkState(database):
        pass

    reader = connect_existing_framework(database, readonly=True)
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden(value INTEGER)")
    finally:
        reader.close()

    first = connect_existing_framework(database, readonly=False, timeout_seconds=0.05)
    second = connect_existing_framework(database, readonly=False, timeout_seconds=0.05)
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.execute("BEGIN IMMEDIATE")
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


@pytest.mark.parametrize("readonly", (False, True))
def test_framework_existing_connections_never_create_missing_state(
    tmp_path: Path,
    readonly: bool,
) -> None:
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        connect_existing_framework(database, readonly=readonly)
    assert not database.exists()


def test_framework_connection_closes_if_configuration_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.persistence import framework_connection

    class _InterruptedConnection:
        row_factory: object | None = None

        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> object:
            raise KeyboardInterrupt("injected configuration interruption")

        def close(self) -> None:
            self.closed = True

    connection = _InterruptedConnection()
    monkeypatch.setattr(
        framework_connection.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(KeyboardInterrupt, match="configuration interruption"):
        connect_existing_framework(tmp_path / "state.sqlite3", readonly=True)
    assert connection.closed is True


def test_framework_state_existing_only_refuses_missing_but_default_creates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework #state ü.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        FrameworkState(database, existing_only=True)
    assert not database.exists()

    with FrameworkState(database) as state:
        assert state._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert database.is_file()


def test_framework_state_existing_only_initializes_an_existing_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "existing-empty.sqlite3"
    database.touch()

    with FrameworkState(database, existing_only=True) as state:
        assert (
            state._connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            is not None
        )


def test_framework_state_existing_only_does_not_recreate_raced_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "raced-framework.sqlite3"
    with FrameworkState(database):
        pass

    real_connect = framework_state_writer.sqlite3.connect
    removed = False

    def remove_before_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal removed
        if kwargs.get("uri") is True and not removed:
            database.unlink()
            removed = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        framework_state_writer.sqlite3,
        "connect",
        remove_before_open,
    )
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        FrameworkState(database, existing_only=True)
    assert removed is True
    assert not database.exists()


# endregion [01]


# region [02] Lower-layer owner factories


@contextmanager
def _path_connection(path: Path, *, readonly: bool) -> Iterator[sqlite3.Connection]:
    connection = path_index_schema._connect(path, readonly=readonly)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _inventory_connection(path: Path, *, readonly: bool) -> Iterator[sqlite3.Connection]:
    connection = inventory_schema._connect(path, readonly=readonly)
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("initialize", "open_database"),
    (
        (path_index_schema.initialize_path_index_schema, _path_connection),
        (inventory_schema.initialize_inventory_schema, _inventory_connection),
    ),
    ids=("path-index", "dedup-inventory"),
)
def test_lower_layer_readers_are_query_only_and_never_create(
    tmp_path: Path,
    initialize: Callable[[Path], None],
    open_database: Callable[..., object],
) -> None:
    database = tmp_path / "owner.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        with open_database(database, readonly=True):  # type: ignore[attr-defined]
            pytest.fail("missing database was opened")
    assert not database.exists()

    initialize(database)
    with open_database(database, readonly=True) as reader:  # type: ignore[attr-defined]
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden(value INTEGER)")


@pytest.mark.parametrize(
    ("owner", "module"),
    (("path-index", path_index_schema), ("dedup-inventory", inventory_schema)),
)
def test_lower_layer_factories_close_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    module: object,
) -> None:
    class _InterruptedConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> object:
            raise KeyboardInterrupt(f"injected {owner} interruption")

        def close(self) -> None:
            self.closed = True

    connection = _InterruptedConnection()
    monkeypatch.setattr(
        module.sqlite3,  # type: ignore[attr-defined]
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(KeyboardInterrupt, match=owner):
        module._connect(tmp_path / "state.sqlite3", readonly=True)  # type: ignore[attr-defined]
    assert connection.closed is True


def test_lower_layer_long_lived_writers_apply_wal_fk_and_busy_timeout(
    tmp_path: Path,
) -> None:
    with SqlitePathIndex(tmp_path / "path.sqlite3") as index:
        connection = index._connection
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    with DedupIndex(tmp_path / "dedup.sqlite3") as inventory:
        connection = inventory._connection
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    ("module", "constructor", "initializer_name"),
    (
        (path_index_module, SqlitePathIndex, "initialize_path_index_schema"),
        (inventory_module, DedupIndex, "initialize_inventory_schema"),
    ),
    ids=("path-index", "dedup-inventory"),
)
def test_lower_layer_writer_does_not_recreate_state_deleted_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    constructor: Callable[[Path], object],
    initializer_name: str,
) -> None:
    database = tmp_path / "deleted-after-validation.sqlite3"
    initialize = getattr(module, initializer_name)

    def initialize_then_delete(path: str | Path) -> None:
        initialize(path)
        Path(path).unlink()

    monkeypatch.setattr(module, initializer_name, initialize_then_delete)
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        constructor(database)
    assert not database.exists()


# endregion [02]


# region [03] Route readers and cross-cache writer


@pytest.mark.parametrize(
    "open_database",
    (document_catalog._readonly_source, semantic_sources._readonly_database),
    ids=("catalog-source", "semantic-source"),
)
def test_route_source_readers_enforce_query_only_fk_and_close(
    tmp_path: Path,
    open_database: Callable[..., object],
) -> None:
    database = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(database)
    observed: sqlite3.Connection | None = None
    with open_database(database) as connection:  # type: ignore[attr-defined]
        observed = connection
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TEMP TABLE forbidden(value INTEGER)")
    assert observed is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed.execute("SELECT 1")


def test_cross_cache_writer_rolls_back_keyboard_interrupt_and_handles_special_uri(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cache #state ü.sqlite3"
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE probe(value INTEGER)")

    def interrupt(connection: sqlite3.Connection) -> int:
        connection.execute("INSERT INTO probe VALUES(1)")
        raise KeyboardInterrupt("injected cross-cache interruption")

    with pytest.raises(KeyboardInterrupt, match="cross-cache interruption"):
        _synchronize_database("fixture", database, required=True, operation=interrupt)

    with sqlite3.connect(database) as verification:
        assert verification.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0

    absent = tmp_path / "absent.sqlite3"
    result = _synchronize_database(
        "fixture", absent, required=True, operation=lambda _connection: 0
    )
    assert result.status == "error"
    assert not absent.exists()


def test_review_evidence_writer_rolls_back_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass

    def interrupt(
        connection: sqlite3.Connection,
        _values: object,
    ) -> list[object]:
        connection.execute(
            "UPDATE review_evidence_progress SET updated_ns=123 "
            "WHERE pipeline_key='human-review-v1'"
        )
        raise KeyboardInterrupt("injected review-evidence interruption")

    monkeypatch.setattr(review_evidence, "_pending_materializations", interrupt)
    with pytest.raises(KeyboardInterrupt, match="review-evidence interruption"):
        review_evidence.materialize_review_evidence(database)

    with sqlite3.connect(database) as verification:
        assert (
            verification.execute("SELECT COUNT(*) FROM review_evidence_progress").fetchone()[0] == 0
        )


# endregion [03]
