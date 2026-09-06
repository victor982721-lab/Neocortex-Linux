"""Incremental text indexing from durable extraction caches."""

from __future__ import annotations
import itertools
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from .semantic_chunking import TextChunkingConfig, TextTokenCounter
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    multilingual_text_model,
    text_chunking_for_model,
)
from .semantic_generation_repository import (
    _enqueue_text_chunk_batch_bounded,
    find_exact_published_generation,
    invalidate_embedding_generations_for_source_change,
    merge_source_head_ledger,
    published_source_head_ledger,
)
from .semantic_generation_worker import GenerationRunner
from .semantic_item_repository import (
    _finalize_semantic_item_refresh,
    _finalize_text_chunk_refresh,
    _stage_text_chunk_batch,
    _upsert_item,
)
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    SemanticItem,
    TextSection,
)
from .semantic_preparation import (
    BackendFactory,
    initialize_models,
    model_cache,
    require_source_databases,
    resolve_text_token_guard,
    text_probe,
)
from .semantic_quality import SEMANTIC_TEXT_QUALITY_POLICY, iter_semantic_text_chunks
from .semantic_service_contracts import (
    SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
    SEMANTIC_DATABASE_NAME,
    STAGING_BATCH_SIZE,
    GenerationWorkResult,
    SemanticIndexResult,
)
from .semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
    TextSourceRecord,
    iter_text_sections_with_metadata,
    semantic_source_heads,
    semantic_text_processing_signature,
    require_readable_source_heads,
)
from .semantic_schema import semantic_database
from .semantic_state import (
    generation_summary,
    prepare_embedding_generation,
    start_embedding_generation,
    update_embedding_generation_cursor,
)
from .semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
    unlimited_semantic_work_budget,
)
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

TextRecordIterator = Callable[[Path, str], Iterator[TextSourceRecord]]
SEMANTIC_PROGRESS_ITEM_INTERVAL = 25


# region [01] Source grouping and staging


def grouped_text_records(
    state_directory: Path,
    source_kind: str,
    *,
    source_record_iterator: TextRecordIterator,
) -> Iterator[tuple[str, Iterator[TextSourceRecord]]]:
    records = source_record_iterator(state_directory, source_kind)
    return itertools.groupby(records, key=lambda record: record.item.item_id)


class _SemanticTextStagingSession:
    """Stage one source through one connection and bounded transactions."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        generation_id: int,
        source_kind: str,
        refresh_token: str,
        chunking: TextChunkingConfig,
        token_counter: TextTokenCounter | None = None,
        cancellation: SQLiteCancellationBridge,
        work_budget: SemanticWorkBudget,
    ) -> None:
        self._connection = connection
        self._generation_id = generation_id
        self._source_kind = source_kind
        self._refresh_token = refresh_token
        self._chunking = chunking
        self._token_counter = token_counter
        self._cancellation = cancellation
        self._work_budget = work_budget
        self._transaction_chunks = 0
        self._transaction_items = 0

    def _begin(self) -> None:
        self._cancellation.checkpoint()
        if not self._connection.in_transaction:
            self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        if not self._connection.in_transaction:
            return
        self._cancellation.checkpoint()
        self._connection.commit()
        self._transaction_chunks = 0
        self._transaction_items = 0

    def stage_item(
        self,
        item: SemanticItem,
        sections: Iterable[TextSection],
    ) -> tuple[int, int, int, bool]:
        """Stage one item while committing oversized work in bounded slices."""

        self._begin()
        _upsert_item(
            self._connection,
            item,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
            invalidate_text_on_fingerprint_change=True,
        )
        chunks_staged = queued = new_jobs = 0
        chunks = iter_semantic_text_chunks(
            item.item_id,
            iter_text_sections_with_metadata(item, sections),
            self._chunking,
            token_counter=self._token_counter,
        )
        while True:
            capacity = STAGING_BATCH_SIZE - self._transaction_chunks
            batch = tuple(itertools.islice(chunks, capacity))
            if not batch:
                break
            self._begin()
            selected_ns = time.time_ns()
            chunks_staged += _stage_text_chunk_batch(
                self._connection,
                batch,
                refresh_token=self._refresh_token,
                updated_ns=selected_ns,
            )
            allowance = self._work_budget.new_job_allowance(STAGING_BATCH_SIZE)
            enqueue_result = _enqueue_text_chunk_batch_bounded(
                self._connection,
                self._generation_id,
                tuple(chunk.chunk_id for chunk in batch),
                max_new_jobs=allowance,
                now_ns=selected_ns,
            )
            queued += enqueue_result.touched
            new_jobs += enqueue_result.new_jobs
            self._work_budget.record_new_jobs(enqueue_result.new_jobs)
            self._work_budget.record_rebound_members(enqueue_result.rebound_members)
            self._transaction_chunks += len(batch)
            self._cancellation.checkpoint()
            if not enqueue_result.complete:
                self._work_budget.mark_job_limit()
                self._commit()
                return chunks_staged, queued, new_jobs, False
            if self._transaction_chunks >= STAGING_BATCH_SIZE:
                self._commit()

        self._begin()
        _finalize_text_chunk_refresh(
            self._connection,
            item_id=item.item_id,
            chunking_signature=self._chunking.signature,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
        )
        self._transaction_items += 1
        self._cancellation.checkpoint()
        if self._transaction_items >= STAGING_BATCH_SIZE:
            self._commit()
        return chunks_staged, queued, new_jobs, True

    def finalize_source(self) -> None:
        """Deactivate unseen source members only after the refresh completed."""

        self._begin()
        _finalize_semantic_item_refresh(
            self._connection,
            source_kind=self._source_kind,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
        )
        self._commit()


def _stage_source(
    database: Path,
    state_directory: Path,
    source_kind: str,
    *,
    generation_id: int,
    refresh_token: str,
    chunking: TextChunkingConfig,
    token_counter: TextTokenCounter | None = None,
    source_record_iterator: TextRecordIterator,
    work_budget: SemanticWorkBudget | None = None,
    cancellation_check: CancellationCheck | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, int, int, bool]:
    if not source_kind.strip() or not refresh_token.strip():
        raise ValueError("source_kind and refresh_token cannot be blank")
    budget = work_budget or unlimited_semantic_work_budget()
    source_items = chunks_staged = queued = 0
    source_new_jobs_before = budget.new_jobs_admitted
    groups = grouped_text_records(
        state_directory,
        source_kind,
        source_record_iterator=source_record_iterator,
    )

    def staging_checkpoint() -> None:
        if cancellation_check is not None:
            cancellation_check()
        budget.checkpoint()

    bridge = SQLiteCancellationBridge(staging_checkpoint)
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            f"stage:{source_kind}",
            f"Preparando texto {source_kind.upper()}",
            0,
            None,
            "documentos",
            metrics=(
                ProgressMetric("chunks", 0),
                ProgressMetric("queued_work", 0),
                ProgressMetric("new_jobs", 0),
            ),
        ),
    )
    with semantic_database(database) as connection:
        with sqlite_cancellation_scope(connection, bridge):
            session = _SemanticTextStagingSession(
                connection,
                generation_id=generation_id,
                source_kind=source_kind,
                refresh_token=refresh_token,
                chunking=chunking,
                token_counter=token_counter,
                cancellation=bridge,
                work_budget=budget,
            )
            source_complete = True
            for _item_id, grouped in groups:
                bridge.checkpoint()
                if not budget.try_admit_item():
                    source_complete = False
                    break
                iterator = iter(grouped)
                first = next(iterator)
                item = first.item
                item_rebounds_before = budget.rebound_members
                sections = itertools.chain(
                    (first.section,),
                    (record.section for record in iterator),
                )
                item_chunks, item_jobs, item_new_jobs, item_complete = session.stage_item(
                    item, sections
                )
                source_items += 1
                chunks_staged += item_chunks
                queued += item_jobs
                if source_items % SEMANTIC_PROGRESS_ITEM_INTERVAL == 0:
                    emit_progress(
                        progress,
                        ProgressEvent(
                            "semantic",
                            f"stage:{source_kind}",
                            f"Preparando texto {source_kind.upper()}",
                            source_items,
                            None,
                            "documentos",
                            metrics=(
                                ProgressMetric("chunks", chunks_staged),
                                ProgressMetric("queued_work", queued),
                                ProgressMetric(
                                    "new_jobs",
                                    budget.new_jobs_admitted - source_new_jobs_before,
                                ),
                            ),
                        ),
                    )
                if not item_complete:
                    source_complete = False
                    break
                if item_new_jobs == 0 and budget.rebound_members == item_rebounds_before:
                    budget.refund_replayed_item()
            if source_complete:
                session.finalize_source()
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            f"stage:{source_kind}",
            (
                f"Texto {source_kind.upper()} preparado"
                if source_complete
                else f"Texto {source_kind.upper()} pausado"
            ),
            source_items,
            source_items,
            "documentos",
            True,
            (
                ProgressMetric("chunks", chunks_staged),
                ProgressMetric("queued_work", queued),
                ProgressMetric(
                    "new_jobs",
                    budget.new_jobs_admitted - source_new_jobs_before,
                ),
            ),
        ),
    )
    return source_items, chunks_staged, queued, source_complete


# endregion [01]


# region [02] Text indexing orchestration


def index_text_embeddings(
    state_directory: Path,
    *,
    source_kinds: Sequence[str],
    model: EmbeddingModelSpec | None,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    chunking: TextChunkingConfig | None,
    backend_factory: BackendFactory,
    source_record_iterator: TextRecordIterator,
    generation_runner: GenerationRunner,
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> SemanticIndexResult:
    """Incrementally embed extracted text; source files are never rescanned."""

    budget = work_budget or unlimited_semantic_work_budget()
    new_jobs_before = budget.new_jobs_admitted
    selected_sources = tuple(dict.fromkeys(source_kinds))
    if not selected_sources or any(
        kind not in SEMANTIC_PLAN_TEXT_SOURCE_KINDS for kind in selected_sources
    ):
        raise ValueError("semantic text sources must name supported durable caches")
    selected_model = model or multilingual_text_model()
    if selected_model.modality is not EmbeddingModality.TEXT:
        raise ValueError("text indexing requires a text model")
    require_source_databases(state_directory, selected_sources)
    base_chunking = chunking or text_chunking_for_model(selected_model)
    database = state_directory / SEMANTIC_DATABASE_NAME
    source_heads = semantic_source_heads(state_directory, selected_sources)
    source_head_payload = [head.as_payload() for head in source_heads]
    replay_entry: dict[str, object] = {
        "channel": "text",
        "source_kinds": list(selected_sources),
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "base_chunking_signature": base_chunking.signature,
        "title_policy": SEMANTIC_TITLE_POLICY,
        "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
        "source_heads": source_head_payload,
    }
    replay_scope = "text:" + ",".join(selected_sources)
    if all(head.complete for head in source_heads):
        published = find_exact_published_generation(
            database,
            model_signature=selected_model.model_signature,
            required_source_head_ledger={replay_scope: replay_entry},
            writer_coordinated=True,
        )
        if published is not None:
            confirmed_heads = semantic_source_heads(state_directory, selected_sources)
            if confirmed_heads == source_heads:
                emit_progress(
                    progress,
                    ProgressEvent(
                        "semantic",
                        "exact-replay:text",
                        "Semantic texto reutilizado sin enumeración",
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
                    (GenerationWorkResult(published, 0, 0, 0, 0),),
                    new_jobs_staged=0,
                    execution_mode="exact_replay",
                    sources_reused=len(selected_sources),
                    sources_enumerated=0,
                )
            source_heads = confirmed_heads
            source_head_payload = [head.as_payload() for head in source_heads]
            replay_entry["source_heads"] = source_head_payload
    require_readable_source_heads(source_heads)
    source_head_ledger = merge_source_head_ledger(
        published_source_head_ledger(
            database,
            model_signature=selected_model.model_signature,
            writer_coordinated=True,
        ),
        scope_key=replay_scope,
        entry=replay_entry,
    )
    cache = model_cache(state_directory, model_cache_override)
    embedding_backend = backend_factory(
        selected_model,
        cache_dir=cache,
        local_files_only=local_files_only,
        threads=threads,
    )
    text_probe(embedding_backend)
    token_guard = resolve_text_token_guard(embedding_backend, selected_model)
    active_chunking = replace(
        base_chunking,
        model_token_limit=token_guard.token_limit,
        tokenizer_signature=token_guard.tokenizer_signature,
    )

    initialize_models(database, (selected_model,))
    processing_signature = semantic_text_processing_signature(
        pipeline_version=SEMANTIC_PIPELINE_VERSION,
        chunking_signature=active_chunking.signature,
        source_kinds=selected_sources,
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=selected_model.model_signature,
        processing_signature=processing_signature,
        provenance={
            "pipeline": SEMANTIC_PIPELINE_VERSION,
            "sources": list(selected_sources),
            "base_chunking_signature": base_chunking.signature,
            "title_policy": SEMANTIC_TITLE_POLICY,
            "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
            "source_heads": source_head_payload,
            "source_head_ledger": source_head_ledger,
            "chunking_signature": active_chunking.signature,
            "tokenizer_signature": token_guard.tokenizer_signature,
            "model_token_limit": token_guard.token_limit,
        },
        cursor={
            "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
            "enumeration_complete": False,
            "selected_sources": list(selected_sources),
            "completed_sources": [],
        },
        materialize_base=False,
    )
    resume_cursor = generation_summary(
        database,
        generation_id,
        writer_coordinated=True,
    ).cursor
    raw_completed_sources = resume_cursor.get("completed_sources", [])
    completed_sources = list(
        dict.fromkeys(
            source
            for source in raw_completed_sources
            if isinstance(source, str) and source in selected_sources
        )
    ) if isinstance(raw_completed_sources, list) else []
    items_staged = chunks_staged = queued = 0
    enumeration_complete = resume_cursor.get("enumeration_complete") is True
    for source_kind in selected_sources:
        if source_kind in completed_sources:
            continue
        refresh_token = f"generation:{generation_id}:source:{source_kind}"
        source_items, source_chunks, source_jobs, source_complete = _stage_source(
            database,
            state_directory,
            source_kind,
            generation_id=generation_id,
            refresh_token=refresh_token,
            chunking=active_chunking,
            token_counter=token_guard.counter,
            source_record_iterator=source_record_iterator,
            work_budget=budget,
            progress=progress,
        )
        items_staged += source_items
        chunks_staged += source_chunks
        queued += source_jobs
        if not source_complete:
            enumeration_complete = False
            update_embedding_generation_cursor(
                database,
                generation_id,
                cursor={
                    "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                    "enumeration_complete": False,
                    "selected_sources": list(selected_sources),
                    "completed_sources": completed_sources,
                    "current_source": source_kind,
                    "truncation_reason": budget.truncation_reason,
                },
            )
            break
        completed_sources.append(source_kind)
        update_embedding_generation_cursor(
            database,
            generation_id,
            cursor={
                "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                "enumeration_complete": False,
                "selected_sources": list(selected_sources),
                "completed_sources": completed_sources,
                "completed_source": source_kind,
                "items": source_items,
            },
        )

    if not enumeration_complete and set(completed_sources) == set(selected_sources):
        enumeration_complete = True

    if enumeration_complete:
        confirmed_heads = semantic_source_heads(state_directory, selected_sources)
        if confirmed_heads != source_heads:
            invalidate_embedding_generations_for_source_change(
                database,
                (generation_id,),
                expected_source_heads=source_head_payload,
                observed_source_heads=[head.as_payload() for head in confirmed_heads],
            )
            raise RuntimeError("semantic source heads changed during text enumeration")
        update_embedding_generation_cursor(
            database,
            generation_id,
            cursor={
                "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                "enumeration_complete": True,
                "selected_sources": list(selected_sources),
                "completed_sources": completed_sources,
            },
        )

    try:
        reused_summary = prepare_embedding_generation(
            database,
            generation_id,
            enumeration_complete=enumeration_complete,
            work_budget=budget,
        )
    except SemanticIndexDeadlineExceeded:
        result = GenerationWorkResult(
            generation_summary(database, generation_id, writer_coordinated=True),
            queued,
            0,
            0,
            0,
        )
    else:
        if reused_summary is None:
            result = generation_runner(
                database,
                generation_id,
                embedding_backend,
                queued=queued,
                work_budget=budget,
                publish_if_complete=enumeration_complete,
            )
        else:
            result = GenerationWorkResult(reused_summary, 0, 0, 0, 0)
    return SemanticIndexResult(
        database,
        selected_sources,
        items_staged,
        chunks_staged,
        (result,),
        new_jobs_staged=budget.new_jobs_admitted - new_jobs_before,
        execution_mode="enumerated",
        sources_reused=0,
        sources_enumerated=len(completed_sources),
        truncated=budget.truncated,
        truncation_reason=budget.truncation_reason,
    )


# endregion [02]
