"""Joint visual and bounded OCR indexing in separate vector spaces."""

from __future__ import annotations
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, HASH_ALGORITHM_64

from .semantic_chunking import TextChunkingConfig, TextTokenCounter
from .semantic_backends import EmbeddingBackend
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    clip_image_model,
    clip_text_model,
    multilingual_text_model,
    text_chunking_for_model,
)
from .semantic_generation_worker import GenerationRunner
from .semantic_schema import SemanticStateError
from .semantic_generation_repository import (
    find_exact_published_generation,
    has_building_embedding_generation,
    invalidate_embedding_generations_for_source_change,
    merge_source_head_ledger,
    published_source_head_ledger,
)
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    TextSection,
    fingerprint_text,
)
from .semantic_preparation import (
    BackendFactory,
    image_probe,
    initialize_models,
    model_cache,
    require_source_databases,
    resolve_text_token_guard,
    text_probe,
)
from .semantic_quality import SEMANTIC_TEXT_QUALITY_POLICY, iter_semantic_text_chunks
from .semantic_service_contracts import (
    IMAGE_OCR_TEXT_CHANNEL,
    SEMANTIC_DATABASE_NAME,
    STAGING_BATCH_SIZE,
    GenerationWorkResult,
    SemanticIndexResult,
)
from .semantic_sources import (
    IMAGE_SOURCE_ADAPTER_VERSION,
    IMAGE_SOURCE_KIND,
    SEMANTIC_SOURCE_HEAD_PROTOCOL,
    ImageSourceRecord,
    semantic_source_heads,
    require_readable_source_heads,
)
from .semantic_state import (
    deactivate_text_chunks_for_item,
    enqueue_image_item_jobs_bounded,
    enqueue_text_chunk_jobs_bounded,
    finalize_semantic_item_refresh,
    finalize_text_chunk_refresh,
    generation_summary,
    prepare_embedding_generation,
    publish_text_channel_revision,
    stage_semantic_items,
    stage_text_chunks,
    start_embedding_generation,
    update_embedding_generation_cursor,
)
from .semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
    unlimited_semantic_work_budget,
)

ImageRecordIterator = Callable[[Path], Iterator[ImageSourceRecord]]
SEMANTIC_PROGRESS_ITEM_INTERVAL = 25


@dataclass(frozen=True, slots=True)
class _ImageIndexSetup:
    database: Path
    image_model: EmbeddingModelSpec
    text_model: EmbeddingModelSpec
    image_backend: EmbeddingBackend
    text_backend: EmbeddingBackend | None
    active_chunking: TextChunkingConfig
    token_counter: TextTokenCounter | None
    image_generation_id: int
    ocr_generation_id: int | None
    initial_cursor: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ImageStageResult:
    items_staged: int
    chunks_staged: int
    image_queued: int
    ocr_queued: int
    enumeration_complete: bool


# region [01] Bounded image and OCR staging


def stage_image_batch(
    database: Path,
    records: Sequence[ImageSourceRecord],
    *,
    refresh_token: str,
    image_generation_id: int,
    ocr_generation_id: int | None,
    include_ocr_text: bool,
    chunking: TextChunkingConfig,
    token_counter: TextTokenCounter | None = None,
    work_budget: SemanticWorkBudget | None = None,
) -> tuple[int, int, int, int]:
    budget = work_budget or unlimited_semantic_work_budget()
    item_count = chunks_staged = image_jobs = text_jobs = 0
    for record in records:
        budget.checkpoint()
        item_count += stage_semantic_items(
            database,
            (record.item,),
            source_kind=IMAGE_SOURCE_KIND,
            refresh_token=refresh_token,
            batch_size=STAGING_BATCH_SIZE,
            invalidate_text_on_fingerprint_change=False,
        )
        budget.checkpoint()
        image_result = enqueue_image_item_jobs_bounded(
            database,
            image_generation_id,
            (record.item.item_id,),
            max_new_jobs=budget.new_job_allowance(STAGING_BATCH_SIZE),
            batch_size=STAGING_BATCH_SIZE,
        )
        image_jobs += image_result.touched
        budget.record_new_jobs(image_result.new_jobs)
        budget.record_rebound_members(image_result.rebound_members)
        budget.checkpoint()
        if not image_result.complete:
            budget.mark_job_limit()
            return item_count, chunks_staged, image_jobs, text_jobs
        if not include_ocr_text or record.ocr_section is None:
            budget.checkpoint()
            deactivate_text_chunks_for_item(
                database,
                item_id=record.item.item_id,
            )
            budget.checkpoint()
            continue
        if record.ocr_section.section_kind != IMAGE_OCR_TEXT_CHANNEL:
            raise ValueError("image OCR sections must use the stable image_ocr text channel")
        ocr_fingerprint = fingerprint_text(record.ocr_section.text)
        publish_text_channel_revision(
            database,
            item_id=record.item.item_id,
            channel=IMAGE_OCR_TEXT_CHANNEL,
            revision_token=(
                f"{HASH_ALGORITHM_128}={ocr_fingerprint.xxh3_128};"
                f"bytes={ocr_fingerprint.byte_count};"
                f"{HASH_ALGORITHM_64}-guard={ocr_fingerprint.xxh3_64_guard}"
            ),
        )
        sections: tuple[TextSection, ...] = (record.ocr_section,)
        chunks = tuple(
            iter_semantic_text_chunks(
                record.item.item_id,
                sections,
                chunking,
                token_counter=token_counter,
            )
        )
        if chunks:
            chunks_staged += stage_text_chunks(
                database,
                chunks,
                refresh_token=refresh_token,
                batch_size=STAGING_BATCH_SIZE,
            )
            budget.checkpoint()
            if ocr_generation_id is not None:
                text_result = enqueue_text_chunk_jobs_bounded(
                    database,
                    ocr_generation_id,
                    (chunk.chunk_id for chunk in chunks),
                    max_new_jobs=budget.new_job_allowance(len(chunks)),
                    batch_size=STAGING_BATCH_SIZE,
                )
                text_jobs += text_result.touched
                budget.record_new_jobs(text_result.new_jobs)
                budget.record_rebound_members(text_result.rebound_members)
                budget.checkpoint()
                if not text_result.complete:
                    budget.mark_job_limit()
                    return item_count, chunks_staged, image_jobs, text_jobs
        budget.checkpoint()
        finalize_text_chunk_refresh(
            database,
            item_id=record.item.item_id,
            chunking_signature=chunking.signature,
            refresh_token=refresh_token,
        )
        budget.checkpoint()
    return item_count, chunks_staged, image_jobs, text_jobs


# endregion [01]


# region [02] Generation setup and execution


def _start_image_generations(
    database: Path,
    *,
    image_model: EmbeddingModelSpec,
    text_model: EmbeddingModelSpec,
    embed_ocr_text: bool,
    chunking: TextChunkingConfig,
    image_provenance: Mapping[str, object],
    ocr_provenance: Mapping[str, object],
    work_budget: SemanticWorkBudget | None = None,
) -> tuple[int, int | None]:
    image_generation_id = start_embedding_generation(
        database,
        model_signature=image_model.model_signature,
        processing_signature=(
            f"{SEMANTIC_PIPELINE_VERSION}|{IMAGE_SOURCE_ADAPTER_VERSION}|images|"
            f"enumeration=bounded-v1|source-head={SEMANTIC_SOURCE_HEAD_PROTOCOL}"
        ),
        provenance=image_provenance,
        materialize_base=False,
        work_budget=work_budget,
    )
    if not embed_ocr_text:
        return image_generation_id, None
    ocr_generation_id = start_embedding_generation(
        database,
        model_signature=text_model.model_signature,
        processing_signature=(
            f"{SEMANTIC_PIPELINE_VERSION}|{IMAGE_SOURCE_ADAPTER_VERSION}|image-ocr|"
            f"{chunking.signature}|quality-policy={SEMANTIC_TEXT_QUALITY_POLICY}|"
            f"enumeration=bounded-v1|source-head={SEMANTIC_SOURCE_HEAD_PROTOCOL}"
        ),
        provenance={
            **ocr_provenance,
            "chunking_signature": chunking.signature,
            "tokenizer_signature": chunking.tokenizer_signature,
            "model_token_limit": chunking.model_token_limit,
            "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
        },
        materialize_base=False,
        work_budget=work_budget,
    )
    return image_generation_id, ocr_generation_id


def _prepare_image_index(
    state_directory: Path,
    *,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    embed_ocr_text: bool,
    ocr_model: EmbeddingModelSpec | None,
    chunking: TextChunkingConfig | None,
    backend_factory: BackendFactory,
    image_provenance: Mapping[str, object],
    ocr_provenance: Mapping[str, object],
    work_budget: SemanticWorkBudget | None = None,
) -> _ImageIndexSetup:
    require_source_databases(state_directory, (IMAGE_SOURCE_KIND,))
    image_model = clip_image_model()
    query_model = clip_text_model()
    text_model = ocr_model or multilingual_text_model()
    if text_model.modality is not EmbeddingModality.TEXT:
        raise ValueError("image OCR indexing requires a text model")
    base_chunking = chunking or text_chunking_for_model(text_model)
    active_chunking = base_chunking
    cache = model_cache(state_directory, model_cache_override)
    image_backend = backend_factory(
        image_model,
        cache_dir=cache,
        local_files_only=local_files_only,
        threads=threads,
    )
    image_probe(image_backend)
    text_backend = None
    exact_token_counter = None
    if embed_ocr_text:
        text_backend = backend_factory(
            text_model,
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
        )
        text_probe(text_backend)
        token_guard = resolve_text_token_guard(text_backend, text_model)
        exact_token_counter = token_guard.counter
        active_chunking = replace(
            base_chunking,
            model_token_limit=token_guard.token_limit,
            tokenizer_signature=token_guard.tokenizer_signature,
        )

    database = state_directory / SEMANTIC_DATABASE_NAME
    models = [image_model, query_model]
    if embed_ocr_text:
        models.append(text_model)
    initialize_models(database, models)
    image_generation_id, ocr_generation_id = _start_image_generations(
        database,
        image_model=image_model,
        text_model=text_model,
        embed_ocr_text=embed_ocr_text,
        chunking=active_chunking,
        image_provenance=image_provenance,
        ocr_provenance=ocr_provenance,
        work_budget=work_budget,
    )
    initial_cursor = {
        "protocol": "bounded-v1",
        "enumeration_complete": False,
        "selected_sources": ["image", "image-ocr"] if embed_ocr_text else ["image"],
    }
    update_embedding_generation_cursor(
        database,
        image_generation_id,
        cursor=initial_cursor,
    )
    if ocr_generation_id is not None:
        update_embedding_generation_cursor(
            database,
            ocr_generation_id,
            cursor=initial_cursor,
        )
    return _ImageIndexSetup(
        database,
        image_model,
        text_model,
        image_backend,
        text_backend,
        active_chunking,
        exact_token_counter,
        image_generation_id,
        ocr_generation_id,
        initial_cursor,
    )


def _stage_image_records(
    state_directory: Path,
    setup: _ImageIndexSetup,
    *,
    embed_ocr_text: bool,
    source_record_iterator: ImageRecordIterator,
    work_budget: SemanticWorkBudget,
    new_jobs_before: int,
    progress: ProgressCallback | None,
) -> _ImageStageResult:
    refresh_token = f"generation:{setup.image_generation_id}:source:image"
    items_staged = chunks_staged = 0
    image_queued = ocr_queued = 0
    records = source_record_iterator(state_directory)
    enumeration_complete = True
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            "stage:image",
            "Preparando imágenes Semantic",
            0,
            None,
            "imágenes",
            metrics=(
                ProgressMetric("chunks", 0),
                ProgressMetric("queued_work", 0),
                ProgressMetric("new_jobs", 0),
            ),
        ),
    )
    for record in records:
        if not work_budget.try_admit_item():
            enumeration_complete = False
            break
        item_new_jobs_before = work_budget.new_jobs_admitted
        item_rebounds_before = work_budget.rebound_members
        item_count, chunk_count, queued_images, queued_ocr = stage_image_batch(
            setup.database,
            (record,),
            refresh_token=refresh_token,
            image_generation_id=setup.image_generation_id,
            ocr_generation_id=setup.ocr_generation_id,
            include_ocr_text=embed_ocr_text,
            chunking=setup.active_chunking,
            token_counter=setup.token_counter,
            work_budget=work_budget,
        )
        items_staged += item_count
        chunks_staged += chunk_count
        image_queued += queued_images
        ocr_queued += queued_ocr
        if items_staged % SEMANTIC_PROGRESS_ITEM_INTERVAL == 0:
            emit_progress(
                progress,
                ProgressEvent(
                    "semantic",
                    "stage:image",
                    "Preparando imágenes Semantic",
                    items_staged,
                    None,
                    "imágenes",
                    metrics=(
                        ProgressMetric("chunks", chunks_staged),
                        ProgressMetric("queued_work", image_queued + ocr_queued),
                        ProgressMetric(
                            "new_jobs",
                            work_budget.new_jobs_admitted - new_jobs_before,
                        ),
                    ),
                ),
            )
        if work_budget.truncated:
            enumeration_complete = False
            break
        if (
            work_budget.new_jobs_admitted == item_new_jobs_before
            and work_budget.rebound_members == item_rebounds_before
        ):
            work_budget.refund_replayed_item()
    return _ImageStageResult(
        items_staged,
        chunks_staged,
        image_queued,
        ocr_queued,
        enumeration_complete,
    )


def _finalize_image_staging(
    setup: _ImageIndexSetup,
    stage: _ImageStageResult,
    *,
    embed_ocr_text: bool,
    work_budget: SemanticWorkBudget,
    new_jobs_before: int,
    progress: ProgressCallback | None,
) -> _ImageStageResult:
    enumeration_complete = stage.enumeration_complete
    refresh_token = f"generation:{setup.image_generation_id}:source:image"
    items_staged = stage.items_staged
    chunks_staged = stage.chunks_staged
    image_queued = stage.image_queued
    ocr_queued = stage.ocr_queued
    if enumeration_complete and work_budget.deadline_expired():
        enumeration_complete = False
    if enumeration_complete:
        finalize_semantic_item_refresh(
            setup.database,
            source_kind=IMAGE_SOURCE_KIND,
            refresh_token=refresh_token,
        )
        if work_budget.deadline_expired():
            enumeration_complete = False
    if enumeration_complete:
        update_embedding_generation_cursor(
            setup.database,
            setup.image_generation_id,
            cursor={
                "protocol": "bounded-v1",
                "enumeration_complete": True,
                "selected_sources": ["image", "image-ocr"] if embed_ocr_text else ["image"],
                "completed_source": "image",
                "items": items_staged,
            },
        )
    else:
        paused_cursor = {
            **setup.initial_cursor,
            "truncation_reason": work_budget.truncation_reason,
            "items": items_staged,
        }
        update_embedding_generation_cursor(
            setup.database,
            setup.image_generation_id,
            cursor=paused_cursor,
        )
        if setup.ocr_generation_id is not None:
            update_embedding_generation_cursor(
                setup.database,
                setup.ocr_generation_id,
                cursor=paused_cursor,
            )
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            "stage:image",
            (
                "Imágenes Semantic preparadas"
                if enumeration_complete
                else "Preparación de imágenes Semantic pausada"
            ),
            items_staged,
            items_staged,
            "imágenes",
            True,
            (
                ProgressMetric("chunks", chunks_staged),
                ProgressMetric("queued_work", image_queued + ocr_queued),
                ProgressMetric(
                    "new_jobs",
                    work_budget.new_jobs_admitted - new_jobs_before,
                ),
            ),
        ),
    )
    return replace(stage, enumeration_complete=enumeration_complete)


def _stage_image_sources(
    state_directory: Path,
    setup: _ImageIndexSetup,
    *,
    embed_ocr_text: bool,
    source_record_iterator: ImageRecordIterator,
    work_budget: SemanticWorkBudget,
    new_jobs_before: int,
    progress: ProgressCallback | None,
) -> _ImageStageResult:
    staged = _stage_image_records(
        state_directory,
        setup,
        embed_ocr_text=embed_ocr_text,
        source_record_iterator=source_record_iterator,
        work_budget=work_budget,
        new_jobs_before=new_jobs_before,
        progress=progress,
    )
    return _finalize_image_staging(
        setup,
        staged,
        embed_ocr_text=embed_ocr_text,
        work_budget=work_budget,
        new_jobs_before=new_jobs_before,
        progress=progress,
    )


def _run_image_generation(
    setup: _ImageIndexSetup,
    generation_id: int,
    backend: EmbeddingBackend,
    *,
    queued: int,
    enumeration_complete: bool,
    work_budget: SemanticWorkBudget,
    generation_runner: GenerationRunner,
) -> GenerationWorkResult:
    try:
        reused = prepare_embedding_generation(
            setup.database,
            generation_id,
            enumeration_complete=enumeration_complete,
            work_budget=work_budget,
        )
    except SemanticIndexDeadlineExceeded:
        return GenerationWorkResult(
            generation_summary(setup.database, generation_id, writer_coordinated=True),
            queued,
            0,
            0,
            0,
        )
    if reused is not None:
        return GenerationWorkResult(reused, 0, 0, 0, 0)
    return generation_runner(
        setup.database,
        generation_id,
        backend,
        queued=queued,
        work_budget=work_budget,
        publish_if_complete=enumeration_complete,
    )


def _run_image_generations(
    setup: _ImageIndexSetup,
    stage: _ImageStageResult,
    *,
    embed_ocr_text: bool,
    work_budget: SemanticWorkBudget,
    generation_runner: GenerationRunner,
) -> tuple[GenerationWorkResult, ...]:
    image_result = _run_image_generation(
        setup,
        setup.image_generation_id,
        setup.image_backend,
        queued=stage.image_queued,
        enumeration_complete=stage.enumeration_complete,
        work_budget=work_budget,
        generation_runner=generation_runner,
    )
    generation_results = [image_result]
    if setup.ocr_generation_id is not None and setup.text_backend is not None and embed_ocr_text:
        if stage.enumeration_complete:
            update_embedding_generation_cursor(
                setup.database,
                setup.ocr_generation_id,
                cursor={
                    "protocol": "bounded-v1",
                    "enumeration_complete": True,
                    "selected_sources": ["image", "image-ocr"],
                    "completed_source": "image-ocr",
                    "chunks": stage.chunks_staged,
                },
            )
        generation_results.append(
            _run_image_generation(
                setup,
                setup.ocr_generation_id,
                setup.text_backend,
                queued=stage.ocr_queued,
                enumeration_complete=stage.enumeration_complete,
                work_budget=work_budget,
                generation_runner=generation_runner,
            )
        )
    return tuple(generation_results)


def index_image_embeddings(
    state_directory: Path,
    *,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    embed_ocr_text: bool,
    ocr_model: EmbeddingModelSpec | None,
    chunking: TextChunkingConfig | None,
    backend_factory: BackendFactory,
    source_record_iterator: ImageRecordIterator,
    generation_runner: GenerationRunner,
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> SemanticIndexResult:
    """Index visual CLIP vectors and retained OCR in separate compatible spaces."""

    budget = work_budget or unlimited_semantic_work_budget()
    new_jobs_before = budget.new_jobs_admitted
    require_source_databases(state_directory, (IMAGE_SOURCE_KIND,))
    database = state_directory / SEMANTIC_DATABASE_NAME
    image_model = clip_image_model()
    text_model = ocr_model or multilingual_text_model()
    if text_model.modality is not EmbeddingModality.TEXT:
        raise ValueError("image OCR indexing requires a text model")
    base_chunking = chunking or text_chunking_for_model(text_model)
    source_heads = semantic_source_heads(state_directory, (IMAGE_SOURCE_KIND,))
    source_head_payload = [head.as_payload() for head in source_heads]
    image_scope = "image:vectors"
    image_entry: dict[str, object] = {
        "channel": "image-vector",
        "source_kinds": ["image"],
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "embed_ocr_text": embed_ocr_text,
        "source_heads": source_head_payload,
    }
    ocr_scope = "text:image-ocr"
    ocr_entry: dict[str, object] = {
        "channel": "text",
        "source_kinds": ["image-ocr"],
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "base_chunking_signature": base_chunking.signature,
        "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
        "source_heads": source_head_payload,
    }
    if all(head.complete for head in source_heads) and not (
        budget.preserve_existing_generations
        and any(
            has_building_embedding_generation(database, model_signature=model.model_signature)
            for model in ((image_model, text_model) if embed_ocr_text else (image_model,))
        )
    ):
        image_published = find_exact_published_generation(
            database,
            model_signature=image_model.model_signature,
            required_source_head_ledger={image_scope: image_entry},
        )
        ocr_published = (
            find_exact_published_generation(
                database,
                model_signature=text_model.model_signature,
                required_source_head_ledger={ocr_scope: ocr_entry},
            )
            if embed_ocr_text
            else None
        )
        if image_published is not None and (not embed_ocr_text or ocr_published is not None):
            confirmed_heads = semantic_source_heads(state_directory, (IMAGE_SOURCE_KIND,))
            if confirmed_heads == source_heads:
                selected_sources = ("image", "image-ocr") if embed_ocr_text else ("image",)
                summaries = [GenerationWorkResult(image_published, 0, 0, 0, 0)]
                if ocr_published is not None:
                    summaries.append(GenerationWorkResult(ocr_published, 0, 0, 0, 0))
                emit_progress(
                    progress,
                    ProgressEvent(
                        "semantic",
                        "exact-replay:image",
                        "Semantic imagen reutilizado sin enumeración",
                        len(selected_sources),
                        len(selected_sources),
                        "fuentes",
                        True,
                        (
                            ProgressMetric("sources", len(selected_sources)),
                            ProgressMetric("reused", len(selected_sources)),
                            ProgressMetric("new_jobs", 0),
                        ),
                    ),
                )
                return SemanticIndexResult(
                    database,
                    selected_sources,
                    0,
                    0,
                    tuple(summaries),
                    new_jobs_staged=0,
                    execution_mode="exact_replay",
                    sources_reused=len(selected_sources),
                    sources_enumerated=0,
                )
            source_heads = confirmed_heads
            source_head_payload = [head.as_payload() for head in source_heads]
            image_entry["source_heads"] = source_head_payload
            ocr_entry["source_heads"] = source_head_payload
    require_readable_source_heads(source_heads)
    image_ledger = merge_source_head_ledger(
        published_source_head_ledger(
            database,
            model_signature=image_model.model_signature,
            writer_coordinated=True,
        ),
        scope_key=image_scope,
        entry=image_entry,
    )
    ocr_ledger = merge_source_head_ledger(
        published_source_head_ledger(
            database,
            model_signature=text_model.model_signature,
            writer_coordinated=True,
        ),
        scope_key=ocr_scope,
        entry=ocr_entry,
    )
    image_provenance: dict[str, object] = {
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "source": "image",
        "source_heads": source_head_payload,
        "source_head_ledger": image_ledger,
    }
    ocr_provenance: dict[str, object] = {
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "source": "image-ocr",
        "base_chunking_signature": base_chunking.signature,
        "source_heads": source_head_payload,
        "source_head_ledger": ocr_ledger,
    }
    setup = _prepare_image_index(
        state_directory,
        model_cache_override=model_cache_override,
        local_files_only=local_files_only,
        work_budget=budget,
        threads=threads,
        embed_ocr_text=embed_ocr_text,
        ocr_model=ocr_model,
        chunking=chunking,
        backend_factory=backend_factory,
        image_provenance=image_provenance,
        ocr_provenance=ocr_provenance,
    )
    stage = _stage_image_sources(
        state_directory,
        setup,
        embed_ocr_text=embed_ocr_text,
        source_record_iterator=source_record_iterator,
        work_budget=budget,
        new_jobs_before=new_jobs_before,
        progress=progress,
    )
    confirmed_heads = semantic_source_heads(state_directory, (IMAGE_SOURCE_KIND,))
    if confirmed_heads != source_heads:
        if budget.preserve_existing_generations:
            raise SemanticStateError("source changed during recovery; generation was kept unpublished")
        invalidate_embedding_generations_for_source_change(
            setup.database,
            (setup.image_generation_id,)
            if setup.ocr_generation_id is None
            else (setup.image_generation_id, setup.ocr_generation_id),
            expected_source_heads=source_head_payload,
            observed_source_heads=[head.as_payload() for head in confirmed_heads],
        )
        raise RuntimeError("semantic source heads changed during image enumeration")
    generation_results = _run_image_generations(
        setup,
        stage,
        embed_ocr_text=embed_ocr_text,
        work_budget=budget,
        generation_runner=generation_runner,
    )
    return SemanticIndexResult(
        setup.database,
        ("image", "image-ocr") if embed_ocr_text else ("image",),
        stage.items_staged,
        stage.chunks_staged,
        generation_results,
        new_jobs_staged=budget.new_jobs_admitted - new_jobs_before,
        execution_mode="enumerated",
        sources_reused=0,
        sources_enumerated=1 if stage.enumeration_complete else 0,
        truncated=budget.truncated,
        truncation_reason=budget.truncation_reason,
    )


# endregion [02]
