"""Stable facade for Neocortex semantic indexing, retrieval and evidence."""

from __future__ import annotations
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypeVar, TypedDict

if TYPE_CHECKING:
    from .semantic_exact_index import ExactIndexHandle

from neocortex.progress import ProgressCallback

from . import semantic_classification_service as _classification
from . import semantic_generation_worker as _worker
from . import semantic_image_index as _image_index
from . import semantic_planner as _planner
from . import semantic_preparation as _preparation
from . import semantic_search_service as _search
from . import semantic_status_service as _status
from . import semantic_text_index as _text_index
from .semantic_backend_supervisor import DeadlineEmbeddingBackend
from .semantic_resources import (
    CoordinatedEmbeddingBackend as _CoordinatedEmbeddingBackend,
    governed_retrieval as _governed_retrieval,
    model_resident_estimate as _model_resident_estimate,
    semantic_backend_gate as _semantic_backend_gate,
)
from .semantic_backends import (
    EmbeddingBackend as EmbeddingBackend,
    FastEmbedBackend as FastEmbedBackend,
    SourceRevisionMismatchError as SourceRevisionMismatchError,
    TextTokenLimitExceededError as TextTokenLimitExceededError,
    reciprocal_rank_fusion as reciprocal_rank_fusion,
)
from .semantic_chunking import (
    TextChunkingConfig as TextChunkingConfig,
    iter_text_chunks as iter_text_chunks,
)
from .semantic_admission import ContentAdmissionPolicy
from .semantic_config import (
    COMPACT_TEXT_MODEL_SIGNATURE as COMPACT_TEXT_MODEL_SIGNATURE,
    SEMANTIC_PIPELINE_VERSION as SEMANTIC_PIPELINE_VERSION,
    clip_image_model as clip_image_model,
    clip_text_model as clip_text_model,
    default_semantic_model_cache as default_semantic_model_cache,
    default_semantic_threads as default_semantic_threads,
    multilingual_text_model as multilingual_text_model,
    production_models as production_models,
    text_chunking_for_model as text_chunking_for_model,
)
from .semantic_lexical import (
    LexicalRanking as LexicalRanking,
    LexicalStatePaths as LexicalStatePaths,
    search_lexical_sources as search_lexical_sources,
)
from .semantic_models import (
    BackendEmbedding as BackendEmbedding,
    CalibrationStatus as CalibrationStatus,
    EmbeddingJobLease as EmbeddingJobLease,
    EmbeddingModality as EmbeddingModality,
    EmbeddingModelSpec as EmbeddingModelSpec,
    EmbeddingRequest as EmbeddingRequest,
    EmbeddingRole as EmbeddingRole,
    EvidenceDisposition as EvidenceDisposition,
    ExactSearchQuery as ExactSearchQuery,
    FusedHit as FusedHit,
    GenerationSummary as GenerationSummary,
    LabelPrototype as LabelPrototype,
    ResolvedSearchHit as ResolvedSearchHit,
    SearchHit as SearchHit,
    SemanticEvidence as SemanticEvidence,
    StoredLabelPrototype as StoredLabelPrototype,
    TextSection as TextSection,
    fingerprint_bytes as fingerprint_bytes,
    fingerprint_text as fingerprint_text,
)
from .semantic_ontology import (
    ONTOLOGY_VERSION as ONTOLOGY_VERSION,
    ConceptSpec as ConceptSpec,
    all_concepts as all_concepts,
    expand_domain_query as expand_domain_query,
)
from .semantic_planner import SemanticPlanBlocked as SemanticPlanBlocked
from .semantic_schema import initialize_semantic_state as initialize_semantic_state
from .image_retrieval_calibration import (
    ImageCalibrationError as ImageCalibrationError,
    ImageCalibrationEvidence as ImageCalibrationEvidence,
    current_image_processing_signature as current_image_processing_signature,
    load_image_retrieval_calibration as load_image_retrieval_calibration,
    measure_image_retrieval_calibration as measure_image_retrieval_calibration,
    persist_image_retrieval_calibration as persist_image_retrieval_calibration,
)
from .semantic_service_contracts import (
    DEFAULT_SEARCH_MAX_VECTORS as DEFAULT_SEARCH_MAX_VECTORS,
    EVIDENCE_PAGE_SIZE as EVIDENCE_PAGE_SIZE,
    IMAGE_OCR_TEXT_CHANNEL as IMAGE_OCR_TEXT_CHANNEL,
    JOB_BATCH_SIZE as JOB_BATCH_SIZE,
    LEASE_HEARTBEAT_INTERVAL_SECONDS as LEASE_HEARTBEAT_INTERVAL_SECONDS,
    LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS as LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS,
    MAX_LEXICAL_CANDIDATE_HITS as MAX_LEXICAL_CANDIDATE_HITS,
    MAX_SEMANTIC_CANDIDATE_HITS as MAX_SEMANTIC_CANDIDATE_HITS,
    MIN_ADVISORY_EVIDENCE_SCORE as MIN_ADVISORY_EVIDENCE_SCORE,
    SEARCH_RESOLUTION_BATCH_SIZE as SEARCH_RESOLUTION_BATCH_SIZE,
    SEMANTIC_DATABASE_NAME as SEMANTIC_DATABASE_NAME,
    SEMANTIC_ONTOLOGY_ID as SEMANTIC_ONTOLOGY_ID,
    SEMANTIC_PROTOTYPE_VERSION as SEMANTIC_PROTOTYPE_VERSION,
    STAGING_BATCH_SIZE as STAGING_BATCH_SIZE,
    WORKER_LEASE_SECONDS as WORKER_LEASE_SECONDS,
    FusedResolvedHit as FusedResolvedHit,
    GenerationWorkResult as GenerationWorkResult,
    ImageRetrievalCalibration as ImageRetrievalCalibration,
    ModelPreparation as ModelPreparation,
    SemanticClassificationResult as SemanticClassificationResult,
    SemanticEvidencePassResult as SemanticEvidencePassResult,
    SemanticIndexResult as SemanticIndexResult,
    SemanticCostCalibration as SemanticCostCalibration,
    SemanticPlan as SemanticPlan,
    SemanticSourcePlan as SemanticSourcePlan,
    SemanticWorkloadPlan as SemanticWorkloadPlan,
    SemanticRanking as SemanticRanking,
    SemanticSearchResult as SemanticSearchResult,
    SemanticStatus as SemanticStatus,
)
from .semantic_sources import (
    IMAGE_SOURCE_KIND as IMAGE_SOURCE_KIND,
    SOURCE_ADAPTER_VERSION as SOURCE_ADAPTER_VERSION,
    TEXT_SOURCE_KINDS as TEXT_SOURCE_KINDS,
    ImageSourceRecord as ImageSourceRecord,
    TextSourceRecord as TextSourceRecord,
    filter_text_source_records,
    iter_image_source_records as iter_image_source_records,
    iter_text_source_records as iter_text_source_records,
    semantic_source_database as semantic_source_database,
)
from .semantic_work_budget import SemanticWorkBudget
from .semantic_state import (
    StaleEmbeddingJobError as StaleEmbeddingJobError,
    claim_embedding_jobs as claim_embedding_jobs,
    complete_embedding_job as complete_embedding_job,
    deactivate_semantic_item_if_fingerprint as deactivate_semantic_item_if_fingerprint,
    deactivate_text_chunks_for_item as deactivate_text_chunks_for_item,
    embedding_request_from_lease as embedding_request_from_lease,
    enqueue_image_item_jobs as enqueue_image_item_jobs,
    enqueue_text_chunk_jobs as enqueue_text_chunk_jobs,
    fail_embedding_job as fail_embedding_job,
    finalize_embedding_generation as finalize_embedding_generation,
    finalize_label_prototype_refresh as finalize_label_prototype_refresh,
    finalize_semantic_evidence_model_refresh as finalize_semantic_evidence_model_refresh,
    finalize_semantic_item_refresh as finalize_semantic_item_refresh,
    finalize_text_chunk_refresh as finalize_text_chunk_refresh,
    generation_summary as generation_summary,
    has_active_embeddings as has_active_embeddings,
    heartbeat_embedding_jobs as heartbeat_embedding_jobs,
    iter_active_embedding_pages as iter_active_embedding_pages,
    load_embedding_model as load_embedding_model,
    load_label_prototypes as load_label_prototypes,
    publish_semantic_evidence_entities as publish_semantic_evidence_entities,
    publish_text_channel_revision as publish_text_channel_revision,
    register_embedding_model as register_embedding_model,
    resolve_search_hits as resolve_search_hits,
    reuse_cached_jobs as reuse_cached_jobs,
    search_exact_page as search_exact_page,
    stage_label_prototypes as stage_label_prototypes,
    stage_semantic_items as stage_semantic_items,
    stage_text_chunks as stage_text_chunks,
    start_embedding_generation as start_embedding_generation,
    update_embedding_generation_cursor as update_embedding_generation_cursor,
    upsert_semantic_item as upsert_semantic_item,
)

_T = TypeVar("_T")

_ADMISSION_POLICY: ContextVar[ContentAdmissionPolicy | None] = ContextVar(
    "neocortex_semantic_admission_policy",
    default=None,
)


@contextmanager
def admission_policy_scope(policy: ContentAdmissionPolicy | None) -> Iterator[None]:
    """Temporarily bind a Framework-owned admission policy to this stage.

    The public service signatures remain backward-compatible; lifecycle
    callers opt into a persisted policy through this narrow context boundary.
    """

    if policy is not None and not isinstance(policy, ContentAdmissionPolicy):
        raise TypeError("admission policy scope requires ContentAdmissionPolicy")
    token = _ADMISSION_POLICY.set(policy)
    try:
        yield
    finally:
        _ADMISSION_POLICY.reset(token)


def _active_admission_policy() -> ContentAdmissionPolicy:
    return _ADMISSION_POLICY.get() or ContentAdmissionPolicy()


def _admitted_text_source_iterator(
    policy: ContentAdmissionPolicy,
) -> Callable[[Path, str], Iterator[TextSourceRecord]]:
    """Use the service's compatibility seam before applying visibility."""

    def iterator(state_directory: Path, source_kind: str) -> Iterator[TextSourceRecord]:
        # ``iter_text_source_records`` is intentionally a module-level alias;
        # focused callers historically monkeypatch it, so do not bypass that
        # seam by closing over the lower-level module directly.
        yield from filter_text_source_records(
            iter_text_source_records(state_directory, source_kind),
            policy,
        )

    return iterator


def _admitted_image_source_iterator(
    policy: ContentAdmissionPolicy,
) -> Callable[[Path], Iterator[ImageSourceRecord]]:
    """Use the image compatibility seam before applying visibility."""

    def iterator(state_directory: Path) -> Iterator[ImageSourceRecord]:
        yield from filter_text_source_records(
            iter_image_source_records(state_directory),
            policy,
        )

    return iterator


class _OptionalExactIndexKwargs(TypedDict, total=False):
    exact_index: NotRequired[ExactIndexHandle]


def _optional_exact_index_kwargs(
    exact_index: ExactIndexHandle | None,
) -> _OptionalExactIndexKwargs:
    if exact_index is None:
        return {}
    return {"exact_index": exact_index}

__all__ = (
    "DEFAULT_SEARCH_MAX_VECTORS",
    "EVIDENCE_PAGE_SIZE",
    "IMAGE_OCR_TEXT_CHANNEL",
    "SEMANTIC_DATABASE_NAME",
    "SEMANTIC_ONTOLOGY_ID",
    "SEMANTIC_PROTOTYPE_VERSION",
    "FusedResolvedHit",
    "GenerationWorkResult",
    "ImageRetrievalCalibration",
    "ModelPreparation",
    "SemanticClassificationResult",
    "SemanticCostCalibration",
    "SemanticEvidencePassResult",
    "SemanticIndexResult",
    "SemanticPlan",
    "SemanticPlanBlocked",
    "SemanticRanking",
    "SemanticSearchResult",
    "SemanticSourcePlan",
    "SemanticStatus",
    "SemanticWorkloadPlan",
    "calibrate_image_retrieval",
    "classify_semantic_index",
    "index_image_embeddings",
    "index_text_embeddings",
    "plan_semantic_index",
    "prepare_semantic_models",
    "search_semantic_index",
    "semantic_plan_payload",
    "semantic_status",
)


# region [01] Preparation facade


def _model_cache(state_directory: Path, override: Path | None) -> Path:
    return _preparation.model_cache(state_directory, override)


def _backend(
    model: EmbeddingModelSpec,
    *,
    cache_dir: Path,
    local_files_only: bool,
    threads: int | None,
) -> EmbeddingBackend:
    from neocortex.runtime.control.read_operation import current_read_operation
    operation = current_read_operation()
    if _semantic_backend_gate() is not None:
        return _coordinated_backend(
            model, cache_dir=cache_dir, local_files_only=local_files_only,
            threads=threads, checkpoint=None if operation is None else operation.checkpoint,
        )
    if operation is not None and operation.requires_supervision:
        operation.checkpoint()
        # The originating allowance remains the only deadline/clock and
        # exception authority. Polling its checkpoint terminates native work
        # without converting Knowledge's typed failure into an index error.
        work = SemanticWorkBudget(cancellation_check=operation.checkpoint)
        supervised = DeadlineEmbeddingBackend(
            model, cache_dir=cache_dir, local_files_only=local_files_only,
            threads=threads, work_budget=work,
        )
        operation.closers.append(supervised.close)
        return supervised
    return _preparation.backend(
        model,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        threads=threads,
    )


def _coordinated_backend(
    model: EmbeddingModelSpec, *, cache_dir: Path, local_files_only: bool,
    threads: int | None, checkpoint: Callable[[], None] | None,
) -> EmbeddingBackend:
    gate = _semantic_backend_gate()
    assert gate is not None
    if local_files_only and model.provider.startswith("fastembed"):
        _preparation.require_local_fastembed_model(model, cache_dir)
    try:
        resident_bytes = _model_resident_estimate(model, cache_dir)
    except _preparation.SemanticModelUnavailableError:
        if local_files_only:
            raise
        # Explicit model preparation can precede the local snapshot.  Bound
        # that cold load conservatively without changing download authority.
        resident_bytes = max(256 * 1024 * 1024, model.dimensions * 4 * 1024 * 1024)

    def check() -> None:
        from .semantic_source_budget import source_read_checkpoint

        source_read_checkpoint()
        if checkpoint is not None:
            checkpoint()
        gate.coordinator.cancellation.checkpoint()

    def build(selected_threads: int) -> EmbeddingBackend:
        # A supervised child permits cancellation during native work and
        # supplies a verifiable PID for resident memory attribution.  The
        # caller's original checkpoint remains the deadline authority.
        return DeadlineEmbeddingBackend(
            model, cache_dir=cache_dir, local_files_only=local_files_only,
            threads=selected_threads,
            work_budget=SemanticWorkBudget(cancellation_check=check),
        )

    return _CoordinatedEmbeddingBackend(
        model, build, gate=gate, max_threads=threads,
        resident_bytes=resident_bytes, checkpoint=check,
    )


def _index_backend_factory(work_budget: SemanticWorkBudget):
    def create(
        model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool,
        threads: int | None,
    ) -> EmbeddingBackend:
        if (work_budget.deadline is not None and _semantic_backend_gate() is not None
            and model.provider.startswith("fastembed")):
            return _coordinated_backend(
                model, cache_dir=cache_dir, local_files_only=local_files_only,
                threads=threads, checkpoint=work_budget.checkpoint,
            )
        if work_budget.deadline is None:
            return _backend(
                model,
                cache_dir=cache_dir,
                local_files_only=local_files_only,
                threads=threads,
            )
        backend = DeadlineEmbeddingBackend(
            model,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
            threads=threads,
            work_budget=work_budget,
        )
        work_budget.register_resource_closer(backend.close)
        return backend

    return create


def _text_probe(backend: EmbeddingBackend) -> None:
    _preparation.text_probe(backend)


def _image_probe(backend: EmbeddingBackend) -> None:
    _preparation.image_probe(backend)


@_governed_retrieval("semantic")
def prepare_semantic_models(
    state_directory: Path,
    *,
    model_cache: Path | None = None,
    include_compact: bool = False,
    local_files_only: bool = False,
    threads: int | None = None,
) -> tuple[ModelPreparation, ...]:
    """Acquire/load explicit production models; this never indexes user content."""

    return _preparation.prepare_semantic_models(
        state_directory,
        model_cache_override=model_cache,
        include_compact=include_compact,
        local_files_only=local_files_only,
        threads=threads,
        backend_factory=_backend,
    )


def _initialize_models(
    database: Path,
    models: Iterable[EmbeddingModelSpec],
) -> None:
    _preparation.initialize_models(database, models)


def _require_source_databases(
    state_directory: Path,
    source_kinds: Iterable[str],
) -> None:
    _preparation.require_source_databases(state_directory, source_kinds)


# endregion [01]


# region [02] Bounded generation facade


def _batches(values: Iterable[_T], size: int) -> Iterator[tuple[_T, ...]]:
    return _worker.batches(values, size)


def _safe_error(exc: BaseException) -> str:
    return _worker.safe_error(exc)


def _embed_requests_isolated(
    backend: EmbeddingBackend,
    requests: Sequence[EmbeddingRequest],
) -> tuple[
    tuple[tuple[int, BackendEmbedding], ...],
    tuple[tuple[int, Exception], ...],
]:
    return _worker.embed_requests_isolated(backend, requests)


def _embed_requests_with_heartbeat(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    *,
    worker_id: str,
    backend: EmbeddingBackend,
    requests: Sequence[EmbeddingRequest],
    lease_seconds: float = WORKER_LEASE_SECONDS,
    heartbeat_interval_seconds: float = LEASE_HEARTBEAT_INTERVAL_SECONDS,
) -> tuple[
    tuple[tuple[int, BackendEmbedding], ...],
    tuple[tuple[int, Exception], ...],
]:
    return _worker.embed_requests_with_heartbeat(
        database,
        leases,
        worker_id=worker_id,
        backend=backend,
        requests=requests,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_jobs=heartbeat_embedding_jobs,
    )


def _run_generation(
    database: Path,
    generation_id: int,
    backend: EmbeddingBackend,
    *,
    queued: int,
    work_budget: SemanticWorkBudget,
    publish_if_complete: bool,
    progress: ProgressCallback | None = None,
) -> GenerationWorkResult:
    return _worker.run_generation(
        database,
        generation_id,
        backend,
        queued=queued,
        work_budget=work_budget,
        publish_if_complete=publish_if_complete,
        heartbeat_jobs=heartbeat_embedding_jobs,
        progress=progress,
    )


# endregion [02]


# region [03] Read-only planning and text/image indexing facade


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
    max_scratch_bytes: int = _planner.DEFAULT_MAX_SCRATCH_BYTES,
    cancellation_check: Callable[[], None] | None = None,
) -> SemanticPlan:
    """Project bounded Semantic work without creating or mutating owner state."""

    return _planner.plan_semantic_index(
        state_directory,
        scope=scope,
        source_kinds=source_kinds,
        text_model=text_model,
        embed_ocr_text=embed_ocr_text,
        chunking=chunking,
        cost_calibrations=cost_calibrations,
        execution_signature=execution_signature,
        scratch_directory=scratch_directory,
        max_scratch_bytes=max_scratch_bytes,
        cancellation_check=cancellation_check,
    )


def semantic_plan_payload(plan: SemanticPlan) -> dict[str, object]:
    """Return the stable JSON-ready Semantic plan representation."""

    return _planner.semantic_plan_payload(plan)


def _grouped_text_records(
    state_directory: Path,
    source_kind: str,
):
    return _text_index.grouped_text_records(
        state_directory,
        source_kind,
        source_record_iterator=iter_text_source_records,
    )


@_governed_retrieval("semantic")
def index_text_embeddings(
    state_directory: Path,
    *,
    source_kinds: Sequence[str] = TEXT_SOURCE_KINDS,
    model: EmbeddingModelSpec | None = None,
    model_cache: Path | None = None,
    local_files_only: bool = True,
    threads: int | None = None,
    chunking: TextChunkingConfig | None = None,
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> SemanticIndexResult:
    """Incrementally embed extracted text; source files are never rescanned."""

    budget = work_budget or SemanticWorkBudget()
    # Keep admission at the source boundary for every public indexing call.
    # The empty policy is deliberately equivalent to the historical iterator,
    # while callers with a persisted/corrected policy can pass it explicitly.
    policy = _active_admission_policy()
    from .semantic_source_budget import semantic_source_read_budget
    from .semantic_source_head_cache import source_head_receipts
    try:
        with semantic_source_read_budget(budget), source_head_receipts(state_directory / SEMANTIC_DATABASE_NAME):
            result = _text_index.index_text_embeddings(
                state_directory,
                source_kinds=source_kinds,
                model=model,
                model_cache_override=model_cache,
                local_files_only=local_files_only,
                threads=threads,
                chunking=chunking,
                backend_factory=_index_backend_factory(budget),
                source_record_iterator=_admitted_text_source_iterator(policy),
                generation_runner=partial(_run_generation, progress=progress),
                work_budget=budget,
                progress=progress,
            )
            if "code" in result.sources and result.complete:
                if len(result.generations) != 1:
                    raise RuntimeError("Code Semantic linking requires exactly one text generation")
                from neocortex.code.search.code_semantic_links import synchronize_code_embedding_links

                summary = result.generations[0].summary
                synchronize_code_embedding_links(
                    state_directory,
                    generation_id=summary.generation_id,
                    model_signature=summary.model_signature,
                )
            return result
    finally:
        budget.close_registered_resources()


@_governed_retrieval("semantic")
def index_image_embeddings(
    state_directory: Path,
    *,
    model_cache: Path | None = None,
    local_files_only: bool = True,
    threads: int | None = None,
    embed_ocr_text: bool = True,
    ocr_model: EmbeddingModelSpec | None = None,
    chunking: TextChunkingConfig | None = None,
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> SemanticIndexResult:
    """Index visual CLIP vectors and retained OCR in separate compatible spaces."""

    budget = work_budget or SemanticWorkBudget()
    policy = _active_admission_policy()
    from .semantic_source_budget import semantic_source_read_budget
    from .semantic_source_head_cache import source_head_receipts
    try:
        with semantic_source_read_budget(budget), source_head_receipts(state_directory / SEMANTIC_DATABASE_NAME):
            return _image_index.index_image_embeddings(
                state_directory,
                model_cache_override=model_cache,
                local_files_only=local_files_only,
                threads=threads,
                embed_ocr_text=embed_ocr_text,
                ocr_model=ocr_model,
                chunking=chunking,
                backend_factory=_index_backend_factory(budget),
                source_record_iterator=_admitted_image_source_iterator(policy),
                generation_runner=partial(_run_generation, progress=progress),
                work_budget=budget,
                progress=progress,
            )
    finally:
        budget.close_registered_resources()


# endregion [03]


# region [04] Search facade


def _query_vector(
    model: EmbeddingModelSpec,
    query: str,
    *,
    cache_dir: Path,
    local_files_only: bool,
    threads: int | None,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[float, ...]:
    return _search.query_vector(
        model,
        query,
        cache_dir=cache_dir,
        local_files_only=local_files_only,
        threads=threads,
        backend_factory=_backend,
        cancellation_check=cancellation_check,
    )


def _registered_model_available(
    database: Path,
    expected: EmbeddingModelSpec,
) -> bool:
    return _search.registered_model_available(database, expected)


def _indexed_model_available(
    database: Path,
    expected: EmbeddingModelSpec,
) -> bool:
    return _search.indexed_model_available(database, expected)


def _unavailable_semantic_ranking(name: str, reason: str) -> SemanticRanking:
    return _search.unavailable_semantic_ranking(name, reason)


def _default_lexical_paths(state_directory: Path) -> LexicalStatePaths:
    return _search.default_lexical_paths(state_directory)


@_governed_retrieval("semantic")
def search_semantic_index(
    state_directory: Path,
    query: str,
    *,
    limit: int = 20,
    candidate_limit: int | None = None,
    max_vectors: int = DEFAULT_SEARCH_MAX_VECTORS,
    include_text: bool = True,
    include_title: bool = False,
    include_images: bool = True,
    include_lexical: bool = True,
    lexical_paths: LexicalStatePaths | None = None,
    semantic_database: Path | None = None,
    text_model: EmbeddingModelSpec | None = None,
    model_cache: Path | None = None,
    local_files_only: bool = True,
    threads: int | None = None,
    evidence_mode: bool = False,
    image_calibration: ImageRetrievalCalibration | None = None,
    image_query_intent: Literal["explicit_visual", "explicit_textual", "ambiguous"] | None = None,
    allow_ambiguous_images: bool = True,
    diagnostic_item_ids: tuple[str, ...] = (),
    cancellation_check: Callable[[], None] | None = None,
    exact_index: ExactIndexHandle | None = None,
) -> SemanticSearchResult:
    """Search incompatible spaces with discovery or concrete-evidence retention."""
    from .semantic_schema import semantic_read_context

    policy = _active_admission_policy()

    with semantic_read_context():
        result = _search.search_semantic_index(
            state_directory,
            query,
            limit=limit,
            candidate_limit=candidate_limit,
            max_vectors=max_vectors,
            include_text=include_text,
            include_title=include_title,
            include_images=include_images,
            include_lexical=include_lexical,
            lexical_paths=lexical_paths,
            semantic_database=semantic_database,
            text_model=text_model,
            model_cache_override=model_cache,
            local_files_only=local_files_only,
            threads=threads,
            backend_factory=_backend,
            lexical_search=search_lexical_sources,
            evidence_mode=evidence_mode,
            image_calibration=image_calibration,
            image_query_intent=image_query_intent,
            allow_ambiguous_images=allow_ambiguous_images,
            diagnostic_item_ids=diagnostic_item_ids,
            cancellation_check=cancellation_check,
            **_optional_exact_index_kwargs(exact_index),
        )
    # Query visibility is a projection over durable results.  Keep vectors and
    # owner evidence intact when a policy correction hides an item; only the
    # returned fused hits are filtered for this request.
    from .semantic_admission import filter_search_hits

    if not hasattr(result, "fused"):
        # Preserve the lightweight facade seam used by callers that replace
        # the lower-level search service with a sentinel during inspection.
        return result
    visible = tuple(filter_search_hits(result.fused, policy))
    if len(visible) == len(result.fused):
        return result
    return type(result)(result.query, result.rankings, result.lexical_rankings, visible)


@_governed_retrieval("semantic")
def calibrate_image_retrieval(
    state_directory: Path,
    dataset_path: Path,
    *,
    model_cache: Path | None = None,
    local_files_only: bool = True,
    threads: int | None = None,
    max_vectors: int = DEFAULT_SEARCH_MAX_VECTORS,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[ImageRetrievalCalibration, ImageCalibrationEvidence]:
    """Measure the durable local CLIP retrieval contract for one image head."""

    database = state_directory / SEMANTIC_DATABASE_NAME
    return measure_image_retrieval_calibration(
        database,
        dataset_path,
        cache=_model_cache(state_directory, model_cache),
        local_files_only=local_files_only,
        threads=threads,
        backend_factory=_backend,
        max_vectors=max_vectors,
        cancellation_check=cancellation_check,
    )


# endregion [04]


# region [05] Classification and evidence facade


def _classification_concepts(
    target_modality: EmbeddingModality,
) -> tuple[ConceptSpec, ...]:
    return _classification.classification_concepts(target_modality)


def _prototype_text(
    concept: ConceptSpec,
    target_modality: EmbeddingModality,
) -> str:
    return _classification.prototype_text(concept, target_modality)


def _label_prototype(
    concept: ConceptSpec,
    query_model: EmbeddingModelSpec,
    target_modality: EmbeddingModality,
) -> LabelPrototype:
    return _classification.label_prototype(concept, query_model, target_modality)


def _prepare_label_prototypes(
    database: Path,
    backend: EmbeddingBackend,
    *,
    target_modality: EmbeddingModality,
) -> tuple[StoredLabelPrototype, ...]:
    return _classification.prepare_label_prototypes(
        database,
        backend,
        target_modality=target_modality,
        concepts_provider=_classification_concepts,
    )


def _selected_prototype_indices(
    scores: Sequence[float],
    prototypes: Sequence[StoredLabelPrototype],
    concept_families: Mapping[str, str],
    max_evidence: int,
) -> tuple[int, ...]:
    return _classification.selected_prototype_indices(
        scores,
        prototypes,
        concept_families,
        max_evidence,
    )


@_governed_retrieval("semantic")
def classify_semantic_index(
    state_directory: Path,
    *,
    include_text: bool = True,
    include_images: bool = True,
    max_evidence_per_entity: int = 8,
    page_size: int = EVIDENCE_PAGE_SIZE,
    text_model: EmbeddingModelSpec | None = None,
    model_cache: Path | None = None,
    local_files_only: bool = True,
    threads: int | None = None,
) -> SemanticClassificationResult:
    """Materialize uncalibrated ontology suggestions without changing file policy."""

    return _classification.classify_semantic_index(
        state_directory,
        include_text=include_text,
        include_images=include_images,
        max_evidence_per_entity=max_evidence_per_entity,
        page_size=page_size,
        text_model=text_model,
        model_cache_override=model_cache,
        local_files_only=local_files_only,
        threads=threads,
        backend_factory=_backend,
        concepts_provider=_classification_concepts,
        prototype_selector=_selected_prototype_indices,
        evidence_publisher=publish_semantic_evidence_entities,
    )


# endregion [05]


# region [06] Read-only status facade


def semantic_status(state_directory: Path, *, generation_limit: int = 10) -> SemanticStatus:
    """Return bounded state counts without creating or migrating the database."""

    return _status.semantic_status(
        state_directory,
        generation_limit=generation_limit,
    )


# endregion [06]
