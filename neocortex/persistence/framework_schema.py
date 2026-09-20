"""Transactional schema management for the framework orchestration database."""

from __future__ import annotations
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from neocortex.platform.policy import sqlite_path_collation

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from neocortex.persistence.framework_content_admission import (
    CONTENT_ADMISSION_EXTENSION_OBJECTS,
    CONTENT_ADMISSION_EXTENSION_TABLES,
    content_admission_extension_present,
    validate_content_admission_extension,
)


SCHEMA_VERSION = 25
_PATH_COLLATION = sqlite_path_collation()


class FrameworkStateIncompatible(RuntimeError):
    """A durable Framework state belongs to an older, non-migratable schema."""

    code = "factory_reset_required"
    action = "factory_reset_required"

    def __init__(self, observed_schema: int, expected_schema: int = SCHEMA_VERSION) -> None:
        self.observed_schema = observed_schema
        self.expected_schema = expected_schema
        super().__init__(
            f"framework schema {observed_schema} is incompatible with the current "
            f"contract; factory_reset_required (expected {expected_schema})"
        )


def _allowed_framework_extension_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Return optional extension tables without weakening the core contract."""

    tables: list[str] = []
    if content_admission_extension_present(connection):
        tables.extend(sorted(CONTENT_ADMISSION_EXTENSION_TABLES))
    return tuple(tables)


def _allowed_framework_extension_objects(connection: sqlite3.Connection) -> tuple[str, ...]:
    objects: list[str] = []
    if content_admission_extension_present(connection):
        objects.extend(sorted(CONTENT_ADMISSION_EXTENSION_OBJECTS))
    return tuple(objects)


# region [01] Canonical schema


def _route_candidates_table_statement(path_collation: str) -> str:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported Framework path collation: {path_collation}")
    return f"""
    CREATE TABLE IF NOT EXISTS route_candidates (
        run_id INTEGER NOT NULL,
        mime TEXT NOT NULL,
        path TEXT NOT NULL COLLATE {path_collation},
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        PRIMARY KEY(run_id, path)
    ) WITHOUT ROWID
    """


_ROUTE_CANDIDATES_TABLE_STATEMENT = _route_candidates_table_statement(_PATH_COLLATION)
_V21_ROUTE_CANDIDATES_TABLE_STATEMENT = _route_candidates_table_statement("NOCASE")

_ROUTE_CANDIDATES_IDENTITY_INDEX_STATEMENT = """
CREATE INDEX IF NOT EXISTS route_candidates_identity_idx
    ON route_candidates(run_id, volume_id, file_id)
"""


_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS initial_runs (
        run_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        status TEXT NOT NULL,
        run_kind TEXT NOT NULL DEFAULT 'initial',
        source_run_id INTEGER,
        current_phase TEXT,
        owner_pid INTEGER,
        heartbeat_ns INTEGER,
        scan_id INTEGER,
        journal_volume TEXT,
        journal_id TEXT,
        start_usn INTEGER,
        end_usn INTEGER,
        reconciliation_records INTEGER,
        inventory_attempts INTEGER,
        inventory_mode TEXT,
        corpus_access_mode TEXT NOT NULL DEFAULT 'normal' CHECK(
            corpus_access_mode IN ('normal','analyze_only')
        ),
        root_device_id_hex TEXT,
        root_file_id_hex TEXT,
        root_birthtime_ns INTEGER,
        state_directory TEXT,
        inventory_policy_signature TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        event_id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        occurred_ns INTEGER NOT NULL,
        level TEXT NOT NULL,
        phase TEXT NOT NULL,
        message TEXT NOT NULL,
        details_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_runs (
        run_id INTEGER NOT NULL,
        route_name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        current_phase TEXT,
        heartbeat_ns INTEGER,
        source_run_id INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT,
        PRIMARY KEY(run_id, route_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS route_phase_runs (
        run_id INTEGER NOT NULL,
        route_name TEXT NOT NULL,
        phase_name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        heartbeat_ns INTEGER,
        source_run_id INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT,
        PRIMARY KEY(run_id, route_name, phase_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS run_actions (
        run_id INTEGER PRIMARY KEY,
        apply_actions INTEGER NOT NULL,
        duplicate_candidates INTEGER NOT NULL,
        duplicates_trashed INTEGER NOT NULL,
        duplicate_skips INTEGER NOT NULL,
        files_checked INTEGER NOT NULL,
        types_detected INTEGER NOT NULL,
        extensions_matching INTEGER NOT NULL,
        unknown_types INTEGER NOT NULL,
        type_cache_hits INTEGER NOT NULL DEFAULT 0,
        type_cache_misses INTEGER NOT NULL DEFAULT 0,
        type_cache_pruned INTEGER NOT NULL DEFAULT 0,
        stale_inventory INTEGER NOT NULL DEFAULT 0,
        rename_candidates INTEGER NOT NULL,
        files_renamed INTEGER NOT NULL,
        rename_skips INTEGER NOT NULL,
        empty_directory_candidates INTEGER NOT NULL,
        empty_directories_trashed INTEGER NOT NULL,
        empty_directory_skips INTEGER NOT NULL,
        errors INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_actions (
        action_id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        action_type TEXT NOT NULL,
        source_path TEXT NOT NULL,
        target_path TEXT,
        detected_mime TEXT,
        evidence TEXT,
        apply_requested INTEGER NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        idempotency_key TEXT,
        expected_identity_json TEXT,
        effect_receipt_json TEXT,
        applying_ns INTEGER,
        corpus_access_mode TEXT NOT NULL DEFAULT 'normal' CHECK(
            corpus_access_mode IN ('normal','analyze_only')
        ),
        protected_root TEXT,
        protected_root_device_id_hex TEXT,
        protected_root_file_id_hex TEXT,
        protected_root_birthtime_ns INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_action_events (
        event_id INTEGER PRIMARY KEY,
        action_id INTEGER NOT NULL,
        occurred_ns INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        stage TEXT NOT NULL,
        detail TEXT,
        evidence_json TEXT,
        FOREIGN KEY(action_id) REFERENCES file_actions(action_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_action_reconciliation_events (
        reconciliation_event_id INTEGER PRIMARY KEY,
        action_id INTEGER NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence>=1),
        previous_event_id INTEGER,
        reconciliation_key TEXT NOT NULL UNIQUE,
        observed_ns INTEGER NOT NULL CHECK(observed_ns>=0),
        recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=0),
        action_status TEXT NOT NULL CHECK(action_status IN (
            'applying','recovery_required'
        )),
        reconciler_signature TEXT NOT NULL CHECK(length(reconciler_signature)>0),
        event_schema_version INTEGER NOT NULL CHECK(event_schema_version=1),
        actor TEXT NOT NULL CHECK(length(trim(actor))>0),
        provenance_json TEXT NOT NULL,
        classification TEXT NOT NULL CHECK(classification IN (
            'confirmed','not_performed','ambiguous','impossible_to_check'
        )),
        recommendation TEXT NOT NULL,
        detail TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        UNIQUE(reconciliation_event_id, action_id),
        UNIQUE(action_id, sequence),
        CHECK(
            (sequence=1 AND previous_event_id IS NULL) OR
            (sequence>1 AND previous_event_id IS NOT NULL)
        ),
        CHECK(
            (classification='confirmed' AND
             recommendation='confirm_action_record') OR
            (classification='not_performed' AND
             recommendation='review_before_new_authorized_attempt') OR
            (classification IN ('ambiguous','impossible_to_check') AND
             recommendation='preserve_evidence_and_review_manually')
        ),
        FOREIGN KEY(action_id) REFERENCES file_actions(action_id) ON DELETE RESTRICT,
        FOREIGN KEY(previous_event_id, action_id)
        REFERENCES file_action_reconciliation_events(
            reconciliation_event_id, action_id
        ) ON DELETE RESTRICT
    )
    """,
    _ROUTE_CANDIDATES_TABLE_STATEMENT,
    """
    CREATE TABLE IF NOT EXISTS content_type_cache (
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        detector_version TEXT NOT NULL,
        status TEXT NOT NULL,
        mime TEXT,
        canonical_extension TEXT,
        accepted_extensions_json TEXT,
        evidence TEXT,
        last_seen_run_id INTEGER NOT NULL DEFAULT 0,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(volume_id, file_id, detector_version)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        route_name TEXT NOT NULL,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT NOT NULL,
        recommendation TEXT NOT NULL CHECK(recommendation IN (
            'retry','keep_protected','manual_review','deletion_candidate'
        )),
        retryable INTEGER NOT NULL CHECK(retryable IN (0,1)),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence_json TEXT NOT NULL,
        detector_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','resolved')),
        first_detected_ns INTEGER NOT NULL,
        last_detected_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL,
        resolved_ns INTEGER,
        resolved_run_id INTEGER,
        resolution_note TEXT,
        PRIMARY KEY(route_name,volume_id,file_id,reason_code)
    ) WITHOUT ROWID
    """,
)

_INDEX_STATEMENTS = (
    """
    CREATE INDEX IF NOT EXISTS run_events_run_idx
        ON run_events(run_id, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_runs_status_idx
        ON route_runs(status, run_id, route_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_phase_status_idx
        ON route_phase_runs(status, run_id, route_name, phase_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_actions_run_idx
        ON file_actions(run_id, action_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS file_actions_idempotency_key_idx
        ON file_actions(idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_actions_recovery_idx
        ON file_actions(status, action_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_action_events_action_idx
        ON file_action_events(action_id, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_action_reconciliation_events_action_idx
        ON file_action_reconciliation_events(action_id, reconciliation_event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_candidates_mime_idx
        ON route_candidates(run_id, mime, path)
    """,
    _ROUTE_CANDIDATES_IDENTITY_INDEX_STATEMENT,
    """
    CREATE INDEX IF NOT EXISTS findings_status_idx
        ON findings(status, recommendation, route_name, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS findings_path_idx
        ON findings(path, route_name, status)
    """,

)


def _file_actions_corpus_policy_insert_trigger_statement(
    path_collation: str,
) -> str:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported Framework path collation: {path_collation}")
    return f"""
    CREATE TRIGGER IF NOT EXISTS file_actions_corpus_policy_insert
    BEFORE INSERT ON file_actions
    WHEN NOT EXISTS(
        SELECT 1 FROM initial_runs AS run
        WHERE run.run_id=NEW.run_id
          AND run.corpus_access_mode=NEW.corpus_access_mode
          AND (
            (NEW.corpus_access_mode='normal'
             AND NEW.protected_root IS NULL
             AND NEW.protected_root_device_id_hex IS NULL
             AND NEW.protected_root_file_id_hex IS NULL
             AND NEW.protected_root_birthtime_ns IS NULL)
            OR
            (NEW.corpus_access_mode='analyze_only'
             AND NEW.protected_root=run.root COLLATE {path_collation}
             AND NEW.protected_root_device_id_hex=run.root_device_id_hex
             AND NEW.protected_root_file_id_hex=run.root_file_id_hex
             AND NEW.protected_root_birthtime_ns=run.root_birthtime_ns)
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'file action corpus policy mismatch');
    END
    """


_FILE_ACTIONS_CORPUS_POLICY_INSERT_TRIGGER_STATEMENT = (
    _file_actions_corpus_policy_insert_trigger_statement(_PATH_COLLATION)
)
_V21_FILE_ACTIONS_CORPUS_POLICY_INSERT_TRIGGER_STATEMENT = (
    _file_actions_corpus_policy_insert_trigger_statement("NOCASE")
)


_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS initial_runs_corpus_policy_no_update
    BEFORE UPDATE OF root,run_kind,corpus_access_mode,root_device_id_hex,
    root_file_id_hex,root_birthtime_ns,state_directory,inventory_policy_signature
    ON initial_runs
    BEGIN
        SELECT RAISE(ABORT, 'initial run corpus policy is immutable');
    END
    """,
    _FILE_ACTIONS_CORPUS_POLICY_INSERT_TRIGGER_STATEMENT,
    """
    CREATE TRIGGER IF NOT EXISTS file_actions_corpus_policy_no_update
    BEFORE UPDATE OF corpus_access_mode,protected_root,
    protected_root_device_id_hex,protected_root_file_id_hex,
    protected_root_birthtime_ns ON file_actions
    BEGIN
        SELECT RAISE(ABORT, 'file action corpus policy is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_events_no_update
    BEFORE UPDATE ON file_action_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_events_no_delete
    BEFORE DELETE ON file_action_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_reconciliation_events_no_update
    BEFORE UPDATE ON file_action_reconciliation_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_reconciliation_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_reconciliation_events_no_delete
    BEFORE DELETE ON file_action_reconciliation_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_reconciliation_events is append-only');
    END
    """,

)

_TABLE_NAMES = (
    "metadata",
    "initial_runs",
    "run_events",
    "route_runs",
    "route_phase_runs",
    "run_actions",
    "file_actions",
    "file_action_events",
    "file_action_reconciliation_events",
    "route_candidates",
    "content_type_cache",
    "findings",
)

_NAMED_INDEXES = {
    "run_events_run_idx": "run_events",
    "route_runs_status_idx": "route_runs",
    "route_phase_status_idx": "route_phase_runs",
    "file_actions_run_idx": "file_actions",
    "file_actions_idempotency_key_idx": "file_actions",
    "file_actions_recovery_idx": "file_actions",
    "file_action_events_action_idx": "file_action_events",
    "file_action_reconciliation_events_action_idx": ("file_action_reconciliation_events"),
    "route_candidates_mime_idx": "route_candidates",
    "route_candidates_identity_idx": "route_candidates",
    "findings_status_idx": "findings",
    "findings_path_idx": "findings",
}


# endregion [01]


# region [03] Contract derivation and validation


@dataclass(frozen=True, slots=True)
class _ColumnContract:
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class _TableContract:
    columns: dict[str, _ColumnContract]
    without_rowid: bool
    strict: bool


@dataclass(frozen=True, slots=True)
class _SchemaContract:
    tables: dict[str, _TableContract]
    indexes: dict[str, tuple[str, tuple[str, ...], bool]]
    unique_keys: frozenset[tuple[str, tuple[str, ...]]]


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_options(connection: sqlite3.Connection) -> dict[str, tuple[bool, bool]]:
    return {
        str(row[1]): (bool(row[4]), bool(row[5]))
        for row in connection.execute("PRAGMA table_list")
        if str(row[2]) == "table"
    }


def _index_columns(connection: sqlite3.Connection, index: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA index_info({_quoted_identifier(index)})").fetchall()
    return tuple(str(row[2]) for row in rows)


def _build_exact_schema(connection: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for statement in _INDEX_STATEMENTS:
        connection.execute(statement)
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(statement)


def _build_v23_exact_schema(connection: sqlite3.Connection) -> None:
    """Build the bounded v22/v23 core contract used by read-only Knowledge."""

    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for statement in _INDEX_STATEMENTS:
        if statement != _ROUTE_CANDIDATES_IDENTITY_INDEX_STATEMENT:
            connection.execute(statement)
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(statement)


def _build_v21_exact_schema(connection: sqlite3.Connection) -> None:
    """Build the bounded v21 core contract used by read-only Knowledge."""

    for statement in _TABLE_STATEMENTS:
        connection.execute(
            _V21_ROUTE_CANDIDATES_TABLE_STATEMENT
            if statement == _ROUTE_CANDIDATES_TABLE_STATEMENT
            else statement
        )
    for statement in _INDEX_STATEMENTS:
        if statement != _ROUTE_CANDIDATES_IDENTITY_INDEX_STATEMENT:
            connection.execute(statement)
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(
            _V21_FILE_ACTIONS_CORPUS_POLICY_INSERT_TRIGGER_STATEMENT
            if statement == _FILE_ACTIONS_CORPUS_POLICY_INSERT_TRIGGER_STATEMENT
            else statement
        )


def _build_v20_exact_schema(connection: sqlite3.Connection) -> None:
    """Build the bounded v20 core contract used by read-only Knowledge."""

    # v20 differs from v21 only by a retired extension, which is not part of
    # the bounded historical reader contract.
    _build_v21_exact_schema(connection)



def _build_v19_exact_schema(connection: sqlite3.Connection) -> None:
    """Build the bounded v19 core contract used by read-only Knowledge."""

    _build_v20_exact_schema(connection)
    for trigger in (
        "initial_runs_corpus_policy_no_update",
        "file_actions_corpus_policy_insert",
        "file_actions_corpus_policy_no_update",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    for column in (
        "inventory_policy_signature",
        "state_directory",
        "root_birthtime_ns",
        "root_file_id_hex",
        "root_device_id_hex",
        "corpus_access_mode",
    ):
        connection.execute(f"ALTER TABLE initial_runs DROP COLUMN {column}")
    for column in (
        "protected_root_birthtime_ns",
        "protected_root_file_id_hex",
        "protected_root_device_id_hex",
        "protected_root",
        "corpus_access_mode",
    ):
        connection.execute(f"ALTER TABLE file_actions DROP COLUMN {column}")


@lru_cache(maxsize=1)
def _exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_exact_schema)


@lru_cache(maxsize=1)
def _v23_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v23_exact_schema)


@lru_cache(maxsize=1)
def _v21_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v21_exact_schema)


@lru_cache(maxsize=1)
def _v20_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v20_exact_schema)


@lru_cache(maxsize=1)
def _v19_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v19_exact_schema)


def validate_framework_schema_v19(connection: sqlite3.Connection) -> None:
    """Validate the bounded core read contract for a legacy v19 owner."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _v19_exact_schema_contract(),
            label="framework v19 read compatibility",
            exact=False,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v19 schema contract validation failed: {exc}") from exc


def validate_framework_schema_v20(connection: sqlite3.Connection) -> None:
    """Validate the bounded core read contract for a legacy v20 owner."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _v20_exact_schema_contract(),
            label="framework v20 read compatibility",
            exact=False,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v20 schema contract validation failed: {exc}") from exc


def validate_framework_schema_v21(connection: sqlite3.Connection) -> None:
    """Validate the bounded core read contract for a legacy v21 owner."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _v21_exact_schema_contract(),
            label="framework v21 read compatibility",
            exact=False,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v21 schema contract validation failed: {exc}") from exc


def validate_framework_schema_v22(connection: sqlite3.Connection) -> None:
    """Validate the bounded core read contract for a legacy v22 owner."""

    _validate_framework_legacy_contract(
        connection,
        _v23_exact_schema_contract(),
        label="framework v22",
    )


def validate_framework_schema_v23(connection: sqlite3.Connection) -> None:
    """Validate the bounded core read contract for a legacy v23 owner."""

    _validate_framework_legacy_contract(
        connection,
        _v23_exact_schema_contract(),
        label="framework v23",
    )


def _validate_framework_legacy_contract(
    connection: sqlite3.Connection, contract: SQLiteSchemaContract, *, label: str
) -> None:
    try:
        validate_sqlite_schema_contract(
            connection,
            contract,
            label=label,
            exact=False,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"{label} schema contract validation failed: {exc}") from exc


def validate_framework_schema(connection: sqlite3.Connection) -> None:
    """Validate the current owner DDL without creating or migrating state."""

    _validate_framework_exact_contract(
        connection, _exact_schema_contract(), label=f"framework v{SCHEMA_VERSION}"
    )


def _validate_framework_exact_contract(
    connection: sqlite3.Connection, contract: SQLiteSchemaContract, *, label: str
) -> None:

    try:
        validate_sqlite_schema_contract(
            connection,
            contract,
            label=label,
            exact=True,
            allowed_extra_tables=_allowed_framework_extension_tables(connection),
            allowed_extra_objects=_allowed_framework_extension_objects(connection),
        )
        validate_content_admission_extension(connection)
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"{label} schema contract validation failed: {exc}") from exc


@lru_cache(maxsize=1)
def _canonical_contract() -> _SchemaContract:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _TABLE_STATEMENTS:
            connection.execute(statement)
        for statement in _INDEX_STATEMENTS:
            connection.execute(statement)

        table_options = _table_options(connection)
        tables: dict[str, _TableContract] = {}
        unique_keys: set[tuple[str, tuple[str, ...]]] = set()
        for table in _TABLE_NAMES:
            columns = {
                str(row[1]): _ColumnContract(
                    declared_type=str(row[2]).upper(),
                    not_null=bool(row[3]),
                    default_sql=None if row[4] is None else str(row[4]),
                    primary_key_position=int(row[5]),
                )
                for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
            }
            without_rowid, strict = table_options[table]
            tables[table] = _TableContract(columns, without_rowid, strict)
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})"):
                if bool(row[2]) and str(row[3]) == "u":
                    unique_keys.add((table, _index_columns(connection, str(row[1]))))

        indexes: dict[str, tuple[str, tuple[str, ...], bool]] = {}
        for index, table in _NAMED_INDEXES.items():
            row = next(
                (
                    item
                    for item in connection.execute(
                        f"PRAGMA index_list({_quoted_identifier(table)})"
                    )
                    if str(item[1]) == index
                ),
                None,
            )
            if row is None:  # pragma: no cover - canonical DDL invariant
                raise AssertionError(f"canonical index was not created: {index}")
            indexes[index] = (
                table,
                _index_columns(connection, index),
                bool(row[2]),
            )
        return _SchemaContract(tables, indexes, frozenset(unique_keys))
    finally:
        connection.close()


def _validate_schema(connection: sqlite3.Connection) -> None:
    expected = _canonical_contract()
    actual_objects = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    table_options = _table_options(connection)
    errors: list[str] = []

    for table, table_contract in expected.tables.items():
        object_type = actual_objects.get(table)
        if object_type != "table":
            errors.append(
                f"required table {table!r} is missing"
                if object_type is None
                else f"required table {table!r} is a {object_type}"
            )
            continue
        actual_columns = {
            str(row[1]): _ColumnContract(
                declared_type=str(row[2]).upper(),
                not_null=bool(row[3]),
                default_sql=None if row[4] is None else str(row[4]),
                primary_key_position=int(row[5]),
            )
            for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
        }
        for column, column_contract in table_contract.columns.items():
            actual = actual_columns.get(column)
            if actual is None:
                errors.append(f"table {table!r} is missing column {column!r}")
            elif actual != column_contract:
                errors.append(f"table {table!r} column {column!r} has an invalid declaration")
        options = table_options.get(table)
        if options is not None and options != (
            table_contract.without_rowid,
            table_contract.strict,
        ):
            errors.append(f"table {table!r} has incompatible table options")

    for index, (table, columns, unique) in expected.indexes.items():
        rows = {
            str(row[1]): row
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
        }
        actual = rows.get(index)
        if actual is None:
            errors.append(f"required index {index!r} is missing from table {table!r}")
            continue
        if bool(actual[2]) != unique or bool(actual[4]):
            errors.append(f"index {index!r} has incompatible options")
        if _index_columns(connection, index) != columns:
            errors.append(f"index {index!r} has incompatible columns")

    for table, columns in expected.unique_keys:
        actual_unique_keys = {
            _index_columns(connection, str(row[1]))
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
            if bool(row[2])
        }
        if columns not in actual_unique_keys:
            errors.append(f"table {table!r} is missing required unique key {columns!r}")

    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(f"framework schema contract validation failed: {detail}")
    try:
        validate_sqlite_schema_contract(
            connection,
            _exact_schema_contract(),
            label="framework",
            exact=True,
            allowed_extra_tables=_allowed_framework_extension_tables(connection),
            allowed_extra_objects=_allowed_framework_extension_objects(connection),
        )
        validate_content_admission_extension(connection)
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework schema contract validation failed: {exc}") from exc


# endregion [03]


# region [04] Initialization transaction


def _application_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    objects = _application_objects(connection)
    if not objects:
        return None
    metadata_type = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='metadata'"
    ).fetchone()
    if metadata_type != ("table",):
        raise RuntimeError("framework database contains objects but no valid metadata table")
    try:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("framework schema metadata is malformed") from exc
    if len(rows) != 1:
        raise RuntimeError("framework schema metadata has no unique schema_version")
    raw_version = str(rows[0][0])
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise RuntimeError(f"framework schema version is not an integer: {raw_version!r}") from exc
    if raw_version != str(version):
        raise RuntimeError(f"framework schema version is not canonical: {raw_version!r}")
    return version


def _require_supported_version(version: int | None) -> None:
    if version is None:
        return
    if version < 1 or version > SCHEMA_VERSION:
        raise RuntimeError(
            f"framework schema {version} is unsupported; expected 1..{SCHEMA_VERSION}"
        )


def _validate_framework_storage_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise RuntimeError(f"{label} has a foreign-key integrity violation")
    integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    if integrity != ("ok",):
        raise RuntimeError(f"{label} failed integrity_check: {integrity!r}")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    # Framework records the durable intent and receipts for filesystem effects.
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA wal_autocheckpoint=4096")
    connection.execute("PRAGMA journal_size_limit=268435456")


def _create_tables(connection: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)


def _create_indexes(connection: sqlite3.Connection) -> None:
    for statement in _INDEX_STATEMENTS:
        connection.execute(statement)


def _create_triggers(connection: sqlite3.Connection) -> None:
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(statement)


def initialize_framework_schema(
    connection: sqlite3.Connection,
    post_migration: Callable[[], None],
) -> None:
    """Create or validate the exact current schema in one atomic transaction.

    Older Framework databases are deliberately not migrated.  Callers must
    invoke the explicit factory-reset operation before creating a fresh schema.
    """

    initial_version = _read_schema_version(connection)
    _require_supported_version(initial_version)
    if initial_version is not None and initial_version < SCHEMA_VERSION:
        raise FrameworkStateIncompatible(initial_version)
    if initial_version == SCHEMA_VERSION:
        # Reject a falsely current database without repairing or otherwise mutating it.
        _validate_schema(connection)
        _validate_framework_storage_integrity(connection, label=f"framework v{initial_version}")

    _configure_connection(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _read_schema_version(connection)
        _require_supported_version(version)
        if version is None:
            _create_tables(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
        elif version < SCHEMA_VERSION:
            # Re-check under the writer transaction in case the owner changed
            # between the initial read and lock acquisition.  No migration or
            # repair is permitted on this path.
            raise FrameworkStateIncompatible(version)

        _create_indexes(connection)
        _create_triggers(connection)
        _validate_schema(connection)
        post_migration()
        _validate_schema(connection)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        source = "new" if initial_version is None else str(initial_version)
        raise RuntimeError(f"framework schema initialization from version {source} failed") from exc
    except BaseException:
        connection.rollback()
        raise


# endregion [04]
