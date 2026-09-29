"""Bounded representation and shared embedding execution helpers.

This module is deliberately independent from the Full Semantic index.  It
contains only the bounded input/cache/backend seam used by Fast Curation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .fast_curation_policy import (
    FAMILY_ORDER,
    CalibrationParameters,
    FastCurationPolicy,
    RankedCandidate,
)
from .fast_curation_policy_bundle import FastCurationPolicyBundle
from .fast_curation_prototypes import (
    FastCurationPrototype,
    PrototypeSet,
)
from .semantic_config import multilingual_text_model
from .semantic_models import (
    EmbeddingModelSpec,
    EmbeddingRequest,
    fingerprint_text,
)
from .semantic_preparation import SemanticModelUnavailableError, backend

if TYPE_CHECKING:
    from .fast_curation_contracts import DocumentClassificationEvidence

REPRESENTATION_VERSION = "fast-curation-representation/v1"
DECISION_SCHEMA = "fast-curation-decision/v1"
MAX_TEXT_CHARS = 24_000
MAX_PATH_CONTEXT_CHARS = 512
MAX_METADATA_ITEMS = 32
MAX_EVIDENCE_SAMPLES = 5
MAX_DECISION_SAMPLES = 32


class EmbeddingCache(Protocol):
    """Minimal cache boundary implemented by curation persistence."""

    def get(self, key: str) -> Sequence[float] | None: ...

    def put(
        self,
        key: str,
        vector: Sequence[float],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None: ...


class DecisionSink(Protocol):
    """Catalog-only decision persistence boundary."""

    def persist_fast_curation_decisions(
        self,
        decisions: Iterable["DocumentClassificationEvidence"],
        *,
        root: Path,
        state_directory: Path,
        framework_state: object | None = None,
        run_id: int | str | None = None,
    ) -> int: ...


@dataclass(slots=True)
class MemoryEmbeddingCache:
    """Small test/default cache; production callers inject the persistent owner."""

    values: dict[str, tuple[float, ...]] = field(default_factory=dict)

    def get(self, key: str) -> tuple[float, ...] | None:
        value = self.values.get(key)
        return None if value is None else tuple(value)

    def put(
        self,
        key: str,
        vector: Sequence[float],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        del metadata
        self.values[key] = tuple(float(value) for value in vector)


@dataclass(frozen=True, slots=True)
class CurationRepresentation:
    """Structural adapter for the compact route-produced representation.

    The route owner may provide another DTO; :func:`coerce_representation`
    accepts it structurally.  ``path_context`` is kept separate from
    ``content_text`` so renames do not invalidate content embeddings and path
    evidence cannot dominate semantic content.
    """

    document_id: str
    content_text: str
    content_fingerprint: str | None = None
    representation_version: str = REPRESENTATION_VERSION
    representation_identity: str | None = None
    path_context: str = ""
    title: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    headings: tuple[str, ...] = ()
    opening: str = ""
    representative_body: str = ""
    conclusion: str = ""
    views: tuple[str, ...] = ()
    derived_sources: tuple[str, ...] = ()
    deterministic_evidence: Mapping[str, object] = field(default_factory=dict)
    structural_evidence: Mapping[str, object] = field(default_factory=dict)
    source_kind: str = "unknown"
    file_key: str | None = None
    input_signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id.strip():
            raise ValueError("document_id must be non-empty")
        if not isinstance(self.source_kind, str) or not self.source_kind.strip():
            raise ValueError("source_kind must be non-empty")
        object.__setattr__(self, "source_kind", self.source_kind.strip())
        if self.file_key is None:
            object.__setattr__(self, "file_key", self.document_id)
        elif not isinstance(self.file_key, str) or not self.file_key.strip():
            raise ValueError("file_key must be non-empty")
        text = _bounded_text(self.content_text, MAX_TEXT_CHARS)
        if not text:
            raise ValueError("content_text cannot be empty")
        if not isinstance(self.representation_version, str) or not self.representation_version.strip():
            raise ValueError("representation_version must be non-empty")
        object.__setattr__(self, "content_text", text)
        object.__setattr__(self, "path_context", _bounded_text(self.path_context, MAX_PATH_CONTEXT_CHARS))
        object.__setattr__(self, "title", _bounded_text(self.title, 1_024))
        object.__setattr__(self, "opening", _bounded_text(self.opening, 4_000))
        object.__setattr__(self, "representative_body", _bounded_text(self.representative_body, MAX_TEXT_CHARS))
        object.__setattr__(self, "conclusion", _bounded_text(self.conclusion, 4_000))
        if self.content_fingerprint is None:
            object.__setattr__(
                self,
                "content_fingerprint",
                fingerprint_text(self.content_text).xxh3_128,
            )
        else:
            fingerprint = _fingerprint_string(self.content_fingerprint)
            if not fingerprint:
                raise ValueError("content_fingerprint must be non-empty")
            object.__setattr__(self, "content_fingerprint", fingerprint)
        if self.input_signature is None:
            object.__setattr__(self, "input_signature", str(self.content_fingerprint))
        elif not isinstance(self.input_signature, str) or not self.input_signature.strip():
            raise ValueError("input_signature must be non-empty")
        if self.representation_identity is not None:
            identity = _fingerprint_string(self.representation_identity)
            if not identity:
                raise ValueError("representation_identity must be non-empty")
            object.__setattr__(self, "representation_identity", identity)
        object.__setattr__(self, "headings", tuple(self.headings))
        object.__setattr__(self, "views", tuple(self.views))
        object.__setattr__(self, "derived_sources", tuple(self.derived_sources))
        if len(self.headings) > 32 or len(self.views) > 4 or len(self.derived_sources) > 32:
            raise ValueError("representation fields exceed bounded limits")
        for name, values in (("headings", self.headings), ("views", self.views), ("derived_sources", self.derived_sources)):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
        metadata = dict(self.metadata or {})
        if len(metadata) > MAX_METADATA_ITEMS:
            raise ValueError("metadata exceeds bounded limits")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "deterministic_evidence", dict(self.deterministic_evidence or {}))
        object.__setattr__(self, "structural_evidence", dict(self.structural_evidence or {}))
        try:
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            json.dumps(self.deterministic_evidence, ensure_ascii=False, allow_nan=False)
            json.dumps(self.structural_evidence, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("representation evidence must be JSON compatible") from exc

    @property
    def text_chars(self) -> int:
        return len(self.content_text)

    @property
    def representation_fingerprint(self) -> str:
        if self.representation_identity is not None:
            return self.representation_identity
        payload = "\0".join((self.representation_version, self.content_text))
        return fingerprint_text(payload).xxh3_128

    def embedding_views(self, *, max_views: int = 1) -> tuple[str, ...]:
        if not 1 <= max_views <= 4:
            raise ValueError("max_views must be between 1 and 4")
        values = self.views or (self.content_text,)
        return tuple(_bounded_text(value, MAX_TEXT_CHARS) for value in values[:max_views])

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "fast-curation-representation/v1",
            "document_id": self.document_id,
            "source_kind": self.source_kind,
            "file_key": self.file_key,
            "input_signature": self.input_signature,
            "content_fingerprint": self.content_fingerprint,
            "representation_version": self.representation_version,
            "representation_fingerprint": self.representation_fingerprint,
            "path_context": self.path_context,
            "text_chars": self.text_chars,
            "derived_sources": list(self.derived_sources),
        }


@dataclass(frozen=True, slots=True)
class FastCurationConfig:
    """Execution bounds; no threshold is inferred here."""

    # The valid fresh-holdout benchmark currently favors Jina (100% precision
    # at 69.2% coverage) over MiniLM (97.5% at 33.3%).  This is model
    # selection evidence only; no calibration thresholds are inferred here.
    model: EmbeddingModelSpec = field(default_factory=multilingual_text_model)
    local_files_only: bool = True
    model_cache_override: Path | None = None
    threads: int | None = None
    # Local benchmark selection: MiniLM, batch 32.  This is an execution
    # bound, not a confidence threshold; callers may override it explicitly.
    batch_size: int | None = 32
    top_k: int = 5
    max_views: int = 1
    max_documents: int = 500_000
    max_decision_samples: int = MAX_DECISION_SAMPLES
    representation_max_chars: int = MAX_TEXT_CHARS
    route_name: str = "fast-curation"
    calibration: CalibrationParameters | FastCurationPolicyBundle | Mapping[str, object] | None = None
    default_safe: bool = False
    benchmark_id: str | None = None

    @classmethod
    def from_policy_bundle(
        cls,
        bundle: FastCurationPolicyBundle,
        **overrides: object,
    ) -> "FastCurationConfig":
        """Bind the exact measured model spec declared by a pure bundle."""

        if not isinstance(bundle, FastCurationPolicyBundle):
            raise TypeError("bundle must be FastCurationPolicyBundle")
        from .semantic_config import compact_multilingual_text_model, multilingual_text_model

        candidates = (compact_multilingual_text_model(), multilingual_text_model())
        selected = next(
            (model for model in candidates if model.model_signature == bundle.model_signature),
            None,
        )
        if selected is None:
            raise ValueError("policy bundle model signature is not a supported local model")
        return cls(model=selected, calibration=bundle, **overrides)

    def __post_init__(self) -> None:
        if self.model.modality.value != "text":
            raise ValueError("Fast Curation document service requires a text model")
        if not isinstance(self.local_files_only, bool):
            raise ValueError("local_files_only must be boolean")
        if self.threads is not None and (not isinstance(self.threads, int) or self.threads < 1):
            raise ValueError("threads must be positive when provided")
        if self.batch_size is not None and (not isinstance(self.batch_size, int) or not 1 <= self.batch_size <= 4096):
            raise ValueError("batch_size must be between 1 and 4096")
        if not 1 <= self.top_k <= 5 or not 1 <= self.max_views <= 4:
            raise ValueError("top_k must be 1..5 and max_views must be 1..4")
        if not 1 <= self.max_documents <= 500_000:
            raise ValueError("max_documents is outside the supported scale")
        if not 1 <= self.max_decision_samples <= 256:
            raise ValueError("max_decision_samples must be 1..256")
        if not 1 <= self.representation_max_chars <= MAX_TEXT_CHARS:
            raise ValueError("representation_max_chars is outside the bounded range")
        if not isinstance(self.default_safe, bool):
            raise ValueError("default_safe must be boolean")
        if self.benchmark_id is not None and (
            not isinstance(self.benchmark_id, str) or not self.benchmark_id.strip()
        ):
            raise ValueError("benchmark_id must be non-empty when provided")


def _bounded_text(value: object, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split())[:limit].rstrip()


def fit_text_to_model(
    backend_instance: object,
    text: str,
    *,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    """Fit one bounded view to the shared backend tokenizer without truncation.

    The function uses the backend's exact tokenizer contract when available;
    it never asks FastEmbed to silently truncate.  Prefix and tail are retained
    during the bounded search so a long route representation does not discard
    every conclusion/table summary after its title.
    """

    selected = _bounded_text(text, max_chars)
    counter = getattr(backend_instance, "text_token_counts", None)
    if not callable(counter) or not selected:
        return selected
    counts, limit = counter((selected,))
    if not counts or int(counts[0]) <= int(limit):
        return selected
    low, high = 1, len(selected)
    best = ""
    while low <= high:
        width = (low + high) // 2
        if width >= len(selected):
            candidate = selected
        else:
            head = max(1, int(width * 0.7))
            tail = max(1, width - head - 5)
            candidate = (selected[:head] + " … " + selected[-tail:]).strip()
        observed, _ = counter((candidate,))
        if observed and int(observed[0]) <= int(limit):
            best = candidate
            low = width + 1
        else:
            high = width - 1
    return best


def _fingerprint_string(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    candidate = getattr(value, "xxh3_128", None)
    if isinstance(candidate, str):
        return candidate.strip()
    if isinstance(value, Mapping):
        candidate = value.get("xxh3_128", value.get("digest"))
        return candidate.strip() if isinstance(candidate, str) else ""
    return str(value).strip()


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def coerce_representation(value: object, *, max_chars: int = MAX_TEXT_CHARS) -> CurationRepresentation:
    """Adapt the route DTO without re-opening or re-extracting source files."""

    if isinstance(value, CurationRepresentation):
        if len(value.content_text) <= max_chars:
            return value
        return CurationRepresentation(
            document_id=value.document_id,
            content_text=value.content_text[:max_chars],
            source_kind=value.source_kind,
            file_key=value.file_key,
            input_signature=value.input_signature,
            content_fingerprint=value.content_fingerprint,
            representation_version=value.representation_version,
            representation_identity=value.representation_identity,
            path_context=value.path_context,
            title=value.title,
            metadata=value.metadata,
            headings=value.headings,
            opening=value.opening,
            representative_body=value.representative_body,
            conclusion=value.conclusion,
            views=value.views,
            derived_sources=value.derived_sources,
            deterministic_evidence=value.deterministic_evidence,
            structural_evidence=value.structural_evidence,
        )
    document_id = _field(value, "document_id", _field(value, "item_id"))
    content = _field(value, "content_text", _field(value, "text", _field(value, "representative_text")))
    if content is None:
        content = _field(value, "content", "")
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("representation requires document_id")
    content = _bounded_text(content, max_chars)
    title = _bounded_text(_field(value, "title", ""), 1_024)
    metadata_value = _field(value, "metadata", {})
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    headings_value = _field(value, "headings", ())
    headings = tuple(str(item) for item in headings_value or () if str(item).strip())[:32]
    opening = _bounded_text(_field(value, "opening", ""), 4_000)
    body = _bounded_text(_field(value, "representative_body", content), max_chars)
    conclusion = _bounded_text(_field(value, "conclusion", _field(value, "tail", "")), 4_000)
    if not content:
        sections = [title, " ".join(headings), opening, body, conclusion]
        content = _bounded_text(" ".join(part for part in sections if part), max_chars)
    if not content:
        raise ValueError("representation has no bounded derived text")
    views_value = _field(value, "views", ())
    views = tuple(_bounded_text(item, max_chars) for item in (views_value or ()) if _bounded_text(item, max_chars))[:4]
    source_value = _field(value, "derived_sources", ())
    if isinstance(source_value, Mapping):
        sources = tuple(str(key) for key in source_value)[:32]
    else:
        sources = tuple(str(item) for item in (source_value or ()))[:32]
    return CurationRepresentation(
        document_id=document_id,
        content_text=content,
        source_kind=str(_field(value, "source_kind", "unknown")),
        file_key=_field(value, "file_key", _field(value, "source_file_key")),
        input_signature=_field(value, "input_signature", _field(value, "input_digest")),
        content_fingerprint=_field(
            value,
            "content_fingerprint",
            _field(value, "text_fingerprint", _field(value, "content_signature", _field(value, "fingerprint"))),
        ),
        representation_version=str(_field(value, "representation_version", REPRESENTATION_VERSION)),
        representation_identity=_field(value, "representation_fingerprint", _field(value, "fingerprint")),
        path_context=_bounded_text(_field(value, "path_context", _field(value, "source_path", "")), MAX_PATH_CONTEXT_CHARS),
        title=title,
        metadata=metadata,
        headings=headings,
        opening=opening,
        representative_body=body,
        conclusion=conclusion,
        views=views,
        derived_sources=sources,
        deterministic_evidence=_mapping(_field(value, "deterministic_evidence", {})),
        structural_evidence=_mapping(_field(value, "structural_evidence", {})),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _cache_get(cache: object, key: str) -> Sequence[float] | None:
    getter = getattr(cache, "get", None)
    if not callable(getter):
        getter = getattr(cache, "get_embedding", None)
    if not callable(getter):
        raise TypeError("embedding cache must implement get(key)")
    value = getter(key)
    return None if value is None else tuple(float(item) for item in value)


def _cache_put(cache: object, key: str, vector: Sequence[float], metadata: Mapping[str, object]) -> None:
    putter = getattr(cache, "put", None)
    if not callable(putter):
        putter = getattr(cache, "put_embedding", None)
    if not callable(putter):
        raise TypeError("embedding cache must implement put(key, vector, metadata=...)")
    try:
        putter(key, tuple(vector), metadata=metadata)
    except TypeError:
        putter(key, tuple(vector))


def _backend_model(backend_instance: object, fallback: EmbeddingModelSpec) -> EmbeddingModelSpec:
    model = getattr(backend_instance, "model", fallback)
    if callable(model):
        model = model()
    return model if isinstance(model, EmbeddingModelSpec) else fallback


def _vector(value: object, *, dimensions: int | None = None) -> tuple[float, ...]:
    raw = getattr(value, "vector", value)
    values = tuple(float(item) for item in raw)
    if dimensions is not None and len(values) != dimensions:
        raise ValueError("embedding vector dimension does not match model")
    norm = math.sqrt(sum(item * item for item in values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("embedding vector has no finite norm")
    return tuple(item / norm for item in values)


def _model_signature(model: EmbeddingModelSpec) -> str:
    return model.model_signature


def _embedding_key(rep: CurationRepresentation, model: EmbeddingModelSpec) -> str:
    value = "\0".join(
        (
            "fast-curation-content-embedding-v1",
            str(rep.content_fingerprint),
            rep.representation_fingerprint,
            rep.representation_version,
            model.model_signature,
            model.vector_space,
            str(model.dimensions),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_embedding_cache_key(
    representation: CurationRepresentation,
    model: EmbeddingModelSpec,
) -> str:
    """Stable content key; path context is intentionally absent."""

    return _embedding_key(representation, model)


def _decision_key(rep: CurationRepresentation, model: EmbeddingModelSpec, prototypes: PrototypeSet, policy: FastCurationPolicy) -> str:
    value = "\0".join(
        (
            "fast-curation-decision-v1",
            _embedding_key(rep, model),
            prototypes.fingerprint,
            prototypes.prototype_version,
            policy.policy_version,
            "" if policy.calibration is None else policy.calibration.calibration_version,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def curation_decision_cache_key(
    representation: CurationRepresentation,
    model: EmbeddingModelSpec,
    prototypes: PrototypeSet,
    policy: FastCurationPolicy,
) -> str:
    """Versioned decision key including prototype, policy and calibration versions."""

    return _decision_key(representation, model, prototypes, policy)


def prototype_embedding_cache_key(
    prototype: FastCurationPrototype,
    model: EmbeddingModelSpec,
) -> str:
    """Stable prototype-vector key, independent from document paths/content."""

    value = "\0".join(
        (
            "fast-curation-prototype-embedding-v1",
            prototype.identity,
            prototype.text_fingerprint,
            model.model_signature,
            model.vector_space,
            str(model.dimensions),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rank_candidates(
    scores: Sequence[float],
    prototypes: Sequence[FastCurationPrototype],
    *,
    top_k: int,
) -> tuple[RankedCandidate, ...]:
    grouped: dict[str, list[tuple[int, float]]] = {}
    for index, (prototype, raw_score) in enumerate(zip(prototypes, scores, strict=True)):
        value = float(raw_score)
        if math.isfinite(value):
            grouped.setdefault(prototype.family, []).append((index, value))
    result: list[RankedCandidate] = []
    for family in FAMILY_ORDER:
        if family not in grouped:
            continue
        ordered = sorted(grouped[family], key=lambda item: (-item[1], prototypes[item[0]].concept_id))[:top_k]
        for rank, (index, score) in enumerate(ordered, start=1):
            prototype = prototypes[index]
            result.append(
                RankedCandidate(
                    prototype_id=prototype.prototype_id,
                    concept_id=prototype.concept_id,
                    family=prototype.family,
                    label=prototype.label,
                    score=max(-1.0, min(1.0, score)),
                    rank=rank,
                    parent_id=prototype.parent_id,
                    destination=prototype.destination,
                )
            )
    return tuple(result)


def _score_matrix(document_vectors: Sequence[Sequence[float]], prototype_vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    try:
        import numpy as np
    except ImportError:
        return [
            [sum(float(left) * float(right) for left, right in zip(document, prototype, strict=True)) for prototype in prototype_vectors]
            for document in document_vectors
        ]
    documents = np.asarray(document_vectors, dtype=np.float32)
    prototypes = np.asarray(prototype_vectors, dtype=np.float32)
    scores = documents @ prototypes.T
    return scores.astype(np.float32, copy=False).tolist()


def _input_iter(inputs: object) -> Iterable[object]:
    if inputs is None:
        return ()
    if isinstance(inputs, Mapping):
        value = inputs.get("representations", inputs.get("documents", ()))
        return value if isinstance(value, Iterable) and not isinstance(value, (str, bytes)) else ()
    iterator = getattr(inputs, "iter_representations", None)
    if callable(iterator):
        return iterator()
    value = getattr(inputs, "representations", inputs)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return value
    raise TypeError("inputs must be an iterable or expose representations")


def _backend_factory_default(model: EmbeddingModelSpec, **kwargs: object) -> object:
    return backend(model, **kwargs)


def _capability_error(exc: BaseException) -> bool:
    if isinstance(exc, (SemanticModelUnavailableError, FileNotFoundError, EOFError, OSError)):
        return True
    message = str(exc).casefold()
    return "model" in message and any(token in message for token in ("unavailable", "missing", "cache", "load"))


def _checkpoint(cancellation: object | None) -> None:
    if cancellation is None:
        return
    callback = getattr(cancellation, "checkpoint", None)
    if callable(callback):
        callback()


def _effective_batch_size(config: FastCurationConfig, backend_instance: object | None) -> int:
    declared = getattr(backend_instance, "max_batch_size", None)
    if callable(declared):
        declared = declared()
    default = 64 if declared is None else int(declared)
    return max(1, min(4096, config.batch_size or default))


def _embed_batches(
    backend_instance: object,
    requests: Sequence[EmbeddingRequest],
    batch_size: int | None,
) -> tuple[object, ...]:
    if not requests:
        return ()
    declared = getattr(backend_instance, "max_batch_size", None)
    if callable(declared):
        declared = declared()
    limit = max(1, min(int(declared or 64), int(batch_size or declared or 64)))
    output: list[object] = []
    for start in range(0, len(requests), limit):
        batch = tuple(requests[start : start + limit])
        values = tuple(backend_instance.embed(batch))
        if len(values) != len(batch):
            raise RuntimeError("Fast Curation backend returned an incomplete batch")
        output.extend(values)
    return tuple(output)


def _outputs_by_request(
    outputs: Sequence[object], requests: Sequence[EmbeddingRequest]
) -> dict[str, object]:
    by_id = {getattr(output, "request_id", None): output for output in outputs}
    if all(request.request_id in by_id for request in requests):
        return {request.request_id: by_id[request.request_id] for request in requests}
    if all(getattr(output, "request_id", None) is None for output in outputs):
        return {
            request.request_id: output
            for request, output in zip(requests, outputs, strict=True)
        }
    raise RuntimeError("Fast Curation backend returned unknown request identities")


def _close_backend(backend_instance: object | None) -> None:
    if backend_instance is None:
        return
    closer = getattr(backend_instance, "close", None)
    if callable(closer):
        closer()
