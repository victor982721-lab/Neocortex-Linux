from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from neocortex.capabilities.formats.audio import state as audio_state
from neocortex.code import code_schema
from neocortex.documents import document_catalog
from neocortex.capabilities.formats.docx import state as docx_state
from neocortex.capabilities.formats.office import state as office_state
from neocortex.capabilities.formats.pdf import pdf_state
from neocortex.persistence import sqlite_connection
from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    SQLiteOpenMode,
    SQLiteWriterPragmas,
    connect_sqlite,
)


_DOCUMENT_WRITER_PRAGMAS = SQLiteWriterPragmas(
    journal_mode="WAL",
    synchronous="NORMAL",
    cache_size_kib=32768,
    wal_autocheckpoint_pages=4096,
    journal_size_limit_bytes=268435456,
)


def _policy(*, label: str = "fixture", row_factory: object = sqlite3.Row):
    return SQLiteConnectionPolicy(
        label=label,
        timeout_seconds=60.0,
        row_factory=row_factory,  # type: ignore[arg-type]
        writer_pragmas=_DOCUMENT_WRITER_PRAGMAS,
    )


# region [01] Shared helper filesystem and lifecycle guarantees


@pytest.mark.parametrize(
    "mode",
    (READONLY_EXISTING, READWRITE_EXISTING),
    ids=("readonly", "readwrite"),
)
def test_existing_modes_never_create_missing_state(
    tmp_path: Path,
    mode: SQLiteOpenMode,
) -> None:
    database = tmp_path / "missing-parent" / "state #owner ü.sqlite3"

    with pytest.raises(sqlite3.OperationalError, match="open database"):
        connect_sqlite(database, mode=mode, policy=_policy())

    assert not database.exists()
    assert not database.parent.exists()


def test_create_and_existing_modes_preserve_special_paths_and_exact_pragmas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nested" / "state #owner ü.sqlite3"
    policy = _policy()

    creator = connect_sqlite(database, mode=READWRITE_CREATE, policy=policy)
    try:
        assert creator.row_factory is sqlite3.Row
        assert creator.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert creator.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert creator.execute("PRAGMA query_only").fetchone()[0] == 0
        assert creator.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert creator.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert creator.execute("PRAGMA cache_size").fetchone()[0] == -32_768
        assert creator.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 4096
        assert creator.execute("PRAGMA journal_size_limit").fetchone()[0] == 268_435_456
        creator.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        creator.commit()
    finally:
        creator.close()

    writer = connect_sqlite(database, mode=READWRITE_EXISTING, policy=policy)
    try:
        writer.execute("INSERT INTO probe VALUES(7)")
        writer.commit()
    finally:
        writer.close()

    reader = connect_sqlite(database, mode=READONLY_EXISTING, policy=policy)
    try:
        assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden(value INTEGER)")
    finally:
        reader.close()


def test_helper_leaves_row_factory_and_transaction_ownership_to_caller(
    tmp_path: Path,
) -> None:
    database = tmp_path / "transaction.sqlite3"
    policy = SQLiteConnectionPolicy(label="plain", row_factory=None)

    setup = connect_sqlite(database, mode=READWRITE_CREATE, policy=policy)
    try:
        assert setup.row_factory is None
        setup.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        setup.commit()
    finally:
        setup.close()

    writer = connect_sqlite(database, mode=READWRITE_EXISTING, policy=policy)
    writer.execute("INSERT INTO probe VALUES(1)")
    writer.close()

    reader = connect_sqlite(database, mode=READONLY_EXISTING, policy=policy)
    try:
        assert reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    finally:
        reader.close()


def test_readonly_connection_preserves_committed_wal_and_primary_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wal-owner.sqlite3"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        writer.execute("INSERT INTO probe VALUES(7)")
        writer.commit()
    finally:
        writer.close()

    wal = Path(f"{database}-wal")
    primary_before = database.read_bytes()
    wal_before = wal.read_bytes()
    assert wal_before

    reader = connect_sqlite(
        database,
        mode=READONLY_EXISTING,
        policy=SQLiteConnectionPolicy(label="WAL reader"),
    )
    try:
        assert reader.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
        assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
    finally:
        reader.close()

    assert database.read_bytes() == primary_before
    assert wal.read_bytes() == wal_before


class _InjectedAbort(BaseException):
    pass


def test_helper_closes_connection_when_configuration_raises_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingConnection:
        row_factory: object | None = None

        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> object:
            raise _InjectedAbort

        def setconfig(self, _option: int, _value: bool) -> None:
            return None

        def getconfig(self, _option: int) -> bool:
            return True

        def close(self) -> None:
            self.closed = True

    connection = _FailingConnection()
    sqlite3_module = sqlite_connection.sqlite3
    real_connect = sqlite3_module.connect
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            sqlite3_module,
            "connect",
            lambda *_args, **_kwargs: connection,
        )

        with pytest.raises(_InjectedAbort):
            connect_sqlite(
                tmp_path / "state.sqlite3",
                mode=READWRITE_CREATE,
                policy=_policy(),
            )

    assert connection.closed is True
    assert sqlite3_module.connect is real_connect


# endregion [01]


# region [02] Equivalent document-owner factory regressions


@dataclass(frozen=True, slots=True)
class _FactoryCase:
    name: str
    label: str
    module: ModuleType
    initialize: Callable[[Path], None]
    connect: Callable[..., sqlite3.Connection]


_FACTORY_CASES = (
    _FactoryCase(
        "pdf",
        "PDF state",
        pdf_state,
        pdf_state.initialize_pdf_state,
        pdf_state.connect_pdf_state,
    ),
    _FactoryCase(
        "docx",
        "DOCX state",
        docx_state,
        docx_state.initialize_docx_state,
        docx_state.connect_docx_state,
    ),
    _FactoryCase(
        "document-catalog",
        "document catalog",
        document_catalog,
        document_catalog.initialize_document_catalog,
        document_catalog.connect_document_catalog,
    ),
)


@pytest.mark.parametrize("case", _FACTORY_CASES, ids=lambda case: case.name)
def test_equivalent_factories_preserve_modes_and_exact_owner_policy(
    tmp_path: Path,
    case: _FactoryCase,
) -> None:
    missing = tmp_path / "missing" / f"{case.name} #state ü.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        case.connect(missing, readonly=True)
    assert not missing.exists()
    assert not missing.parent.exists()

    database = tmp_path / f"{case.name} #state ü.sqlite3"
    case.initialize(database)

    reader = case.connect(database, readonly=True)
    try:
        assert reader.row_factory is sqlite3.Row
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        reader.close()

    writer = case.connect(database, readonly=False)
    try:
        assert writer.row_factory is sqlite3.Row
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert writer.execute("PRAGMA cache_size").fetchone()[0] == -32_768
        assert writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 4096
        assert writer.execute("PRAGMA journal_size_limit").fetchone()[0] == 268_435_456
    finally:
        writer.close()


class _PragmaCursor:
    def __init__(self, value: int) -> None:
        self.value = value

    def fetchone(self) -> tuple[int]:
        return (self.value,)


class _DisabledPragmaConnection:
    row_factory: object | None = None

    def __init__(self, *, foreign_keys: int, query_only: int) -> None:
        self.foreign_keys = foreign_keys
        self.query_only = query_only
        self.closed = False

    def execute(self, statement: str) -> _PragmaCursor:
        if statement == "PRAGMA foreign_keys":
            return _PragmaCursor(self.foreign_keys)
        if statement == "PRAGMA query_only":
            return _PragmaCursor(self.query_only)
        return _PragmaCursor(1)

    def setconfig(self, _option: int, _value: bool) -> None:
        return None

    def getconfig(self, _option: int) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("case", _FACTORY_CASES, ids=lambda case: case.name)
@pytest.mark.parametrize(
    ("readonly", "foreign_keys", "query_only", "message"),
    (
        (False, 0, 0, "could not enable foreign keys"),
        (True, 1, 0, "could not enforce query-only mode"),
    ),
    ids=("foreign-keys", "query-only"),
)
def test_equivalent_factories_preserve_errors_close_and_monkeypatch_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _FactoryCase,
    readonly: bool,
    foreign_keys: int,
    query_only: int,
    message: str,
) -> None:
    connection = _DisabledPragmaConnection(
        foreign_keys=foreign_keys,
        query_only=query_only,
    )
    sqlite3_module = case.module.sqlite3
    real_connect = sqlite3_module.connect
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            sqlite3_module,
            "connect",
            lambda *_args, **_kwargs: connection,
        )

        expected = re.escape(f"{case.label} {message}")
        with pytest.raises(RuntimeError, match=expected):
            case.connect(tmp_path / "state.sqlite3", readonly=readonly)

    assert connection.closed is True
    assert sqlite3_module.connect is real_connect


# endregion [02]


# region [03] Media and code owner adoption regressions


@dataclass(frozen=True, slots=True)
class _RouteOwnerFactoryCase:
    name: str
    label: str
    module: ModuleType
    initialize: Callable[[Path], None]
    open_database: Callable[..., AbstractContextManager[sqlite3.Connection]]
    cache_size_kib: int
    wal_autocheckpoint_pages: int
    journal_size_limit_bytes: int


_ROUTE_OWNER_FACTORY_CASES = (
    _RouteOwnerFactoryCase(
        "audio",
        "audio state",
        audio_state,
        audio_state.initialize_audio_state,
        audio_state.audio_database,
        65_536,
        2_048,
        268_435_456,
    ),
    _RouteOwnerFactoryCase(
        "office",
        "Office state",
        office_state,
        office_state.initialize_office_state,
        office_state.office_database,
        32_768,
        2_048,
        134_217_728,
    ),
    _RouteOwnerFactoryCase(
        "code",
        "code state",
        code_schema,
        code_schema.initialize_code_state,
        code_schema.code_database,
        32_768,
        2_048,
        268_435_456,
    ),
)


@pytest.mark.parametrize(
    "case",
    _ROUTE_OWNER_FACTORY_CASES,
    ids=lambda case: case.name,
)
def test_route_owner_factories_preserve_modes_pragmas_and_transaction_ownership(
    tmp_path: Path,
    case: _RouteOwnerFactoryCase,
) -> None:
    missing = tmp_path / "missing" / f"{case.name} #state ü.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="open database"):
        with case.open_database(missing, readonly=True):
            pytest.fail("missing read-only state was opened")
    assert not missing.exists()
    assert not missing.parent.exists()

    created = tmp_path / "created" / f"{case.name} #state ü.sqlite3"
    with case.open_database(created, readonly=False) as creator:
        assert creator.execute("PRAGMA query_only").fetchone()[0] == 0
    assert created.is_file()

    database = tmp_path / f"{case.name} #initialized ü.sqlite3"
    case.initialize(database)
    with case.open_database(database, readonly=True) as reader:
        assert reader.row_factory is sqlite3.Row
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TEMP TABLE forbidden(value INTEGER)")

    with case.open_database(database, readonly=False) as writer:
        assert writer.row_factory is sqlite3.Row
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        assert writer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert writer.execute("PRAGMA cache_size").fetchone()[0] == (-case.cache_size_kib)
        assert writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == (
            case.wal_autocheckpoint_pages
        )
        assert writer.execute("PRAGMA journal_size_limit").fetchone()[0] == (
            case.journal_size_limit_bytes
        )
        writer.execute("INSERT INTO metadata(key,value) VALUES('transaction_probe','pending')")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        writer.execute("SELECT 1")

    with case.open_database(database, readonly=True) as reader:
        assert (
            reader.execute("SELECT value FROM metadata WHERE key='transaction_probe'").fetchone()
            is None
        )


@pytest.mark.parametrize(
    "case",
    _ROUTE_OWNER_FACTORY_CASES,
    ids=lambda case: case.name,
)
@pytest.mark.parametrize(
    ("readonly", "foreign_keys", "query_only", "message"),
    (
        (False, 0, 0, "could not enable foreign keys"),
        (True, 1, 0, "could not enforce query-only mode"),
    ),
    ids=("foreign-keys", "query-only"),
)
def test_route_owner_factories_preserve_errors_close_and_monkeypatch_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _RouteOwnerFactoryCase,
    readonly: bool,
    foreign_keys: int,
    query_only: int,
    message: str,
) -> None:
    connection = _DisabledPragmaConnection(
        foreign_keys=foreign_keys,
        query_only=query_only,
    )
    sqlite3_module = case.module.sqlite3
    real_connect = sqlite3_module.connect
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            sqlite3_module,
            "connect",
            lambda *_args, **_kwargs: connection,
        )

        expected = re.escape(f"{case.label} {message}")
        with pytest.raises(RuntimeError, match=expected):
            with case.open_database(
                tmp_path / "state.sqlite3",
                readonly=readonly,
            ):
                pytest.fail("disabled SQLite safeguards opened an owner database")

    assert connection.closed is True
    assert sqlite3_module.connect is real_connect


@pytest.mark.parametrize(
    "case",
    _ROUTE_OWNER_FACTORY_CASES,
    ids=lambda case: case.name,
)
def test_route_owner_readers_do_not_recreate_state_deleted_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _RouteOwnerFactoryCase,
) -> None:
    database = tmp_path / f"raced-{case.name}.sqlite3"
    case.initialize(database)
    if case.name == "code":
        with case.open_database(database, readonly=False) as writer:
            code_schema.checkpoint_code_wal(writer)
        code_schema.remove_checkpointed_code_sidecars(database)
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()

    real_connect = sqlite_connection.sqlite3.connect
    removed = False

    def remove_before_open(
        database_arg: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        nonlocal removed
        if uri and not removed:
            database.unlink()
            removed = True
        return real_connect(database_arg, uri=uri, timeout=timeout)

    sqlite3_module = case.module.sqlite3
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(sqlite3_module, "connect", remove_before_open)

        with pytest.raises(sqlite3.OperationalError, match="open database"):
            with case.open_database(database, readonly=True):
                pytest.fail("raced read-only state was recreated")

    assert removed is True
    assert not database.exists()
    assert sqlite3_module.connect is real_connect


# endregion [03]
