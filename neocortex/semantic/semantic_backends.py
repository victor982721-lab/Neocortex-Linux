"""Pluggable embedding backends and rank-only cross-space fusion."""

from __future__ import annotations
import importlib
import importlib.util
import itertools
import math
import threading
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform.policy import stat_birthtime_ns
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

import xxhash

from .semantic_config import fastembed_cache_contract
from .semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    ExactSearchPage,
    FusedHit,
    FusionEvidence,
    SearchHit,
    normalize_vector,
)


# region [01] Backend protocol and bounded batching


class EmbeddingBackend(Protocol):
    """Minimal interface implemented by production and test encoders."""

    @property
    def model(self) -> EmbeddingModelSpec: ...

    @property
    def max_batch_size(self) -> int: ...

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        """Embed one already-bounded batch in request order."""


def _validated_backend_results(
    model: EmbeddingModelSpec,
    requests: Sequence[EmbeddingRequest],
    results: Sequence[BackendEmbedding],
) -> tuple[BackendEmbedding, ...]:
    if len(results) != len(requests):
        raise RuntimeError(f"backend returned {len(results)} results for {len(requests)} requests")
    validated: list[BackendEmbedding] = []
    for request, result in zip(requests, results, strict=True):
        if request.request_id != result.request_id:
            raise RuntimeError("backend changed request order or identifiers")
        if request.role not in model.supported_roles:
            raise ValueError(
                f"role {request.role.value!r} is unsupported by {model.model_signature}"
            )
        vector, original_norm = normalize_vector(result.vector, model.dimensions)
        provenance = dict(result.provenance)
        provenance["normalized_by_adapter"] = True
        provenance.setdefault("input_l2_norm", original_norm)
        validated.append(
            BackendEmbedding(
                request_id=result.request_id,
                vector=vector,
                provenance=provenance,
            )
        )
    return tuple(validated)


def iter_embedding_batches(
    backend: EmbeddingBackend,
    requests: Iterable[EmbeddingRequest],
    *,
    batch_size: int | None = None,
) -> Iterator[BackendEmbedding]:
    """Submit an input stream in bounded batches and validate every result."""

    selected_size = backend.max_batch_size if batch_size is None else batch_size
    if not 1 <= selected_size <= backend.max_batch_size:
        raise ValueError("batch_size must be within the backend's declared bound")
    iterator = iter(requests)
    while batch := tuple(itertools.islice(iterator, selected_size)):
        for request in batch:
            if request.role not in backend.model.supported_roles:
                raise ValueError(
                    f"role {request.role.value!r} is unsupported by {backend.model.model_signature}"
                )
        yield from _validated_backend_results(
            backend.model,
            batch,
            backend.embed(batch),
        )


# endregion [01]


# region [02] FastEmbed ONNX CPU backend


@dataclass(frozen=True, slots=True)
class BackendAvailability:
    installed: bool
    version: str | None
    providers: tuple[str, ...]
    detail: str


def fastembed_availability() -> BackendAvailability:
    """Inspect FastEmbed/ONNX availability without loading or downloading a model."""

    if importlib.util.find_spec("fastembed") is None:
        return BackendAvailability(False, None, (), "fastembed is not installed")
    try:
        fastembed = importlib.import_module("fastembed")
        version = str(getattr(fastembed, "__version__", "unknown"))
        providers: tuple[str, ...] = ()
        if importlib.util.find_spec("onnxruntime") is not None:
            runtime = importlib.import_module("onnxruntime")
            providers = tuple(str(value) for value in runtime.get_available_providers())
        return BackendAvailability(True, version, providers, "available")
    except Exception as exc:  # optional dependency diagnostics must be honest
        return BackendAvailability(
            False,
            None,
            (),
            f"fastembed import failed: {type(exc).__name__}: {exc}",
        )


class SourceRevisionMismatchError(RuntimeError):
    """Raised rather than persisting an embedding for mutated image bytes."""


class TextTokenLimitExceededError(ValueError):
    """Raised instead of allowing a production tokenizer to truncate text."""


def _path_revision(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    birthtime_ns = stat_birthtime_ns(stat)
    return int(stat.st_size), int(stat.st_mtime_ns), birthtime_ns


def _verify_declared_revision(
    path: Path,
    actual: tuple[int, int, int],
    declared: Mapping[str, object],
) -> None:
    if not ({"size", "size_bytes"} & declared.keys()) or "mtime_ns" not in declared:
        raise ValueError(
            "image requests require declared size[_bytes] and mtime_ns revision fields"
        )
    aliases = {
        "size": actual[0],
        "size_bytes": actual[0],
        "mtime_ns": actual[1],
        "birthtime_ns": actual[2],
    }
    for name, value in aliases.items():
        if name not in declared:
            continue
        candidate = declared[name]
        if isinstance(candidate, bool) or not isinstance(candidate, (int, str)):
            raise ValueError(f"source revision field {name!r} must be an integer")
        try:
            expected = int(candidate)
        except ValueError as exc:
            raise ValueError(f"source revision field {name!r} must be an integer") from exc
        if expected != value:
            raise SourceRevisionMismatchError(
                f"declared {name} no longer matches image source: {path}"
            )


def _raw_file_xxh3_128(path: Path) -> str:
    digest = xxhash.xxh3_128()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _verify_image_source(
    request: EmbeddingRequest,
    *,
    verify_content_digest: bool = True,
) -> tuple[int, int, int]:
    path = request.image_path
    if path is None:
        raise ValueError("image path is required for source verification")
    try:
        before = _path_revision(path)
        _verify_declared_revision(path, before, request.source_revision)
        if not verify_content_digest:
            return before
        expected_digest = request.source_revision.get("raw_content_xxh3_128")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 32
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise ValueError("image requests require source_revision.raw_content_xxh3_128")
        actual_digest = _raw_file_xxh3_128(path)
        after = _path_revision(path)
    except (SourceRevisionMismatchError, ValueError):
        raise
    except OSError as exc:
        raise SourceRevisionMismatchError(
            f"image source is unavailable for XXH3 verification: {path}"
        ) from exc
    if after != before:
        raise SourceRevisionMismatchError(
            f"image source changed while its XXH3 digest was verified: {path}"
        )
    if actual_digest != expected_digest:
        raise SourceRevisionMismatchError(
            f"image bytes no longer match raw_content_xxh3_128: {path}"
        )
    return after


class FastEmbedBackend:
    """FastEmbed adapter with explicit offline/cache/thread controls.

    ``local_files_only`` defaults to true.  Consequently constructing this
    backend never authorizes a model download; a missing cached model fails
    explicitly.  Callers must opt into network acquisition outside this layer.
    """

    def __init__(
        self,
        model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool = True,
        threads: int | None = None,
        providers: Sequence[str] = ("CPUExecutionProvider",),
        batch_size: int | None = None,
        parallel: int | None = None,
    ) -> None:
        if importlib.util.find_spec("fastembed") is None:
            raise RuntimeError("FastEmbedBackend requires the optional fastembed package")
        if threads is not None and threads < 1:
            raise ValueError("threads must be positive")
        if parallel is not None and parallel < 1:
            raise ValueError("parallel must be positive")
        if not providers:
            raise ValueError("at least one ONNX provider must be explicit")
        selected_batch = (
            (64 if model.modality is EmbeddingModality.TEXT else 8)
            if batch_size is None
            else batch_size
        )
        if not 1 <= selected_batch <= 4096:
            raise ValueError("batch_size must be between 1 and 4096")
        runtime = importlib.import_module("fastembed")
        embedding_type = (
            runtime.TextEmbedding
            if model.modality is EmbeddingModality.TEXT
            else runtime.ImageEmbedding
        )
        supported = {
            str(candidate["model"]): int(candidate["dim"])
            for candidate in embedding_type.list_supported_models()
        }
        actual_dimensions = supported.get(model.model_id)
        if actual_dimensions is None:
            raise ValueError(
                f"FastEmbed does not list {model.model_id!r} for {model.modality.value} embeddings"
            )
        if actual_dimensions != model.dimensions:
            raise ValueError(
                f"model declares {model.dimensions} dimensions but FastEmbed reports "
                f"{actual_dimensions}"
            )
        self._model_spec = model
        self._cache_dir = Path(cache_dir)
        self._batch_size = selected_batch
        self._parallel = parallel
        self._tokenizer_lock = threading.RLock()
        self._runtime_model = embedding_type(
            model_name=model.model_id,
            cache_dir=str(cache_dir),
            threads=threads,
            providers=tuple(providers),
            lazy_load=True,
            local_files_only=local_files_only,
        )

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model_spec

    @property
    def max_batch_size(self) -> int:
        return self._batch_size

    def _embed_text_role(
        self,
        role: EmbeddingRole,
        requests: Sequence[EmbeddingRequest],
    ) -> list[tuple[int, BackendEmbedding]]:
        indexed = [
            (index, request) for index, request in enumerate(requests) if request.role is role
        ]
        if not indexed:
            return []
        texts = [request.text for _, request in indexed]
        if any(text is None for text in texts):
            raise ValueError("FastEmbed text requests require text payloads")
        selected_texts = [str(text) for text in texts]
        with self._tokenizer_lock:
            token_counts, token_limit = self.text_token_counts(selected_texts)
            oversized = [
                (request.request_id, token_count)
                for (_, request), token_count in zip(indexed, token_counts, strict=True)
                if token_count > token_limit
            ]
            if oversized:
                request_id, token_count = oversized[0]
                raise TextTokenLimitExceededError(
                    f"request {request_id!r} requires {token_count} tokens but "
                    f"{self.model.model_id!r} accepts {token_limit}; refusing truncation"
                )
            kwargs = {"batch_size": self._batch_size, "parallel": self._parallel}
            if role is EmbeddingRole.QUERY:
                vectors = tuple(self._runtime_model.query_embed(selected_texts, **kwargs))
            else:
                vectors = tuple(self._runtime_model.passage_embed(selected_texts, **kwargs))
        output: list[tuple[int, BackendEmbedding]] = []
        for (index, request), vector, token_count in zip(
            indexed,
            vectors,
            token_counts,
            strict=True,
        ):
            output.append(
                (
                    index,
                    BackendEmbedding(
                        request_id=request.request_id,
                        vector=tuple(float(value) for value in vector),
                        provenance={
                            "backend": "fastembed",
                            "role": role.value,
                            "model_id": self.model.model_id,
                            "token_count": token_count,
                            "token_limit": token_limit,
                            "token_truncated": False,
                        },
                    ),
                )
            )
        if len(output) != len(indexed):
            raise RuntimeError("FastEmbed returned fewer text vectors than requested")
        return output

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        """Return exact untruncated counts from the pinned production tokenizer."""

        if self.model.modality is not EmbeddingModality.TEXT:
            raise ValueError("token counts require a text embedding model")
        if not texts:
            raise ValueError("token counts require at least one text")
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("token count payloads must be strings")
        return self._untruncated_token_counts(texts)

    def text_tokenizer_contract(self) -> tuple[str, int]:
        """Bind exact token sizing to the cached model snapshot revision."""

        _, token_limit = self.text_token_counts(("Neocortex exact tokenizer contract",))
        contract = fastembed_cache_contract(self.model.model_signature)
        reference = (
            self._cache_dir
            / ("models--" + contract.repository_id.replace("/", "--"))
            / "refs"
            / "main"
        )
        try:
            revision = reference.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("FastEmbed tokenizer cache revision is unavailable") from exc
        if not 40 <= len(revision) <= 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise RuntimeError("FastEmbed tokenizer cache revision is invalid")
        identity = (
            f"exact-token-fit-v1\0{self.model.model_signature}\0"
            f"{contract.repository_id}\0{revision}\0{token_limit}"
        )
        digest = xxhash.xxh3_128_hexdigest(identity.encode("utf-8"))
        return f"exact-token-fit-v1:xxh3-128:{digest}", token_limit

    def _untruncated_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        """Count exact tokenizer inputs while restoring FastEmbed truncation."""

        inner_model: Any = getattr(self._runtime_model, "model", None)
        if inner_model is None:
            raise RuntimeError("FastEmbed text model does not expose its tokenizer")
        with self._tokenizer_lock:
            if getattr(inner_model, "tokenizer", None) is None:
                inner_model.load_onnx_model()
            tokenizer: Any = inner_model.tokenizer
            truncation = tokenizer.truncation
            if not isinstance(truncation, dict) or not isinstance(
                truncation.get("max_length"), int
            ):
                raise RuntimeError("FastEmbed tokenizer has no explicit truncation contract")
            token_limit = int(truncation["max_length"])
            tokenizer.no_truncation()
            try:
                encodings = tokenizer.encode_batch(list(texts))
                counts = tuple(int(sum(encoding.attention_mask)) for encoding in encodings)
            finally:
                tokenizer.enable_truncation(**truncation)
        return counts, token_limit

    def _embed_images(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> tuple[BackendEmbedding, ...]:
        images: list[Path] = []
        before_revisions: list[tuple[int, int, int]] = []
        for request in requests:
            if request.role is not EmbeddingRole.IMAGE:
                raise ValueError("image backends accept only the image role")
            if request.image_path is None:
                raise ValueError(
                    "FastEmbed image requests require image_path; decode bytes in the "
                    "validated image route before submission"
                )
            images.append(request.image_path)
            before_revisions.append(_verify_image_source(request))
        vectors = self._runtime_model.embed(
            images,
            batch_size=self._batch_size,
            parallel=self._parallel,
        )
        raw_output = tuple(
            BackendEmbedding(
                request_id=request.request_id,
                vector=tuple(float(value) for value in vector),
                provenance={
                    "backend": "fastembed",
                    "role": EmbeddingRole.IMAGE.value,
                    "model_id": self.model.model_id,
                },
            )
            for request, vector in zip(requests, vectors, strict=True)
        )
        if len(raw_output) != len(requests):
            raise RuntimeError("FastEmbed returned fewer image vectors than requested")
        output: list[BackendEmbedding] = []
        for request, before, result in zip(
            requests,
            before_revisions,
            raw_output,
            strict=True,
        ):
            after = _verify_image_source(request, verify_content_digest=False)
            if after != before:
                raise SourceRevisionMismatchError(
                    f"image source revision changed during embedding: {request.image_path}"
                )
            provenance = dict(result.provenance)
            provenance["source_revision_verified_before_after"] = True
            provenance["source_content_xxh3_128_verified"] = True
            provenance["source_stat_revision"] = {
                "size_bytes": after[0],
                "mtime_ns": after[1],
                "birthtime_ns": after[2],
            }
            output.append(
                BackendEmbedding(
                    request_id=result.request_id,
                    vector=result.vector,
                    provenance=provenance,
                )
            )
        return tuple(output)

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        if not requests:
            return ()
        if len(requests) > self.max_batch_size:
            raise ValueError("request batch exceeds the configured FastEmbed bound")
        if self.model.modality is EmbeddingModality.IMAGE:
            return _validated_backend_results(
                self.model,
                requests,
                self._embed_images(requests),
            )
        indexed = self._embed_text_role(EmbeddingRole.QUERY, requests)
        indexed.extend(self._embed_text_role(EmbeddingRole.PASSAGE, requests))
        indexed.sort(key=lambda pair: pair[0])
        ordered = tuple(result for _, result in indexed)
        return _validated_backend_results(self.model, requests, ordered)


# endregion [02]


# region [03] Exact-page merge and reciprocal-rank fusion


def merge_exact_search_pages(
    pages: Iterable[ExactSearchPage],
    *,
    limit: int,
) -> tuple[SearchHit, ...]:
    """Merge page-local top-k results into the exact global top-k."""

    if limit < 1:
        raise ValueError("limit must be positive")
    best: dict[str, SearchHit] = {}
    for page in pages:
        for hit in page.hits:
            prior = best.get(hit.item_id)
            if (
                prior is None
                or hit.score > prior.score
                or (
                    hit.score == prior.score
                    and (hit.indexed_model_signature, hit.entity_id)
                    < (prior.indexed_model_signature, prior.entity_id)
                )
            ):
                best[hit.item_id] = hit
    return tuple(
        sorted(best.values(), key=lambda hit: (-hit.score, hit.item_id, hit.entity_id))[:limit]
    )


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[SearchHit]],
    *,
    k: float = 60.0,
    limit: int = 100,
    weights: Mapping[str, float] | None = None,
) -> tuple[FusedHit, ...]:
    """Fuse independent rankings by item rank, never by raw vector scores."""

    if not math.isfinite(k) or k <= 0:
        raise ValueError("k must be a positive finite value")
    if limit < 1:
        raise ValueError("limit must be positive")
    selected_weights = {} if weights is None else dict(weights)
    unknown_weights = set(selected_weights).difference(rankings)
    if unknown_weights:
        raise ValueError(f"weights name unknown rankings: {sorted(unknown_weights)}")
    totals: dict[str, float] = {}
    evidence: dict[str, list[FusionEvidence]] = {}
    for ranking_name, hits in rankings.items():
        if not ranking_name.strip():
            raise ValueError("ranking names cannot be blank")
        weight = float(selected_weights.get(ranking_name, 1.0))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("ranking weights must be positive and finite")
        seen_items: set[str] = set()
        item_rank = 0
        for hit in hits:
            if hit.item_id in seen_items:
                continue
            seen_items.add(hit.item_id)
            item_rank += 1
            contribution = weight / (k + item_rank)
            totals[hit.item_id] = totals.get(hit.item_id, 0.0) + contribution
            evidence.setdefault(hit.item_id, []).append(
                FusionEvidence(
                    ranking=ranking_name,
                    rank=item_rank,
                    raw_score=hit.score,
                    contribution=contribution,
                    entity_id=hit.entity_id,
                    indexed_model_signature=hit.indexed_model_signature,
                    query_model_signature=hit.query_model_signature,
                    ref_id=hit.ref_id,
                    generation_id=hit.generation_id,
                )
            )
    ordered = sorted(totals, key=lambda item_id: (-totals[item_id], item_id))[:limit]
    return tuple(
        FusedHit(
            item_id=item_id,
            score=totals[item_id],
            evidence=tuple(
                sorted(
                    evidence[item_id],
                    key=lambda value: (value.ranking, value.rank),
                )
            ),
        )
        for item_id in ordered
    )


# endregion [03]
