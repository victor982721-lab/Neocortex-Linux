"""Ontology prototypes and advisory-only semantic evidence materialization."""

from __future__ import annotations
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .semantic_backends import EmbeddingBackend
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    clip_image_model,
    clip_text_model,
    multilingual_text_model,
)
from .semantic_generation_worker import batches
from .semantic_models import (
    CalibrationStatus,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    EvidenceDisposition,
    LabelPrototype,
    SemanticEvidence,
    StoredLabelPrototype,
    fingerprint_text,
)
from .semantic_ontology import ONTOLOGY_VERSION, ConceptSpec, all_concepts
from .semantic_preparation import BackendFactory, model_cache
from .semantic_search_service import (
    indexed_model_available,
    registered_model_available,
)
from .semantic_service_contracts import (
    MIN_ADVISORY_EVIDENCE_SCORE,
    SEMANTIC_DATABASE_NAME,
    SEMANTIC_ONTOLOGY_ID,
    SEMANTIC_PROTOTYPE_VERSION,
    STAGING_BATCH_SIZE,
    SemanticClassificationResult,
    SemanticEvidencePassResult,
)
from .semantic_state import (
    finalize_label_prototype_refresh,
    finalize_semantic_evidence_model_refresh,
    iter_active_embedding_pages,
    load_label_prototypes,
    publish_semantic_evidence_entities,
    stage_label_prototypes,
)


class ConceptsProvider(Protocol):
    def __call__(
        self,
        target_modality: EmbeddingModality,
    ) -> tuple[ConceptSpec, ...]: ...


class PrototypeSelector(Protocol):
    def __call__(
        self,
        scores: Sequence[float],
        prototypes: Sequence[StoredLabelPrototype],
        concept_families: Mapping[str, str],
        max_evidence: int,
    ) -> tuple[int, ...]: ...


class EvidencePublisher(Protocol):
    def __call__(
        self,
        path: Path,
        evidence: Iterable[SemanticEvidence],
        *,
        entities: Iterable[tuple[str, str]],
        ontology_id: str,
        ontology_version: str,
        query_model_signature: str,
        indexed_model_signature: str,
        vector_space: str,
        refresh_token: str,
        updated_ns: int | None = None,
    ) -> tuple[int, int]: ...


# region [01] Ontology prototypes


def classification_concepts(
    target_modality: EmbeddingModality,
) -> tuple[ConceptSpec, ...]:
    concepts = all_concepts()
    if target_modality is EmbeddingModality.TEXT:
        return concepts
    visual_families = {
        "entity",
        "equipment",
        "activity",
        "operational_context",
        "safety_condition",
    }
    return tuple(concept for concept in concepts if concept.family in visual_families)


def prototype_text(
    concept: ConceptSpec,
    target_modality: EmbeddingModality,
) -> str:
    if target_modality is EmbeddingModality.IMAGE:
        base = concept.prototype(modality="image")
    else:
        base = concept.prototype(modality="text")
    aliases = ", ".join(concept.aliases[:12])
    return f"{base} Términos relacionados: {aliases}."


def label_prototype(
    concept: ConceptSpec,
    query_model: EmbeddingModelSpec,
    target_modality: EmbeddingModality,
) -> LabelPrototype:
    text = prototype_text(concept, target_modality)
    model_token = fingerprint_text(query_model.model_signature).xxh3_128
    return LabelPrototype(
        prototype_id=(
            f"prototype:{ONTOLOGY_VERSION}:{SEMANTIC_PROTOTYPE_VERSION}:"
            f"{model_token}:{concept.concept_id}"
        ),
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        concept_id=concept.concept_id,
        prototype_version=SEMANTIC_PROTOTYPE_VERSION,
        model_signature=query_model.model_signature,
        vector_space=query_model.vector_space,
        text=text,
        fingerprint=fingerprint_text(text),
        calibration_status=CalibrationStatus.UNCALIBRATED,
        provenance={
            "pipeline": SEMANTIC_PIPELINE_VERSION,
            "target_modality": target_modality.value,
            "concept_family": concept.family,
            "authority": "advisory-only",
        },
    )


def _validate_loaded_prototypes(
    expected: Sequence[LabelPrototype],
    loaded_by_id: Mapping[str, StoredLabelPrototype],
) -> tuple[LabelPrototype, ...]:
    missing: list[LabelPrototype] = []
    for prototype in expected:
        stored = loaded_by_id.get(prototype.prototype_id)
        if stored is None:
            missing.append(prototype)
            continue
        if (
            stored.prototype.concept_id != prototype.concept_id
            or stored.prototype.text != prototype.text
            or stored.prototype.fingerprint != prototype.fingerprint
        ):
            raise RuntimeError(
                "prototype content changed without a prototype-version bump"
            )
    return tuple(missing)


def _embed_missing_prototypes(
    database: Path,
    backend: EmbeddingBackend,
    missing: Sequence[LabelPrototype],
) -> None:
    for prototype_batch in batches(missing, backend.max_batch_size):
        requests = tuple(
            EmbeddingRequest(
                request_id=prototype.prototype_id,
                role=EmbeddingRole.QUERY,
                fingerprint=prototype.fingerprint,
                text=prototype.text,
            )
            for prototype in prototype_batch
        )
        outputs = tuple(backend.embed(requests))
        if len(outputs) != len(prototype_batch):
            raise RuntimeError("prototype backend returned an incomplete batch")
        if tuple(output.request_id for output in outputs) != tuple(
            prototype.prototype_id for prototype in prototype_batch
        ):
            raise RuntimeError("prototype backend returned results out of order")
        stage_label_prototypes(
            database,
            (
                (prototype, output.vector)
                for prototype, output in zip(prototype_batch, outputs, strict=True)
            ),
            batch_size=STAGING_BATCH_SIZE,
            activate=False,
        )


def prepare_label_prototypes(
    database: Path,
    backend: EmbeddingBackend,
    *,
    target_modality: EmbeddingModality,
    concepts_provider: ConceptsProvider = classification_concepts,
) -> tuple[StoredLabelPrototype, ...]:
    concepts = concepts_provider(target_modality)
    expected = tuple(
        label_prototype(concept, backend.model, target_modality) for concept in concepts
    )
    if not expected:
        raise RuntimeError("ontology has no concepts for semantic classification")
    loaded = load_label_prototypes(
        database,
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        prototype_version=SEMANTIC_PROTOTYPE_VERSION,
        vector_space=backend.model.vector_space,
        model_signatures=(backend.model.model_signature,),
        limit=10_000,
    )
    loaded_by_id = {value.prototype.prototype_id: value for value in loaded}
    _embed_missing_prototypes(
        database,
        backend,
        _validate_loaded_prototypes(expected, loaded_by_id),
    )
    finalize_label_prototype_refresh(
        database,
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        prototype_version=SEMANTIC_PROTOTYPE_VERSION,
        vector_space=backend.model.vector_space,
        model_signature=backend.model.model_signature,
        active_prototype_ids=tuple(prototype.prototype_id for prototype in expected),
    )
    loaded = load_label_prototypes(
        database,
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        prototype_version=SEMANTIC_PROTOTYPE_VERSION,
        vector_space=backend.model.vector_space,
        model_signatures=(backend.model.model_signature,),
        limit=max(1, len(concepts) + 1),
    )
    if len(loaded) != len(expected) or {
        value.prototype.prototype_id for value in loaded
    } != {prototype.prototype_id for prototype in expected}:
        raise RuntimeError("stored ontology prototype set is incomplete")
    return loaded


# endregion [01]


# region [02] Evidence selection and publication


def selected_prototype_indices(
    scores: Sequence[float],
    prototypes: Sequence[StoredLabelPrototype],
    concept_families: Mapping[str, str],
    max_evidence: int,
) -> tuple[int, ...]:
    eligible = tuple(
        index
        for index, raw_score in enumerate(scores)
        if math.isfinite(float(raw_score))
        and float(raw_score) > MIN_ADVISORY_EVIDENCE_SCORE
    )
    ordered = sorted(
        eligible,
        key=lambda index: (
            -float(scores[index]),
            prototypes[index].prototype.concept_id,
        ),
    )
    selected: list[int] = []
    selected_set: set[int] = set()
    represented_families: set[str] = set()
    for index in ordered:
        family = concept_families[prototypes[index].prototype.concept_id]
        if family in represented_families:
            continue
        selected.append(index)
        selected_set.add(index)
        represented_families.add(family)
        if len(selected) >= max_evidence:
            return tuple(selected)
    for index in ordered:
        if index in selected_set:
            continue
        selected.append(index)
        if len(selected) >= max_evidence:
            break
    return tuple(selected)


def _evidence_for_record(
    record,
    score_row,
    prototypes: Sequence[StoredLabelPrototype],
    selected: Sequence[int],
    *,
    indexed_model: EmbeddingModelSpec,
    query_backend: EmbeddingBackend,
) -> list[SemanticEvidence]:
    evidence: list[SemanticEvidence] = []
    for rank, prototype_index in enumerate(selected, start=1):
        prototype = prototypes[prototype_index].prototype
        score = max(-1.0, min(1.0, float(score_row[prototype_index])))
        evidence.append(
            SemanticEvidence(
                item_id=record.item_id,
                source_entity_id=record.entity_id,
                ontology_id=SEMANTIC_ONTOLOGY_ID,
                ontology_version=ONTOLOGY_VERSION,
                concept_id=prototype.concept_id,
                prototype_id=prototype.prototype_id,
                query_model_signature=query_backend.model.model_signature,
                indexed_model_signature=indexed_model.model_signature,
                vector_space=indexed_model.vector_space,
                score=score,
                rank=rank,
                generation_id=record.generation_id,
                calibration_status=CalibrationStatus.UNCALIBRATED,
                disposition=EvidenceDisposition.ADVISORY,
                provenance={
                    "pipeline": SEMANTIC_PIPELINE_VERSION,
                    "selection": "best-per-concept-family-then-global-v1",
                    "eligibility": "finite-cosine-score>0-v1",
                    "authority": "advisory-only",
                },
            )
        )
    return evidence


def _publish_evidence_page(
    database: Path,
    records,
    scores,
    prototypes: Sequence[StoredLabelPrototype],
    concept_families: Mapping[str, str],
    *,
    indexed_model: EmbeddingModelSpec,
    query_backend: EmbeddingBackend,
    max_evidence_per_entity: int,
    refresh_token: str,
    prototype_selector: PrototypeSelector,
    evidence_publisher: EvidencePublisher,
) -> tuple[int, int, int, int]:
    entities_scored = entities_abstained = evidence_staged = stale_deactivated = 0
    for start in range(0, len(records), STAGING_BATCH_SIZE):
        evidence_batch: list[SemanticEvidence] = []
        entity_batch: list[tuple[str, str]] = []
        stop = min(start + STAGING_BATCH_SIZE, len(records))
        for row_index in range(start, stop):
            record = records[row_index]
            entity_batch.append((record.item_id, record.entity_id))
            selected = prototype_selector(
                scores[row_index],
                prototypes,
                concept_families,
                max_evidence_per_entity,
            )
            if not selected:
                entities_abstained += 1
            evidence_batch.extend(
                _evidence_for_record(
                    record,
                    scores[row_index],
                    prototypes,
                    selected,
                    indexed_model=indexed_model,
                    query_backend=query_backend,
                )
            )
            entities_scored += 1
        published, entity_deactivated = evidence_publisher(
            database,
            evidence_batch,
            entities=entity_batch,
            ontology_id=SEMANTIC_ONTOLOGY_ID,
            ontology_version=ONTOLOGY_VERSION,
            query_model_signature=query_backend.model.model_signature,
            indexed_model_signature=indexed_model.model_signature,
            vector_space=indexed_model.vector_space,
            refresh_token=refresh_token,
        )
        evidence_staged += published
        stale_deactivated += entity_deactivated
    return entities_scored, entities_abstained, evidence_staged, stale_deactivated


def classify_embedding_model(
    database: Path,
    *,
    indexed_model: EmbeddingModelSpec,
    query_backend: EmbeddingBackend,
    max_evidence_per_entity: int,
    page_size: int,
    concepts_provider: ConceptsProvider = classification_concepts,
    prototype_selector: PrototypeSelector = selected_prototype_indices,
    evidence_publisher: EvidencePublisher = publish_semantic_evidence_entities,
) -> SemanticEvidencePassResult:
    try:
        import numpy as np
    except ImportError as exc:  # fastembed normally supplies this dependency
        raise RuntimeError(
            "semantic evidence scoring requires NumPy; install Neocortex[semantic]"
        ) from exc

    prototypes = prepare_label_prototypes(
        database,
        query_backend,
        target_modality=indexed_model.modality,
        concepts_provider=concepts_provider,
    )
    prototype_matrix = np.asarray(
        [prototype.vector for prototype in prototypes],
        dtype=np.float32,
    )
    concept_families = {
        concept.concept_id: concept.family
        for concept in concepts_provider(indexed_model.modality)
    }
    refresh_token = (
        f"evidence:{ONTOLOGY_VERSION}:{indexed_model.model_signature}:{time.time_ns()}"
    )
    entities_scored = entities_abstained = evidence_staged = 0
    entity_stale_deactivated = 0
    for page in iter_active_embedding_pages(
        database,
        indexed_model.model_signature,
        page_size=page_size,
        text_scope=(
            "content" if indexed_model.modality is EmbeddingModality.TEXT else "all"
        ),
    ):
        if not page.records:
            continue
        record_matrix = np.asarray(
            [record.vector for record in page.records],
            dtype=np.float32,
        )
        scores = record_matrix @ prototype_matrix.T
        scored, abstained, staged, deactivated = _publish_evidence_page(
            database,
            page.records,
            scores,
            prototypes,
            concept_families,
            indexed_model=indexed_model,
            query_backend=query_backend,
            max_evidence_per_entity=max_evidence_per_entity,
            refresh_token=refresh_token,
            prototype_selector=prototype_selector,
            evidence_publisher=evidence_publisher,
        )
        entities_scored += scored
        entities_abstained += abstained
        evidence_staged += staged
        entity_stale_deactivated += deactivated
    globally_deactivated = finalize_semantic_evidence_model_refresh(
        database,
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        vector_space=indexed_model.vector_space,
        indexed_model_signature=indexed_model.model_signature,
        refresh_token=refresh_token,
    )
    return SemanticEvidencePassResult(
        indexed_model_signature=indexed_model.model_signature,
        query_model_signature=query_backend.model.model_signature,
        vector_space=indexed_model.vector_space,
        prototypes=len(prototypes),
        entities_scored=entities_scored,
        evidence_staged=evidence_staged,
        stale_evidence_deactivated=(entity_stale_deactivated + globally_deactivated),
        entities_abstained=entities_abstained,
    )


# endregion [02]


# region [03] Classification orchestration


def _classify_text(
    database: Path,
    selected_model: EmbeddingModelSpec,
    *,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    max_evidence_per_entity: int,
    page_size: int,
    backend_factory: BackendFactory,
    concepts_provider: ConceptsProvider,
    prototype_selector: PrototypeSelector,
    evidence_publisher: EvidencePublisher,
) -> SemanticEvidencePassResult:
    text_backend = backend_factory(
        selected_model,
        cache_dir=cache,
        local_files_only=local_files_only,
        threads=threads,
    )
    return classify_embedding_model(
        database,
        indexed_model=selected_model,
        query_backend=text_backend,
        max_evidence_per_entity=max_evidence_per_entity,
        page_size=page_size,
        concepts_provider=concepts_provider,
        prototype_selector=prototype_selector,
        evidence_publisher=evidence_publisher,
    )


def _classify_image(
    database: Path,
    *,
    cache: Path,
    local_files_only: bool,
    threads: int | None,
    max_evidence_per_entity: int,
    page_size: int,
    backend_factory: BackendFactory,
    concepts_provider: ConceptsProvider,
    prototype_selector: PrototypeSelector,
    evidence_publisher: EvidencePublisher,
) -> SemanticEvidencePassResult:
    query_model = clip_text_model()
    indexed_model = clip_image_model()
    image_query_backend = backend_factory(
        query_model,
        cache_dir=cache,
        local_files_only=local_files_only,
        threads=threads,
    )
    return classify_embedding_model(
        database,
        indexed_model=indexed_model,
        query_backend=image_query_backend,
        max_evidence_per_entity=max_evidence_per_entity,
        page_size=page_size,
        concepts_provider=concepts_provider,
        prototype_selector=prototype_selector,
        evidence_publisher=evidence_publisher,
    )


def classify_semantic_index(
    state_directory: Path,
    *,
    include_text: bool,
    include_images: bool,
    max_evidence_per_entity: int,
    page_size: int,
    text_model: EmbeddingModelSpec | None,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    backend_factory: BackendFactory,
    concepts_provider: ConceptsProvider,
    prototype_selector: PrototypeSelector,
    evidence_publisher: EvidencePublisher,
) -> SemanticClassificationResult:
    """Materialize uncalibrated ontology suggestions without changing file policy."""

    if not (include_text or include_images):
        raise ValueError("at least one semantic modality must be selected")
    if not 1 <= max_evidence_per_entity <= 32:
        raise ValueError("max evidence per entity must be between 1 and 32")
    if not 1 <= page_size <= 10_000:
        raise ValueError("semantic evidence page size must be between 1 and 10000")
    database = state_directory / SEMANTIC_DATABASE_NAME
    if not database.is_file():
        raise FileNotFoundError(f"semantic index does not exist: {database}")

    cache = model_cache(state_directory, model_cache_override)
    passes: list[SemanticEvidencePassResult] = []
    skipped: dict[str, str] = {}
    if include_text:
        selected_text_model = text_model or multilingual_text_model()
        if selected_text_model.modality is not EmbeddingModality.TEXT:
            raise ValueError("semantic text classification requires a text model")
        if not indexed_model_available(database, selected_text_model):
            skipped["text"] = "text_model_not_indexed"
        else:
            passes.append(
                _classify_text(
                    database,
                    selected_text_model,
                    cache=cache,
                    local_files_only=local_files_only,
                    threads=threads,
                    max_evidence_per_entity=max_evidence_per_entity,
                    page_size=page_size,
                    backend_factory=backend_factory,
                    concepts_provider=concepts_provider,
                    prototype_selector=prototype_selector,
                    evidence_publisher=evidence_publisher,
                )
            )
    if include_images:
        query_model = clip_text_model()
        indexed_model = clip_image_model()
        if not (
            registered_model_available(database, query_model)
            and indexed_model_available(database, indexed_model)
        ):
            skipped["image"] = "clip_models_not_indexed"
        else:
            passes.append(
                _classify_image(
                    database,
                    cache=cache,
                    local_files_only=local_files_only,
                    threads=threads,
                    max_evidence_per_entity=max_evidence_per_entity,
                    page_size=page_size,
                    backend_factory=backend_factory,
                    concepts_provider=concepts_provider,
                    prototype_selector=prototype_selector,
                    evidence_publisher=evidence_publisher,
                )
            )
    return SemanticClassificationResult(
        semantic_database=database,
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        passes=tuple(passes),
        skipped=skipped,
    )


# endregion [03]
