"""Bounded document-level Semantic service for physical organization.

Fast Curation is intentionally not a Semantic index: it consumes compact
derived representations, embeds one bounded document view in normal use, and
returns a single calibrated decision or abstention.  It never moves files and
never opens the Full Semantic database.  Catalog persistence is supplied by an
explicit sink owned by the curation/persistence layer.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .fast_curation_contracts import (
    DECISION_SCHEMA,
    DocumentClassificationEvidence,
    FastCurationMetrics,
    FastCurationRunResult,
)

from .fast_curation_embeddings import (
    CurationRepresentation,
    DecisionSink,
    EmbeddingRequest,
    EmbeddingCache,
    FastCurationConfig,
    MemoryEmbeddingCache,
    MAX_METADATA_ITEMS,
    MAX_PATH_CONTEXT_CHARS,
    REPRESENTATION_VERSION,
    _backend_factory_default,
    _backend_model,
    _bounded_text,
    _cache_get,
    _cache_put,
    _capability_error,
    _checkpoint,
    _close_backend,
    _decision_key,
    _effective_batch_size,
    _embed_batches,
    _embedding_key,
    _input_iter,
    _field,
    _outputs_by_request,
    _rank_candidates,
    _score_matrix,
    _vector,
    coerce_representation,
    content_embedding_cache_key,
    curation_decision_cache_key,
    fit_text_to_model,
    prototype_embedding_cache_key,
)

from .fast_curation_policy import (
    CurationDecision,
    FAMILY_ORDER,
    FastCurationPolicy,
    PolicyDecision,
)
from .fast_curation_policy_bundle import (
    DEFAULT_POLICY_BUNDLE_DATA,
    DecisionVersionValidation,
    FastCurationPolicyBundle,
    POLICY_BUNDLE_SCHEMA,
    default_calibrated_policy,
    load_calibrated_policy,
    validate_record_versions,
)
from .fast_curation_prototypes import (
    FastCurationPrototype,
    PrototypeSet,
    controlled_kind_directory,
    controlled_kind_lookup,
    default_prototypes,
    prototype_set_from_records,
)
from .semantic_models import EmbeddingModelSpec, EmbeddingRole, fingerprint_text
from .semantic_preparation import model_cache


def _make_evidence(
    rep: CurationRepresentation,
    decision: PolicyDecision,
    *,
    model: EmbeddingModelSpec,
    prototypes: PrototypeSet,
    policy: FastCurationPolicy,
    source_path: str | None = None,
) -> DocumentClassificationEvidence:
    family_order = tuple(
        family
        for family in FAMILY_ORDER
        if family in decision.selected_by_family
    )
    selected = {
        family: decision.selected_by_family[family].concept_id for family in family_order
    }
    primary_family = family_order[0] if family_order else None
    top1 = None if primary_family is None else decision.top1_scores.get(primary_family)
    top2 = None if primary_family is None else decision.top2_scores.get(primary_family)
    margin = None if primary_family is None else decision.margins.get(primary_family)
    # Destination directory names are owned by Organization's controlled
    # ontology mapping.  Curation returns concept IDs only and never invents
    # a filesystem folder from model text.
    destination = None
    semantic = dict(decision.semantic_evidence)
    semantic.setdefault("score_kind", "cosine_similarity_not_probability")
    semantic.setdefault("model_signature", model.model_signature)
    semantic.setdefault("vector_space", model.vector_space)
    semantic.setdefault("prototype_set_fingerprint", prototypes.fingerprint)
    semantic.setdefault("document_role", "passage")
    semantic.setdefault("prototype_role", "query")
    semantic.setdefault("top_k", len(decision.candidates))
    return DocumentClassificationEvidence(
        document_id=rep.document_id,
        source_kind=rep.source_kind,
        file_key=rep.file_key,
        input_signature=rep.input_signature,
        ontology_id=prototypes.ontology_id,
        model_signature=model.model_signature,
        vector_space=model.vector_space,
        representation_version=rep.representation_version,
        representation_fingerprint=rep.representation_fingerprint,
        ontology_version=prototypes.ontology_version,
        prototype_version=prototypes.prototype_version,
        prototype_set_fingerprint=prototypes.fingerprint,
        policy_version=policy.policy_version,
        calibration_version=None if policy.calibration is None else policy.calibration.calibration_version,
        top_candidates=decision.candidates[:15],
        top1_score=top1,
        top2_score=top2,
        margin=margin,
        calibrated_decision=decision.decision.value,
        decision_reason=decision.reason,
        decision_confidence=decision.decision_confidence,
        confidence_kind=decision.confidence_kind,
        selected_by_family=selected,
        destination=destination,
        deterministic_evidence=rep.deterministic_evidence,
        semantic_evidence=semantic,
        metadata_evidence={"metadata_keys": tuple(sorted(str(key) for key in rep.metadata)[:MAX_METADATA_ITEMS])},
        structural_evidence=rep.structural_evidence,
        source_path=source_path or (rep.path_context or None),
        content_embedding_key=_embedding_key(rep, model),
        decision_cache_key=_decision_key(rep, model, prototypes, policy),
    )


class FastCurationSemantic:
    """Reusable invocation owner around :func:`run_fast_curation`.

    It carries only immutable execution choices and injected persistence seams;
    each ``run`` still receives the current root/input stream explicitly.
    """

    def __init__(
        self,
        config: FastCurationConfig,
        *,
        policy: FastCurationPolicy | None = None,
        prototypes: PrototypeSet | Iterable[FastCurationPrototype] | None = None,
        embedding_cache: EmbeddingCache | None = None,
        backend_factory: object | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.prototypes = prototypes
        self.embedding_cache = embedding_cache
        self.backend_factory = backend_factory

    def run(
        self,
        root: Path,
        state_directory: Path,
        *,
        framework_state: object | None = None,
        run_id: int | str | None = None,
        inputs: object = None,
        decision_sink: DecisionSink | None = None,
        policy_bundle: FastCurationPolicyBundle | None = None,
        cancellation: object | None = None,
        resource_coordinator: object | None = None,
    ) -> FastCurationRunResult:
        return run_fast_curation(
            self.config,
            root,
            state_directory,
            framework_state=framework_state,
            run_id=run_id,
            inputs=inputs,
            policy=self.policy,
            policy_bundle=policy_bundle,
            prototypes=self.prototypes,
            embedding_cache=self.embedding_cache,
            decision_sink=decision_sink,
            backend_factory=self.backend_factory,
            cancellation=cancellation,
            resource_coordinator=resource_coordinator,
        )


def run_fast_curation(
    config: FastCurationConfig,
    root: Path,
    state_directory: Path,
    *,
    framework_state: object | None = None,
    run_id: int | str | None = None,
    inputs: object = None,
    policy: FastCurationPolicy | None = None,
    policy_bundle: FastCurationPolicyBundle | None = None,
    prototypes: PrototypeSet | Iterable[FastCurationPrototype] | None = None,
    embedding_cache: EmbeddingCache | None = None,
    decision_sink: DecisionSink | None = None,
    backend_factory: object | None = None,
    cancellation: object | None = None,
    resource_coordinator: object | None = None,
) -> FastCurationRunResult:
    """Run bounded curation evidence before Organization.

    The function only calls the injected catalog decision sink.  It does not
    create organization plans, move files, update Full Semantic state, or use
    path names as semantic content.
    """

    root = Path(root)
    state_directory = Path(state_directory)
    if not root.is_dir():
        raise ValueError(f"curation root is not an existing directory: {root}")
    if not state_directory.is_dir():
        raise ValueError(f"state_directory is not an existing directory: {state_directory}")
    # The orchestrator owns the shared GlobalResourceCoordinator scope.  This
    # service never creates a competing coordinator; the handle is accepted so
    # callers can make that ownership explicit at the boundary.
    del resource_coordinator
    if policy_bundle is not None and not isinstance(policy_bundle, FastCurationPolicyBundle):
        raise TypeError("policy_bundle must be FastCurationPolicyBundle")
    selected_policy = policy
    if selected_policy is None and policy_bundle is not None:
        selected_policy = policy_bundle.policy
    if selected_policy is None and config.calibration is not None:
        if isinstance(config.calibration, FastCurationPolicyBundle):
            selected_policy = config.calibration.policy
        else:
            selected_policy = FastCurationPolicy.from_calibration(config.calibration)
    selected_policy = selected_policy or FastCurationPolicy()
    if prototypes is None:
        prototype_set = default_prototypes()
    elif isinstance(prototypes, PrototypeSet):
        prototype_set = prototypes
    else:
        prototype_values = tuple(prototypes)
        if prototype_values and all(isinstance(value, Mapping) for value in prototype_values):
            prototype_set = prototype_set_from_records(prototype_values)
        else:
            prototype_set = PrototypeSet(prototype_values)
    effective_bundle = policy_bundle or (
        config.calibration if isinstance(config.calibration, FastCurationPolicyBundle) else None
    )
    bundle_prototype_mismatch = bool(
        effective_bundle is not None
        and (
            effective_bundle.prototype_set_fingerprint != prototype_set.fingerprint
            or effective_bundle.prototype_scope != prototype_set.ontology_id
        )
    )
    cache = MemoryEmbeddingCache() if embedding_cache is None else embedding_cache
    factory = backend_factory or _backend_factory_default
    calibration_ready = selected_policy.calibrated
    if not calibration_ready:
        # Do not load a local model merely to manufacture uncalibrated scores.
        # The bounded input still receives an explicit calibration_unavailable
        # abstention and may be persisted for later replay.
        def unavailable_factory(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("model unavailable until measured calibration is loaded")

        factory = unavailable_factory
    started = time.monotonic()
    reps = _input_iter(inputs)
    model = config.model
    backend_instance: object | None = None
    model_available = False
    errors: list[str] = []
    prototypes_vectors: tuple[tuple[float, ...], ...] | None = None
    try:
        backend_instance = factory(
            model,
            cache_dir=model_cache(state_directory, config.model_cache_override),
            local_files_only=config.local_files_only,
            threads=config.threads,
        )
        actual_model = _backend_model(backend_instance, model)
        if actual_model.model_signature != model.model_signature or actual_model.vector_space != model.vector_space:
            raise ValueError("Fast Curation backend model identity does not match config")
        model = actual_model
        prototype_vectors: list[tuple[float, ...] | None] = []
        missing_prototypes: list[FastCurationPrototype] = []
        for prototype in prototype_set.prototypes:
            cached = _cache_get(cache, prototype_embedding_cache_key(prototype, model))
            if cached is None:
                prototype_vectors.append(None)
                missing_prototypes.append(prototype)
                continue
            try:
                prototype_vectors.append(_vector(cached, dimensions=model.dimensions))
            except ValueError:
                prototype_vectors.append(None)
                missing_prototypes.append(prototype)
        requests = tuple(
            EmbeddingRequest(
                request_id=f"prototype:{prototype.prototype_id}",
                role=EmbeddingRole.QUERY,
                fingerprint=fingerprint_text(
                    fitted_text := fit_text_to_model(
                        backend_instance, prototype.text, max_chars=6_000
                    )
                ),
                text=fitted_text,
            )
            for prototype in missing_prototypes
        )
        outputs = _embed_batches(backend_instance, requests, config.batch_size)
        by_id = _outputs_by_request(outputs, requests)
        for index, prototype in enumerate(prototype_set.prototypes):
            if prototype_vectors[index] is not None:
                continue
            request = next(
                request
                for request in requests
                if request.request_id == f"prototype:{prototype.prototype_id}"
            )
            value = _vector(by_id[request.request_id], dimensions=model.dimensions)
            prototype_vectors[index] = value
            _cache_put(
                cache,
                prototype_embedding_cache_key(prototype, model),
                value,
                metadata={
                    "prototype_id": prototype.prototype_id,
                    "prototype_version": prototype.prototype_version,
                    "model_signature": model.model_signature,
                    "vector_space": model.vector_space,
                },
            )
        prototypes_vectors = tuple(value for value in prototype_vectors if value is not None)
        if len(prototypes_vectors) != len(prototype_set.prototypes):
            raise RuntimeError("Fast Curation prototype vector set is incomplete")
        model_available = True
    except BaseException as exc:
        if _capability_error(exc):
            errors.append("capability_unavailable")
            model_available = False
            if not calibration_ready:
                errors.clear()
        else:
            if backend_instance is not None:
                _close_backend(backend_instance)
            raise

    reason_counts: dict[str, int] = {}
    samples: list[DocumentClassificationEvidence] = []
    persisted = 0
    all_sink_batch: list[DocumentClassificationEvidence] = []
    documents_seen = classified = abstained = cache_hits = cache_misses = 0
    embeddings_produced = vectors_produced = escalated = 0
    try:
        iterator = iter(reps)
        while documents_seen < config.max_documents:
            _checkpoint(cancellation)
            raw_batch: list[object] = []
            for _ in range(_effective_batch_size(config, backend_instance)):
                try:
                    raw_batch.append(next(iterator))
                except StopIteration:
                    break
            if not raw_batch:
                break
            representations: list[CurationRepresentation] = []
            for raw in raw_batch:
                try:
                    representations.append(coerce_representation(raw, max_chars=config.representation_max_chars))
                except (TypeError, ValueError) as exc:
                    document_id = str(_field(raw, "document_id", _field(raw, "item_id", "unknown")))
                    representations.append(
                        CurationRepresentation(
                            document_id=document_id or "unknown",
                            content_text="unavailable derived representation",
                            path_context=_bounded_text(_field(raw, "path_context", ""), MAX_PATH_CONTEXT_CHARS),
                            deterministic_evidence={"representation_error": type(exc).__name__},
                        )
                    )
            document_vectors: list[tuple[float, ...] | None] = []
            missing_requests: list[EmbeddingRequest] = []
            missing_indices: list[int] = []
            for index, rep in enumerate(representations):
                key = _embedding_key(rep, model)
                cached = _cache_get(cache, key) if model_available else None
                if cached is not None:
                    try:
                        document_vectors.append(_vector(cached, dimensions=model.dimensions))
                        cache_hits += 1
                        vectors_produced += 1
                        continue
                    except ValueError:
                        pass
                document_vectors.append(None)
                if model_available:
                    view = fit_text_to_model(
                        backend_instance,
                        rep.embedding_views(max_views=1)[0],
                        max_chars=config.representation_max_chars,
                    )
                    if not view:
                        cache_misses += 1
                        continue
                    missing_requests.append(
                        EmbeddingRequest(
                            request_id=f"document:{rep.document_id}:{rep.representation_fingerprint}",
                            role=EmbeddingRole.PASSAGE,
                            fingerprint=fingerprint_text(view),
                            text=view,
                        )
                    )
                    missing_indices.append(index)
                    cache_misses += 1
            if model_available and missing_requests:
                outputs = _embed_batches(backend_instance, tuple(missing_requests), config.batch_size)
                by_id = _outputs_by_request(outputs, missing_requests)
                for request, index in zip(missing_requests, missing_indices, strict=True):
                    value = _vector(by_id[request.request_id], dimensions=model.dimensions)
                    document_vectors[index] = value
                    rep = representations[index]
                    _cache_put(
                        cache,
                        _embedding_key(rep, model),
                        value,
                        metadata={
                            "representation_version": rep.representation_version,
                            "representation_fingerprint": rep.representation_fingerprint,
                            "model_signature": model.model_signature,
                            "vector_space": model.vector_space,
                        },
                    )
                    embeddings_produced += 1
                    vectors_produced += 1
            if model_available and prototypes_vectors is not None:
                valid_vectors = [value for value in document_vectors if value is not None]
                score_rows = _score_matrix(valid_vectors, prototypes_vectors)
                score_index = 0
            else:
                score_rows = []
                score_index = 0
            batch_decisions: list[DocumentClassificationEvidence] = []
            for rep, vector in zip(representations, document_vectors, strict=True):
                _checkpoint(cancellation)
                if model_available and vector is not None and prototypes_vectors is not None:
                    scores = score_rows[score_index]
                    score_index += 1
                    candidates = _rank_candidates(scores, prototype_set.prototypes, top_k=config.top_k)
                    semantic = {"candidate_count": len(candidates), "score_kind": "cosine_similarity_not_probability"}
                    decision = selected_policy.evaluate(
                        candidates,
                        text_chars=rep.text_chars,
                        model_available=True,
                        model_signature=model.model_signature,
                        representation_quality="adequate",
                        deterministic_evidence=rep.deterministic_evidence,
                        semantic_evidence=semantic,
                        structural_evidence=rep.structural_evidence,
                    )
                else:
                    decision = selected_policy.evaluate(
                        (),
                        text_chars=0 if model_available else rep.text_chars,
                        model_available=model_available or not calibration_ready,
                        model_signature=model.model_signature,
                        deterministic_evidence=rep.deterministic_evidence,
                        structural_evidence=rep.structural_evidence,
                    )
                if bundle_prototype_mismatch and model_available:
                    decision = replace(
                        decision,
                        decision=CurationDecision.ABSTAIN,
                        reason="prototype_set_mismatch",
                        selected_by_family={},
                        top1_scores={},
                        top2_scores={},
                        margins={},
                        decision_confidence=None,
                        confidence_kind="not_calibrated",
                        calibrated=False,
                    )
                evidence = _make_evidence(
                    rep,
                    decision,
                    model=model,
                    prototypes=prototype_set,
                    policy=selected_policy,
                )
                documents_seen += 1
                if evidence.classified:
                    classified += 1
                else:
                    abstained += 1
                    reason_counts[evidence.decision_reason] = reason_counts.get(evidence.decision_reason, 0) + 1
                if len(samples) < config.max_decision_samples:
                    samples.append(evidence)
                batch_decisions.append(evidence)
            all_sink_batch.extend(batch_decisions)
            if decision_sink is not None and len(all_sink_batch) >= 256:
                persisted += _persist_decisions(
                    decision_sink,
                    all_sink_batch,
                    root=root,
                    state_directory=state_directory,
                    framework_state=framework_state,
                    run_id=run_id,
                )
                all_sink_batch.clear()
        if decision_sink is not None and all_sink_batch:
            persisted += _persist_decisions(
                decision_sink,
                all_sink_batch,
                root=root,
                state_directory=state_directory,
                framework_state=framework_state,
                run_id=run_id,
            )
            all_sink_batch.clear()
    finally:
        _close_backend(backend_instance)
    elapsed = time.monotonic() - started
    metrics = FastCurationMetrics(
        documents_seen=documents_seen,
        classified=classified,
        abstained=abstained,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        embeddings_produced=embeddings_produced,
        vectors_produced=vectors_produced,
        documents_escalated=escalated,
        model_available=model_available,
        calibration_loaded=selected_policy.calibrated,
        persisted_decisions=persisted,
        elapsed_seconds=elapsed,
        abstention_reasons=tuple(sorted(reason_counts.items())[:32]),
    )
    status = "completed" if not errors else "capability_partial"
    return FastCurationRunResult(status, root, metrics, tuple(samples), tuple(errors[:32]))


def _persist_decisions(
    sink: object,
    decisions: Sequence[DocumentClassificationEvidence],
    *,
    root: Path,
    state_directory: Path,
    framework_state: object | None,
    run_id: int | str | None,
) -> int:
    method = getattr(sink, "persist_fast_curation_decisions", None)
    if not callable(method):
        raise TypeError("decision sink must implement persist_fast_curation_decisions")
    result = method(
        tuple(decisions),
        root=root,
        state_directory=state_directory,
        framework_state=framework_state,
        run_id=run_id,
    )
    return len(decisions) if result is None else int(result)


__all__ = [
    "DECISION_SCHEMA",
    "DEFAULT_POLICY_BUNDLE_DATA",
    "POLICY_BUNDLE_SCHEMA",
    "REPRESENTATION_VERSION",
    "CurationRepresentation",
    "DecisionSink",
    "DecisionVersionValidation",
    "DocumentClassificationEvidence",
    "EmbeddingCache",
    "FastCurationConfig",
    "FastCurationMetrics",
    "FastCurationPolicyBundle",
    "FastCurationRunResult",
    "FastCurationSemantic",
    "MemoryEmbeddingCache",
    "coerce_representation",
    "content_embedding_cache_key",
    "controlled_kind_directory",
    "controlled_kind_lookup",
    "curation_decision_cache_key",
    "default_calibrated_policy",
    "default_prototypes",
    "fit_text_to_model",
    "load_calibrated_policy",
    "prototype_embedding_cache_key",
    "run_fast_curation",
    "validate_record_versions",
]
