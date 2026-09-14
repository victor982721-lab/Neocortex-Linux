"""Read-only Semantic workload planning over validated durable owner caches.

The planner deliberately has no path to model loading, generations, staging or
jobs. Source cardinality spills to a private temporary SQLite database. An
explicit ``scratch_directory`` registers that workspace with the runtime:
successful plans retire it, while failures remain retained for recovery when
the runtime can record that state. Text projections without an externally
bound exact tokenizer contract are explicitly marked pre-tokenizer.
"""

from __future__ import annotations
from contextlib import AbstractContextManager
import sqlite3  # noqa: F401 - exposed for the planner's bounded test seam
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .semantic_chunking import TextChunkingConfig
from . import semantic_sources as _sources  # noqa: F401 - injected source seam
from .semantic_contract_payloads import (
    build_semantic_plan_payload as _build_semantic_plan_payload,
)
from .semantic_models import EmbeddingModelSpec, canonical_json, fingerprint_text
from .semantic_plan_errors import (
    SemanticPlanBlocked as SemanticPlanBlocked,
    SemanticScratchLimitExceeded as SemanticScratchLimitExceeded,
    cleanup_preserving_primary as _cleanup_preserving_primary,
)
from .semantic_plan_owners import (
    _plan_source_snapshots as _owner_plan_source_snapshots,
    _plan_text_database_group as _owner_plan_text_database_group,
    _semantic_reuse_snapshot as _owner_semantic_reuse_snapshot,
    _validate_dedup_schema as _validate_dedup_schema,
    _validate_semantic_cache as _validate_semantic_cache,
    _validate_source_schema as _validate_source_schema,
    _validated_dedup_schema as _owner_validated_dedup_schema,
)
from .semantic_plan_results import (
    _PlanConfiguration as _PlanConfiguration,
    _ScratchPlanResult as _ScratchPlanResult,
    _WorkloadSpec as _WorkloadSpec,
    _cost_calibrations_by_key as _cost_calibrations_by_key,
    _freeze_workload as _freeze_workload,
    _plan_images as _plan_images,
    _plan_text_source as _plan_text_source,
    _prepare_plan_configuration as _prepare_plan_configuration,
    assemble_semantic_plan as _assemble_semantic_plan_result,
    build_plan_signature_payload as _build_plan_signature_payload,
)
from .semantic_plan_scratch import (
    CONTENT_BATCH_SIZE as CONTENT_BATCH_SIZE,
    DEFAULT_MAX_SCRATCH_BYTES as DEFAULT_MAX_SCRATCH_BYTES,
    _ContentAccumulator as _ContentAccumulator,
    _create_scratch_database as _create_scratch_database,
    _registered_scratch_workspace as _registered_scratch_workspace,
    _ScratchBudget as _ScratchBudget,
)
from .semantic_service_contracts import (
    SEMANTIC_DATABASE_NAME,
    SemanticCostCalibration,
    SemanticPlan,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
)
from .semantic_sources import TEXT_SOURCE_KINDS
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

# region [01] Planner constant and dynamic owner seams


PLAN_ALGORITHM_VERSION = "semantic-readonly-plan-v4"


def _plan_text_database_group(
    state_directory: Path,
    database: Path,
    source_kinds: Sequence[str],
    *,
    chunking: TextChunkingConfig,
    workload: _WorkloadSpec,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
) -> Mapping[str, SemanticSourcePlan]:
    return _owner_plan_text_database_group(
        state_directory,
        database,
        source_kinds,
        chunking=chunking,
        workload=workload,
        accumulator=accumulator,
        bridge=bridge,
        validate_source_schema=_validate_source_schema,
        plan_text_source=_plan_text_source,
    )


def _validated_dedup_schema(
    path: Path,
    bridge: SQLiteCancellationBridge,
) -> tuple[int, str] | None:
    return _owner_validated_dedup_schema(
        path,
        bridge,
        validate_dedup_schema=_validate_dedup_schema,
    )


def _semantic_reuse_snapshot(
    semantic_path: Path,
    accumulator: _ContentAccumulator,
    specs: Sequence[_WorkloadSpec],
    bridge: SQLiteCancellationBridge,
) -> tuple[int | None, str]:
    return _owner_semantic_reuse_snapshot(
        semantic_path,
        accumulator,
        specs,
        bridge,
        validate_semantic_cache=_validate_semantic_cache,
    )


def _plan_source_snapshots(
    state_directory: Path,
    *,
    configuration: _PlanConfiguration,
    accumulator: _ContentAccumulator,
    bridge: SQLiteCancellationBridge,
) -> tuple[SemanticSourcePlan, ...]:
    return _owner_plan_source_snapshots(
        state_directory,
        configuration=configuration,
        accumulator=accumulator,
        bridge=bridge,
        plan_text_database_group=_plan_text_database_group,
        validated_dedup_schema=_validated_dedup_schema,
        validate_source_schema=_validate_source_schema,
        plan_images=_plan_images,
    )


# endregion [01]

# region [05] Cost projection and public API


def _plan_payload_for_signature(
    *,
    scope: str,
    selected_sources: tuple[str, ...],
    semantic_schema_version: int | None,
    source_plans: Sequence[SemanticSourcePlan],
    workloads: Sequence[SemanticWorkloadPlan],
    chunking_signature: str | None,
    content_set_xxh3_128: str,
    semantic_snapshot_xxh3_128: str,
) -> Mapping[str, object]:
    return _build_plan_signature_payload(
        algorithm_version=PLAN_ALGORITHM_VERSION,
        scope=scope,
        selected_sources=selected_sources,
        semantic_schema_version=semantic_schema_version,
        source_plans=source_plans,
        workloads=workloads,
        chunking_signature=chunking_signature,
        content_set_xxh3_128=content_set_xxh3_128,
        semantic_snapshot_xxh3_128=semantic_snapshot_xxh3_128,
    )


def _assemble_semantic_plan(
    *,
    configuration: _PlanConfiguration,
    semantic_path: Path,
    result: _ScratchPlanResult,
    max_scratch_bytes: int,
    checkpoint: Callable[[], None],
) -> SemanticPlan:
    return _assemble_semantic_plan_result(
        configuration=configuration,
        semantic_path=semantic_path,
        result=result,
        max_scratch_bytes=max_scratch_bytes,
        plan_algorithm_version=PLAN_ALGORITHM_VERSION,
        checkpoint=checkpoint,
    )


def plan_semantic_index(
    state_directory: Path,
    *,
    scope: str = "all",
    source_kinds: Sequence[str] = TEXT_SOURCE_KINDS,
    text_model: EmbeddingModelSpec | None = None,
    embed_ocr_text: bool = True,
    chunking: TextChunkingConfig | None = None,
    cost_calibrations: Sequence[SemanticCostCalibration] = (),
    execution_signature: str | None = None,
    scratch_directory: Path | None = None,
    max_scratch_bytes: int = DEFAULT_MAX_SCRATCH_BYTES,
    cancellation_check: CancellationCheck | None = None,
) -> SemanticPlan:
    """Project bounded Semantic work without creating or mutating owner state."""

    configuration = _prepare_plan_configuration(
        scope=scope,
        source_kinds=source_kinds,
        text_model=text_model,
        embed_ocr_text=embed_ocr_text,
        chunking=chunking,
        execution_signature=execution_signature,
        scratch_directory=scratch_directory,
        max_scratch_bytes=max_scratch_bytes,
    )
    selected_sources = configuration.selected_sources
    active_chunking = configuration.active_chunking
    workload_specs = configuration.workload_specs
    semantic_path = state_directory / SEMANTIC_DATABASE_NAME
    calibrations = _cost_calibrations_by_key(cost_calibrations)
    bridge = SQLiteCancellationBridge(cancellation_check)
    bridge.checkpoint()

    if scratch_directory is None:
        scratch_context: AbstractContextManager[str | Path] = tempfile.TemporaryDirectory(
            prefix="neocortex-semantic-plan-",
        )
    else:
        scratch_context = _registered_scratch_workspace(
            scratch_directory,
            run_id=None,
            metadata={
                "component": "semantic-planner",
                "operation": "plan_semantic_index",
                "scope": scope,
                "plan_algorithm_version": PLAN_ALGORITHM_VERSION,
            },
        )

    with scratch_context as temporary:
        scratch_root = Path(temporary)
        scratch_path = scratch_root / "content-keys.sqlite3"
        budget = _ScratchBudget(scratch_root, max_scratch_bytes)
        connection = _create_scratch_database(scratch_path, budget)
        planning_error: BaseException | None = None
        try:
            with sqlite_cancellation_scope(connection, bridge):
                accumulator = _ContentAccumulator(connection, bridge, budget)
                frozen_sources = _plan_source_snapshots(
                    state_directory,
                    configuration=configuration,
                    accumulator=accumulator,
                    bridge=bridge,
                )
                accumulator.flush()
                semantic_version, semantic_snapshot = _semantic_reuse_snapshot(
                    semantic_path,
                    accumulator,
                    workload_specs,
                    bridge,
                )
                content_set = accumulator.content_set_xxh3_128()
                global_summary = accumulator.global_summary()
                workload_plans = tuple(
                    _freeze_workload(
                        spec,
                        accumulator.workload_summary(spec.name),
                        calibrations=calibrations,
                        execution_signature=execution_signature,
                    )
                    for spec in workload_specs
                )
                signature_payload = _plan_payload_for_signature(
                    scope=scope,
                    selected_sources=selected_sources,
                    semantic_schema_version=semantic_version,
                    source_plans=frozen_sources,
                    workloads=workload_plans,
                    chunking_signature=(
                        None if active_chunking is None else active_chunking.signature
                    ),
                    content_set_xxh3_128=content_set,
                    semantic_snapshot_xxh3_128=semantic_snapshot,
                )
                plan_fingerprint = fingerprint_text(
                    canonical_json(signature_payload)
                ).xxh3_128
                bridge.checkpoint()
                connection.commit()
                budget.checkpoint()
                scratch_result = _ScratchPlanResult(
                    semantic_schema_version=semantic_version,
                    source_plans=frozen_sources,
                    workloads=workload_plans,
                    content_set_xxh3_128=content_set,
                    semantic_snapshot_xxh3_128=semantic_snapshot,
                    plan_fingerprint=plan_fingerprint,
                    global_summary=global_summary,
                    scratch_storage_bytes=budget.peak_bytes,
                )
        except BaseException as exc:
            planning_error = exc
            raise
        finally:
            if planning_error is None:
                connection.close()
            else:
                _cleanup_preserving_primary(
                    connection.close,
                    planning_error,
                    label="semantic planner final scratch close cleanup",
                )

        return _assemble_semantic_plan(
            configuration=configuration,
            semantic_path=semantic_path,
            result=scratch_result,
            max_scratch_bytes=max_scratch_bytes,
            checkpoint=bridge.checkpoint,
        )


def semantic_plan_payload(plan: SemanticPlan) -> dict[str, object]:
    """Return a stable JSON-ready representation of a Semantic plan."""

    return _build_semantic_plan_payload(plan)


__all__ = [
    "CONTENT_BATCH_SIZE",
    "DEFAULT_MAX_SCRATCH_BYTES",
    "PLAN_ALGORITHM_VERSION",
    "SemanticPlanBlocked",
    "SemanticScratchLimitExceeded",
    "plan_semantic_index",
    "semantic_plan_payload",
]


# endregion [05]
