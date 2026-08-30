"""Read-only durable-owner validation and snapshot projection.
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_plan_owners.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


This internal module owns bounded SQLite retry, schema/data-version fences,
shared physical text-owner snapshots, and read-only image/dedup attachment.
Projection callbacks are explicit so the planner facade retains dynamic test
and integration seams without importing the facade back into this leaf.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

import xxhash

from .semantic_chunking import TextChunkingConfig
from .semantic_models import (
    EmbeddingModelSpec,
    canonical_json,
    fingerprint_chunks,
    fingerprint_text,
)
from .semantic_plan_errors import (
    SemanticPlanBlocked,
    cleanup_preserving_primary as _cleanup_preserving_primary,
)
from .semantic_plan_results import (
    _PlanConfiguration,
    _WorkloadSpec,
    _model_contract_payload,
)
from .semantic_plan_scratch import _ContentAccumulator
from .semantic_schema import (
    SEMANTIC_SCHEMA_VERSION,
    _read_schema_version,
    _validate_version_contract,
)
from .semantic_service_contracts import SemanticSourcePlan
from .semantic_sources import (
    IMAGE_SOURCE_KIND,
    semantic_source_database,
)
from neocortex.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from neocortex.persistence.sqlite_paths import readonly_sqlite_uri
from neocortex.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)
# endregion [01]

# region [02] Implementación


# These retry bounds are part of the planner's deterministic lock policy.
PLANNER_BUSY_TIMEOUT_MS = 25
PLANNER_BUSY_RETRY_ATTEMPTS = 8
PLANNER_BUSY_RETRY_DELAY_SECONDS = 0.025


_RetryResult = TypeVar("_RetryResult")


def _is_sqlite_busy(exc: BaseException) -> bool:
    pending: list[BaseException] = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, sqlite3.OperationalError):
            code = getattr(current, "sqlite_errorcode", None)
            primary_code = None if code is None else int(code) & 0xFF
            if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
                marker in str(current).lower()
                for marker in ("database is locked", "database table is locked")
            ):
                return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _retry_busy(
    label: str,
    bridge: SQLiteCancellationBridge,
    operation: Callable[[], _RetryResult],
) -> _RetryResult:
    for attempt in range(1, PLANNER_BUSY_RETRY_ATTEMPTS + 1):
        bridge.checkpoint()
        try:
            return operation()
        except (sqlite3.OperationalError, SemanticPlanBlocked) as exc:
            if not _is_sqlite_busy(exc):
                raise
            if attempt == PLANNER_BUSY_RETRY_ATTEMPTS:
                raise SemanticPlanBlocked(
                    f"{label} remained busy after {attempt} bounded attempts"
                ) from exc
            bridge.checkpoint()
            time.sleep(PLANNER_BUSY_RETRY_DELAY_SECONDS)
            bridge.checkpoint()
    raise AssertionError("bounded SQLite retry loop did not terminate")


@contextmanager
def _planner_readonly_database(
    path: Path,
    bridge: SQLiteCancellationBridge,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        readonly_sqlite_uri(path),
        uri=True,
        timeout=PLANNER_BUSY_TIMEOUT_MS / 1000.0,
    )
    primary_error: BaseException | None = None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={PLANNER_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise SemanticPlanBlocked("planner reader could not enable foreign keys")
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise SemanticPlanBlocked("planner reader is not query-only")
        bridge.checkpoint()
        yield connection
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            connection.close()
        else:
            _cleanup_preserving_primary(
                connection.close,
                primary_error,
                label="semantic planner read-only owner close cleanup",
            )


@dataclass(frozen=True, slots=True)
class _AttachedDedupSnapshot:
    initial_data_version: int | None
    schema_snapshot_xxh3_128: str | None


def _schema_snapshot_xxh3_128(
    connection: sqlite3.Connection,
    *,
    schema: str = "main",
) -> str:
    if schema not in {"main", "dedup", "scratch"}:
        raise ValueError("unsupported SQLite schema name")
    rows = connection.execute(
        f"""SELECT type,name,tbl_name,COALESCE(sql,'')
        FROM {schema}.sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type,name,tbl_name"""
    )
    return fingerprint_chunks(
        (canonical_json({"row": list(row)}).encode("utf-8") + b"\n") for row in rows
    ).xxh3_128


def _data_version(connection: sqlite3.Connection, *, schema: str = "main") -> int:
    if schema not in {"main", "dedup"}:
        raise ValueError("unsupported SQLite data-version schema")
    return int(connection.execute(f"PRAGMA {schema}.data_version").fetchone()[0])


def _require_current_schema(
    connection: sqlite3.Connection,
    *,
    label: str,
    expected_version: int,
    validator: Callable[[sqlite3.Connection], None],
) -> int:
    try:
        version = read_application_schema_version(connection, label=label)
        if version != expected_version:
            raise SemanticPlanBlocked(f"{label} schema is {version!r}; expected {expected_version}")
        validator(connection)
    except SemanticPlanBlocked:
        raise
    except (RuntimeError, sqlite3.Error) as exc:
        raise SemanticPlanBlocked(
            f"{label} schema validation failed: {type(exc).__name__}: {exc}"
        ) from exc
    return expected_version


def _validate_pdf_source(connection: sqlite3.Connection) -> None:
    from neocortex.capabilities.formats.pdf import pdf_schema

    pdf_schema.validate_pdf_metadata(connection)
    pdf_schema.validate_pdf_schema(connection)


def _validate_docx_source(connection: sqlite3.Connection) -> None:
    from neocortex.capabilities.formats.docx import schema as docx_schema

    docx_schema.validate_docx_metadata(connection)
    docx_schema.validate_docx_schema(connection)


def _validate_source_schema(
    connection: sqlite3.Connection,
    source_kind: str,
) -> int:
    if source_kind == "pdf":
        from neocortex.capabilities.formats.pdf import pdf_schema

        return _require_current_schema(
            connection,
            label="pdf",
            expected_version=pdf_schema.PDF_SCHEMA_VERSION,
            validator=_validate_pdf_source,
        )
    if source_kind == "docx":
        from neocortex.capabilities.formats.docx import schema as docx_schema

        return _require_current_schema(
            connection,
            label="docx",
            expected_version=docx_schema.DOCX_SCHEMA_VERSION,
            validator=_validate_docx_source,
        )
    if source_kind in {"xlsx", "pptx", "odt"}:
        from neocortex.capabilities.formats.office import state as office_state

        return _require_current_schema(
            connection,
            label="office",
            expected_version=office_state.OFFICE_SCHEMA_VERSION,
            validator=lambda connection: validate_sqlite_schema_contract(
                connection,
                office_state._office_schema_contract(),
                label="office semantic source",
                exact=True,
            ),
        )
    if source_kind == "audio":
        from neocortex.capabilities.formats.audio import state as audio_state

        return _require_current_schema(
            connection,
            label="audio",
            expected_version=audio_state.AUDIO_SCHEMA_VERSION,
            validator=lambda connection: validate_sqlite_schema_contract(
                connection,
                audio_state._audio_schema_contract(),
                label="audio semantic source",
                exact=True,
            ),
        )
    if source_kind == "archive":
        from neocortex.capabilities.formats.archive import state as archive_state

        return _require_current_schema(
            connection,
            label="archive",
            expected_version=archive_state.ARCHIVE_SCHEMA_VERSION,
            validator=lambda connection: validate_sqlite_schema_contract(
                connection,
                archive_state.archive_schema_contract(),
                label="archive semantic source",
                exact=True,
            ),
        )
    if source_kind == "text":
        from neocortex.capabilities.formats.text import text_state

        return _require_current_schema(
            connection,
            label="text",
            expected_version=text_state.TEXT_SCHEMA_VERSION,
            validator=lambda connection: validate_sqlite_schema_contract(
                connection,
                text_state.text_schema_contract(),
                label="text semantic source",
                exact=True,
            ),
        )
    if source_kind == "code":
        from neocortex.code import code_schema

        def validate_code(connection: sqlite3.Connection) -> None:
            if code_schema._read_version(connection) != code_schema.CODE_SCHEMA_VERSION:
                raise SemanticPlanBlocked("code schema is not current")
            code_schema.validate_code_schema(connection)
            code_schema._validate_migration_history(connection)

        return _require_current_schema(
            connection,
            label="code",
            expected_version=code_schema.CODE_SCHEMA_VERSION,
            validator=validate_code,
        )
    if source_kind == IMAGE_SOURCE_KIND:
        from neocortex.capabilities.formats.image import state as image_state

        return _require_current_schema(
            connection,
            label="image",
            expected_version=image_state.SCHEMA_VERSION,
            validator=image_state._validate_current_image_schema,
        )
    raise ValueError(f"unsupported semantic source: {source_kind}")


def _validate_dedup_schema(connection: sqlite3.Connection) -> int:
    from neocortex.deduplication import schema as inventory_schema

    return _require_current_schema(
        connection,
        label="dedup inventory",
        expected_version=inventory_schema.SCHEMA_VERSION,
        validator=inventory_schema.validate_inventory_schema,
    )


def _validate_semantic_cache(
    connection: sqlite3.Connection,
    models: Iterable[EmbeddingModelSpec],
) -> int:
    try:
        version = _read_schema_version(connection)
        if version != SEMANTIC_SCHEMA_VERSION:
            raise SemanticPlanBlocked(
                f"semantic schema is {version!r}; expected {SEMANTIC_SCHEMA_VERSION}"
            )
        _validate_version_contract(connection, version)
        from .semantic_repository_common import _model_from_row

        for model in models:
            row = connection.execute(
                "SELECT * FROM embedding_models WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()
            if row is None:
                continue
            try:
                persisted = _model_from_row(row)
            except (TypeError, ValueError, RuntimeError) as exc:
                raise SemanticPlanBlocked(
                    f"semantic model metadata is malformed: {model.model_signature}: {exc}"
                ) from exc
            if persisted != model:
                raise SemanticPlanBlocked(
                    "semantic model signature is bound to an incompatible full "
                    f"contract: {model.model_signature}"
                )
            space = connection.execute(
                """SELECT dimensions,distance,normalization FROM vector_spaces
                WHERE vector_space=?""",
                (model.vector_space,),
            ).fetchone()
            expected_space = (model.dimensions, model.distance, model.normalization)
            if (
                space is None
                or (
                    int(space["dimensions"]),
                    str(space["distance"]),
                    str(space["normalization"]),
                )
                != expected_space
            ):
                raise SemanticPlanBlocked(
                    f"semantic vector-space contract is incompatible: {model.vector_space}"
                )
    except SemanticPlanBlocked:
        raise
    except (RuntimeError, sqlite3.Error) as exc:
        raise SemanticPlanBlocked(
            f"semantic cache validation failed: {type(exc).__name__}: {exc}"
        ) from exc
    return version


def _plan_text_database_group(
    state_directory: Path,
    database: Path,
    source_kinds: Sequence[str],
    *,
    chunking: TextChunkingConfig,
    workload: _WorkloadSpec,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
    validate_source_schema: Callable[..., int],
    plan_text_source: Callable[..., SemanticSourcePlan],
) -> Mapping[str, SemanticSourcePlan]:
    if not source_kinds:
        raise ValueError("physical text-source group cannot be empty")
    if not database.is_file():
        raise SemanticPlanBlocked(
            f"semantic source state is missing: {','.join(source_kinds)}={database}"
        )
    try:
        with _planner_readonly_database(database, bridge) as owner:
            with sqlite_cancellation_scope(owner, bridge):
                first_kind = source_kinds[0]

                def validate_snapshot() -> tuple[int, int, str]:
                    if owner.in_transaction:
                        owner.rollback()
                    try:
                        owner.execute("BEGIN")
                        initial_data_version = _data_version(owner)
                        schema_version = validate_source_schema(owner, first_kind)
                        schema_snapshot = _schema_snapshot_xxh3_128(owner)
                        return initial_data_version, schema_version, schema_snapshot
                    except BaseException as exc:
                        if owner.in_transaction:
                            _cleanup_preserving_primary(
                                owner.rollback,
                                exc,
                                label=("semantic planner text snapshot rollback cleanup"),
                            )
                        raise

                initial_data_version, schema_version, schema_snapshot = _retry_busy(
                    f"{','.join(source_kinds)} owner",
                    bridge,
                    validate_snapshot,
                )
                view_plans = tuple(
                    plan_text_source(
                        state_directory,
                        source_kind,
                        connection=owner,
                        schema_version=schema_version,
                        schema_snapshot_xxh3_128=schema_snapshot,
                        chunking=chunking,
                        workload=workload,
                        accumulator=accumulator,
                        checkpoint=bridge.checkpoint,
                    )
                    for source_kind in source_kinds
                )
                bridge.checkpoint()
                owner.rollback()
                if _data_version(owner) != initial_data_version:
                    raise SemanticPlanBlocked(
                        f"{','.join(source_kinds)} owner changed during planning"
                    )
                database_snapshot = fingerprint_text(
                    canonical_json(
                        {
                            "schema": schema_snapshot,
                            "views": [
                                {
                                    "source_kind": plan.source_kind,
                                    "view_snapshot_xxh3_128": (plan.snapshot_xxh3_128),
                                }
                                for plan in view_plans
                            ],
                        }
                    )
                ).xxh3_128
                return {
                    plan.source_kind: replace(
                        plan,
                        snapshot_xxh3_128=database_snapshot,
                    )
                    for plan in view_plans
                }
    except SemanticPlanBlocked:
        raise
    except (RuntimeError, sqlite3.Error) as exc:
        raise SemanticPlanBlocked(
            f"{','.join(source_kinds)} owner projection failed: {type(exc).__name__}: {exc}"
        ) from exc


def _validated_dedup_schema(
    path: Path,
    bridge: SQLiteCancellationBridge,
    *,
    validate_dedup_schema: Callable[..., int],
) -> tuple[int, str] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise SemanticPlanBlocked(f"dedup state is not a regular file: {path}")
    try:
        with _planner_readonly_database(path, bridge) as connection:
            with sqlite_cancellation_scope(connection, bridge):

                def validate_snapshot() -> tuple[int, str]:
                    if connection.in_transaction:
                        connection.rollback()
                    try:
                        connection.execute("BEGIN")
                        initial_data_version = _data_version(connection)
                        version = validate_dedup_schema(connection)
                        schema_snapshot = _schema_snapshot_xxh3_128(connection)
                        bridge.checkpoint()
                        connection.rollback()
                        if _data_version(connection) != initial_data_version:
                            raise SemanticPlanBlocked(
                                "dedup inventory changed during planner validation"
                            )
                        return version, schema_snapshot
                    except BaseException as exc:
                        if connection.in_transaction:
                            _cleanup_preserving_primary(
                                connection.rollback,
                                exc,
                                label=("semantic planner dedup snapshot rollback cleanup"),
                            )
                        raise

                return _retry_busy(
                    "dedup inventory",
                    bridge,
                    validate_snapshot,
                )
    except SemanticPlanBlocked:
        raise
    except (RuntimeError, sqlite3.Error) as exc:
        raise SemanticPlanBlocked(
            f"dedup inventory validation failed: {type(exc).__name__}: {exc}"
        ) from exc


def _semantic_reuse_matches(
    connection: sqlite3.Connection,
    batch: Sequence[tuple[object, ...]],
) -> tuple[tuple[object, ...], ...]:
    if not batch:
        return ()
    placeholders = ",".join("(?,?,?,?,?,?,?,?)" for _row in batch)
    parameters = tuple(value for row in batch for value in row)
    rows = connection.execute(
        f"""WITH planned(
            model_signature,role,modality,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,dimensions,vector_dtype
        ) AS (VALUES {placeholders})
        SELECT model_signature,role,modality,content_xxh3_128,content_bytes,
               content_xxh3_64_guard,dimensions,vector_dtype
        FROM planned
        WHERE EXISTS(
            SELECT 1 FROM vector_payloads AS payload
            WHERE payload.model_signature=planned.model_signature
              AND payload.content_xxh3_128=planned.content_xxh3_128
              AND payload.content_bytes=planned.content_bytes
              AND payload.content_xxh3_64_guard=planned.content_xxh3_64_guard
              AND payload.dimensions=planned.dimensions
              AND payload.vector_dtype=planned.vector_dtype
              AND typeof(payload.vector_blob)='blob'
              AND length(payload.vector_blob)=planned.dimensions *
                  CASE planned.vector_dtype
                      WHEN 'float16' THEN 2
                      WHEN 'float32' THEN 4
                      ELSE 0
                  END)
        ORDER BY model_signature,role,modality,content_xxh3_128,
                 content_bytes,content_xxh3_64_guard""",
        parameters,
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _semantic_reuse_snapshot(
    semantic_path: Path,
    accumulator: _ContentAccumulator,
    specs: Sequence[_WorkloadSpec],
    bridge: SQLiteCancellationBridge,
    *,
    validate_semantic_cache: Callable[..., int],
) -> tuple[int | None, str]:
    if not semantic_path.exists():
        return None, fingerprint_text("semantic-cache-absent-v1").xxh3_128
    if not semantic_path.is_file():
        raise SemanticPlanBlocked(f"semantic state is not a regular file: {semantic_path}")
    try:
        with _planner_readonly_database(semantic_path, bridge) as connection:
            with sqlite_cancellation_scope(connection, bridge):
                unique_models = tuple(
                    {spec.model.model_signature: spec.model for spec in specs}.values()
                )

                def validate_snapshot() -> tuple[int, int, str]:
                    if connection.in_transaction:
                        connection.rollback()
                    try:
                        connection.execute("BEGIN")
                        initial_data_version = _data_version(connection)
                        version = validate_semantic_cache(connection, unique_models)
                        schema_snapshot = _schema_snapshot_xxh3_128(connection)
                        return initial_data_version, version, schema_snapshot
                    except BaseException as exc:
                        if connection.in_transaction:
                            _cleanup_preserving_primary(
                                connection.rollback,
                                exc,
                                label=("semantic planner semantic snapshot rollback cleanup"),
                            )
                        raise

                initial_data_version, version, schema_snapshot = _retry_busy(
                    "semantic cache",
                    bridge,
                    validate_snapshot,
                )
                snapshot_hasher = xxhash.xxh3_128()
                snapshot_hasher.update(
                    canonical_json(
                        {
                            "schema": schema_snapshot,
                            "version": version,
                        }
                    ).encode("utf-8")
                )
                for model in sorted(
                    unique_models,
                    key=lambda value: value.model_signature,
                ):
                    registered = connection.execute(
                        "SELECT 1 FROM embedding_models WHERE model_signature=?",
                        (model.model_signature,),
                    ).fetchone()
                    snapshot_hasher.update(b"\n")
                    snapshot_hasher.update(
                        canonical_json(
                            {
                                "contract": _model_contract_payload(model),
                                "registered": registered is not None,
                            }
                        ).encode("utf-8")
                    )
                for batch in accumulator.reuse_lookup_batches():
                    bridge.checkpoint()
                    reusable = _semantic_reuse_matches(connection, batch)
                    for row in reusable:
                        snapshot_hasher.update(b"\n")
                        snapshot_hasher.update(
                            canonical_json({"reusable": list(row)}).encode("utf-8")
                        )
                    accumulator.mark_preexisting_reuse(reusable)
                bridge.checkpoint()
                connection.rollback()
                if _data_version(connection) != initial_data_version:
                    raise SemanticPlanBlocked(
                        "semantic cache changed during validation and reuse projection"
                    )
                return version, snapshot_hasher.hexdigest()
    except SemanticPlanBlocked:
        raise
    except sqlite3.Error as exc:
        bridge.reraise_if_captured(exc)
        raise SemanticPlanBlocked(
            f"semantic cache projection failed: {type(exc).__name__}: {exc}"
        ) from exc


def _plan_text_sources(
    state_directory: Path,
    *,
    selected_sources: tuple[str, ...],
    chunking: TextChunkingConfig,
    workload: _WorkloadSpec,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
    plan_text_database_group: Callable[..., Mapping[str, SemanticSourcePlan]],
) -> tuple[SemanticSourcePlan, ...]:
    physical_groups: dict[Path, list[str]] = {}
    for source_kind in selected_sources:
        source_path = semantic_source_database(state_directory, source_kind)
        physical_groups.setdefault(source_path, []).append(source_kind)
    plans_by_kind: dict[str, SemanticSourcePlan] = {}
    for source_path, grouped_kinds in physical_groups.items():
        bridge.checkpoint()
        plans_by_kind.update(
            plan_text_database_group(
                state_directory,
                source_path,
                tuple(grouped_kinds),
                chunking=chunking,
                workload=workload,
                accumulator=accumulator,
                bridge=bridge,
            )
        )
    return tuple(plans_by_kind[source] for source in selected_sources)


def _begin_validated_image_snapshot(
    owner: sqlite3.Connection,
    *,
    validate_source_schema: Callable[..., int],
) -> tuple[int, int, str]:
    if owner.in_transaction:
        owner.rollback()
    try:
        owner.execute("BEGIN")
        initial_version = _data_version(owner)
        schema_version = validate_source_schema(owner, IMAGE_SOURCE_KIND)
        schema_snapshot = _schema_snapshot_xxh3_128(owner)
        return initial_version, schema_version, schema_snapshot
    except BaseException as exc:
        if owner.in_transaction:
            _cleanup_preserving_primary(
                owner.rollback,
                exc,
                label="semantic planner image snapshot rollback cleanup",
            )
        raise


@contextmanager
def _attached_validated_dedup(
    owner: sqlite3.Connection,
    *,
    dedup_path: Path,
    dedup_info: tuple[int, str] | None,
) -> Iterator[_AttachedDedupSnapshot]:
    attached = False
    primary_error: BaseException | None = None
    try:
        if dedup_info is None:
            yield _AttachedDedupSnapshot(None, None)
            return
        owner.execute(
            "ATTACH DATABASE ? AS dedup",
            (readonly_sqlite_uri(dedup_path),),
        )
        attached = True
        initial_data_version = _data_version(owner, schema="dedup")
        dedup_version, validated_schema = dedup_info
        attached_version_row = owner.execute(
            "SELECT value FROM dedup.metadata WHERE key='schema_version'"
        ).fetchone()
        attached_version = None if attached_version_row is None else int(attached_version_row[0])
        attached_schema = _schema_snapshot_xxh3_128(owner, schema="dedup")
        if attached_version != dedup_version or attached_schema != validated_schema:
            raise SemanticPlanBlocked(
                "dedup schema changed between exact validation and image projection"
            )
        yield _AttachedDedupSnapshot(initial_data_version, attached_schema)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if attached and not owner.in_transaction:
            if primary_error is None:
                owner.execute("DETACH DATABASE dedup")
            else:
                _cleanup_preserving_primary(
                    lambda: owner.execute("DETACH DATABASE dedup"),
                    primary_error,
                    label="semantic planner dedup detach cleanup",
                )


def _verify_image_owner_versions(
    owner: sqlite3.Connection,
    *,
    initial_image_data_version: int,
    dedup_snapshot: _AttachedDedupSnapshot,
) -> None:
    if _data_version(owner) != initial_image_data_version:
        raise SemanticPlanBlocked("image owner changed during planning")
    if (
        dedup_snapshot.initial_data_version is not None
        and _data_version(owner, schema="dedup") != dedup_snapshot.initial_data_version
    ):
        raise SemanticPlanBlocked("dedup owner changed during image planning")


def _project_open_image_owner(
    state_directory: Path,
    *,
    owner: sqlite3.Connection,
    dedup_path: Path,
    dedup_info: tuple[int, str] | None,
    chunking: TextChunkingConfig | None,
    image_workload: _WorkloadSpec,
    ocr_workload: _WorkloadSpec | None,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
    validate_source_schema: Callable[..., int],
    plan_images: Callable[..., SemanticSourcePlan],
) -> SemanticSourcePlan:
    with sqlite_cancellation_scope(owner, bridge):
        initial_version, image_version, image_schema_snapshot = _retry_busy(
            "image owner",
            bridge,
            lambda: _begin_validated_image_snapshot(
                owner,
                validate_source_schema=validate_source_schema,
            ),
        )
        with _attached_validated_dedup(
            owner,
            dedup_path=dedup_path,
            dedup_info=dedup_info,
        ) as dedup_snapshot:
            image_plan = plan_images(
                state_directory,
                connection=owner,
                schema_version=image_version,
                schema_snapshot_xxh3_128=image_schema_snapshot,
                dedup_schema_snapshot_xxh3_128=(dedup_snapshot.schema_snapshot_xxh3_128),
                chunking=chunking,
                image_workload=image_workload,
                ocr_workload=ocr_workload,
                accumulator=accumulator,
                checkpoint=bridge.checkpoint,
            )
            bridge.checkpoint()
            owner.rollback()
            _verify_image_owner_versions(
                owner,
                initial_image_data_version=initial_version,
                dedup_snapshot=dedup_snapshot,
            )
        return image_plan


def _plan_image_source(
    state_directory: Path,
    *,
    chunking: TextChunkingConfig | None,
    specs: Mapping[str, _WorkloadSpec],
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
    validated_dedup_schema: Callable[..., tuple[int, str] | None],
    validate_source_schema: Callable[..., int],
    plan_images: Callable[..., SemanticSourcePlan],
) -> SemanticSourcePlan:
    image_path = semantic_source_database(state_directory, IMAGE_SOURCE_KIND)
    if not image_path.is_file():
        raise SemanticPlanBlocked(f"image owner state is missing: {image_path}")
    dedup_path = state_directory / "dedup.sqlite3"
    dedup_info = validated_dedup_schema(dedup_path, bridge)
    try:
        with _planner_readonly_database(image_path, bridge) as owner:
            return _project_open_image_owner(
                state_directory,
                owner=owner,
                dedup_path=dedup_path,
                dedup_info=dedup_info,
                chunking=chunking,
                image_workload=specs["image"],
                ocr_workload=specs.get("image_ocr"),
                accumulator=accumulator,
                bridge=bridge,
                validate_source_schema=validate_source_schema,
                plan_images=plan_images,
            )
    except SemanticPlanBlocked:
        raise
    except (RuntimeError, sqlite3.Error) as exc:
        raise SemanticPlanBlocked(
            f"image owner projection failed: {type(exc).__name__}: {exc}"
        ) from exc


def _plan_source_snapshots(
    state_directory: Path,
    *,
    configuration: _PlanConfiguration,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
    plan_text_database_group: Callable[..., Mapping[str, SemanticSourcePlan]],
    validated_dedup_schema: Callable[..., tuple[int, str] | None],
    validate_source_schema: Callable[..., int],
    plan_images: Callable[..., SemanticSourcePlan],
) -> tuple[SemanticSourcePlan, ...]:
    specs = {spec.name: spec for spec in configuration.workload_specs}
    source_plans: list[SemanticSourcePlan] = []
    if configuration.scope in {"text", "all"}:
        assert configuration.active_chunking is not None
        source_plans.extend(
            _plan_text_sources(
                state_directory,
                selected_sources=configuration.selected_sources,
                chunking=configuration.active_chunking,
                workload=specs["text"],
                accumulator=accumulator,
                bridge=bridge,
                plan_text_database_group=plan_text_database_group,
            )
        )
    if configuration.scope in {"image", "all"}:
        source_plans.append(
            _plan_image_source(
                state_directory,
                chunking=configuration.active_chunking,
                specs=specs,
                accumulator=accumulator,
                bridge=bridge,
                validated_dedup_schema=validated_dedup_schema,
                validate_source_schema=validate_source_schema,
                plan_images=plan_images,
            )
        )
    return tuple(source_plans)


# endregion [02]
