"""Bounded read-only status for protected self-analysis evidence."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from _01_Enumeracion import (
    JournalCursor,
    JournalDiscontinuityError,
    NtfsUsnError,
    UsnJournalReader,
)
from _02_Deduplicacion import inventory_schema
from neocortex.sqlite_schema_contract import read_metadata_schema_version

from . import framework_schema
from .corpus_access import CorpusAccessPolicy, ProtectedAnalysisRootError
from .self_analysis import (
    MAX_SELF_ANALYSIS_MANIFEST_BYTES,
    SELF_ANALYSIS_MANIFEST_MESSAGE,
    SELF_ANALYSIS_MANIFEST_PHASE,
    build_self_analysis_inventory_policy,
)
from .self_analysis_freshness import (
    FreshnessFences,
    JournalStatus,
    SelfAnalysisFreshness,
    evaluate_self_analysis_freshness,
)
from .self_analysis_manifest import (
    InvalidSelfAnalysisManifest,
    canonical_self_analysis_manifest,
    decode_self_analysis_manifest,
    manifest_birthtime_ns,
    manifest_integer,
    manifest_mapping,
)


# region [01] Public status schema


ManifestStatus = Literal["valid", "missing", "ambiguous", "invalid"]
JournalProbe = Callable[[JournalCursor], JournalStatus]
_MAX_STATUS_TEXT_BYTES = 32_768
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class QuiescentSQLiteUnavailable(RuntimeError):
    """An immutable status snapshot could not be proven quiescent."""


@dataclass(frozen=True, slots=True)
class _SQLiteFileFence:
    device: int
    inode: int
    size: int
    mtime_ns: int
    birthtime_ns: int | None


def _sqlite_sidecars(database: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{database}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES)


def require_sqlite_sidecars_absent(database: Path) -> None:
    """Abstain when WAL, SHM, or rollback evidence is present."""

    sidecars = tuple(sidecar for sidecar in _sqlite_sidecars(database) if os.path.lexists(sidecar))
    if sidecars:
        raise QuiescentSQLiteUnavailable(f"SQLite state has active sidecars: {database}")


def _capture_sqlite_fence(database: Path) -> _SQLiteFileFence:
    try:
        metadata = os.stat(database, follow_symlinks=False)
    except OSError as exc:
        raise QuiescentSQLiteUnavailable(f"SQLite state is unavailable: {database}") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or database.is_symlink()
        or attributes & reparse
        or metadata.st_size <= 0
    ):
        raise QuiescentSQLiteUnavailable(f"SQLite state is not a stable regular file: {database}")
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    return _SQLiteFileFence(
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        None if birthtime is None else int(birthtime),
    )


def _require_quiescent_sqlite(
    database: Path,
    expected: _SQLiteFileFence,
) -> None:
    require_sqlite_sidecars_absent(database)
    if _capture_sqlite_fence(database) != expected:
        raise QuiescentSQLiteUnavailable(
            f"SQLite state changed during read-only status: {database}"
        )


@contextmanager
def quiescent_sqlite_database(database: Path, *, timeout_seconds: float = 10.0):
    """Open a sidecar-free immutable snapshot and fence it before and after."""

    if timeout_seconds <= 0:
        raise ValueError("SQLite status timeout must be positive")
    database = Path(os.path.abspath(os.fspath(database)))
    expected = _capture_sqlite_fence(database)
    _require_quiescent_sqlite(database, expected)
    uri = f"{database.as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("SQLite status could not enable foreign keys")
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("SQLite status is not query-only")
        _require_quiescent_sqlite(database, expected)
        yield connection
    finally:
        connection.close()
        _require_quiescent_sqlite(database, expected)


@dataclass(frozen=True, slots=True)
class CodeRunStatusEvidence:
    """The newest code-owned run needed to bind framework evidence."""

    analysis_run_id: int
    framework_run_id: int
    scan_id: int
    processing_signature: str
    status: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.analysis_run_id,
                self.framework_run_id,
                self.scan_id,
            )
        ):
            raise ValueError("code run status identifiers must be positive integers")
        if (
            not self.processing_signature
            or self.processing_signature.strip() != self.processing_signature
            or len(self.processing_signature.encode("utf-8")) > 4096
        ):
            raise ValueError("code run status has no processing signature")
        if self.status not in {
            "running",
            "completed",
            "partial",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise ValueError("code run status is unsupported")


@dataclass(frozen=True, slots=True)
class SelfAnalysisStatus:
    """JSON-ready status for the latest relevant self-analysis run."""

    manifest_status: ManifestStatus
    manifest: dict[str, object] | None
    freshness: SelfAnalysisFreshness

    def as_payload(self) -> dict[str, object]:
        return {
            "manifest_status": self.manifest_status,
            "manifest": self.manifest,
            "freshness": self.freshness.as_payload(),
        }


def _negative_status(manifest_status: ManifestStatus) -> SelfAnalysisStatus:
    return SelfAnalysisStatus(
        manifest_status,
        None,
        SelfAnalysisFreshness(False, False, False, "unavailable", False),
    )


# endregion [01]


# region [02] Strict manifest decoding and framework binding


_InvalidManifest = InvalidSelfAnalysisManifest
_mapping = manifest_mapping
_integer = manifest_integer
_decode_manifest = decode_self_analysis_manifest


_FRAMEWORK_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "root",
        "completed_ns",
        "status",
        "run_kind",
        "scan_id",
        "journal_volume",
        "journal_id",
        "start_usn",
        "end_usn",
        "reconciliation_records",
        "inventory_attempts",
        "inventory_mode",
        "corpus_access_mode",
        "root_device_id_hex",
        "root_file_id_hex",
        "root_birthtime_ns",
        "state_directory",
        "inventory_policy_signature",
    }
)
_RUN_EVENT_COLUMNS = frozenset(
    {
        "event_id",
        "run_id",
        "occurred_ns",
        "level",
        "phase",
        "message",
        "details_json",
    }
)


@dataclass(frozen=True, slots=True)
class _FrameworkManifestEvidence:
    manifest: dict[str, object]
    linked_run: sqlite3.Row
    event: sqlite3.Row


@dataclass(frozen=True, slots=True)
class _FrameworkManifestRead:
    status: ManifestStatus | None
    evidence: _FrameworkManifestEvidence | None = None


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[str]:
    return frozenset(str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _framework_run_rows(
    connection: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT run_id,
        CASE WHEN length(CAST(root AS BLOB)) BETWEEN 1 AND 32768
            THEN root END AS root,
        completed_ns,
        CASE WHEN length(CAST(status AS BLOB)) BETWEEN 1 AND 32
            THEN status END AS status,
        CASE WHEN length(CAST(run_kind AS BLOB)) BETWEEN 1 AND 32
            THEN run_kind END AS run_kind,
        scan_id,
        CASE WHEN length(CAST(journal_volume AS BLOB)) BETWEEN 1 AND 32768
            THEN journal_volume END AS journal_volume,
        CASE WHEN length(CAST(journal_id AS BLOB)) BETWEEN 1 AND 32768
            THEN journal_id END AS journal_id,
        start_usn,end_usn,reconciliation_records,inventory_attempts,
        CASE WHEN length(CAST(inventory_mode AS BLOB)) BETWEEN 1 AND 32
            THEN inventory_mode END AS inventory_mode,
        CASE WHEN length(CAST(corpus_access_mode AS BLOB)) BETWEEN 1 AND 32
            THEN corpus_access_mode END AS corpus_access_mode,
        CASE WHEN length(CAST(root_device_id_hex AS BLOB)) BETWEEN 1 AND 32768
            THEN root_device_id_hex END AS root_device_id_hex,
        CASE WHEN length(CAST(root_file_id_hex AS BLOB)) BETWEEN 1 AND 32768
            THEN root_file_id_hex END AS root_file_id_hex,
        root_birthtime_ns,
        CASE WHEN length(CAST(state_directory AS BLOB)) BETWEEN 1 AND 32768
            THEN state_directory END AS state_directory,
        CASE WHEN length(CAST(inventory_policy_signature AS BLOB)) BETWEEN 1 AND 32768
            THEN inventory_policy_signature END AS inventory_policy_signature
        FROM initial_runs WHERE run_id=? LIMIT 2""",
        (run_id,),
    ).fetchall()


def _manifest_event_rows(
    connection: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT event_id,run_id,occurred_ns,level,
        length(CAST(details_json AS BLOB)) AS details_bytes,
        CASE WHEN length(CAST(details_json AS BLOB))<=? THEN details_json END
            AS bounded_details_json
        FROM run_events WHERE run_id=? AND phase=? AND message=?
        ORDER BY event_id DESC LIMIT 2""",
        (
            MAX_SELF_ANALYSIS_MANIFEST_BYTES,
            run_id,
            SELF_ANALYSIS_MANIFEST_PHASE,
            SELF_ANALYSIS_MANIFEST_MESSAGE,
        ),
    ).fetchall()


def _journal_is_available(journal: Mapping[str, object]) -> bool:
    """Treat legacy v1 journals as available and v2 evidence explicitly."""

    return journal.get("status", "available") == "available"


def _journal_matches_framework(
    journal: Mapping[str, object],
    run_row: sqlite3.Row,
) -> bool:
    if not _journal_is_available(journal):
        return all(
            run_row[name] is None
            for name in ("journal_volume", "journal_id", "start_usn", "end_usn")
        )
    if any(
        run_row[name] is None for name in ("journal_volume", "journal_id", "start_usn", "end_usn")
    ):
        return False
    return all(
        (
            str(run_row["journal_volume"]) == journal["volume"],
            str(run_row["journal_id"]) == journal["journal_id"],
            int(run_row["start_usn"]) == journal["start_usn"],
            int(run_row["end_usn"]) == journal["end_usn"],
        )
    )


def _manifest_matches_framework(
    manifest: Mapping[str, object],
    run_row: sqlite3.Row,
    event_row: sqlite3.Row,
    state_directory: Path,
) -> bool:
    run = _mapping(manifest["run"], label="manifest run")
    identity = _mapping(run["root_identity"], label="manifest root identity")
    inventory = _mapping(manifest["inventory"], label="manifest inventory")
    journal = _mapping(inventory["journal"], label="manifest journal")
    policy = _mapping(inventory["policy"], label="manifest policy")
    expected_state = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(state_directory))))
    recorded_state = os.path.normcase(
        os.path.realpath(os.path.abspath(str(run_row["state_directory"])))
    )
    return all(
        (
            int(event_row["run_id"]) == int(run_row["run_id"]) == run["run_id"],
            str(event_row["level"]) == "info",
            int(event_row["occurred_ns"]) == int(run_row["completed_ns"]),
            str(run_row["run_kind"]) == run["run_kind"] == "self_analysis",
            str(run_row["status"]) == run["status"] == "completed",
            str(run_row["corpus_access_mode"]) == run["corpus_access_mode"] == "analyze_only",
            str(run_row["root"]) == run["root"],
            str(run_row["root_device_id_hex"]) == identity["device_id_hex"],
            str(run_row["root_file_id_hex"]) == identity["file_id_hex"],
            int(run_row["root_birthtime_ns"]) == identity["birthtime_ns"],
            str(run_row["state_directory"]) == run["state_directory"],
            recorded_state == expected_state,
            int(run_row["scan_id"]) == inventory["scan_id"],
            str(run_row["inventory_mode"]) == inventory["mode"],
            int(run_row["inventory_attempts"]) == inventory["attempts"],
            int(run_row["reconciliation_records"]) == inventory["reconciliation_records"],
            _journal_matches_framework(journal, run_row),
            str(run_row["inventory_policy_signature"]) == policy["signature"],
        )
    )


def _framework_schema_status(
    connection: sqlite3.Connection,
) -> Literal["current", "legacy", "invalid"]:
    version = read_metadata_schema_version(
        connection,
        label="framework self-analysis status",
    )
    if version is None or version < framework_schema.SCHEMA_VERSION:
        return "legacy"
    if version != framework_schema.SCHEMA_VERSION:
        return "invalid"
    framework_schema._validate_schema(connection)
    columns_are_current = _FRAMEWORK_RUN_COLUMNS.issubset(
        _table_columns(connection, "initial_runs")
    ) and _RUN_EVENT_COLUMNS.issubset(_table_columns(connection, "run_events"))
    return "current" if columns_are_current else "invalid"


def _decode_bound_manifest(
    linked_run: sqlite3.Row,
    event: sqlite3.Row,
    state_directory: Path,
) -> _FrameworkManifestRead:
    try:
        manifest = _decode_manifest(
            event["bounded_details_json"],
            event["details_bytes"],
        )
        if not _manifest_matches_framework(
            manifest,
            linked_run,
            event,
            state_directory,
        ):
            raise _InvalidManifest("manifest does not match framework evidence")
    except (KeyError, TypeError, ValueError):
        return _FrameworkManifestRead("invalid")
    return _FrameworkManifestRead(
        "valid",
        _FrameworkManifestEvidence(manifest, linked_run, event),
    )


def _classify_framework_rows(
    connection: sqlite3.Connection,
    linked_rows: list[sqlite3.Row],
    state_directory: Path,
) -> _FrameworkManifestRead:
    if len(linked_rows) != 1:
        return _FrameworkManifestRead("invalid")
    linked_run = linked_rows[0]
    if linked_run["run_kind"] is None or linked_run["status"] is None:
        return _FrameworkManifestRead("invalid")
    if str(linked_run["run_kind"]) != "self_analysis":
        return _FrameworkManifestRead(None)
    if str(linked_run["status"]) != "completed":
        return _FrameworkManifestRead("missing")
    events = _manifest_event_rows(connection, int(linked_run["run_id"]))
    if not events:
        return _FrameworkManifestRead("missing")
    if len(events) != 1:
        return _FrameworkManifestRead("ambiguous")
    return _decode_bound_manifest(linked_run, events[0], state_directory)


def _read_framework_manifest(
    database: Path,
    state_directory: Path,
    framework_run_id: int,
) -> _FrameworkManifestRead:
    try:
        with _readonly_transaction(database) as connection:
            schema_status = _framework_schema_status(connection)
            if schema_status == "legacy":
                return _FrameworkManifestRead(None)
            if schema_status == "invalid":
                return _FrameworkManifestRead("invalid")
            linked_rows = _framework_run_rows(connection, framework_run_id)
            return _classify_framework_rows(
                connection,
                linked_rows,
                state_directory,
            )
    except QuiescentSQLiteUnavailable:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return _FrameworkManifestRead("invalid")


# endregion [02]


# region [03] Inventory, root, and USN freshness fences


@dataclass(frozen=True, slots=True)
class _CheckpointEvidence:
    root: str
    scan_id: int
    volume: str
    journal_id: str
    next_usn: int
    valid: bool
    scan_root: str
    root_device_id: int
    root_file_id: int
    root_birthtime_ns: int
    scan_status: str
    completed_ns: int
    errors: int


_CHECKPOINT_REQUIRED_FIELDS = (
    "root_volume_id",
    "root_file_id",
    "root_birthtime_ns",
    "root",
    "volume",
    "journal_id",
    "scan_root",
    "scan_status",
    "completed_ns",
    "errors",
)


def _checkpoint_from_rows(
    rows: list[sqlite3.Row],
) -> _CheckpointEvidence | None:
    if len(rows) != 1:
        return None
    row = rows[0]
    if any(row[name] is None for name in _CHECKPOINT_REQUIRED_FIELDS):
        return None
    if type(row["valid"]) is not int or row["valid"] not in {0, 1}:
        return None
    try:
        return _CheckpointEvidence(
            root=str(row["root"]),
            scan_id=int(row["scan_id"]),
            volume=str(row["volume"]),
            journal_id=str(row["journal_id"]),
            next_usn=int(row["next_usn"]),
            valid=row["valid"] == 1,
            scan_root=str(row["scan_root"]),
            root_device_id=int.from_bytes(row["root_volume_id"], "little"),
            root_file_id=int.from_bytes(row["root_file_id"], "little"),
            root_birthtime_ns=int(row["root_birthtime_ns"]),
            scan_status=str(row["scan_status"]),
            completed_ns=int(row["completed_ns"]),
            errors=int(row["errors"]),
        )
    except (TypeError, ValueError, OverflowError):
        return None


@contextmanager
def _readonly_transaction(database: Path):
    with quiescent_sqlite_database(database) as connection:
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()


@contextmanager
def _readonly_inventory(database: Path):
    with _readonly_transaction(database) as connection:
        yield connection


def _read_checkpoint(database: Path, root: str) -> _CheckpointEvidence | None:
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        return None
    try:
        with _readonly_inventory(database) as connection:
            version = read_metadata_schema_version(
                connection,
                label="dedup inventory self-analysis status",
            )
            if version != inventory_schema.SCHEMA_VERSION:
                return None
            inventory_schema.validate_inventory_schema(connection)
            rows = connection.execute(
                """SELECT
                CASE WHEN length(CAST(c.root AS BLOB)) BETWEEN 1 AND 32768
                    THEN c.root END AS root,
                c.scan_id,
                CASE WHEN length(CAST(c.volume AS BLOB)) BETWEEN 1 AND 32768
                    THEN c.volume END AS volume,
                CASE WHEN length(CAST(c.journal_id AS BLOB)) BETWEEN 1 AND 32768
                    THEN c.journal_id END AS journal_id,
                c.next_usn,c.valid,
                CASE WHEN length(CAST(s.root AS BLOB)) BETWEEN 1 AND 32768
                    THEN s.root END AS scan_root,
                CASE WHEN length(s.root_volume_id)=16 THEN s.root_volume_id END
                    AS root_volume_id,
                CASE WHEN length(s.root_file_id)=16 THEN s.root_file_id END
                    AS root_file_id,
                s.root_birthtime_ns,
                CASE WHEN length(CAST(s.status AS BLOB)) BETWEEN 1 AND 32
                    THEN s.status END AS scan_status,
                s.completed_ns,s.errors
                FROM inventory_checkpoints c JOIN scans s ON s.scan_id=c.scan_id
                WHERE c.root=? ORDER BY c.scan_id DESC LIMIT 2""",
                (os.path.abspath(root),),
            ).fetchall()
    except QuiescentSQLiteUnavailable:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return None
    return _checkpoint_from_rows(rows)


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _checkpoint_matches(
    checkpoint: _CheckpointEvidence | None,
    manifest: Mapping[str, object],
    *,
    current_policy_signature: str | None,
) -> bool:
    if checkpoint is None or current_policy_signature is None:
        return False
    run = _mapping(manifest["run"], label="manifest run")
    identity = _mapping(run["root_identity"], label="manifest root identity")
    inventory = _mapping(manifest["inventory"], label="manifest inventory")
    journal = _mapping(inventory["journal"], label="manifest journal")
    policy = _mapping(inventory["policy"], label="manifest policy")
    if not _journal_is_available(journal):
        return False
    return all(
        (
            checkpoint.valid,
            checkpoint.scan_status == "complete",
            checkpoint.completed_ns > 0,
            checkpoint.errors == 0,
            _path_key(checkpoint.root) == _path_key(str(run["root"])),
            _path_key(checkpoint.scan_root) == _path_key(str(run["root"])),
            checkpoint.scan_id == inventory["scan_id"],
            checkpoint.volume == journal["volume"],
            checkpoint.journal_id == journal["journal_id"],
            checkpoint.next_usn == journal["end_usn"],
            f"{checkpoint.root_device_id:x}" == identity["device_id_hex"],
            f"{checkpoint.root_file_id:x}" == identity["file_id_hex"],
            checkpoint.root_birthtime_ns == identity["birthtime_ns"],
            current_policy_signature == policy["signature"],
        )
    )


def probe_self_analysis_journal(cursor: JournalCursor) -> JournalStatus:
    """Classify one bounded USN probe without persisting its cursor."""

    try:
        with UsnJournalReader(
            cursor.volume,
            cursor,
            timeout_seconds=0,
            bytes_to_wait_for=0,
        ) as reader:
            return "unchanged" if reader.poll() is None else "advanced"
    except JournalDiscontinuityError:
        return "discontinuous"
    except (NtfsUsnError, OSError, RuntimeError, ValueError):
        return "unavailable"


# endregion [03]


# region [04] Integrated read-only status


def _code_run_is_still_latest(
    state_directory: Path,
    expected: CodeRunStatusEvidence,
) -> bool:
    database = Path(state_directory) / "code.sqlite3"
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        return False
    try:
        with quiescent_sqlite_database(database) as connection:
            row = connection.execute(
                """SELECT analysis_run_id,framework_run_id,scan_id,
                CASE WHEN length(CAST(processing_signature AS BLOB))
                    BETWEEN 1 AND 4096 THEN processing_signature END
                    AS processing_signature,
                CASE WHEN length(CAST(status AS BLOB)) BETWEEN 1 AND 32
                    THEN status END AS status
                FROM analysis_runs
                ORDER BY analysis_run_id DESC LIMIT 1"""
            ).fetchone()
    except QuiescentSQLiteUnavailable:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return False
    return row is not None and (
        int(row["analysis_run_id"]),
        int(row["framework_run_id"]),
        int(row["scan_id"]),
        str(row["processing_signature"]),
        str(row["status"]),
    ) == (
        expected.analysis_run_id,
        expected.framework_run_id,
        expected.scan_id,
        expected.processing_signature,
        expected.status,
    )


def _framework_current_fence_matches(
    linked: list[sqlite3.Row],
    latest: sqlite3.Row | None,
    events: list[sqlite3.Row],
    *,
    run_id: int,
    event_id: int,
    canonical: str,
) -> bool:
    if len(linked) != 1 or latest is None or len(events) != 1:
        return False
    return all(
        (
            str(linked[0]["run_kind"]) == "self_analysis",
            str(linked[0]["status"]) == "completed",
            int(latest["run_id"]) == run_id,
            int(events[0]["event_id"]) == event_id,
            int(events[0]["details_bytes"]) == len(canonical.encode("utf-8")),
            events[0]["bounded_details_json"] == canonical,
        )
    )


def _framework_run_is_still_current(
    database: Path,
    *,
    run_id: int,
    event_id: int,
    manifest: Mapping[str, object],
) -> bool:
    canonical = canonical_self_analysis_manifest(manifest)
    try:
        with _readonly_transaction(database) as connection:
            version = read_metadata_schema_version(
                connection,
                label="framework self-analysis status fence",
            )
            if version != framework_schema.SCHEMA_VERSION:
                return False
            framework_schema._validate_schema(connection)
            linked = connection.execute(
                """SELECT
                CASE WHEN length(CAST(run_kind AS BLOB)) BETWEEN 1 AND 32
                    THEN run_kind END AS run_kind,
                CASE WHEN length(CAST(status AS BLOB)) BETWEEN 1 AND 32
                    THEN status END AS status
                FROM initial_runs
                WHERE run_id=? LIMIT 2""",
                (run_id,),
            ).fetchall()
            latest = connection.execute(
                """SELECT run_id FROM initial_runs
                WHERE run_kind='self_analysis'
                ORDER BY run_id DESC LIMIT 1"""
            ).fetchone()
            events = connection.execute(
                """SELECT event_id,
                length(CAST(details_json AS BLOB)) AS details_bytes,
                CASE WHEN length(CAST(details_json AS BLOB))<=? THEN details_json END
                    AS bounded_details_json
                FROM run_events WHERE run_id=? AND phase=? AND message=?
                ORDER BY event_id DESC LIMIT 2""",
                (
                    MAX_SELF_ANALYSIS_MANIFEST_BYTES,
                    run_id,
                    SELF_ANALYSIS_MANIFEST_PHASE,
                    SELF_ANALYSIS_MANIFEST_MESSAGE,
                ),
            ).fetchall()
            return _framework_current_fence_matches(
                linked,
                latest,
                events,
                run_id=run_id,
                event_id=event_id,
                canonical=canonical,
            )
    except QuiescentSQLiteUnavailable:
        raise
    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
        return False


def _framework_link_matches(
    latest_code_run: CodeRunStatusEvidence,
    manifest: Mapping[str, object],
) -> bool:
    run = _mapping(manifest["run"], label="manifest run")
    inventory = _mapping(manifest["inventory"], label="manifest inventory")
    code = _mapping(manifest["code"], label="manifest code evidence")
    return all(
        (
            latest_code_run.status == "completed",
            latest_code_run.framework_run_id == run["run_id"],
            latest_code_run.scan_id == inventory["scan_id"],
            latest_code_run.processing_signature == code["processing_signature"],
        )
    )


def _root_identity_is_current(policy: CorpusAccessPolicy) -> bool:
    try:
        policy.verify_root_identity()
    except (OSError, RuntimeError, ValueError, ProtectedAnalysisRootError):
        return False
    return True


def _current_inventory_policy_signature(
    run: Mapping[str, object],
) -> str | None:
    try:
        return build_self_analysis_inventory_policy(
            Path(str(run["root"])),
            Path(str(run["state_directory"])),
        ).signature
    except (OSError, RuntimeError, ValueError):
        return None


def _probe_manifest_journal(
    journal: Mapping[str, object],
    journal_probe: JournalProbe | None,
) -> JournalStatus:
    if not _journal_is_available(journal):
        return "unavailable"
    cursor = JournalCursor(
        str(journal["volume"]),
        int(str(journal["journal_id"])),
        _integer(journal["end_usn"], label="manifest end USN"),
    )
    effective_probe = probe_self_analysis_journal if journal_probe is None else journal_probe
    try:
        observed = effective_probe(cursor)
    except Exception:
        return "unavailable"
    if observed not in {
        "unchanged",
        "advanced",
        "discontinuous",
        "unavailable",
    }:
        return "unavailable"
    return observed


def _linked_framework_fences(
    state_directory: Path,
    framework_database: Path,
    latest_code_run: CodeRunStatusEvidence,
    evidence: _FrameworkManifestEvidence,
    *,
    link_matches: bool,
) -> tuple[bool, bool, bool]:
    if not link_matches:
        return False, False, False
    code_before = _code_run_is_still_latest(state_directory, latest_code_run)
    framework_current = _framework_run_is_still_current(
        framework_database,
        run_id=_integer(
            evidence.manifest["run"]["run_id"],  # type: ignore[index]
            label="manifest run_id",
            minimum=1,
        ),
        event_id=int(evidence.event["event_id"]),
        manifest=evidence.manifest,
    )
    code_after = _code_run_is_still_latest(state_directory, latest_code_run)
    return code_before, framework_current, code_after


def _evaluate_manifest_freshness(
    state_directory: Path,
    framework_database: Path,
    latest_code_run: CodeRunStatusEvidence,
    evidence: _FrameworkManifestEvidence,
    journal_probe: JournalProbe | None,
) -> SelfAnalysisFreshness:
    manifest = evidence.manifest
    run = _mapping(manifest["run"], label="manifest run")
    identity = _mapping(run["root_identity"], label="manifest root identity")
    inventory = _mapping(manifest["inventory"], label="manifest inventory")
    journal = _mapping(inventory["journal"], label="manifest journal")
    access_policy = CorpusAccessPolicy.from_storage(
        "analyze_only",
        str(run["root"]),
        str(identity["device_id_hex"]),
        str(identity["file_id_hex"]),
        manifest_birthtime_ns(
            identity["birthtime_ns"],
            label="manifest root birthtime",
            schema=str(manifest["schema"]),
        ),
    )
    root_before = _root_identity_is_current(access_policy)
    current_policy_signature = _current_inventory_policy_signature(run)
    inventory_database = Path(state_directory) / "dedup.sqlite3"
    checkpoint_before = _read_checkpoint(inventory_database, str(run["root"]))
    checkpoint_before_matches = _checkpoint_matches(
        checkpoint_before,
        manifest,
        current_policy_signature=current_policy_signature,
    )
    journal_status = _probe_manifest_journal(journal, journal_probe)
    checkpoint_after = _read_checkpoint(inventory_database, str(run["root"]))
    checkpoint_unchanged = checkpoint_after is not None and checkpoint_after == checkpoint_before
    root_after = _root_identity_is_current(access_policy)
    link_matches = _framework_link_matches(latest_code_run, manifest)
    code_before, framework_current, code_after = _linked_framework_fences(
        state_directory,
        framework_database,
        latest_code_run,
        evidence,
        link_matches=link_matches,
    )
    return evaluate_self_analysis_freshness(
        FreshnessFences(
            root_before=root_before,
            root_after=root_after,
            framework_link_matches=link_matches,
            code_before=code_before,
            framework_still_current=framework_current,
            code_after=code_after,
            checkpoint_before_matches=checkpoint_before_matches,
            checkpoint_unchanged=checkpoint_unchanged,
            journal_status=journal_status,
        )
    )


def read_self_analysis_status(
    state_directory: Path,
    latest_code_run: CodeRunStatusEvidence | None,
    *,
    journal_probe: JournalProbe | None = None,
) -> SelfAnalysisStatus | None:
    """Read linked manifest and freshness without creating or migrating state."""

    if latest_code_run is None:
        return None
    state_directory = Path(state_directory)
    framework_database = state_directory / "framework.sqlite3"
    require_sqlite_sidecars_absent(framework_database)
    if not framework_database.is_file():
        return None
    manifest_read = _read_framework_manifest(
        framework_database,
        state_directory,
        latest_code_run.framework_run_id,
    )
    if manifest_read.evidence is None:
        if manifest_read.status is None:
            return None
        return _negative_status(manifest_read.status)
    freshness = _evaluate_manifest_freshness(
        state_directory,
        framework_database,
        latest_code_run,
        manifest_read.evidence,
        journal_probe,
    )
    return SelfAnalysisStatus(
        "valid",
        manifest_read.evidence.manifest,
        freshness,
    )


__all__ = [
    "CodeRunStatusEvidence",
    "JournalStatus",
    "QuiescentSQLiteUnavailable",
    "SelfAnalysisFreshness",
    "SelfAnalysisStatus",
    "probe_self_analysis_journal",
    "quiescent_sqlite_database",
    "read_self_analysis_status",
    "require_sqlite_sidecars_absent",
]


# endregion [04]
