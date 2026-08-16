"""Read-only logical snapshots across independently published SQLite owners.

There is no distributed transaction across owner databases.  This collector
records that boundary explicitly, observes every owner twice on the same
read-only connection and every complete owner vector twice, retries the
complete capture at most once, and reports a changed snapshot rather than
inventing atomicity.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from _02_Deduplicacion.inventory_schema import validate_inventory_schema
from neocortex.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.platform_policy import sqlite_path_collation

from . import (
    audio_state,
    document_catalog_schema,
    office_state,
    text_state,
    video_state,
)
from . import semantic_schema as semantic_schema_module
from .code_schema import validate_code_schema
from .capabilities.formats.archive import state as archive_state
from .capabilities.formats.docx.schema import validate_docx_schema
from .framework_schema import (
    validate_framework_schema_v19,
    validate_framework_schema_v20,
    validate_framework_schema_v21,
    validate_framework_schema_v22,
)
from .knowledge_contracts import (
    ActiveModel,
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PublicationHead,
    SnapshotConsistency,
)
from .pdf_schema import validate_pdf_schema
from .semantic_models import canonical_json
from .sqlite_cancellation import SQLiteCancellationBridge, sqlite_cancellation_scope
from .sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    capture_sqlite_immutable_fence,
)
from .sqlite_paths import readonly_sqlite_uri
from .state_topology_contracts import STATE_STORE_REGISTRY


# region [01] Owner registry and state paths


MAX_SNAPSHOT_HEADS = 1_024
_PATH_COLLATION = sqlite_path_collation()

CancellationCheck = Callable[[], None]


@dataclass(slots=True)
class _CancellationController:
    callback: CancellationCheck | None
    raised_exception: BaseException | None = None

    def checkpoint(self) -> None:
        if self.callback is None:
            return
        try:
            self.callback()
        except BaseException as exc:
            self.raised_exception = exc
            raise
        self.raised_exception = None

    def raised_here(self, exc: BaseException) -> bool:
        return self.raised_exception is exc


_STATE_PATH_NAMES = tuple(store.knowledge_path_attribute for store in STATE_STORE_REGISTRY.stores)


class KnowledgeStateRootError(RuntimeError):
    """An existing Knowledge state root cannot be inspected safely."""

    def __init__(self, root: Path, reason: str, detail: str | None = None) -> None:
        self.root = root
        self.reason = reason
        self.detail = detail
        message = f"Knowledge state root {reason}: {root}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


def _state_roots(paths: KnowledgeStatePaths) -> tuple[Path, ...]:
    roots: dict[str, Path] = {}
    for name in _STATE_PATH_NAMES:
        value = getattr(paths, name)
        if value is None:
            continue
        root = Path(value).parent.absolute()
        key = os.path.normcase(os.path.normpath(os.fspath(root)))
        roots.setdefault(key, root)
    return tuple(roots.values())


def _inaccessible_root(root: Path, exc: OSError) -> KnowledgeStateRootError:
    detail = exc.strerror or str(exc)
    return KnowledgeStateRootError(root, "is inaccessible", detail)


def _require_stable_root_presence(
    before: tuple[Path, ...],
    after: tuple[Path, ...],
) -> None:
    def by_key(values: tuple[Path, ...]) -> dict[str, Path]:
        return {os.path.normcase(os.path.normpath(os.fspath(value))): value for value in values}

    before_by_key = by_key(before)
    after_by_key = by_key(after)
    changed_keys = before_by_key.keys() ^ after_by_key.keys()
    if not changed_keys:
        return
    changed_key = min(changed_keys)
    root = before_by_key.get(changed_key) or after_by_key[changed_key]
    raise KnowledgeStateRootError(root, "changed during snapshot capture")


@dataclass(frozen=True, slots=True)
class KnowledgeStatePaths:
    inventory: Path
    framework: Path
    catalog: Path
    pdf: Path
    docx: Path
    office: Path
    audio: Path
    image: Path
    semantic: Path
    code: Path
    archive: Path | None = None
    text: Path | None = None
    video: Path | None = None

    def validate_roots(self) -> tuple[Path, ...]:
        """Permit missing roots but fail closed for unusable existing roots."""

        present: list[Path] = []
        for root in _state_roots(self):
            try:
                os.lstat(root)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _inaccessible_root(root, exc) from exc

            try:
                root_stat = os.stat(root)
            except OSError as exc:
                raise _inaccessible_root(root, exc) from exc

            if not stat.S_ISDIR(root_stat.st_mode):
                raise KnowledgeStateRootError(root, "is not a directory")

            try:
                with os.scandir(root) as entries:
                    next(entries, None)
            except FileNotFoundError as exc:
                raise KnowledgeStateRootError(
                    root,
                    "changed during validation",
                    exc.strerror or str(exc),
                ) from exc
            except NotADirectoryError as exc:
                raise KnowledgeStateRootError(
                    root,
                    "is not a directory",
                    exc.strerror or str(exc),
                ) from exc
            except OSError as exc:
                raise _inaccessible_root(root, exc) from exc
            present.append(root)
        return tuple(present)

    @classmethod
    def from_directory(cls, state_directory: Path) -> KnowledgeStatePaths:
        requested_root = Path(state_directory)
        try:
            root = requested_root.absolute()
        except OSError as exc:
            raise _inaccessible_root(requested_root, exc) from exc
        return cls(
            **{
                store.knowledge_path_attribute: root / store.database_name
                for store in STATE_STORE_REGISTRY.stores
            }
        )


type _Validator = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class _OwnerSpec:
    owner: str
    expected_schema: int
    validate: _Validator
    read_kind: str
    legacy_read_validators: tuple[tuple[int, _Validator], ...] = ()

    def validator_for(self, observed_schema: int) -> _Validator | None:
        if observed_schema == self.expected_schema:
            return self.validate
        return next(
            (
                validator
                for version, validator in self.legacy_read_validators
                if version == observed_schema
            ),
            None,
        )


def _validate_catalog(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        document_catalog_schema.document_catalog_schema_contract(),
        label="document catalog",
        exact=True,
    )


def _validate_office(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        office_state._office_schema_contract(),
        label="Office state",
        exact=True,
    )


def _validate_audio(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        audio_state._audio_schema_contract(),
        label="audio state",
        exact=True,
    )


def _validate_archive(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        archive_state.archive_schema_contract(),
        label="archive state",
        exact=True,
    )


def _validate_text(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        text_state.text_schema_contract(),
        label="text state",
        exact=True,
    )


def _validate_image(connection: sqlite3.Connection) -> None:
    from .capabilities.formats.image import state as image_state

    image_state._validate_current_image_schema(connection)


def _validate_semantic(connection: sqlite3.Connection) -> None:
    semantic_schema_module._validate_schema(
        connection,
        STATE_STORE_REGISTRY.by_owner("semantic").expected_schema_version,
    )


def _owner_spec(
    owner: str,
    validate: _Validator,
    legacy_read_validators: tuple[tuple[int, _Validator], ...] = (),
) -> _OwnerSpec:
    contract = STATE_STORE_REGISTRY.by_owner(owner)
    return _OwnerSpec(
        contract.state_owner_id,
        contract.expected_schema_version,
        validate,
        contract.knowledge_read_kind,
        legacy_read_validators,
    )


_OWNER_VALIDATORS: dict[
    str,
    tuple[_Validator, tuple[tuple[int, _Validator], ...]],
] = {
    "inventory": (validate_inventory_schema, ()),
    "framework": (
        validate_framework_schema_v22,
        (
            (19, validate_framework_schema_v19),
            (20, validate_framework_schema_v20),
            (21, validate_framework_schema_v21),
        ),
    ),
    "catalog": (_validate_catalog, ()),
    "pdf": (validate_pdf_schema, ()),
    "docx": (validate_docx_schema, ()),
    "office": (_validate_office, ()),
    "audio": (_validate_audio, ()),
    "video": (video_state.validate_video_schema, ()),
    "image": (_validate_image, ()),
    "semantic": (_validate_semantic, ()),
    "code": (validate_code_schema, ()),
    "archive": (_validate_archive, ()),
    "text": (_validate_text, ()),
}


def _owner_specs(paths: KnowledgeStatePaths) -> tuple[_OwnerSpec, ...]:
    """Expose additive owners only after their databases exist."""

    if set(_OWNER_VALIDATORS) != {store.state_owner_id for store in STATE_STORE_REGISTRY.stores}:
        raise RuntimeError("Knowledge owner validators disagree with the state store registry")
    specs: list[_OwnerSpec] = []
    for contract in STATE_STORE_REGISTRY.stores:
        path = getattr(paths, contract.knowledge_path_attribute)
        if path is None:
            continue
        if contract.knowledge_capture_mode == "if_present":
            try:
                path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                # Let normal owner capture classify an inaccessible configured path.
                pass
        validator, legacy_validators = _OWNER_VALIDATORS[contract.state_owner_id]
        specs.append(
            _owner_spec(
                contract.state_owner_id,
                validator,
                legacy_validators,
            )
        )
    return tuple(specs)


# endregion [01]


# region [02] Read-only connection and version boundary


def _connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    uri = readonly_sqlite_uri(path)
    if immutable:
        uri = f"{uri}&immutable=1"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=60,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("Knowledge reader could not enable foreign keys")
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("Knowledge reader could not enforce query-only mode")
    except BaseException:
        connection.close()
        raise
    return connection


def _observed_schema_version(
    connection: sqlite3.Connection,
    spec: _OwnerSpec,
) -> int | None:
    metadata_version = read_application_schema_version(
        connection,
        label=spec.owner,
    )
    pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if spec.owner in {"semantic", "code"}:
        if metadata_version is None and pragma_version == 0:
            return None
        if metadata_version is None:
            return pragma_version
        if pragma_version not in {0, metadata_version}:
            raise RuntimeError(f"{spec.owner} metadata and PRAGMA user_version disagree")
    return metadata_version


def _sqlite_error_code(exc: BaseException) -> str:
    name = getattr(exc, "sqlite_errorname", None)
    if isinstance(name, str) and name:
        return name
    return type(exc).__name__


def _is_corrupt_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in ("file is not a database", "database disk image is malformed")
    )


# endregion [02]


# region [03] Bounded owner-specific heads and logical watermarks


@dataclass(frozen=True, slots=True)
class _LogicalObservation:
    publications: tuple[PublicationHead, ...]
    watermarks: tuple[LogicalWatermark, ...]
    active_models: tuple[ActiveModel, ...]


def _limited_rows(
    connection: sqlite3.Connection,
    sql: str,
) -> tuple[sqlite3.Row, ...]:
    rows = tuple(connection.execute(sql, (MAX_SNAPSHOT_HEADS + 1,)).fetchall())
    if len(rows) > MAX_SNAPSHOT_HEADS:
        raise RuntimeError(f"Knowledge snapshot exceeds {MAX_SNAPSHOT_HEADS} publication heads")
    return rows


def _inventory_duplicate_plan_signal(row: sqlite3.Row) -> str | None:
    completed_ns = row["duplicate_plan_completed_ns"]
    if completed_ns is None:
        return None
    return (
        "duplicate-plan-v1:"
        f"{int(completed_ns)}:"
        f"{int(row['duplicate_group_count'])}:"
        f"{int(row['duplicate_redundant_files'])}:"
        f"{int(row['duplicate_reclaimable_bytes'])}"
    )


def _inventory_observation(connection: sqlite3.Connection) -> _LogicalObservation:
    rows = _limited_rows(
        connection,
        f"""SELECT c.root,c.scan_id,c.updated_ns,
        p.group_count AS duplicate_group_count,
        p.redundant_files AS duplicate_redundant_files,
        p.reclaimable_bytes AS duplicate_reclaimable_bytes,
        p.completed_ns AS duplicate_plan_completed_ns,
        s.scan_id AS matched_scan_id,s.status AS scan_status
        FROM inventory_checkpoints c
        LEFT JOIN scans s ON s.scan_id=c.scan_id
        AND c.root=s.root COLLATE {_PATH_COLLATION}
        LEFT JOIN duplicate_plan_summaries p ON p.scan_id=c.scan_id
        WHERE c.valid=1
        ORDER BY c.root COLLATE NOCASE LIMIT ?""",
    )
    if any(row["matched_scan_id"] is None for row in rows):
        raise RuntimeError("valid inventory checkpoint points to a missing or root-mismatched scan")
    if any(str(row["scan_status"]) != "complete" for row in rows):
        raise RuntimeError("valid inventory checkpoint points to a non-complete scan")
    heads = tuple(
        PublicationHead(
            scope=str(row["root"]),
            publication_id=f"inventory-scan:{int(row['scan_id'])}",
            generation=int(row["scan_id"]),
            model_signature=_inventory_duplicate_plan_signal(row),
        )
        for row in rows
    )
    watermarks = (
        LogicalWatermark("published_roots", str(len(rows))),
        LogicalWatermark(
            "latest_checkpoint_updated_ns",
            str(max((int(row["updated_ns"]) for row in rows), default=0)),
        ),
    )
    return _LogicalObservation(heads, watermarks, ())


def _catalog_observation(connection: sqlite3.Connection) -> _LogicalObservation:
    rows = _limited_rows(
        connection,
        """SELECT p.source_kind,p.generation_id,p.published_ns,g.status,
        g.generation_id AS matched_generation_id,
        g.source_kind AS generation_source_kind
        FROM catalog_publications p
        LEFT JOIN catalog_generations g ON g.generation_id=p.generation_id
        ORDER BY p.source_kind LIMIT ?""",
    )
    if any(row["matched_generation_id"] is None for row in rows):
        raise RuntimeError("catalog publication points to a missing generation")
    if any(str(row["source_kind"]) != str(row["generation_source_kind"]) for row in rows):
        raise RuntimeError("catalog publication source kind mismatches its generation")
    if any(str(row["status"]) != "published" for row in rows):
        raise RuntimeError("catalog publication points to a non-published generation")
    heads = tuple(
        PublicationHead(
            scope=str(row["source_kind"]),
            publication_id=f"catalog:{int(row['generation_id'])}",
            generation=int(row["generation_id"]),
        )
        for row in rows
    )
    return _LogicalObservation(
        heads,
        (LogicalWatermark("published_source_kinds", str(len(rows))),),
        (),
    )


def _semantic_observation(connection: sqlite3.Connection) -> _LogicalObservation:
    rows = _limited_rows(
        connection,
        """SELECT h.model_signature,h.generation_id,h.published_ns,
        g.generation_id AS matched_generation_id,
        g.status,g.processing_signature,
        g.model_signature AS generation_model_signature,
        m.model_signature AS matched_model_signature,
        m.vector_space,m.modality,m.dimensions
        FROM published_embedding_heads h
        LEFT JOIN embedding_generations g ON g.generation_id=h.generation_id
        LEFT JOIN embedding_models m ON m.model_signature=h.model_signature
        ORDER BY h.model_signature LIMIT ?""",
    )
    if any(row["matched_generation_id"] is None for row in rows):
        raise RuntimeError("semantic head points to a missing generation")
    if any(row["matched_model_signature"] is None for row in rows):
        raise RuntimeError("semantic head points to a missing model")
    if any(str(row["model_signature"]) != str(row["generation_model_signature"]) for row in rows):
        raise RuntimeError("semantic head model signature mismatches its generation")
    if any(str(row["status"]) != "ready" for row in rows):
        raise RuntimeError("semantic head points to a non-ready generation")
    heads = tuple(
        PublicationHead(
            scope=f"model:{row['model_signature']}",
            publication_id=f"semantic:{int(row['generation_id'])}",
            generation=int(row["generation_id"]),
            model_signature=str(row["model_signature"]),
        )
        for row in rows
    )
    models = tuple(
        ActiveModel(
            signature=str(row["model_signature"]),
            vector_space=str(row["vector_space"]),
            modality=str(row["modality"]),
            dimensions=int(row["dimensions"]),
            generation=int(row["generation_id"]),
        )
        for row in rows
    )
    watermarks = (
        LogicalWatermark("published_models", str(len(rows))),
        LogicalWatermark(
            "processing_signatures",
            "|".join(f"{row['model_signature']}={row['processing_signature']}" for row in rows)
            or "none",
        ),
    )
    return _LogicalObservation(heads, watermarks, models)


def _aggregate_observation(
    connection: sqlite3.Connection,
    *,
    table: str,
    updated_column: str,
    run_column: str,
) -> _LogicalObservation:
    allowed = {
        ("documents", "updated_ns", "last_seen_run_id"),
        ("images", "updated_ns", "last_seen_run_id"),
    }
    if (table, updated_column, run_column) not in allowed:
        raise AssertionError("unregistered Knowledge aggregate watermark")
    row = connection.execute(
        f"""SELECT COUNT(*) AS row_count,
        COALESCE(MAX({updated_column}),0) AS updated_ns,
        COALESCE(MAX({run_column}),0) AS last_run FROM {table}"""
    ).fetchone()
    return _LogicalObservation(
        (),
        (
            LogicalWatermark("current_rows", str(int(row["row_count"]))),
            LogicalWatermark("latest_updated_ns", str(int(row["updated_ns"]))),
            LogicalWatermark("latest_owner_run", str(int(row["last_run"]))),
            LogicalWatermark("visibility", "best_effort_non_generational"),
        ),
        (),
    )


def _framework_observation(connection: sqlite3.Connection) -> _LogicalObservation:
    row = connection.execute(
        """SELECT
        (SELECT COALESCE(MAX(run_id),0) FROM initial_runs) AS run_id,
        (SELECT COALESCE(MAX(event_id),0) FROM run_events) AS event_id,
        (SELECT COALESCE(MAX(action_id),0) FROM file_actions) AS action_id"""
    ).fetchone()
    watermarks = [
        LogicalWatermark("latest_run_id", str(int(row["run_id"]))),
        LogicalWatermark("latest_event_id", str(int(row["event_id"]))),
        LogicalWatermark("latest_action_id", str(int(row["action_id"]))),
    ]
    review_tasks_available = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='review_task_batches'"""
    ).fetchone()
    review_heads: tuple[PublicationHead, ...] = ()
    if review_tasks_available is not None:
        from .review_task_repository import (
            _audit_latest_review_task_source_publications_from_prevalidated_connection,
        )

        publications = _audit_latest_review_task_source_publications_from_prevalidated_connection(
            connection,
            limit=MAX_SNAPSHOT_HEADS,
        ).publications
        review_heads = tuple(
            PublicationHead(
                scope=f"review-task-source:{publication.publication_id}",
                publication_id=publication.publication_id,
                generation=publication.revision,
                model_signature=publication.fence.source_snapshot_fingerprint,
            )
            for publication in publications
        )
        review = connection.execute(
            """SELECT
            (SELECT COUNT(*) FROM review_task_batches) AS batch_count,
            (SELECT COALESCE(MAX(confirmed_ns),0)
             FROM review_task_batches) AS batch_confirmed_ns,
            (SELECT COUNT(*) FROM review_task_events) AS event_count,
            (SELECT COALESCE(MAX(recorded_ns),0)
             FROM review_task_events) AS event_recorded_ns,
            (SELECT COUNT(*) FROM review_task_source_publications)
             AS source_publication_count,
            (SELECT COALESCE(MAX(confirmed_ns),0)
             FROM review_task_source_publications)
             AS source_publication_confirmed_ns"""
        ).fetchone()
        watermarks.extend(
            (
                LogicalWatermark(
                    "review_task_batches",
                    f"{int(review['batch_count'])}:{int(review['batch_confirmed_ns'])}",
                ),
                LogicalWatermark(
                    "review_task_events",
                    f"{int(review['event_count'])}:{int(review['event_recorded_ns'])}",
                ),
                LogicalWatermark(
                    "review_task_source_publications",
                    f"{int(review['source_publication_count'])}:"
                    f"{int(review['source_publication_confirmed_ns'])}",
                ),
            )
        )
    watermarks.append(LogicalWatermark("visibility", "best_effort_non_generational"))
    return _LogicalObservation(review_heads, tuple(watermarks), ())


def _code_observation(connection: sqlite3.Connection) -> _LogicalObservation:
    row = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM files WHERE status='current') AS current_files,
        (SELECT COALESCE(MAX(version_id),0) FROM file_versions) AS version_id,
        (SELECT COALESCE(MAX(analysis_run_id),0) FROM analysis_runs) AS run_id"""
    ).fetchone()
    return _LogicalObservation(
        (),
        (
            LogicalWatermark("current_files", str(int(row["current_files"]))),
            LogicalWatermark("latest_version_id", str(int(row["version_id"]))),
            LogicalWatermark("latest_analysis_run_id", str(int(row["run_id"]))),
            LogicalWatermark("visibility", "best_effort_non_generational"),
        ),
        (),
    )


def _logical_observation(
    connection: sqlite3.Connection,
    spec: _OwnerSpec,
) -> _LogicalObservation:
    if spec.read_kind == "inventory":
        return _inventory_observation(connection)
    if spec.read_kind == "catalog":
        return _catalog_observation(connection)
    if spec.read_kind == "semantic":
        return _semantic_observation(connection)
    if spec.read_kind == "framework":
        return _framework_observation(connection)
    if spec.read_kind == "code":
        return _code_observation(connection)
    if spec.read_kind == "images":
        return _aggregate_observation(
            connection,
            table="images",
            updated_column="updated_ns",
            run_column="last_seen_run_id",
        )
    if spec.read_kind == "documents":
        return _aggregate_observation(
            connection,
            table="documents",
            updated_column="updated_ns",
            run_column="last_seen_run_id",
        )
    raise AssertionError(f"unknown Knowledge owner observation: {spec.read_kind}")


# endregion [03]


# region [04] Double observation and bounded global retry


def _owner_path(paths: KnowledgeStatePaths, owner: str) -> Path:
    path = getattr(paths, owner)
    if path is None:
        raise AssertionError(f"Knowledge owner {owner} has no configured path")
    return path


def _capture_available_owner(
    path: Path,
    spec: _OwnerSpec,
    *,
    attempt: int,
    between_observations: Callable[[str, int], None] | None,
    cancellation: _CancellationController,
    immutable: bool,
) -> tuple[OwnerSnapshot, tuple[ActiveModel, ...]]:
    immutable_fence: SQLiteImmutableFence | None = None
    if immutable:
        immutable_fence = capture_sqlite_immutable_fence(path)
        if immutable_fence != capture_sqlite_immutable_fence(path):
            raise ImmutableSQLiteUnavailable("SQLite owner changed before immutable read")
    connection = _connect_readonly(path, immutable=immutable)
    sqlite_cancellation = SQLiteCancellationBridge(
        cancellation.checkpoint if cancellation.callback is not None else None
    )
    try:
        cancellation.checkpoint()
        data_version_before = int(connection.execute("PRAGMA data_version").fetchone()[0])
        connection.execute("BEGIN")
        try:
            cancellation.checkpoint()
            with sqlite_cancellation_scope(connection, sqlite_cancellation):
                observed_version = _observed_schema_version(connection, spec)
                if observed_version is None:
                    connection.execute("ROLLBACK")
                    return (
                        OwnerSnapshot(
                            owner=spec.owner,
                            state=OwnerAvailability.INCOMPATIBLE,
                            expected_schema_version=spec.expected_schema,
                            error_code="schema_version_absent",
                            data_version_before=data_version_before,
                            data_version_after=data_version_before,
                        ),
                        (),
                    )
                if observed_version > spec.expected_schema:
                    connection.execute("ROLLBACK")
                    return (
                        OwnerSnapshot(
                            owner=spec.owner,
                            state=OwnerAvailability.FUTURE,
                            expected_schema_version=spec.expected_schema,
                            observed_schema_version=observed_version,
                            error_code="future_schema",
                            data_version_before=data_version_before,
                            data_version_after=data_version_before,
                        ),
                        (),
                    )
                validator = spec.validator_for(observed_version)
                if validator is None:
                    connection.execute("ROLLBACK")
                    return (
                        OwnerSnapshot(
                            owner=spec.owner,
                            state=OwnerAvailability.INCOMPATIBLE,
                            expected_schema_version=spec.expected_schema,
                            observed_schema_version=observed_version,
                            error_code="legacy_schema",
                            data_version_before=data_version_before,
                            data_version_after=data_version_before,
                        ),
                        (),
                    )
                validator(connection)
                before = _logical_observation(connection, spec)
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

        cancellation.checkpoint()
        if between_observations is not None:
            between_observations(spec.owner, attempt)

        connection.execute("BEGIN")
        try:
            cancellation.checkpoint()
            with sqlite_cancellation_scope(connection, sqlite_cancellation):
                after_version = _observed_schema_version(connection, spec)
                if after_version != observed_version:
                    after = before
                else:
                    validator(connection)
                    after = _logical_observation(connection, spec)
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        cancellation.checkpoint()
        data_version_after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        logical_changed = after_version != observed_version or after != before
        if logical_changed and data_version_after == data_version_before:
            data_version_after = data_version_before + 1
        warning_parts: list[str] = []
        if observed_version < spec.expected_schema:
            warning_parts.append(
                f"legacy_schema_read_compatible:{observed_version}->{spec.expected_schema}"
            )
        if logical_changed:
            warning_parts.append("logical_watermark_changed")
        return (
            OwnerSnapshot(
                owner=spec.owner,
                state=OwnerAvailability.AVAILABLE,
                expected_schema_version=spec.expected_schema,
                observed_schema_version=observed_version,
                publications=after.publications,
                watermarks=after.watermarks,
                data_version_before=data_version_before,
                data_version_after=data_version_after,
                warning=";".join(warning_parts) or None,
            ),
            after.active_models,
        )
    finally:
        connection.close()
        if immutable_fence is not None:
            if immutable_fence != capture_sqlite_immutable_fence(path):
                raise ImmutableSQLiteUnavailable("SQLite owner changed during immutable read")


def _owner_state_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        detail = f"{path}: {exc.strerror or str(exc)}"
        raise KnowledgeStateRootError(
            path.parent,
            "contains an inaccessible owner state path",
            detail,
        ) from exc

    try:
        metadata = os.stat(path)
    except OSError as exc:
        detail = f"{path}: {exc.strerror or str(exc)}"
        raise KnowledgeStateRootError(
            path.parent,
            "contains an inaccessible owner state path",
            detail,
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise KnowledgeStateRootError(
            path.parent,
            "contains a non-file owner state path",
            str(path),
        )
    return True


def _capture_owner(
    path: Path,
    spec: _OwnerSpec,
    *,
    attempt: int,
    between_observations: Callable[[str, int], None] | None,
    cancellation: _CancellationController,
    immutable: bool,
) -> tuple[OwnerSnapshot, tuple[ActiveModel, ...]]:
    if not _owner_state_exists(path):
        return (
            OwnerSnapshot(
                owner=spec.owner,
                state=OwnerAvailability.ABSENT,
                expected_schema_version=spec.expected_schema,
            ),
            (),
        )
    try:
        return _capture_available_owner(
            path,
            spec,
            attempt=attempt,
            between_observations=between_observations,
            cancellation=cancellation,
            immutable=immutable,
        )
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        if cancellation.raised_here(exc):
            raise
        state = (
            OwnerAvailability.CORRUPT if _is_corrupt_error(exc) else OwnerAvailability.INCOMPATIBLE
        )
        return (
            OwnerSnapshot(
                owner=spec.owner,
                state=state,
                expected_schema_version=spec.expected_schema,
                error_code=_sqlite_error_code(exc),
                warning=str(exc)[:512] or type(exc).__name__,
            ),
            (),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _capture_vector(
    paths: KnowledgeStatePaths,
    *,
    attempt: int,
    between_observations: Callable[[str, int], None] | None,
    cancellation: _CancellationController,
    immutable: bool,
) -> tuple[tuple[OwnerSnapshot, ...], tuple[ActiveModel, ...]]:
    owners: list[OwnerSnapshot] = []
    models: list[ActiveModel] = []
    for spec in _owner_specs(paths):
        cancellation.checkpoint()
        owner, active_models = _capture_owner(
            _owner_path(paths, spec.owner),
            spec,
            attempt=attempt,
            between_observations=between_observations,
            cancellation=cancellation,
            immutable=immutable,
        )
        owners.append(owner)
        models.extend(active_models)
        cancellation.checkpoint()
    return tuple(owners), tuple(models)


def _logical_vector_signature(
    owners: tuple[OwnerSnapshot, ...],
    models: tuple[ActiveModel, ...],
) -> str:
    return canonical_json(
        {
            "owners": [
                owner.identity_dict() for owner in sorted(owners, key=lambda item: item.owner)
            ],
            "active_models": [
                model.to_dict() for model in sorted(models, key=lambda item: item.signature)
            ],
        }
    )


def _changed_vector_owners(
    first_owners: tuple[OwnerSnapshot, ...],
    second_owners: tuple[OwnerSnapshot, ...],
    first_models: tuple[ActiveModel, ...],
    second_models: tuple[ActiveModel, ...],
) -> frozenset[str]:
    first_identities = {owner.owner: owner.identity_dict() for owner in first_owners}
    second_identities = {owner.owner: owner.identity_dict() for owner in second_owners}
    changed = {
        owner
        for owner in first_identities.keys() | second_identities.keys()
        if first_identities.get(owner) != second_identities.get(owner)
    }
    first_model_signature = canonical_json(
        {
            "active_models": [
                model.to_dict() for model in sorted(first_models, key=lambda item: item.signature)
            ]
        }
    )
    second_model_signature = canonical_json(
        {
            "active_models": [
                model.to_dict() for model in sorted(second_models, key=lambda item: item.signature)
            ]
        }
    )
    if first_model_signature != second_model_signature:
        changed.add("semantic")
    changed.update(owner.owner for owner in (*first_owners, *second_owners) if owner.changed)
    return frozenset(changed)


def _carry_change_evidence(
    first_owners: tuple[OwnerSnapshot, ...],
    second_owners: tuple[OwnerSnapshot, ...],
    changed_owners: frozenset[str],
) -> tuple[OwnerSnapshot, ...]:
    first_by_owner = {owner.owner: owner for owner in first_owners}
    result: list[OwnerSnapshot] = []
    for owner in second_owners:
        if owner.owner not in changed_owners or owner.changed:
            result.append(owner)
            continue
        first = first_by_owner.get(owner.owner)
        if first is not None and first.changed:
            before = first.data_version_before
            after = first.data_version_after
            assert before is not None and after is not None
            warning = first.warning or "owner_changed_in_first_vector"
        else:
            before = owner.data_version_before
            if before is None:
                before = 0
            after = owner.data_version_after
            if after is None or after == before:
                after = before + 1
            warning = owner.warning or "logical_vector_changed"
        result.append(
            replace(
                owner,
                data_version_before=before,
                data_version_after=after,
                warning=warning,
            )
        )
    return tuple(result)


def collect_knowledge_snapshot(
    paths: KnowledgeStatePaths,
    *,
    source_version: str,
    cancellation_check: CancellationCheck | None = None,
    _between_observations: Callable[[str, int], None] | None = None,
    _immutable_owners: bool = False,
) -> KnowledgeSnapshot:
    """Capture all registered owners, retrying the global view exactly once."""

    cancellation = _CancellationController(cancellation_check)
    cancellation.checkpoint()
    for attempt in (1, 2):
        cancellation.checkpoint()
        roots_before = paths.validate_roots()
        cancellation.checkpoint()
        first_owners, first_models = _capture_vector(
            paths,
            attempt=attempt,
            between_observations=_between_observations,
            cancellation=cancellation,
            immutable=_immutable_owners,
        )
        cancellation.checkpoint()
        roots_between = paths.validate_roots()
        _require_stable_root_presence(roots_before, roots_between)
        cancellation.checkpoint()
        second_owners, second_models = _capture_vector(
            paths,
            attempt=attempt,
            between_observations=None,
            cancellation=cancellation,
            immutable=_immutable_owners,
        )
        cancellation.checkpoint()
        roots_after = paths.validate_roots()
        _require_stable_root_presence(roots_between, roots_after)
        cancellation.checkpoint()
        logical_changed = _logical_vector_signature(
            first_owners, first_models
        ) != _logical_vector_signature(second_owners, second_models)
        changed_owners = _changed_vector_owners(
            first_owners,
            second_owners,
            first_models,
            second_models,
        )
        changed = logical_changed or bool(changed_owners)
        if not changed or attempt == 2:
            consistency = (
                SnapshotConsistency.SNAPSHOT_CHANGED if changed else SnapshotConsistency.STABLE
            )
            warnings = (
                ("one or more owners changed during the bounded second capture",) if changed else ()
            )
            owners = (
                _carry_change_evidence(
                    first_owners,
                    second_owners,
                    changed_owners,
                )
                if changed
                else second_owners
            )
            snapshot = KnowledgeSnapshot.create(
                source_version=source_version,
                captured_at_utc=_utc_now(),
                captured_monotonic_ns=time.monotonic_ns(),
                owners=owners,
                active_models=second_models,
                consistency=consistency,
                attempts=attempt,
                warnings=warnings,
            )
            cancellation.checkpoint()
            return snapshot
        cancellation.checkpoint()
    raise AssertionError("bounded Knowledge snapshot loop did not return")


# endregion [04]


__all__ = (
    "MAX_SNAPSHOT_HEADS",
    "KnowledgeStatePaths",
    "KnowledgeStateRootError",
    "collect_knowledge_snapshot",
)
