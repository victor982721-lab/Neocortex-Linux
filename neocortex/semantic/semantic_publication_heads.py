"""Fresh, fenced observations of the Semantic publication heads.

This module is deliberately a reader-only bridge.  It does not read the
cross-owner epoch marker: the owner databases are the only source that can
prove the current head set.  Each owner is opened through
``SQLiteReadSession`` and its source fence is checked again before the
observation is returned.

Semantic has one durable head per model signature.  The public
``StateOwnerHead`` therefore contains an aggregate revision and a canonical
digest of the complete published set, rather than just the largest generation
identifier.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    SQLiteReadSession,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.persistence.state_publication import StateOwnerHead

from .semantic_models import canonical_json
from .semantic_schema import (
    SEMANTIC_SCHEMA_VERSION,
    SemanticStateError,
    _read_schema_version,
    _validate_semantic_read_schema,
)


SEMANTIC_PUBLICATION_HEADS_PROTOCOL = "neocortex.semantic-publication-heads/v1"

MAX_SEMANTIC_PUBLICATION_HEADS = 1_024
MAX_APPLICATION_OBJECTS = 512
MAX_JSON_BYTES = 256 * 1024
MAX_AGGREGATE_DIGEST_BYTES = 64 * 1024 * 1024
READ_TIMEOUT_SECONDS = 60.0
SQL_PROGRESS_OPCODES = 1_000
# A publication boundary can race a writer's final WAL checkpoint.  A bounded
# retry gives that lifecycle race one fresh fence without enlarging the
# snapshot budget or weakening the immutable-read contract.  Persistent
# oversize/active snapshots still fail closed after the retry window.
PUBLICATION_HEAD_SNAPSHOT_RETRIES = 2
PUBLICATION_HEAD_RETRY_DELAY_SECONDS = 0.01


_EXPECTED_SCHEMA_VERSIONS = {
    "semantic": SEMANTIC_SCHEMA_VERSION,
}


class PublicationHeadsError(SemanticStateError):
    """Base error for an unauthenticated or unavailable owner-head read."""


class PublicationHeadsSchemaError(PublicationHeadsError):
    """An owner schema, publication row, or immutable generation is invalid."""


class PublicationHeadsStateError(PublicationHeadsError):
    """An owner is absent only through an unproven or inconsistent state."""


class PublicationHeadsDriftError(PublicationHeadsError):
    """An owner changed while its fresh observation was being assembled."""


# Keep descriptive aliases available to callers that name the Semantic bridge
# or the integrated boundary explicitly.
SemanticPublicationHeadsError = PublicationHeadsError
IntegratedOwnerHeadsError = PublicationHeadsError


@dataclass(frozen=True, slots=True)
class _PathEntry:
    suffix: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _OwnerCapture:
    path: Path
    fence: SQLiteImmutableFence | None
    stamp: tuple[_PathEntry, ...] | None
    absent_or_empty: bool


@dataclass(frozen=True, slots=True)
class _SemanticHead:
    model_signature: str
    modality: str
    vector_space: str
    generation_id: int
    processing_signature: str
    published_ns: int


@dataclass(frozen=True, slots=True)
class _SemanticObservation:
    owner_head: StateOwnerHead
    generation_heads: tuple[tuple[str, int], ...]
    semantic_heads: tuple[_SemanticHead, ...]
    capture: _OwnerCapture


_LEASE_CONSTRUCTOR_TOKEN = object()
_ACTIVE_OWNER_LEASES: ContextVar[tuple[object, ...]] = ContextVar(
    "semantic_publication_active_owner_leases",
    default=(),
)


class _SemanticOwnerLease:
    """Opaque, context-registered existing-owner connection."""

    __slots__ = ("_connection", "_fence", "_owner_identity")

    def __init__(
        self,
        constructor_token: object,
        connection: sqlite3.Connection,
        fence: SQLiteImmutableFence,
        owner_identity: tuple[int, int],
    ) -> None:
        if constructor_token is not _LEASE_CONSTRUCTOR_TOKEN:
            raise TypeError("Semantic owner leases are created only by their context")
        self._connection = connection
        self._fence = fence
        self._owner_identity = owner_identity

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def fence(self) -> SQLiteImmutableFence:
        return self._fence

    @property
    def owner_identity(self) -> tuple[int, int]:
        return self._owner_identity


def _active_owner_lease(value: object) -> bool:
    return any(candidate is value for candidate in _ACTIVE_OWNER_LEASES.get())


class _ObservationControls:
    """One cooperative budget shared by bounded Semantic owner reads."""

    def __init__(
        self,
        snapshot_budget: SQLiteSnapshotBudget | None,
        *,
        deadline_monotonic: float | None,
        cancellation_check: Callable[[], bool | None] | None,
    ) -> None:
        if snapshot_budget is not None and not isinstance(
            snapshot_budget, SQLiteSnapshotBudget
        ):
            raise TypeError("snapshot_budget must be a SQLiteSnapshotBudget or None")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
            or float(deadline_monotonic) < 0
        ):
            raise ValueError("deadline_monotonic must be finite and non-negative")
        if cancellation_check is not None and not callable(cancellation_check):
            raise TypeError("cancellation_check must be callable")
        self.snapshot_budget = snapshot_budget
        self.deadline_monotonic = (
            None if deadline_monotonic is None else float(deadline_monotonic)
        )
        self.cancellation_check = cancellation_check
        self._clock = (
            time.monotonic if snapshot_budget is None else snapshot_budget.monotonic_clock
        )
        self._budget_deadline = (
            None
            if snapshot_budget is None
            else self._clock() + snapshot_budget.prepare_timeout_seconds
        )
        self._sql_failure: BaseException | None = None

    @property
    def active(self) -> bool:
        return (
            self.snapshot_budget is not None
            or self.deadline_monotonic is not None
            or self.cancellation_check is not None
        )

    def _check_callback(
        self,
        callback: Callable[[], bool | None] | None,
    ) -> None:
        if callback is None:
            return
        decision = callback()
        if decision is not None and decision is not False:
            raise PublicationHeadsError("publication-head observation was cancelled")

    def checkpoint(self) -> None:
        if self.snapshot_budget is not None:
            self._check_callback(self.snapshot_budget.cancellation_check)
        self._check_callback(self.cancellation_check)
        if (
            self.deadline_monotonic is not None
            and self._clock() >= self.deadline_monotonic
        ):
            raise PublicationHeadsError("publication-head observation deadline exceeded")
        if self._budget_deadline is not None and self._clock() >= self._budget_deadline:
            raise PublicationHeadsError("publication-head snapshot budget deadline exceeded")

    def record_sql_failure(self, failure: BaseException) -> None:
        if self._sql_failure is None:
            self._sql_failure = failure

    def raise_sql_failure_or_checkpoint(self, error: BaseException) -> None:
        """Preserve a progress-handler stop before classifying SQL errors."""

        if self._sql_failure is not None:
            raise self._sql_failure from error
        self.checkpoint()

    def session_budget(self) -> SQLiteSnapshotBudget | None:
        if not self.active:
            return None

        def callback() -> bool | None:
            self.checkpoint()
            return False

        if self.snapshot_budget is None:
            return SQLiteSnapshotBudget(
                cancellation_check=callback,
                monotonic_clock=self._clock,
            )
        return replace(self.snapshot_budget, cancellation_check=callback)


@dataclass(slots=True)
class _SQLProgress:
    controls: _ObservationControls
    failure: BaseException | None = None

    def __call__(self) -> int:
        try:
            self.controls.checkpoint()
        except BaseException as exc:  # sqlite progress callbacks cannot re-raise safely
            self.failure = exc
            self.controls.record_sql_failure(exc)
            return 1
        return 0


@contextmanager
def _sql_progress(
    connection: sqlite3.Connection,
    controls: _ObservationControls,
) -> Iterator[None]:
    """Abort long SQLite scans cooperatively, preserving the root cause."""

    if not controls.active:
        yield
        return
    controls.checkpoint()
    progress = _SQLProgress(controls)
    connection.set_progress_handler(progress, SQL_PROGRESS_OPCODES)
    try:
        try:
            yield
        except sqlite3.OperationalError as exc:
            if progress.failure is not None:
                raise progress.failure from exc
            raise
    finally:
        connection.set_progress_handler(None, 0)
    if progress.failure is not None:
        raise progress.failure
    controls.checkpoint()


def _required_text(value: object, *, label: str, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PublicationHeadsSchemaError(f"{label} must be non-empty trimmed text")
    if len(value.encode("utf-8", "surrogatepass")) > maximum:
        raise PublicationHeadsSchemaError(f"{label} exceeds its bound")
    return value


def _required_integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PublicationHeadsSchemaError(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _canonical_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise PublicationHeadsSchemaError(f"{label} must be JSON text")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_JSON_BYTES:
        raise PublicationHeadsSchemaError(f"{label} exceeds its bound")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationHeadsSchemaError(f"{label} is malformed") from exc
    if not isinstance(decoded, dict):
        raise PublicationHeadsSchemaError(f"{label} must be an object")
    try:
        canonical = canonical_json(decoded)
    except (TypeError, ValueError) as exc:
        raise PublicationHeadsSchemaError(f"{label} is not canonical JSON") from exc
    if len(canonical.encode("utf-8", "surrogatepass")) > MAX_JSON_BYTES:
        raise PublicationHeadsSchemaError(f"{label} exceeds its bound")
    return decoded


def _digest_payload(
    value: object,
    *,
    label: str,
    maximum_bytes: int = MAX_AGGREGATE_DIGEST_BYTES,
) -> str:
    try:
        encoded = canonical_json(value).encode("utf-8", "surrogatepass")
    except (TypeError, ValueError) as exc:
        raise PublicationHeadsSchemaError(f"{label} cannot be canonicalized") from exc
    if len(encoded) > maximum_bytes:
        raise PublicationHeadsSchemaError(f"{label} exceeds its bound")
    return hashlib.sha256(encoded).hexdigest()


def _empty_owner_head(owner: str, *, schema_version: int | None = None) -> StateOwnerHead:
    expected_schema = _EXPECTED_SCHEMA_VERSIONS.get(owner)
    if expected_schema is None:
        raise PublicationHeadsSchemaError(f"unknown empty owner: {owner}")
    selected_schema = expected_schema if schema_version is None else schema_version
    allowed_schemas = {7, 8, 9, expected_schema}
    if selected_schema not in allowed_schemas:
        raise PublicationHeadsSchemaError(
            f"empty {owner} schema differs from the expected owner schema"
        )
    digest = _digest_payload(
        {
            "contract": SEMANTIC_PUBLICATION_HEADS_PROTOCOL,
            "owner": owner,
            "schema_version": selected_schema,
            "heads": [],
        },
        label=f"empty {owner} publication heads",
    )
    return StateOwnerHead(
        owner=owner,
        revision=0,
        digest_sha256=digest,
        schema_version=selected_schema,
    )


def _state_directory(path: Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise PublicationHeadsStateError("state directory must be absolute")
    selected = Path(selected.absolute())
    current = Path(selected.anchor)
    missing_component = False
    for component in selected.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # A completely absent state root is a legitimate empty baseline
            # for this read-only observer.  Keep checking only the existing
            # prefix so a symlinked ancestor is never accepted.
            missing_component = True
            continue
        except OSError as exc:
            raise PublicationHeadsStateError("state directory cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PublicationHeadsStateError("state directory cannot contain symlinks")
        if missing_component:
            raise PublicationHeadsStateError("state directory changed while being inspected")
    try:
        metadata = selected.lstat()
    except FileNotFoundError:
        return selected
    except OSError as exc:
        raise PublicationHeadsStateError("state directory cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicationHeadsStateError("state directory must be a real directory")
    return selected


def _path_stamp(path: Path) -> tuple[_PathEntry, ...]:
    entries: list[_PathEntry] = []
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PublicationHeadsStateError(
                f"owner path cannot be inspected: {candidate.name}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PublicationHeadsStateError(
                f"owner path is not a regular file: {candidate.name}"
            )
        entries.append(
            _PathEntry(
                suffix,
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_mode),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
                int(metadata.st_ctime_ns),
            )
        )
    return tuple(entries)


def _empty_or_absent_capture(path: Path) -> _OwnerCapture | None:
    stamp = _path_stamp(path)
    main = next((entry for entry in stamp if entry.suffix == ""), None)
    sidecars = tuple(entry for entry in stamp if entry.suffix != "")
    if main is None:
        if sidecars:
            raise PublicationHeadsStateError(
                f"owner sidecars exist without its database: {path.name}"
            )
        return _OwnerCapture(path, None, stamp, True)
    if main.size == 0:
        if sidecars:
            raise PublicationHeadsStateError(
                f"empty owner has sidecars: {path.name}"
            )
        return _OwnerCapture(path, None, stamp, True)
    return None


def _verify_capture(capture: _OwnerCapture) -> None:
    if capture.fence is not None:
        try:
            observed = capture_sqlite_read_fence(capture.path)
        except (FileNotFoundError, ImmutableSQLiteUnavailable, OSError) as exc:
            raise PublicationHeadsDriftError(
                f"owner changed during read: {capture.path.name}"
            ) from exc
        if observed != capture.fence:
            raise PublicationHeadsDriftError(
                f"owner changed during read: {capture.path.name}"
            )
        return
    try:
        observed_stamp = _path_stamp(capture.path)
    except PublicationHeadsError:
        raise
    if observed_stamp != capture.stamp:
        raise PublicationHeadsDriftError(
            f"owner changed during absence observation: {capture.path.name}"
        )


def _verify_lease_close_fence(
    path: Path,
    expected: SQLiteImmutableFence,
) -> None:
    """Require owner bytes/WAL identity to survive a coordinated lease close.

    SQLite may update or create only the shared-memory bookkeeping sidecar as
    readers join/leave a WAL owner. Main bytes, WAL/journal identities and all
    their metadata remain exact; any difference there is owner drift, not
    harmless reader bookkeeping.
    """

    try:
        observed = capture_sqlite_read_fence(path)
    except (FileNotFoundError, ImmutableSQLiteUnavailable, OSError) as exc:
        raise PublicationHeadsDriftError(
            f"owner changed while closing lease: {path.name}"
        ) from exc
    if observed.main != expected.main:
        raise PublicationHeadsDriftError(
            f"owner changed while closing lease: {path.name}"
        )
    expected_sidecars = dict(expected.sidecars)
    observed_sidecars = dict(observed.sidecars)
    for suffix in ("-journal", "-wal"):
        if observed_sidecars.get(suffix) != expected_sidecars.get(suffix):
            raise PublicationHeadsDriftError(
                f"owner {suffix} changed while closing lease: {path.name}"
            )


def _application_objects(
    connection: sqlite3.Connection,
    controls: _ObservationControls | None = None,
) -> tuple[str, ...]:
    if controls is not None:
        controls.checkpoint()
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY name LIMIT ?",
        (MAX_APPLICATION_OBJECTS + 1,),
    ).fetchall()
    if len(rows) > MAX_APPLICATION_OBJECTS:
        raise PublicationHeadsSchemaError("SQLite application objects exceed their bound")
    return tuple(_required_text(row[0], label="SQLite object name") for row in rows)


def _semantic_schema(
    connection: sqlite3.Connection,
    controls: _ObservationControls | None = None,
) -> int | None:
    if controls is not None:
        controls.checkpoint()
    try:
        version = _read_schema_version(connection)
    except (RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        if controls is not None:
            controls.raise_sql_failure_or_checkpoint(exc)
        raise PublicationHeadsSchemaError("Semantic schema metadata is invalid") from exc
    if version is None:
        return None
    if version not in {7, 8, 9, 10}:
        raise PublicationHeadsSchemaError(
            f"Semantic schema is not the current publication schema: {version!r}"
        )
    try:
        _validate_semantic_read_schema(connection)
    except (RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        if controls is not None:
            controls.raise_sql_failure_or_checkpoint(exc)
        raise PublicationHeadsSchemaError("Semantic schema contract is invalid") from exc
    return version


def _validate_semantic_generation(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    model_signature: str,
    generation_id: int,
    controls: _ObservationControls | None = None,
) -> _SemanticHead:
    if controls is not None:
        controls.checkpoint()
    model_model_signature = _required_text(
        row["model_model_signature"], label="Semantic model signature"
    )
    if model_model_signature != model_signature:
        raise PublicationHeadsSchemaError("Semantic head model binding differs")
    modality = _required_text(row["modality"], label="Semantic model modality")
    if modality not in {"text", "image"}:
        raise PublicationHeadsSchemaError("Semantic model modality is invalid")
    vector_space = _required_text(row["vector_space"], label="Semantic vector space")
    generation_model = _required_text(
        row["generation_model_signature"], label="Semantic generation model signature"
    )
    if generation_model != model_signature:
        raise PublicationHeadsSchemaError("Semantic generation model binding differs")
    if row["generation_status"] != "ready":
        raise PublicationHeadsSchemaError(
            f"Semantic head points at a non-ready generation: {generation_id}"
        )
    processing_signature = _required_text(
        row["processing_signature"], label="Semantic processing signature"
    )
    _required_integer(row["completed_ns"], label="Semantic completion timestamp", minimum=0)
    if _required_integer(row["base_clone_complete"], label="Semantic clone state") != 1:
        raise PublicationHeadsSchemaError("Semantic published generation is not fully cloned")
    for name in (
        "pending_count",
        "leased_count",
        "done_count",
        "error_count",
        "stale_count",
    ):
        _required_integer(row[name], label=f"Semantic {name}")
    if any(
        _required_integer(row[name], label=f"Semantic {name}") != 0
        for name in ("pending_count", "leased_count", "error_count", "stale_count")
    ):
        raise PublicationHeadsSchemaError(
            f"Semantic published generation is partial: {generation_id}"
        )
    _canonical_object(row["cursor_json"], label="Semantic generation cursor")
    job_rows = connection.execute(
        "SELECT status,COUNT(*) AS count FROM embedding_jobs "
        "WHERE generation_id=? GROUP BY status ORDER BY status",
        (generation_id,),
    ).fetchall()
    if controls is not None:
        controls.checkpoint()
    if any(str(job["status"]) != "done" and int(job["count"]) for job in job_rows):
        raise PublicationHeadsSchemaError(
            f"Semantic published generation has unfinished jobs: {generation_id}"
        )
    mismatched_member = connection.execute(
        "SELECT 1 FROM embedding_generation_members "
        "WHERE generation_id=? AND model_signature<>? LIMIT 1",
        (generation_id, model_signature),
    ).fetchone()
    if mismatched_member is not None:
        raise PublicationHeadsSchemaError(
            f"Semantic published generation has a mismatched member: {generation_id}"
        )
    return _SemanticHead(
        model_signature=model_signature,
        modality=modality,
        vector_space=vector_space,
        generation_id=generation_id,
        processing_signature=processing_signature,
        published_ns=_required_integer(
            row["published_ns"], label="Semantic published timestamp", minimum=0
        ),
    )


def _read_semantic_heads(
    connection: sqlite3.Connection,
    controls: _ObservationControls | None = None,
) -> tuple[_SemanticHead, ...]:
    rows = connection.execute(
        """SELECT h.model_signature AS head_model_signature,
            h.generation_id AS head_generation_id,h.published_ns,
            m.model_signature AS model_model_signature,m.modality,m.vector_space,
            g.model_signature AS generation_model_signature,g.status AS generation_status,
            g.processing_signature,g.completed_ns,g.base_clone_complete,
            g.pending_count,g.leased_count,g.done_count,g.error_count,g.stale_count,
            g.cursor_json
        FROM published_embedding_heads h
        LEFT JOIN embedding_models m ON m.model_signature=h.model_signature
        LEFT JOIN embedding_generations g ON g.generation_id=h.generation_id
        ORDER BY h.model_signature LIMIT ?""",
        (MAX_SEMANTIC_PUBLICATION_HEADS + 1,),
    ).fetchall()
    if len(rows) > MAX_SEMANTIC_PUBLICATION_HEADS:
        raise PublicationHeadsSchemaError("Semantic publication heads exceed their bound")
    result: list[_SemanticHead] = []
    for row in rows:
        if controls is not None:
            controls.checkpoint()
        model_signature = _required_text(
            row["head_model_signature"], label="Semantic head model signature"
        )
        generation_id = _required_integer(
            row["head_generation_id"], label="Semantic head generation", minimum=1
        )
        result.append(
            _validate_semantic_generation(
                connection,
                row,
                model_signature=model_signature,
                generation_id=generation_id,
                controls=controls,
            )
        )
    return tuple(result)


def _semantic_owner_head(
    heads: tuple[_SemanticHead, ...],
    *,
    schema_version: int,
) -> StateOwnerHead:
    if not heads:
        return _empty_owner_head("semantic", schema_version=schema_version)
    payload = {
        "contract": SEMANTIC_PUBLICATION_HEADS_PROTOCOL,
        "owner": "semantic",
        "schema_version": schema_version,
        "heads": [
            {
                "model_signature": head.model_signature,
                "modality": head.modality,
                "vector_space": head.vector_space,
                "generation_id": head.generation_id,
                "processing_signature": head.processing_signature,
                "published_ns": head.published_ns,
            }
            for head in heads
        ],
    }
    return StateOwnerHead(
        owner="semantic",
        revision=max(head.generation_id for head in heads),
        digest_sha256=_digest_payload(payload, label="Semantic publication heads"),
        schema_version=schema_version,
    )


@contextmanager
def _semantic_owner_lease(
    state_directory: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
    timeout_seconds: float = READ_TIMEOUT_SECONDS,
) -> Iterator[_SemanticOwnerLease | None]:
    """Lend an existing Semantic owner to an integrated mutating boundary.

    This is intentionally not a public read mode.  The integrated lifecycle
    already owns the state lock, so it may open the existing owner read-write,
    pin one transaction, and provide that connection to the bounded projection
    helper.  No owner is created, migrated, checkpointed, or opened through a
    live ``mode=ro`` reader; absent state yields ``None`` for the normal empty
    baseline.
    """

    if checkpoint is not None and not callable(checkpoint):
        raise TypeError("Semantic owner lease checkpoint must be callable")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
    ):
        raise ValueError("Semantic owner lease timeout must be finite and positive")
    timeout = float(timeout_seconds)
    path = _state_directory(state_directory) / "semantic.sqlite3"
    if checkpoint is not None:
        checkpoint()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        yield None
        return
    except OSError as exc:
        raise PublicationHeadsStateError("Semantic owner cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PublicationHeadsStateError("Semantic owner must be a regular file")
    # A zero-byte owner with no sidecars is the documented empty baseline. Do
    # not send it through the writer lease, whose SQLite fence intentionally
    # rejects non-databases; the observer will return the same stable empty
    # head without creating or opening state.
    if _empty_or_absent_capture(path) is not None:
        if checkpoint is not None:
            checkpoint()
        yield None
        return

    from neocortex.persistence.sqlite_paths import existing_sqlite_uri
    from neocortex.persistence.sqlite_writer_snapshot import SQLiteProgressConnection

    connection: sqlite3.Connection | None = None
    try:
        initial_fence = capture_sqlite_read_fence(path)
        if (initial_fence.main.device, initial_fence.main.inode) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise PublicationHeadsDriftError("Semantic owner changed before its lease opened")
        if checkpoint is not None:
            checkpoint()
        connection = sqlite3.connect(
            existing_sqlite_uri(path),
            uri=True,
            timeout=timeout,
            factory=SQLiteProgressConnection,
        )
        connected_fence = capture_sqlite_read_fence(path)
        if connected_fence.main != initial_fence.main:
            connection.close()
            connection = None
            raise PublicationHeadsDriftError(
                "Semantic owner changed immediately after lease connect"
            )
    except (OSError, sqlite3.Error, ImmutableSQLiteUnavailable) as exc:
        if connection is not None:
            connection.close()
        raise PublicationHeadsStateError("Semantic owner lease cannot be opened") from exc
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    expected_close_fence: SQLiteImmutableFence | None = None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout * 1000))}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        progress_failure: BaseException | None = None

        def progress() -> int:
            nonlocal progress_failure
            try:
                if checkpoint is not None:
                    checkpoint()
            except BaseException as exc:
                progress_failure = exc
                return 1
            return 0

        connection.set_progress_handler(progress, SQL_PROGRESS_OPCODES)
        # Establish and release one empty read transaction before the caller
        # captures its owner fence.  SQLite may materialize a residual
        # ``-wal=0``/``-shm=32KiB`` pair when a read-write lease first joins a
        # WAL owner; treating that normal lease lifecycle as mid-read drift
        # would reject an otherwise pinned, bounded projection.
        try:
            if checkpoint is not None:
                checkpoint()
            connection.execute("BEGIN")
            connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
            if progress_failure is not None:
                raise progress_failure
            connection.rollback()
            if checkpoint is not None:
                checkpoint()
            current_fence = capture_sqlite_read_fence(path)
            if current_fence.main != initial_fence.main:
                raise PublicationHeadsDriftError("Semantic owner changed while acquiring its lease")
            expected_close_fence = current_fence
            lease = _SemanticOwnerLease(
                _LEASE_CONSTRUCTOR_TOKEN,
                connection=connection,
                fence=current_fence,
                owner_identity=(current_fence.main.device, current_fence.main.inode),
            )
            lease_token = _ACTIVE_OWNER_LEASES.set(
                (*_ACTIVE_OWNER_LEASES.get(), lease)
            )
            try:
                yield lease
            finally:
                _ACTIVE_OWNER_LEASES.reset(lease_token)
        except sqlite3.OperationalError as exc:
            if progress_failure is not None:
                raise progress_failure from exc
            raise
    except BaseException as exc:
        assert connection is not None
        if connection.in_transaction:
            try:
                connection.rollback()
            except BaseException as rollback_failure:
                exc.add_note(
                    "Semantic owner lease rollback cleanup failed: "
                    f"{type(rollback_failure).__name__}: {rollback_failure}"
                )
        raise
    finally:
        assert connection is not None
        primary = sys.exception()
        cleanup_errors: list[BaseException] = []
        try:
            connection.set_progress_handler(None, 0)
        except BaseException as restore_failure:
            cleanup_errors.append(restore_failure)
        try:
            connection.close()
        except BaseException as close_failure:
            cleanup_errors.append(close_failure)
        if expected_close_fence is not None:
            try:
                _verify_lease_close_fence(path, expected_close_fence)
            except BaseException as fence_failure:
                cleanup_errors.append(fence_failure)
        if primary is not None:
            for cleanup_failure in cleanup_errors:
                primary.add_note(
                    "Semantic owner lease cleanup failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
        elif cleanup_errors:
            first_error, *additional = cleanup_errors
            for additional_failure in additional:
                first_error.add_note(
                    "Semantic owner lease cleanup also failed: "
                    f"{type(additional_failure).__name__}: {additional_failure}"
                )
            raise first_error


def _observe_semantic_from_owner(
    state_directory: Path,
    controls: _ObservationControls,
    lease: _SemanticOwnerLease,
) -> _SemanticObservation:
    """Project only publication-head facts from a leased Semantic owner."""

    from neocortex.persistence.sqlite_writer_snapshot import (
        SQLiteProgressConnection,
        writer_coordinated_sqlite_snapshot,
    )

    if not isinstance(lease, _SemanticOwnerLease) or not _active_owner_lease(lease):
        raise PublicationHeadsStateError(
            "coordinated Semantic publication reads require an authenticated lease"
        )
    writer_connection = lease.connection
    if not isinstance(writer_connection, SQLiteProgressConnection):
        raise PublicationHeadsStateError(
            "coordinated Semantic publication reads require the owner lease"
        )
    path = _state_directory(state_directory) / "semantic.sqlite3"
    source_fence = lease.fence
    if capture_sqlite_read_fence(path) != source_fence:
        raise PublicationHeadsDriftError("Semantic owner changed before its projection")
    owner_identity = lease.owner_identity
    projected: list[tuple[int | None, tuple[_SemanticHead, ...]]] = []

    def projection(
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        _budget_state: object,
    ) -> None:
        controls.checkpoint()
        schema_version = _semantic_schema(source, controls)
        heads = () if schema_version is None else _read_semantic_heads(source, controls)
        raw_heads = [
            {
                "model_signature": head.model_signature,
                "modality": head.modality,
                "vector_space": head.vector_space,
                "generation_id": head.generation_id,
                "processing_signature": head.processing_signature,
                "published_ns": head.published_ns,
            }
            for head in heads
        ]
        payload = canonical_json({"schema_version": schema_version, "heads": raw_heads})
        if len(payload.encode("utf-8", "surrogatepass")) > MAX_AGGREGATE_DIGEST_BYTES:
            raise PublicationHeadsSchemaError("Semantic publication-head projection exceeds its bound")
        target.execute(
            "CREATE TABLE semantic_publication_projection(schema_version INTEGER, payload TEXT NOT NULL)"
        )
        target.execute(
            "INSERT INTO semantic_publication_projection(schema_version,payload) VALUES(?,?)",
            (schema_version, payload),
        )
        projected.append((schema_version, heads))
        controls.checkpoint()

    with writer_coordinated_sqlite_snapshot(
        writer_connection,
        path,
        owner_identity=owner_identity,
        projection=projection,
        timeout_seconds=READ_TIMEOUT_SECONDS,
        budget=controls.session_budget(),
    ):
        controls.checkpoint()
    if len(projected) != 1:
        raise PublicationHeadsStateError("Semantic publication-head projection is incomplete")
    _verify_capture(_OwnerCapture(path, source_fence, None, False))
    controls.checkpoint()
    schema_version, heads = projected[0]
    selected_schema = SEMANTIC_SCHEMA_VERSION if schema_version is None else schema_version
    return _SemanticObservation(
        _semantic_owner_head(heads, schema_version=selected_schema),
        tuple((head.model_signature, head.generation_id) for head in heads),
        heads,
        _OwnerCapture(path, source_fence, None, False),
    )


def _observe_semantic(
    state_directory: Path,
    controls: _ObservationControls,
    *,
    owner_lease: _SemanticOwnerLease | None = None,
) -> _SemanticObservation:
    path = state_directory / "semantic.sqlite3"
    controls.checkpoint()
    empty_capture = _empty_or_absent_capture(path)
    if empty_capture is not None:
        controls.checkpoint()
        observation = _SemanticObservation(
            _empty_owner_head("semantic", schema_version=SEMANTIC_SCHEMA_VERSION),
            (),
            (),
            empty_capture,
        )
        _verify_capture(empty_capture)
        controls.checkpoint()
        return observation

    if owner_lease is not None:
        return _observe_semantic_from_owner(state_directory, controls, owner_lease)

    last_snapshot_budget_error: SQLiteSnapshotBudgetExceeded | None = None
    for attempt in range(PUBLICATION_HEAD_SNAPSHOT_RETRIES):
        try:
            controls.checkpoint()
            mode = preferred_sqlite_read_mode(path)
            session = SQLiteReadSession(
                path,
                mode=mode,
                timeout_seconds=READ_TIMEOUT_SECONDS,
                max_attempts=2,
                budget=controls.session_budget(),
            )
            with session as connection:
                with _sql_progress(connection, controls):
                    connection.execute("BEGIN")
                    schema_version = _semantic_schema(connection, controls)
                    if schema_version is None:
                        heads: tuple[_SemanticHead, ...] = ()
                    else:
                        heads = _read_semantic_heads(connection, controls)
                    capture = _OwnerCapture(path, session.source_fence, None, False)
            _verify_capture(capture)
            controls.checkpoint()
        except SQLiteSnapshotBudgetExceeded as exc:
            last_snapshot_budget_error = exc
            if attempt + 1 >= PUBLICATION_HEAD_SNAPSHOT_RETRIES:
                break
            # Do not turn an exhausted snapshot into an unbounded wait.  The
            # short, cancellable pause only lets a writer finish a checkpoint;
            # the next attempt recaptures mode, fence and the full snapshot
            # budget from scratch.
            controls.checkpoint()
            time.sleep(PUBLICATION_HEAD_RETRY_DELAY_SECONDS)
            controls.checkpoint()
            continue
        except PublicationHeadsError:
            raise
        except Exception as exc:
            raise PublicationHeadsError(
                f"Semantic publication heads could not be observed: {type(exc).__name__}"
            ) from exc
        else:
            last_snapshot_budget_error = None
            break
    if last_snapshot_budget_error is not None:
        raise PublicationHeadsError(
            "Semantic publication heads could not be observed: "
            f"{type(last_snapshot_budget_error).__name__}"
        ) from last_snapshot_budget_error
    if schema_version is None:
        owner_head = _empty_owner_head(
            "semantic",
            schema_version=SEMANTIC_SCHEMA_VERSION,
        )
    else:
        owner_head = _semantic_owner_head(heads, schema_version=schema_version)
    return _SemanticObservation(
        owner_head,
        tuple((head.model_signature, head.generation_id) for head in heads),
        heads,
        capture,
    )


def observe_integrated_owner_heads(
    state_directory: Path,
    *,
    snapshot_budget: SQLiteSnapshotBudget | None = None,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
    _writer_lease: _SemanticOwnerLease | None = None,
) -> tuple[StateOwnerHead, ...]:
    """Observe the fresh Semantic owner head through a bounded read-only fence.

    A missing or truly empty database is represented by revision ``0`` and the
    stable Semantic empty-set digest.  A malformed, partial, future, or
    drifting owner raises ``PublicationHeadsError``; no database is created or
    migrated.
    """

    controls = _ObservationControls(
        snapshot_budget,
        deadline_monotonic=deadline_monotonic,
        cancellation_check=cancellation_check,
    )
    selected = _state_directory(state_directory)
    semantic = _observe_semantic(selected, controls, owner_lease=_writer_lease)
    return (semantic.owner_head,)


def observe_semantic_generation_heads(
    state_directory: Path,
    *,
    snapshot_budget: SQLiteSnapshotBudget | None = None,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
    _writer_lease: _SemanticOwnerLease | None = None,
) -> tuple[tuple[str, int], ...]:
    """Return every validated published Semantic ``(model, generation)`` head."""

    controls = _ObservationControls(
        snapshot_budget,
        deadline_monotonic=deadline_monotonic,
        cancellation_check=cancellation_check,
    )
    selected = _state_directory(state_directory)
    return _observe_semantic(
        selected,
        controls,
        owner_lease=_writer_lease,
    ).generation_heads


__all__ = [
    "MAX_AGGREGATE_DIGEST_BYTES",
    "MAX_APPLICATION_OBJECTS",
    "MAX_SEMANTIC_PUBLICATION_HEADS",
    "SEMANTIC_PUBLICATION_HEADS_PROTOCOL",
    "IntegratedOwnerHeadsError",
    "PublicationHeadsDriftError",
    "PublicationHeadsError",
    "PublicationHeadsSchemaError",
    "PublicationHeadsStateError",
    "SemanticPublicationHeadsError",
    "observe_integrated_owner_heads",
    "observe_semantic_generation_heads",
]
