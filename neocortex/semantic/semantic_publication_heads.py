"""Fresh, fenced observations of the Semantic and Code publication heads.

This module is deliberately a reader-only bridge.  It does not read the
cross-owner epoch marker: the owner databases are the only source that can
prove the current head set.  Each owner is opened through
``SQLiteReadSession`` and its source fence is checked again before the
observation is returned.

Semantic has one durable head per model signature.  Code has named graph
heads and a current projection of Semantic links in the existing Code owner.
The public ``StateOwnerHead`` therefore contains an aggregate revision and a
canonical digest of the complete published set, rather than just the largest
generation identifier.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from neocortex.code.code_graph_generations import CodeGraphGenerationStore
from neocortex.code.code_schema import (
    CODE_SCHEMA_VERSION,
    _read_version,
    validate_code_schema,
)
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    SQLiteSnapshotBudget,
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
    _validate_version_contract,
)


SEMANTIC_PUBLICATION_HEADS_PROTOCOL = "neocortex.semantic-publication-heads/v1"
CODE_PUBLICATION_HEADS_PROTOCOL = "neocortex.code-publication-heads/v1"

MAX_SEMANTIC_PUBLICATION_HEADS = 1_024
MAX_CODE_GRAPH_HEADS = 1_024
MAX_ACTIVE_CODE_LINKS = 100_000
MAX_APPLICATION_OBJECTS = 512
MAX_JSON_BYTES = 256 * 1024
MAX_AGGREGATE_DIGEST_BYTES = 64 * 1024 * 1024
READ_TIMEOUT_SECONDS = 60.0
SQL_PROGRESS_OPCODES = 1_000


_EXPECTED_SCHEMA_VERSIONS = {
    "semantic": SEMANTIC_SCHEMA_VERSION,
    "code": CODE_SCHEMA_VERSION,
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
# or the integrated boundary explicitly.  They intentionally share one base
# contract so a caller can catch either the focused or aggregate error.
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


@dataclass(frozen=True, slots=True)
class _CodeHead:
    head_name: str
    generation_id: str
    generation_digest: str
    revision: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class _CodeLink:
    chunk_id: int
    semantic_item_id: str
    model_signature: str
    vector_space: str
    generation_id: int
    provenance: dict[str, object]


class _ObservationControls:
    """One cooperative budget shared by the Semantic and Code owner reads."""

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
    if selected_schema != expected_schema:
        raise PublicationHeadsSchemaError(
            f"empty {owner} schema differs from the expected owner schema"
        )
    protocol = (
        SEMANTIC_PUBLICATION_HEADS_PROTOCOL
        if owner == "semantic"
        else CODE_PUBLICATION_HEADS_PROTOCOL
    )
    digest = _digest_payload(
        {
            "contract": protocol,
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
    if version != SEMANTIC_SCHEMA_VERSION:
        raise PublicationHeadsSchemaError(
            f"Semantic schema is not the current publication schema: {version!r}"
        )
    try:
        _validate_version_contract(connection, version)
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


def _observe_semantic(
    state_directory: Path,
    controls: _ObservationControls,
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
    except PublicationHeadsError:
        raise
    except Exception as exc:
        raise PublicationHeadsError(
            f"Semantic publication heads could not be observed: {type(exc).__name__}"
        ) from exc
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


def _code_schema(
    connection: sqlite3.Connection,
    controls: _ObservationControls | None = None,
) -> int | None:
    if controls is not None:
        controls.checkpoint()
    try:
        objects = _application_objects(connection, controls)
        if not objects:
            pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if pragma_version != 0:
                raise PublicationHeadsSchemaError(
                    f"Code empty database declares an unsupported schema: {pragma_version!r}"
                )
            return None
        version = _read_version(connection)
        if version != CODE_SCHEMA_VERSION:
            raise PublicationHeadsSchemaError(
                f"Code schema is not the current publication schema: {version!r}"
            )
        validate_code_schema(connection)
    except PublicationHeadsError:
        raise
    except (RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        if controls is not None:
            controls.raise_sql_failure_or_checkpoint(exc)
        raise PublicationHeadsSchemaError("Code schema contract is invalid") from exc
    return CODE_SCHEMA_VERSION


def _read_code_graph_heads(
    connection: sqlite3.Connection,
    controls: _ObservationControls | None = None,
) -> tuple[_CodeHead, ...]:
    try:
        store = CodeGraphGenerationStore(connection)
    except Exception as exc:
        if controls is not None:
            controls.raise_sql_failure_or_checkpoint(exc)
        raise PublicationHeadsSchemaError("Code graph schema contract is invalid") from exc
    rows = connection.execute(
        """SELECT head_name,generation_id,generation_digest,revision,updated_ns
        FROM graph_heads ORDER BY head_name LIMIT ?""",
        (MAX_CODE_GRAPH_HEADS + 1,),
    ).fetchall()
    if len(rows) > MAX_CODE_GRAPH_HEADS:
        raise PublicationHeadsSchemaError("Code graph heads exceed their bound")
    result: list[_CodeHead] = []
    for row in rows:
        if controls is not None:
            controls.checkpoint()
        head_name = _required_text(row["head_name"], label="Code graph head name")
        generation_id = _required_text(
            row["generation_id"], label="Code graph generation identifier"
        )
        generation_digest = _required_text(
            row["generation_digest"], label="Code graph generation digest"
        )
        revision = _required_integer(row["revision"], label="Code graph head revision", minimum=1)
        updated_ns = _required_integer(
            row["updated_ns"], label="Code graph head timestamp", minimum=1
        )
        head = store.get_head(head_name)
        if head is None or (
            head.generation_id,
            head.generation_digest,
            head.revision,
            head.updated_ns,
        ) != (generation_id, generation_digest, revision, updated_ns):
            raise PublicationHeadsSchemaError("Code graph head changed during observation")
        generation = store.get_generation(generation_id)
        if generation is None or generation.status != "published":
            raise PublicationHeadsSchemaError(
                f"Code graph head points at a non-published generation: {generation_id}"
            )
        if generation.generation_digest != generation_digest:
            raise PublicationHeadsSchemaError("Code graph generation digest differs from head")
        if generation.completed_ns is None or generation.completed_ns <= 0:
            raise PublicationHeadsSchemaError("Code published generation timestamp is invalid")
        snapshot = connection.execute(
            "SELECT status,input_digest,input_count FROM graph_input_snapshots "
            "WHERE snapshot_id=?",
            (generation.snapshot_id,),
        ).fetchone()
        if snapshot is None or snapshot["status"] != "sealed":
            raise PublicationHeadsSchemaError(
                f"Code graph generation snapshot is not sealed: {generation_id}"
            )
        _required_text(snapshot["input_digest"], label="Code graph input digest")
        _required_integer(snapshot["input_count"], label="Code graph input count")
        source_run_id = generation.metadata.get("source_run_id")
        if source_run_id is not None:
            source_run_id = _required_integer(
                source_run_id, label="Code graph source run", minimum=1
            )
            try:
                store.validate_source_run_id(source_run_id)
            except Exception as exc:
                raise PublicationHeadsSchemaError(
                    f"Code graph source run is not a completed producer: {generation_id}"
                ) from exc
        result.append(
            _CodeHead(head_name, generation_id, generation_digest, revision, updated_ns)
        )
    return tuple(result)


def _read_current_code_links(
    connection: sqlite3.Connection,
    semantic_heads: tuple[_SemanticHead, ...],
    controls: _ObservationControls | None = None,
) -> tuple[_CodeLink, ...]:
    expected = {
        (head.model_signature, head.generation_id): head.vector_space
        for head in semantic_heads
    }
    rows = connection.execute(
        """SELECT e.chunk_id,e.semantic_item_id,e.model_signature,e.vector_space,
            e.generation_id,e.active,e.provenance_json,
            CASE WHEN c.chunk_id IS NOT NULL
                AND v.version_id IS NOT NULL
                AND f.current_version_id=v.version_id
                AND f.status='current'
                AND v.invalidated_ns IS NULL THEN 1 ELSE 0 END AS is_current
        FROM embedding_links e
        LEFT JOIN code_chunks c ON c.chunk_id=e.chunk_id
        LEFT JOIN file_versions v ON v.version_id=c.version_id
        LEFT JOIN files f ON f.current_version_id=v.version_id
        WHERE e.active=1
        ORDER BY e.model_signature,e.generation_id,e.chunk_id,e.semantic_item_id
        LIMIT ?""",
        (MAX_ACTIVE_CODE_LINKS + 1,),
    ).fetchall()
    if len(rows) > MAX_ACTIVE_CODE_LINKS:
        raise PublicationHeadsSchemaError("active Code Semantic links exceed their bound")
    result: list[_CodeLink] = []
    seen: set[tuple[int, str, int]] = set()
    for row in rows:
        if controls is not None:
            controls.checkpoint()
        if _required_integer(row["active"], label="Code Semantic link active flag") != 1:
            raise PublicationHeadsSchemaError("Code Semantic link active flag is invalid")
        chunk_id = _required_integer(row["chunk_id"], label="Code Semantic chunk", minimum=1)
        semantic_item_id = _required_text(
            row["semantic_item_id"], label="Code Semantic item identity"
        )
        model_signature = _required_text(
            row["model_signature"], label="Code Semantic model signature"
        )
        vector_space = _required_text(row["vector_space"], label="Code Semantic vector space")
        generation_id = _required_integer(
            row["generation_id"], label="Code Semantic generation", minimum=1
        )
        key = (chunk_id, model_signature, generation_id)
        if key in seen:
            raise PublicationHeadsSchemaError("Code Semantic links are not unique")
        seen.add(key)
        expected_space = expected.get((model_signature, generation_id))
        if expected_space is None:
            raise PublicationHeadsSchemaError(
                "active Code Semantic link has no matching published Semantic head"
            )
        if vector_space != expected_space:
            raise PublicationHeadsSchemaError(
                "active Code Semantic link vector space differs from its Semantic head"
            )
        if _required_integer(row["is_current"], label="Code Semantic current flag") != 1:
            raise PublicationHeadsSchemaError(
                "active Code Semantic link does not resolve to a current Code chunk"
            )
        provenance = _canonical_object(
            row["provenance_json"], label="Code Semantic link provenance"
        )
        result.append(
            _CodeLink(
                chunk_id,
                semantic_item_id,
                model_signature,
                vector_space,
                generation_id,
                provenance,
            )
        )
    return tuple(result)


def _code_owner_head(
    graph_heads: tuple[_CodeHead, ...],
    links: tuple[_CodeLink, ...],
    *,
    schema_version: int,
) -> StateOwnerHead:
    if not graph_heads and not links:
        return _empty_owner_head("code", schema_version=schema_version)
    payload = {
        "contract": CODE_PUBLICATION_HEADS_PROTOCOL,
        "owner": "code",
        "schema_version": schema_version,
        "graph_heads": [
            {
                "head_name": head.head_name,
                "generation_id": head.generation_id,
                "generation_digest": head.generation_digest,
                "revision": head.revision,
                "updated_ns": head.updated_ns,
            }
            for head in graph_heads
        ],
        "embedding_links": [
            {
                "chunk_id": link.chunk_id,
                "semantic_item_id": link.semantic_item_id,
                "model_signature": link.model_signature,
                "vector_space": link.vector_space,
                "generation_id": link.generation_id,
                "provenance": link.provenance,
            }
            for link in links
        ],
    }
    return StateOwnerHead(
        owner="code",
        revision=max((head.revision for head in graph_heads), default=0),
        digest_sha256=_digest_payload(payload, label="Code publication heads"),
        schema_version=schema_version,
    )


def _observe_code(
    state_directory: Path,
    semantic: _SemanticObservation,
    controls: _ObservationControls,
) -> tuple[StateOwnerHead, _OwnerCapture]:
    path = state_directory / "code.sqlite3"
    controls.checkpoint()
    empty_capture = _empty_or_absent_capture(path)
    if empty_capture is not None:
        controls.checkpoint()
        _verify_capture(empty_capture)
        controls.checkpoint()
        return (
            _empty_owner_head("code", schema_version=CODE_SCHEMA_VERSION),
            empty_capture,
        )
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
                schema_version = _code_schema(connection, controls)
                if schema_version is None:
                    graph_heads: tuple[_CodeHead, ...] = ()
                    links: tuple[_CodeLink, ...] = ()
                else:
                    graph_heads = _read_code_graph_heads(connection, controls)
                    links = _read_current_code_links(
                        connection,
                        semantic.semantic_heads,
                        controls,
                    )
                capture = _OwnerCapture(path, session.source_fence, None, False)
        _verify_capture(capture)
        controls.checkpoint()
    except PublicationHeadsError:
        raise
    except Exception as exc:
        raise PublicationHeadsError(
            f"Code publication heads could not be observed: {type(exc).__name__}"
        ) from exc
    if schema_version is None:
        return _empty_owner_head("code", schema_version=CODE_SCHEMA_VERSION), capture
    return _code_owner_head(graph_heads, links, schema_version=schema_version), capture


def observe_integrated_owner_heads(
    state_directory: Path,
    *,
    include_code: bool = False,
    snapshot_budget: SQLiteSnapshotBudget | None = None,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
) -> tuple[StateOwnerHead, ...]:
    """Observe fresh Semantic (and optionally Code) owner heads.

    The result is one aggregate ``StateOwnerHead`` per requested owner.  A
    missing or truly empty database is represented by revision ``0`` and the
    stable owner/schema-specific empty-set digest.  A malformed, partial,
    future, or drifting owner raises ``PublicationHeadsError`` instead; no
    publication epoch JSON is consulted and no database is created or
    migrated.  ``snapshot_budget`` bounds detached SQLite preparation;
    ``deadline_monotonic`` and ``cancellation_check`` are shared across both
    owner reads and are also installed as SQLite progress checkpoints.
    """

    if not isinstance(include_code, bool):
        raise TypeError("include_code must be a boolean")
    controls = _ObservationControls(
        snapshot_budget,
        deadline_monotonic=deadline_monotonic,
        cancellation_check=cancellation_check,
    )
    selected = _state_directory(state_directory)
    semantic = _observe_semantic(selected, controls)
    if not include_code:
        return (semantic.owner_head,)
    code_head, code_capture = _observe_code(selected, semantic, controls)
    # Code links are authenticated against the Semantic set read earlier;
    # prove that set did not drift while the second owner was read.
    _verify_capture(semantic.capture)
    _verify_capture(code_capture)
    controls.checkpoint()
    return semantic.owner_head, code_head


def observe_semantic_generation_heads(
    state_directory: Path,
    *,
    snapshot_budget: SQLiteSnapshotBudget | None = None,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
) -> tuple[tuple[str, int], ...]:
    """Return every validated published Semantic ``(model, generation)`` head."""

    controls = _ObservationControls(
        snapshot_budget,
        deadline_monotonic=deadline_monotonic,
        cancellation_check=cancellation_check,
    )
    selected = _state_directory(state_directory)
    return _observe_semantic(selected, controls).generation_heads


__all__ = [
    "CODE_PUBLICATION_HEADS_PROTOCOL",
    "MAX_ACTIVE_CODE_LINKS",
    "MAX_AGGREGATE_DIGEST_BYTES",
    "MAX_APPLICATION_OBJECTS",
    "MAX_CODE_GRAPH_HEADS",
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
