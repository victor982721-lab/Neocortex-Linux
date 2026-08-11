"""Pure validation helpers for the public Semantic service contracts."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/semantic_contract_validation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import math
from collections.abc import Set
from pathlib import Path
from typing import Protocol
# endregion [01]

# region [02] Implementación


class _SemanticCostCalibration(Protocol):
    """Structural input required by the cost-calibration validator."""

    @property
    def calibration_signature(self) -> str: ...

    @property
    def execution_signature(self) -> str: ...

    @property
    def processing_signature(self) -> str: ...

    @property
    def workload(self) -> str: ...

    @property
    def model_signature(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def contents_per_second(self) -> float: ...

    @property
    def sample_contents(self) -> int: ...

    @property
    def sample_input_bytes(self) -> int: ...


class _SemanticSourcePlan(Protocol):
    """Structural input required by source and plan validation."""

    @property
    def source_kind(self) -> str: ...

    @property
    def database(self) -> Path: ...

    @property
    def schema_version(self) -> int: ...

    @property
    def resources(self) -> int: ...

    @property
    def sections(self) -> int: ...

    @property
    def chunks(self) -> int: ...

    @property
    def embedding_entities(self) -> int: ...

    @property
    def source_bytes(self) -> int: ...

    @property
    def section_text_bytes(self) -> int: ...

    @property
    def input_bytes(self) -> int: ...

    @property
    def snapshot_xxh3_128(self) -> str: ...


class _SemanticWorkloadPlan(Protocol):
    """Structural input required by workload and plan validation."""

    @property
    def name(self) -> str: ...

    @property
    def modality(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def model_signature(self) -> str: ...

    @property
    def vector_space(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def provider(self) -> str: ...

    @property
    def supported_roles(self) -> tuple[str, ...]: ...

    @property
    def vector_dtype(self) -> str: ...

    @property
    def normalization(self) -> str: ...

    @property
    def distance(self) -> str: ...

    @property
    def model_provenance_json(self) -> str: ...

    @property
    def processing_signature(self) -> str: ...

    @property
    def embedding_entities(self) -> int: ...

    @property
    def unique_contents(self) -> int: ...

    @property
    def preexisting_reusable_contents(self) -> int: ...

    @property
    def planned_reusable_contents(self) -> int: ...

    @property
    def new_unique_contents(self) -> int: ...

    @property
    def input_bytes(self) -> int: ...

    @property
    def unique_input_bytes(self) -> int: ...

    @property
    def new_vector_blob_bytes_lower_bound(self) -> int: ...

    @property
    def model_request_contents_lower_bound(self) -> int: ...

    @property
    def model_request_contents_upper_bound(self) -> int: ...

    @property
    def estimated_model_seconds_lower_bound(self) -> float | None: ...

    @property
    def estimated_model_seconds_upper_bound(self) -> float | None: ...

    @property
    def cost_calibration_signature(self) -> str | None: ...

    @property
    def cost_execution_signature(self) -> str | None: ...

    @property
    def cost_calibration_contents_per_second(self) -> float | None: ...

    @property
    def cost_calibration_sample_contents(self) -> int | None: ...

    @property
    def cost_calibration_sample_input_bytes(self) -> int | None: ...

    @property
    def cost_unavailable_reason(self) -> str | None: ...


class _SemanticPlan(Protocol):
    """Structural input required by the aggregate plan validator."""

    @property
    def scope(self) -> str: ...

    @property
    def selected_sources(self) -> tuple[str, ...]: ...

    @property
    def semantic_database(self) -> Path: ...

    @property
    def semantic_schema_version(self) -> int | None: ...

    @property
    def source_plans(self) -> tuple[_SemanticSourcePlan, ...]: ...

    @property
    def workloads(self) -> tuple[_SemanticWorkloadPlan, ...]: ...

    @property
    def text_chunking_signature(self) -> str | None: ...

    @property
    def content_set_xxh3_128(self) -> str: ...

    @property
    def semantic_snapshot_xxh3_128(self) -> str: ...

    @property
    def plan_signature(self) -> str: ...

    @property
    def resources(self) -> int: ...

    @property
    def sections(self) -> int: ...

    @property
    def chunks(self) -> int: ...

    @property
    def embedding_entities(self) -> int: ...

    @property
    def source_bytes(self) -> int: ...

    @property
    def section_text_bytes(self) -> int: ...

    @property
    def input_bytes(self) -> int: ...

    @property
    def unique_contents(self) -> int: ...

    @property
    def unique_input_bytes(self) -> int: ...

    @property
    def reusable_unique_contents(self) -> int: ...

    @property
    def new_unique_contents(self) -> int: ...

    @property
    def new_vector_blob_bytes_lower_bound(self) -> int: ...

    @property
    def model_request_contents_lower_bound(self) -> int: ...

    @property
    def model_request_contents_upper_bound(self) -> int: ...

    @property
    def estimated_model_seconds_lower_bound(self) -> float | None: ...

    @property
    def estimated_model_seconds_upper_bound(self) -> float | None: ...

    @property
    def scratch_storage_bytes(self) -> int: ...

    @property
    def max_scratch_bytes(self) -> int: ...

    @property
    def originals_verified(self) -> bool | None: ...

    @property
    def execution_ready(self) -> bool | None: ...

    @property
    def dry_run(self) -> bool: ...

    @property
    def jobs_created(self) -> int: ...

    @property
    def state_mutated(self) -> bool: ...

    @property
    def sqlite_read_snapshot_may_touch_shm(self) -> bool: ...


def _require_nonnegative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_xxh3_128(name: str, value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase XXH3-128 digest")


def _require_optional_seconds(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")


def _validate_cost_calibration_identity(
    calibration: _SemanticCostCalibration,
) -> None:
    for name, value in (
        ("calibration_signature", calibration.calibration_signature),
        ("execution_signature", calibration.execution_signature),
        ("processing_signature", calibration.processing_signature),
        ("workload", calibration.workload),
        ("model_signature", calibration.model_signature),
        ("role", calibration.role),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} cannot be blank")
    if calibration.role not in {"passage", "image"}:
        raise ValueError("semantic cost calibration role is unsupported")


def _validate_cost_calibration_rate(calibration: _SemanticCostCalibration) -> None:
    if isinstance(calibration.contents_per_second, bool) or not isinstance(
        calibration.contents_per_second,
        (int, float),
    ):
        raise ValueError("contents_per_second must be a finite number")
    if not math.isfinite(calibration.contents_per_second) or calibration.contents_per_second <= 0:
        raise ValueError("contents_per_second must be finite and positive")


def _validate_cost_calibration_sample(
    calibration: _SemanticCostCalibration,
) -> None:
    if (
        isinstance(calibration.sample_contents, bool)
        or not isinstance(calibration.sample_contents, int)
        or isinstance(calibration.sample_input_bytes, bool)
        or not isinstance(calibration.sample_input_bytes, int)
        or calibration.sample_contents < 1
        or calibration.sample_input_bytes < 0
    ):
        raise ValueError("semantic calibration sample bounds are invalid")


def validate_semantic_cost_calibration(
    calibration: _SemanticCostCalibration,
) -> None:
    """Validate one measured cost calibration without external state."""

    _validate_cost_calibration_identity(calibration)
    _validate_cost_calibration_rate(calibration)
    _validate_cost_calibration_sample(calibration)


def _validate_source_identity(
    source: _SemanticSourcePlan,
    *,
    text_source_kinds: Set[str],
) -> None:
    if not isinstance(source.source_kind, str) or not source.source_kind.strip():
        raise ValueError("source_kind cannot be blank")
    if source.source_kind not in (*text_source_kinds, "image"):
        raise ValueError("source_kind is unsupported")
    if not isinstance(source.database, Path):
        raise ValueError("source database must be a Path")
    if (
        isinstance(source.schema_version, bool)
        or not isinstance(source.schema_version, int)
        or source.schema_version < 1
    ):
        raise ValueError("schema_version must be positive")


def _validate_source_counts(source: _SemanticSourcePlan) -> None:
    for name in (
        "resources",
        "sections",
        "chunks",
        "embedding_entities",
        "source_bytes",
        "section_text_bytes",
        "input_bytes",
    ):
        _require_nonnegative_int(name, getattr(source, name))
    if source.chunks > source.embedding_entities:
        raise ValueError("source chunks cannot exceed embedding entities")
    if source.resources == 0 and any(
        getattr(source, name) != 0
        for name in (
            "sections",
            "chunks",
            "embedding_entities",
            "source_bytes",
            "section_text_bytes",
            "input_bytes",
        )
    ):
        raise ValueError("an empty source cannot expose derived work")
    if source.sections == 0 and source.section_text_bytes != 0:
        raise ValueError("section text bytes require source sections")
    if source.chunks > 0 and source.sections == 0:
        raise ValueError("source chunks require source sections")
    if source.embedding_entities == 0 and source.input_bytes != 0:
        raise ValueError("source input bytes require embedding entities")


def validate_semantic_source_plan(
    source: _SemanticSourcePlan,
    *,
    text_source_kinds: Set[str],
) -> None:
    """Validate one route-owned source projection without external state."""

    _validate_source_identity(source, text_source_kinds=text_source_kinds)
    _validate_source_counts(source)
    _require_xxh3_128("snapshot_xxh3_128", source.snapshot_xxh3_128)


def _validate_workload_identity(workload: _SemanticWorkloadPlan) -> None:
    for name in (
        "name",
        "role",
        "model_signature",
        "vector_space",
        "model_id",
        "model_version",
        "provider",
        "processing_signature",
    ):
        value = getattr(workload, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} cannot be blank")
    if workload.modality not in {"text", "image"}:
        raise ValueError("workload modality is unsupported")
    allowed_roles = {"query", "passage"} if workload.modality == "text" else {"image"}
    if (
        not isinstance(workload.supported_roles, tuple)
        or not workload.supported_roles
        or any(not isinstance(role, str) for role in workload.supported_roles)
        or len(set(workload.supported_roles)) != len(workload.supported_roles)
        or any(role not in allowed_roles for role in workload.supported_roles)
        or workload.role not in workload.supported_roles
    ):
        raise ValueError("workload roles are incompatible with its model")
    if (
        isinstance(workload.dimensions, bool)
        or not isinstance(workload.dimensions, int)
        or not 1 <= workload.dimensions <= 65_536
    ):
        raise ValueError("dimensions must be between 1 and 65536")
    if workload.vector_dtype not in {"float16", "float32"}:
        raise ValueError("vector_dtype is unsupported")
    if workload.normalization != "l2" or workload.distance != "cosine":
        raise ValueError("workload model must use l2/cosine")
    try:
        provenance = json.loads(workload.model_provenance_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("model_provenance_json is invalid") from exc
    if not isinstance(provenance, dict):
        raise ValueError("model_provenance_json must contain an object")
    if (
        json.dumps(
            provenance,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != workload.model_provenance_json
    ):
        raise ValueError("model_provenance_json must be canonical")


def _validate_workload_counts(workload: _SemanticWorkloadPlan) -> None:
    for name in (
        "embedding_entities",
        "unique_contents",
        "preexisting_reusable_contents",
        "planned_reusable_contents",
        "new_unique_contents",
        "input_bytes",
        "unique_input_bytes",
        "new_vector_blob_bytes_lower_bound",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
    ):
        _require_nonnegative_int(name, getattr(workload, name))
    if workload.unique_contents > workload.embedding_entities:
        raise ValueError("unique contents cannot exceed embedding entities")
    if (workload.embedding_entities == 0) != (workload.unique_contents == 0):
        raise ValueError("workload entities and unique contents must be jointly empty")
    if workload.embedding_entities == 0 and (
        workload.input_bytes != 0 or workload.unique_input_bytes != 0
    ):
        raise ValueError("an empty workload cannot expose input bytes")
    if (
        workload.preexisting_reusable_contents
        + workload.planned_reusable_contents
        + workload.new_unique_contents
        != workload.unique_contents
    ):
        raise ValueError("workload reuse counts do not partition unique contents")
    if workload.unique_input_bytes > workload.input_bytes:
        raise ValueError("unique input bytes cannot exceed total input bytes")
    if workload.model_request_contents_lower_bound != workload.new_unique_contents:
        raise ValueError("request lower bound must equal new unique contents")
    if not (
        workload.model_request_contents_lower_bound
        <= workload.model_request_contents_upper_bound
        <= workload.embedding_entities
    ):
        raise ValueError("workload request range is inconsistent")
    width = 2 if workload.vector_dtype == "float16" else 4
    if workload.new_vector_blob_bytes_lower_bound != (
        workload.new_unique_contents * workload.dimensions * width
    ):
        raise ValueError("vector blob lower bound is inconsistent")


def _validate_workload_seconds(workload: _SemanticWorkloadPlan) -> None:
    _require_optional_seconds(
        "estimated_model_seconds_lower_bound",
        workload.estimated_model_seconds_lower_bound,
    )
    _require_optional_seconds(
        "estimated_model_seconds_upper_bound",
        workload.estimated_model_seconds_upper_bound,
    )
    if (workload.estimated_model_seconds_lower_bound is None) != (
        workload.estimated_model_seconds_upper_bound is None
    ):
        raise ValueError("both model-second bounds must be available together")
    if (
        workload.estimated_model_seconds_lower_bound is not None
        and workload.estimated_model_seconds_upper_bound is not None
        and workload.estimated_model_seconds_lower_bound
        > workload.estimated_model_seconds_upper_bound
    ):
        raise ValueError("model-second range is inverted")


def _validate_complete_workload_calibration(workload: _SemanticWorkloadPlan) -> None:
    assert workload.cost_calibration_signature is not None
    assert workload.cost_execution_signature is not None
    assert workload.cost_calibration_contents_per_second is not None
    assert workload.cost_calibration_sample_contents is not None
    assert workload.cost_calibration_sample_input_bytes is not None
    if (
        not isinstance(workload.cost_calibration_signature, str)
        or not workload.cost_calibration_signature.strip()
    ):
        raise ValueError("cost_calibration_signature cannot be blank")
    if (
        not isinstance(workload.cost_execution_signature, str)
        or not workload.cost_execution_signature.strip()
    ):
        raise ValueError("cost_execution_signature cannot be blank")
    if (
        isinstance(workload.cost_calibration_contents_per_second, bool)
        or not isinstance(
            workload.cost_calibration_contents_per_second,
            (int, float),
        )
        or not math.isfinite(workload.cost_calibration_contents_per_second)
        or workload.cost_calibration_contents_per_second <= 0
        or isinstance(workload.cost_calibration_sample_contents, bool)
        or not isinstance(workload.cost_calibration_sample_contents, int)
        or workload.cost_calibration_sample_contents < 1
        or isinstance(workload.cost_calibration_sample_input_bytes, bool)
        or not isinstance(workload.cost_calibration_sample_input_bytes, int)
        or workload.cost_calibration_sample_input_bytes < 0
    ):
        raise ValueError("workload calibration metadata is invalid")
    if workload.cost_unavailable_reason is not None:
        raise ValueError("calibrated cost cannot be marked unavailable")


def _validate_workload_calibration(workload: _SemanticWorkloadPlan) -> None:
    calibration_values = (
        workload.cost_calibration_signature,
        workload.cost_execution_signature,
        workload.cost_calibration_contents_per_second,
        workload.cost_calibration_sample_contents,
        workload.cost_calibration_sample_input_bytes,
    )
    if workload.cost_unavailable_reason is not None and (
        not isinstance(workload.cost_unavailable_reason, str)
        or not workload.cost_unavailable_reason.strip()
    ):
        raise ValueError("cost_unavailable_reason cannot be blank")
    has_calibration = all(value is not None for value in calibration_values)
    if any(value is not None for value in calibration_values) and not has_calibration:
        raise ValueError("cost calibration metadata must be complete")
    if has_calibration:
        _validate_complete_workload_calibration(workload)
    elif workload.model_request_contents_upper_bound > 0:
        if not (workload.cost_unavailable_reason or "").strip():
            raise ValueError("uncalibrated work requires an unavailable reason")
        if workload.estimated_model_seconds_lower_bound is not None:
            raise ValueError("uncalibrated work cannot expose model seconds")
    elif (
        workload.estimated_model_seconds_lower_bound != 0.0
        or workload.estimated_model_seconds_upper_bound != 0.0
        or workload.cost_unavailable_reason is not None
    ):
        raise ValueError("zero-work cost bounds must be exact zero")


def validate_semantic_workload_plan(workload: _SemanticWorkloadPlan) -> None:
    """Validate one model/role work projection without external state."""

    _validate_workload_identity(workload)
    _validate_workload_counts(workload)
    _validate_workload_seconds(workload)
    _validate_workload_calibration(workload)


def _validate_plan_identity(
    plan: _SemanticPlan,
    *,
    text_source_kinds: Set[str],
    source_plan_type: type[object],
    workload_plan_type: type[object],
) -> None:
    if not isinstance(plan.scope, str):
        raise ValueError("semantic plan scope must be a string")
    if plan.scope not in {"text", "image", "all"}:
        raise ValueError("semantic plan scope is unsupported")
    if not isinstance(plan.selected_sources, tuple) or any(
        not isinstance(source, str) or source not in text_source_kinds
        for source in plan.selected_sources
    ):
        raise ValueError("selected_sources must contain supported text sources")
    if len(set(plan.selected_sources)) != len(plan.selected_sources):
        raise ValueError("selected_sources cannot contain duplicates")
    if not isinstance(plan.semantic_database, Path):
        raise ValueError("semantic_database must be a Path")
    if plan.semantic_schema_version is not None and (
        isinstance(plan.semantic_schema_version, bool)
        or not isinstance(plan.semantic_schema_version, int)
        or plan.semantic_schema_version < 1
    ):
        raise ValueError("semantic_schema_version must be positive")
    if plan.text_chunking_signature is not None and (
        not isinstance(plan.text_chunking_signature, str)
        or not plan.text_chunking_signature.strip()
    ):
        raise ValueError("text_chunking_signature cannot be blank")
    if not isinstance(plan.source_plans, tuple) or any(
        not isinstance(source, source_plan_type) for source in plan.source_plans
    ):
        raise ValueError("source_plans must contain SemanticSourcePlan values")
    if not isinstance(plan.workloads, tuple) or any(
        not isinstance(workload, workload_plan_type) for workload in plan.workloads
    ):
        raise ValueError("workloads must contain SemanticWorkloadPlan values")


def _validate_plan_topology(
    plan: _SemanticPlan,
    *,
    image_ocr_text_channel: str,
) -> None:
    source_kinds = tuple(source.source_kind for source in plan.source_plans)
    if len(set(source_kinds)) != len(source_kinds):
        raise ValueError("source plans cannot contain duplicate owners")
    workload_names = tuple(workload.name for workload in plan.workloads)
    if len(set(workload_names)) != len(workload_names):
        raise ValueError("workloads cannot contain duplicate names")
    allowed_workload_names: set[tuple[str, ...]]
    if plan.scope == "text":
        expected_source_kinds = plan.selected_sources
        allowed_workload_names = {("text",)}
    elif plan.scope == "image":
        expected_source_kinds = ("image",)
        allowed_workload_names = {("image",), ("image", image_ocr_text_channel)}
    else:
        expected_source_kinds = (*plan.selected_sources, "image")
        allowed_workload_names = {
            ("text", "image"),
            ("text", "image", image_ocr_text_channel),
        }
    if plan.scope in {"text", "all"} and not plan.selected_sources:
        raise ValueError("text scope requires selected sources")
    if plan.scope == "image" and plan.selected_sources:
        raise ValueError("image scope cannot select text sources")
    if source_kinds != expected_source_kinds:
        raise ValueError("selected sources and source plans are inconsistent")
    if workload_names not in allowed_workload_names:
        raise ValueError("workload topology is inconsistent with plan scope")
    expected_workload_contracts = {
        "text": ("text", "passage"),
        "image": ("image", "image"),
        image_ocr_text_channel: ("text", "passage"),
    }
    if any(
        (workload.modality, workload.role) != expected_workload_contracts[workload.name]
        for workload in plan.workloads
    ):
        raise ValueError("workload role or modality is inconsistent with its owner")


def _validate_plan_snapshots_and_signatures(plan: _SemanticPlan) -> None:
    physical_snapshots: dict[Path, tuple[int, str]] = {}
    for source in plan.source_plans:
        snapshot = (source.schema_version, source.snapshot_xxh3_128)
        previous = physical_snapshots.setdefault(source.database, snapshot)
        if previous != snapshot:
            raise ValueError("source plans for one physical database require one snapshot")
    if any(workload.modality == "text" for workload in plan.workloads) != (
        plan.text_chunking_signature is not None
    ):
        raise ValueError("text chunking signature does not match plan workloads")
    if not isinstance(plan.plan_signature, str) or not plan.plan_signature.strip():
        raise ValueError("plan_signature cannot be blank")
    _require_xxh3_128("content_set_xxh3_128", plan.content_set_xxh3_128)
    _require_xxh3_128(
        "semantic_snapshot_xxh3_128",
        plan.semantic_snapshot_xxh3_128,
    )


def _validate_plan_numeric_bounds(plan: _SemanticPlan) -> None:
    for name in (
        "resources",
        "sections",
        "chunks",
        "embedding_entities",
        "source_bytes",
        "section_text_bytes",
        "input_bytes",
        "unique_contents",
        "unique_input_bytes",
        "reusable_unique_contents",
        "new_unique_contents",
        "new_vector_blob_bytes_lower_bound",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
        "scratch_storage_bytes",
        "max_scratch_bytes",
        "jobs_created",
    ):
        _require_nonnegative_int(name, getattr(plan, name))
    if not isinstance(plan.sqlite_read_snapshot_may_touch_shm, bool):
        raise ValueError("sqlite_read_snapshot_may_touch_shm must be boolean")
    if not isinstance(plan.state_mutated, bool):
        raise ValueError("state_mutated must be boolean")
    if plan.max_scratch_bytes < 1:
        raise ValueError("max_scratch_bytes must be positive")
    if plan.scratch_storage_bytes > plan.max_scratch_bytes:
        raise ValueError("scratch storage exceeds its hard bound")


def _validate_plan_source_aggregates(plan: _SemanticPlan) -> None:
    if plan.resources != sum(source.resources for source in plan.source_plans):
        raise ValueError("plan resource aggregate is inconsistent")
    if plan.sections != sum(source.sections for source in plan.source_plans):
        raise ValueError("plan section aggregate is inconsistent")
    if plan.chunks != sum(source.chunks for source in plan.source_plans):
        raise ValueError("plan chunk aggregate is inconsistent")
    if plan.embedding_entities != sum(source.embedding_entities for source in plan.source_plans):
        raise ValueError("plan entity aggregate is inconsistent")
    if plan.embedding_entities != sum(workload.embedding_entities for workload in plan.workloads):
        raise ValueError("workload entity aggregate is inconsistent")
    if plan.source_bytes != sum(source.source_bytes for source in plan.source_plans):
        raise ValueError("plan source-byte aggregate is inconsistent")
    if plan.section_text_bytes != sum(source.section_text_bytes for source in plan.source_plans):
        raise ValueError("plan section-byte aggregate is inconsistent")
    if plan.input_bytes != sum(source.input_bytes for source in plan.source_plans):
        raise ValueError("plan source input-byte aggregate is inconsistent")
    if plan.input_bytes != sum(workload.input_bytes for workload in plan.workloads):
        raise ValueError("plan workload input-byte aggregate is inconsistent")


def _validate_plan_content_aggregates(plan: _SemanticPlan) -> None:
    if plan.unique_input_bytes > plan.input_bytes:
        raise ValueError("unique input bytes cannot exceed total input bytes")
    if plan.unique_input_bytes > sum(workload.unique_input_bytes for workload in plan.workloads):
        raise ValueError("plan unique input bytes exceed workload evidence")
    if plan.unique_contents > plan.embedding_entities or (
        plan.unique_contents > sum(workload.unique_contents for workload in plan.workloads)
    ):
        raise ValueError("plan unique-content aggregate is inconsistent")
    if plan.reusable_unique_contents + plan.new_unique_contents != plan.unique_contents:
        raise ValueError("global reuse counts do not partition unique contents")
    if plan.reusable_unique_contents > sum(
        workload.preexisting_reusable_contents for workload in plan.workloads
    ):
        raise ValueError("global preexisting reuse exceeds workload evidence")
    if plan.new_unique_contents != sum(workload.new_unique_contents for workload in plan.workloads):
        raise ValueError("global new-content aggregate is inconsistent")
    if plan.model_request_contents_lower_bound != plan.new_unique_contents:
        raise ValueError("global request lower bound must equal new unique contents")
    if plan.model_request_contents_lower_bound != sum(
        workload.model_request_contents_lower_bound for workload in plan.workloads
    ):
        raise ValueError("global request lower bound is inconsistent")
    if plan.model_request_contents_upper_bound != sum(
        workload.model_request_contents_upper_bound for workload in plan.workloads
    ):
        raise ValueError("global request upper bound is inconsistent")
    if not (
        plan.model_request_contents_lower_bound
        <= plan.model_request_contents_upper_bound
        <= plan.embedding_entities
    ):
        raise ValueError("global request range is inconsistent")
    if plan.new_vector_blob_bytes_lower_bound != sum(
        workload.new_vector_blob_bytes_lower_bound for workload in plan.workloads
    ):
        raise ValueError("global vector lower bound is inconsistent")


def _validate_plan_cost_aggregates(plan: _SemanticPlan) -> None:
    _require_optional_seconds(
        "estimated_model_seconds_lower_bound",
        plan.estimated_model_seconds_lower_bound,
    )
    _require_optional_seconds(
        "estimated_model_seconds_upper_bound",
        plan.estimated_model_seconds_upper_bound,
    )
    available_workloads = all(
        workload.estimated_model_seconds_lower_bound is not None for workload in plan.workloads
    )
    expected_lower = (
        sum(workload.estimated_model_seconds_lower_bound or 0.0 for workload in plan.workloads)
        if available_workloads
        else None
    )
    expected_upper = (
        sum(workload.estimated_model_seconds_upper_bound or 0.0 for workload in plan.workloads)
        if available_workloads
        else None
    )
    if plan.estimated_model_seconds_lower_bound != expected_lower or (
        plan.estimated_model_seconds_upper_bound != expected_upper
    ):
        raise ValueError("plan model-second aggregates are inconsistent")


def _validate_plan_operational_state(plan: _SemanticPlan) -> None:
    if plan.dry_run is not True or plan.jobs_created != 0 or plan.state_mutated:
        raise ValueError("semantic plans must remain non-mutating dry runs")
    if plan.originals_verified is not None and not isinstance(
        plan.originals_verified,
        bool,
    ):
        raise ValueError("originals_verified must be tri-state")
    if plan.execution_ready is not None and not isinstance(plan.execution_ready, bool):
        raise ValueError("execution_ready must be tri-state")
    if plan.scope in {"image", "all"} and plan.originals_verified is not False:
        raise ValueError("cache-only image plans cannot verify originals")
    if plan.scope == "text" and plan.originals_verified is not None:
        raise ValueError("text-only plans do not assess image originals")


def validate_semantic_plan(
    plan: _SemanticPlan,
    *,
    text_source_kinds: Set[str],
    image_ocr_text_channel: str,
    source_plan_type: type[object],
    workload_plan_type: type[object],
) -> None:
    """Validate a complete read-only Semantic plan without external state."""

    _validate_plan_identity(
        plan,
        text_source_kinds=text_source_kinds,
        source_plan_type=source_plan_type,
        workload_plan_type=workload_plan_type,
    )
    _validate_plan_topology(plan, image_ocr_text_channel=image_ocr_text_channel)
    _validate_plan_snapshots_and_signatures(plan)
    _validate_plan_numeric_bounds(plan)
    _validate_plan_source_aggregates(plan)
    _validate_plan_content_aggregates(plan)
    _validate_plan_cost_aggregates(plan)
    _validate_plan_operational_state(plan)


# endregion [02]
