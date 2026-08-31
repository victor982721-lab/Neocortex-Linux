"""Pure Semantic plan configuration, projection and result assembly helpers.
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_plan_results.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


This internal module owns immutable planning specifications and deterministic
result construction. Durable owner validation and orchestration remain in the
planner facade.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
import itertools
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import xxhash

from . import semantic_sources as _sources
from .semantic_chunking import TextChunkingConfig
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    clip_image_model,
    multilingual_text_model,
    text_chunking_for_model,
)
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    SemanticItem,
    TextSection,
    VectorDType,
    canonical_json,
    fingerprint_text,
)
from .semantic_plan_errors import SemanticPlanBlocked
from .semantic_plan_scratch import MIN_MAX_SCRATCH_BYTES, _ContentAccumulator
from .semantic_quality import SEMANTIC_TEXT_QUALITY_POLICY, iter_semantic_text_chunks
from .semantic_service_contracts import (
    SemanticCostCalibration,
    SemanticPlan,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
)
from .semantic_sources import (
    IMAGE_SOURCE_ADAPTER_VERSION,
    IMAGE_SOURCE_KIND,
    SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
    SOURCE_ADAPTER_VERSION,
    TEXT_SOURCE_KINDS,
    TextSourceRecord,
    iter_text_source_records,
    semantic_text_processing_signature,
    semantic_source_database,
)
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class _WorkloadSpec:
    name: str
    model: EmbeddingModelSpec
    role: EmbeddingRole
    processing_signature: str

    @property
    def vector_width(self) -> int:
        return 2 if self.model.vector_dtype is VectorDType.FLOAT16 else 4

    @property
    def vector_blob_bytes(self) -> int:
        return self.model.dimensions * self.vector_width


@dataclass(frozen=True, slots=True)
class _PlanConfiguration:
    scope: str
    selected_sources: tuple[str, ...]
    active_chunking: TextChunkingConfig | None
    workload_specs: tuple[_WorkloadSpec, ...]


@dataclass(frozen=True, slots=True)
class _ScratchPlanResult:
    semantic_schema_version: int | None
    source_plans: tuple[SemanticSourcePlan, ...]
    workloads: tuple[SemanticWorkloadPlan, ...]
    content_set_xxh3_128: str
    semantic_snapshot_xxh3_128: str
    plan_fingerprint: str
    global_summary: Mapping[str, int]
    scratch_storage_bytes: int


@dataclass(slots=True)
class _SourceCounters:
    source_kind: str
    database: Path
    schema_version: int
    resources: int = 0
    sections: int = 0
    chunks: int = 0
    embedding_entities: int = 0
    source_bytes: int = 0
    section_text_bytes: int = 0
    input_bytes: int = 0

    def freeze(self, *, snapshot_xxh3_128: str) -> SemanticSourcePlan:
        return SemanticSourcePlan(
            source_kind=self.source_kind,
            database=self.database,
            schema_version=self.schema_version,
            resources=self.resources,
            sections=self.sections,
            chunks=self.chunks,
            embedding_entities=self.embedding_entities,
            source_bytes=self.source_bytes,
            section_text_bytes=self.section_text_bytes,
            input_bytes=self.input_bytes,
            snapshot_xxh3_128=snapshot_xxh3_128,
        )


def _model_contract_payload(model: EmbeddingModelSpec) -> Mapping[str, object]:
    return {
        "model_signature": model.model_signature,
        "vector_space": model.vector_space,
        "modality": model.modality.value,
        "model_id": model.model_id,
        "model_version": model.model_version,
        "dimensions": model.dimensions,
        "provider": model.provider,
        "supported_roles": [role.value for role in model.supported_roles],
        "vector_dtype": model.vector_dtype.value,
        "normalization": model.normalization,
        "distance": model.distance,
        "provenance": dict(model.provenance),
    }


def _validate_workload_specs(specs: Sequence[_WorkloadSpec]) -> None:
    names: set[str] = set()
    models: dict[str, EmbeddingModelSpec] = {}
    roles: dict[str, set[EmbeddingRole]] = {}
    for spec in specs:
        if spec.name in names:
            raise SemanticPlanBlocked(f"duplicate semantic workload name: {spec.name}")
        names.add(spec.name)
        if spec.role not in spec.model.supported_roles:
            raise SemanticPlanBlocked(
                f"semantic model {spec.model.model_signature!r} does not support "
                f"the {spec.role.value!r} role"
            )
        existing = models.setdefault(spec.model.model_signature, spec.model)
        if existing != spec.model:
            raise SemanticPlanBlocked(
                "one semantic model signature is bound to colliding contracts: "
                f"{spec.model.model_signature}"
            )
        roles.setdefault(spec.model.model_signature, set()).add(spec.role)
    collision = next(
        (signature for signature, values in roles.items() if len(values) > 1),
        None,
    )
    if collision is not None:
        raise SemanticPlanBlocked(
            f"vector payload identity cannot distinguish model roles for signature: {collision}"
        )


def _resource_size(item: SemanticItem) -> int:
    revision = item.source_revision
    raw = revision.get("size", revision.get("size_bytes"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise SemanticPlanBlocked("semantic source resource has no valid byte size")
    return raw


def _plan_text_source(
    state_directory: Path,
    source_kind: str,
    *,
    connection: sqlite3.Connection,
    schema_version: int,
    schema_snapshot_xxh3_128: str,
    chunking: TextChunkingConfig,
    workload: _WorkloadSpec,
    accumulator: _ContentAccumulator,
    checkpoint: Callable[[], None],
) -> SemanticSourcePlan:
    database = semantic_source_database(state_directory, source_kind)
    counters = _SourceCounters(source_kind, database, schema_version)
    snapshot_hasher = xxhash.xxh3_128()
    snapshot_hasher.update(
        canonical_json(
            {
                "adapter": SOURCE_ADAPTER_VERSION,
                "schema": schema_snapshot_xxh3_128,
                "source_kind": source_kind,
            }
        ).encode("utf-8")
    )
    records = iter_text_source_records(
        state_directory,
        source_kind,
        connection=connection,
    )
    groups = itertools.groupby(records, key=lambda record: record.item.item_id)
    for _item_id, grouped in groups:
        checkpoint()
        iterator = iter(grouped)
        first = next(iterator)
        item = first.item
        source_records = itertools.chain((first,), iterator)
        counters.resources += 1
        counters.source_bytes += _resource_size(item)

        def source_sections(
            records: Iterator[TextSourceRecord] = source_records,
        ) -> Iterator[TextSection]:
            for record in records:
                checkpoint()
                yield record.section

        def sections(item_value: SemanticItem = item) -> Iterator[TextSection]:
            for section in _sources.iter_text_sections_with_metadata(
                item_value,
                source_sections(),
            ):
                checkpoint()
                encoded_bytes = len(section.text.encode("utf-8"))
                section_fingerprint = fingerprint_text(section.text)
                snapshot_hasher.update(b"\n")
                snapshot_hasher.update(
                    canonical_json(
                        {
                            "item_id": item_value.item_id,
                            "item_fingerprint": {
                                "xxh3_128": item_value.fingerprint.xxh3_128,
                                "bytes": item_value.fingerprint.byte_count,
                                "guard": item_value.fingerprint.xxh3_64_guard,
                            },
                            "path": item_value.path,
                            "source_revision": dict(item_value.source_revision),
                            "section_kind": section.section_kind,
                            "section_id": section.section_id,
                            "section_fingerprint": {
                                "xxh3_128": section_fingerprint.xxh3_128,
                                "bytes": section_fingerprint.byte_count,
                                "guard": section_fingerprint.xxh3_64_guard,
                            },
                        }
                    ).encode("utf-8")
                )
                counters.sections += 1
                counters.section_text_bytes += encoded_bytes
                yield section

        for chunk in iter_semantic_text_chunks(
            item.item_id,
            sections(),
            chunking,
        ):
            checkpoint()
            input_bytes = chunk.fingerprint.byte_count
            counters.chunks += 1
            counters.embedding_entities += 1
            counters.input_bytes += input_bytes
            accumulator.add(
                workload,
                chunk.fingerprint,
                source_payload_bytes=input_bytes,
            )
    return counters.freeze(snapshot_xxh3_128=snapshot_hasher.hexdigest())


def _plan_images(
    state_directory: Path,
    *,
    connection: sqlite3.Connection,
    schema_version: int,
    schema_snapshot_xxh3_128: str,
    dedup_schema_snapshot_xxh3_128: str | None,
    chunking: TextChunkingConfig | None,
    image_workload: _WorkloadSpec,
    ocr_workload: _WorkloadSpec | None,
    accumulator: _ContentAccumulator,
    checkpoint: Callable[[], None],
) -> SemanticSourcePlan:
    image_database = semantic_source_database(state_directory, IMAGE_SOURCE_KIND)
    dedup_database = state_directory / "dedup.sqlite3"
    counters = _SourceCounters(IMAGE_SOURCE_KIND, image_database, schema_version)
    snapshot_hasher = xxhash.xxh3_128()
    snapshot_hasher.update(
        canonical_json(
            {
                "adapter": IMAGE_SOURCE_ADAPTER_VERSION,
                "dedup_schema": dedup_schema_snapshot_xxh3_128,
                "schema": schema_snapshot_xxh3_128,
                "source_kind": IMAGE_SOURCE_KIND,
            }
        ).encode("utf-8")
    )
    dedup_attached = dedup_schema_snapshot_xxh3_128 is not None
    rows = _sources._image_rows(
        image_database,
        dedup_database if dedup_attached else None,
        connection,
        dedup_attached=dedup_attached,
    )
    for row in rows:
        checkpoint()
        snapshot = _sources._snapshot_from_image_row(row)
        if row["full_digest"] is None:
            raise SemanticPlanBlocked(
                "image resource lacks a cached full dedup fingerprint; "
                "the read-only planner refuses to read the original file: "
                f"{snapshot.path}"
            )
        fingerprint = _sources._image_descriptor_fingerprint(
            row["full_digest"],
            snapshot.size,
        )
        snapshot_hasher.update(b"\n")
        snapshot_hasher.update(
            canonical_json(
                {
                    "file_key": str(row["file_key"]),
                    "path": snapshot.path,
                    "size": snapshot.size,
                    "mtime_ns": snapshot.mtime_ns,
                    "birthtime_ns": snapshot.birthtime_ns,
                    "processing_signature": str(row["processing_signature"]),
                    "full_digest": bytes(row["full_digest"]).hex(),
                }
            ).encode("utf-8")
        )
        counters.resources += 1
        counters.source_bytes += snapshot.size
        counters.embedding_entities += 1
        counters.input_bytes += snapshot.size
        accumulator.add(
            image_workload,
            fingerprint,
            source_payload_bytes=snapshot.size,
        )

        if ocr_workload is None or row["ocr_text_zlib"] is None:
            continue
        if chunking is None:
            raise AssertionError("OCR planning requires a text chunking contract")
        ocr_text = _sources._decode_text(
            row["ocr_text_zlib"],
            int(row["ocr_text_chars"]),
        )
        if fingerprint_text(ocr_text).xxh3_128 != str(row["ocr_text_xxh3_128"]):
            raise SemanticPlanBlocked(f"image OCR fingerprint mismatch: {snapshot.path}")
        ocr_fingerprint = fingerprint_text(ocr_text)
        snapshot_hasher.update(b"\n")
        snapshot_hasher.update(
            canonical_json(
                {
                    "file_key": str(row["file_key"]),
                    "ocr_fingerprint": {
                        "xxh3_128": ocr_fingerprint.xxh3_128,
                        "bytes": ocr_fingerprint.byte_count,
                        "guard": ocr_fingerprint.xxh3_64_guard,
                    },
                    "ocr_truncated": bool(row["ocr_text_truncated"]),
                }
            ).encode("utf-8")
        )
        counters.sections += 1
        counters.section_text_bytes += len(ocr_text.encode("utf-8"))
        section = TextSection(
            section_kind="image_ocr",
            section_id="ocr",
            text=ocr_text,
            provenance={"adapter": IMAGE_SOURCE_ADAPTER_VERSION},
        )
        item_id = _sources._item_id(IMAGE_SOURCE_KIND, str(row["file_key"]))
        for chunk in iter_semantic_text_chunks(item_id, (section,), chunking):
            checkpoint()
            input_bytes = chunk.fingerprint.byte_count
            counters.chunks += 1
            counters.embedding_entities += 1
            counters.input_bytes += input_bytes
            accumulator.add(
                ocr_workload,
                chunk.fingerprint,
                source_payload_bytes=input_bytes,
            )
    return counters.freeze(snapshot_xxh3_128=snapshot_hasher.hexdigest())


def _select_plan_sources(
    scope: str,
    source_kinds: Sequence[str],
) -> tuple[str, ...]:
    if scope not in {"text", "image", "all"}:
        raise ValueError("semantic plan scope must be text, image or all")
    selected_sources = tuple(dict.fromkeys(source_kinds)) if scope != "image" else ()
    if scope in {"text", "all"} and (
        not selected_sources or any(source not in TEXT_SOURCE_KINDS for source in selected_sources)
    ):
        raise ValueError("semantic plan text sources must name durable text caches")
    return selected_sources


def _validate_plan_runtime_options(
    *,
    embed_ocr_text: bool,
    execution_signature: str | None,
    scratch_directory: Path | None,
    max_scratch_bytes: int,
) -> None:
    if execution_signature is not None and not execution_signature.strip():
        raise ValueError("execution_signature cannot be blank")
    if (
        isinstance(max_scratch_bytes, bool)
        or not isinstance(max_scratch_bytes, int)
        or not MIN_MAX_SCRATCH_BYTES <= max_scratch_bytes <= 16 * 1024 * 1024 * 1024 * 1024
    ):
        raise ValueError("max_scratch_bytes must be an integer between 65536 and 16 TiB")
    if scratch_directory is not None and not scratch_directory.is_dir():
        raise ValueError("semantic plan scratch_directory must be an existing directory")
    if not isinstance(embed_ocr_text, bool):
        raise ValueError("embed_ocr_text must be a boolean")


def _validate_plan_options(
    *,
    scope: str,
    source_kinds: Sequence[str],
    embed_ocr_text: bool,
    execution_signature: str | None,
    scratch_directory: Path | None,
    max_scratch_bytes: int,
) -> tuple[str, ...]:
    selected_sources = _select_plan_sources(scope, source_kinds)
    _validate_plan_runtime_options(
        embed_ocr_text=embed_ocr_text,
        execution_signature=execution_signature,
        scratch_directory=scratch_directory,
        max_scratch_bytes=max_scratch_bytes,
    )
    return selected_sources


def _resolve_text_contract(
    *,
    scope: str,
    text_model: EmbeddingModelSpec | None,
    embed_ocr_text: bool,
    chunking: TextChunkingConfig | None,
) -> tuple[EmbeddingModelSpec | None, TextChunkingConfig | None]:
    needs_text_contract = scope in {"text", "all"} or (scope in {"image", "all"} and embed_ocr_text)
    selected_text_model = text_model or multilingual_text_model() if needs_text_contract else None
    if selected_text_model is not None and (
        selected_text_model.modality is not EmbeddingModality.TEXT
    ):
        raise ValueError("semantic plan text model must have text modality")
    active_chunking = (
        chunking or text_chunking_for_model(selected_text_model)
        if selected_text_model is not None
        else None
    )
    return selected_text_model, active_chunking


def _build_workload_specs(
    *,
    scope: str,
    selected_sources: tuple[str, ...],
    selected_text_model: EmbeddingModelSpec | None,
    active_chunking: TextChunkingConfig | None,
    embed_ocr_text: bool,
) -> tuple[_WorkloadSpec, ...]:
    specs: list[_WorkloadSpec] = []
    planning_chunking_signature = (
        None
        if active_chunking is None
        else active_chunking.signature
        if active_chunking.model_token_limit is not None
        else f"{active_chunking.signature}|tokenizer-contract=unresolved-v1"
    )
    if scope in {"text", "all"}:
        assert selected_text_model is not None
        assert active_chunking is not None
        assert planning_chunking_signature is not None
        specs.append(
            _WorkloadSpec(
                "text",
                selected_text_model,
                EmbeddingRole.PASSAGE,
                semantic_text_processing_signature(
                    pipeline_version=SEMANTIC_PIPELINE_VERSION,
                    chunking_signature=planning_chunking_signature,
                    source_kinds=selected_sources,
                ),
            )
        )
    if scope in {"image", "all"}:
        specs.append(
            _WorkloadSpec(
                "image",
                clip_image_model(),
                EmbeddingRole.IMAGE,
                (
                    f"{SEMANTIC_PIPELINE_VERSION}|{IMAGE_SOURCE_ADAPTER_VERSION}|images|"
                    f"enumeration={SEMANTIC_TEXT_ENUMERATION_PROTOCOL}"
                ),
            )
        )
        if embed_ocr_text:
            assert selected_text_model is not None
            assert active_chunking is not None
            assert planning_chunking_signature is not None
            specs.append(
                _WorkloadSpec(
                    "image_ocr",
                    selected_text_model,
                    EmbeddingRole.PASSAGE,
                    (
                        f"{SEMANTIC_PIPELINE_VERSION}|{IMAGE_SOURCE_ADAPTER_VERSION}|"
                        f"image-ocr|{planning_chunking_signature}|"
                        f"quality-policy={SEMANTIC_TEXT_QUALITY_POLICY}|"
                        f"enumeration={SEMANTIC_TEXT_ENUMERATION_PROTOCOL}"
                    ),
                )
            )
    _validate_workload_specs(specs)
    return tuple(specs)


def _prepare_plan_configuration(
    *,
    scope: str,
    source_kinds: Sequence[str],
    text_model: EmbeddingModelSpec | None,
    embed_ocr_text: bool,
    chunking: TextChunkingConfig | None,
    execution_signature: str | None,
    scratch_directory: Path | None,
    max_scratch_bytes: int,
) -> _PlanConfiguration:
    selected_sources = _validate_plan_options(
        scope=scope,
        source_kinds=source_kinds,
        embed_ocr_text=embed_ocr_text,
        execution_signature=execution_signature,
        scratch_directory=scratch_directory,
        max_scratch_bytes=max_scratch_bytes,
    )
    selected_text_model, active_chunking = _resolve_text_contract(
        scope=scope,
        text_model=text_model,
        embed_ocr_text=embed_ocr_text,
        chunking=chunking,
    )
    workload_specs = _build_workload_specs(
        scope=scope,
        selected_sources=selected_sources,
        selected_text_model=selected_text_model,
        active_chunking=active_chunking,
        embed_ocr_text=embed_ocr_text,
    )
    return _PlanConfiguration(
        scope=scope,
        selected_sources=selected_sources,
        active_chunking=active_chunking,
        workload_specs=workload_specs,
    )


def _cost_calibrations_by_key(
    calibrations: Sequence[SemanticCostCalibration],
) -> dict[tuple[str, str, str, str, str], SemanticCostCalibration]:
    indexed: dict[tuple[str, str, str, str, str], SemanticCostCalibration] = {}
    for calibration in calibrations:
        snapshot = SemanticCostCalibration(
            calibration_signature=calibration.calibration_signature,
            execution_signature=calibration.execution_signature,
            processing_signature=calibration.processing_signature,
            workload=calibration.workload,
            model_signature=calibration.model_signature,
            role=calibration.role,
            contents_per_second=calibration.contents_per_second,
            sample_contents=calibration.sample_contents,
            sample_input_bytes=calibration.sample_input_bytes,
        )
        key = (
            snapshot.execution_signature,
            snapshot.processing_signature,
            snapshot.workload,
            snapshot.model_signature,
            snapshot.role,
        )
        if key in indexed:
            raise ValueError("duplicate exact semantic cost calibration")
        indexed[key] = snapshot
    return indexed


def _freeze_workload(
    spec: _WorkloadSpec,
    summary: Mapping[str, int],
    *,
    calibrations: Mapping[tuple[str, str, str, str, str], SemanticCostCalibration],
    execution_signature: str | None,
) -> SemanticWorkloadPlan:
    new_unique = summary["new_unique"]
    uncached_entities = summary["uncached_embedding_entities"]
    calibration = None
    if execution_signature is not None:
        calibration = calibrations.get(
            (
                execution_signature,
                spec.processing_signature,
                spec.name,
                spec.model.model_signature,
                spec.role.value,
            )
        )
    if uncached_entities == 0:
        estimated_seconds_lower = 0.0
        estimated_seconds_upper = 0.0
        unavailable_reason = None
        calibration_signature = None
        calibration_execution = None
        calibration_rate = None
        calibration_sample_contents = None
        calibration_sample_bytes = None
    elif calibration is None:
        estimated_seconds_lower = None
        estimated_seconds_upper = None
        unavailable_reason = "no_exact_cost_calibration"
        calibration_signature = None
        calibration_execution = None
        calibration_rate = None
        calibration_sample_contents = None
        calibration_sample_bytes = None
    else:
        estimated_seconds_lower = new_unique / calibration.contents_per_second
        estimated_seconds_upper = uncached_entities / calibration.contents_per_second
        unavailable_reason = None
        calibration_signature = calibration.calibration_signature
        calibration_execution = calibration.execution_signature
        calibration_rate = calibration.contents_per_second
        calibration_sample_contents = calibration.sample_contents
        calibration_sample_bytes = calibration.sample_input_bytes
    model = spec.model
    return SemanticWorkloadPlan(
        name=spec.name,
        modality=model.modality.value,
        role=spec.role.value,
        model_signature=model.model_signature,
        vector_space=model.vector_space,
        model_id=model.model_id,
        model_version=model.model_version,
        dimensions=model.dimensions,
        provider=model.provider,
        supported_roles=tuple(role.value for role in model.supported_roles),
        vector_dtype=model.vector_dtype.value,
        normalization=model.normalization,
        distance=model.distance,
        model_provenance_json=canonical_json(model.provenance),
        processing_signature=spec.processing_signature,
        embedding_entities=summary["embedding_entities"],
        unique_contents=summary["unique_contents"],
        preexisting_reusable_contents=summary["preexisting_reusable"],
        planned_reusable_contents=summary["planned_reusable"],
        new_unique_contents=new_unique,
        input_bytes=summary["input_bytes"],
        unique_input_bytes=summary["unique_input_bytes"],
        new_vector_blob_bytes_lower_bound=summary["vector_blob_bytes"],
        model_request_contents_lower_bound=new_unique,
        model_request_contents_upper_bound=uncached_entities,
        estimated_model_seconds_lower_bound=estimated_seconds_lower,
        estimated_model_seconds_upper_bound=estimated_seconds_upper,
        cost_calibration_signature=calibration_signature,
        cost_execution_signature=calibration_execution,
        cost_calibration_contents_per_second=calibration_rate,
        cost_calibration_sample_contents=calibration_sample_contents,
        cost_calibration_sample_input_bytes=calibration_sample_bytes,
        cost_unavailable_reason=unavailable_reason,
    )


def build_plan_signature_payload(
    *,
    algorithm_version: str,
    scope: str,
    selected_sources: tuple[str, ...],
    semantic_schema_version: int | None,
    source_plans: Sequence[SemanticSourcePlan],
    workloads: Sequence[SemanticWorkloadPlan],
    chunking_signature: str | None,
    content_set_xxh3_128: str,
    semantic_snapshot_xxh3_128: str,
) -> Mapping[str, object]:
    return {
        "algorithm": algorithm_version,
        "scope": scope,
        "selected_sources": list(selected_sources),
        "semantic_schema_version": semantic_schema_version,
        "semantic_snapshot_xxh3_128": semantic_snapshot_xxh3_128,
        "chunking_signature": chunking_signature,
        "content_set_xxh3_128": content_set_xxh3_128,
        "sources": [
            {
                "source_kind": source.source_kind,
                "schema_version": source.schema_version,
                "resources": source.resources,
                "sections": source.sections,
                "chunks": source.chunks,
                "embedding_entities": source.embedding_entities,
                "source_bytes": source.source_bytes,
                "section_text_bytes": source.section_text_bytes,
                "input_bytes": source.input_bytes,
                "snapshot_xxh3_128": source.snapshot_xxh3_128,
            }
            for source in source_plans
        ],
        "workloads": [
            {
                "name": workload.name,
                "model": {
                    "model_signature": workload.model_signature,
                    "vector_space": workload.vector_space,
                    "modality": workload.modality,
                    "model_id": workload.model_id,
                    "model_version": workload.model_version,
                    "dimensions": workload.dimensions,
                    "provider": workload.provider,
                    "supported_roles": list(workload.supported_roles),
                    "vector_dtype": workload.vector_dtype,
                    "normalization": workload.normalization,
                    "distance": workload.distance,
                    "provenance_json": workload.model_provenance_json,
                },
                "processing_signature": workload.processing_signature,
                "role": workload.role,
                "unique_contents": workload.unique_contents,
                "new_unique_contents": workload.new_unique_contents,
                "request_lower": workload.model_request_contents_lower_bound,
                "request_upper": workload.model_request_contents_upper_bound,
                "seconds_lower": workload.estimated_model_seconds_lower_bound,
                "seconds_upper": workload.estimated_model_seconds_upper_bound,
                "calibration": (
                    None
                    if workload.cost_calibration_signature is None
                    else {
                        "signature": workload.cost_calibration_signature,
                        "execution": workload.cost_execution_signature,
                        "contents_per_second": (workload.cost_calibration_contents_per_second),
                        "sample_contents": (workload.cost_calibration_sample_contents),
                        "sample_input_bytes": (workload.cost_calibration_sample_input_bytes),
                    }
                ),
                "cost_unavailable_reason": workload.cost_unavailable_reason,
            }
            for workload in workloads
        ],
    }


def _model_second_bounds(
    workloads: Sequence[SemanticWorkloadPlan],
) -> tuple[float | None, float | None]:
    available = all(
        workload.estimated_model_seconds_lower_bound is not None for workload in workloads
    )
    if not available:
        return None, None
    return (
        sum(workload.estimated_model_seconds_lower_bound or 0.0 for workload in workloads),
        sum(workload.estimated_model_seconds_upper_bound or 0.0 for workload in workloads),
    )


def assemble_semantic_plan(
    *,
    configuration: _PlanConfiguration,
    semantic_path: Path,
    result: _ScratchPlanResult,
    max_scratch_bytes: int,
    plan_algorithm_version: str,
    checkpoint: Callable[[], None],
) -> SemanticPlan:
    estimated_seconds_lower, estimated_seconds_upper = _model_second_bounds(result.workloads)
    resources = sum(source.resources for source in result.source_plans)
    sections = sum(source.sections for source in result.source_plans)
    chunks = sum(source.chunks for source in result.source_plans)
    embedding_entities = sum(source.embedding_entities for source in result.source_plans)
    source_bytes = sum(source.source_bytes for source in result.source_plans)
    section_text_bytes = sum(source.section_text_bytes for source in result.source_plans)
    checkpoint()
    return SemanticPlan(
        scope=configuration.scope,
        selected_sources=configuration.selected_sources,
        semantic_database=semantic_path,
        semantic_schema_version=result.semantic_schema_version,
        source_plans=result.source_plans,
        workloads=result.workloads,
        text_chunking_signature=(
            None
            if configuration.active_chunking is None
            else configuration.active_chunking.signature
        ),
        content_set_xxh3_128=result.content_set_xxh3_128,
        semantic_snapshot_xxh3_128=result.semantic_snapshot_xxh3_128,
        plan_signature=(f"{plan_algorithm_version}:xxh3-128:{result.plan_fingerprint}"),
        resources=resources,
        sections=sections,
        chunks=chunks,
        embedding_entities=embedding_entities,
        source_bytes=source_bytes,
        section_text_bytes=section_text_bytes,
        input_bytes=result.global_summary["input_bytes"],
        unique_contents=result.global_summary["unique_contents"],
        unique_input_bytes=result.global_summary["unique_input_bytes"],
        reusable_unique_contents=result.global_summary["reusable_unique_contents"],
        new_unique_contents=result.global_summary["new_unique_contents"],
        new_vector_blob_bytes_lower_bound=result.global_summary["vector_blob_bytes"],
        model_request_contents_lower_bound=result.global_summary["new_unique_contents"],
        model_request_contents_upper_bound=sum(
            workload.model_request_contents_upper_bound for workload in result.workloads
        ),
        estimated_model_seconds_lower_bound=estimated_seconds_lower,
        estimated_model_seconds_upper_bound=estimated_seconds_upper,
        scratch_storage_bytes=result.scratch_storage_bytes,
        max_scratch_bytes=max_scratch_bytes,
        originals_verified=(False if configuration.scope in {"image", "all"} else None),
        execution_ready=None,
        estimate_kind=(
            "model_only_request_range_from_exact_content_projection"
            if configuration.active_chunking is None
            or configuration.active_chunking.model_token_limit is not None
            else "model_only_request_range_from_pre_tokenizer_content_projection"
        ),
    )


# endregion [02]
