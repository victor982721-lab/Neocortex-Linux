"""Embedding backend construction, readiness probes and source prerequisites."""

from __future__ import annotations
import tempfile
import time
import gc
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform.policy import stat_birthtime_ns
from typing import Protocol, cast

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, xxhash

from .semantic_backends import (
    EmbeddingBackend,
    FastEmbedBackend,
    fastembed_availability,
)
from .semantic_chunking import TextTokenCounter
from .semantic_config import (
    COMPACT_TEXT_MODEL_SIGNATURE,
    FASTEMBED_RUNTIME_VERSION,
    SemanticModelUnavailableError,
    default_semantic_parallel,
    default_semantic_model_cache,
    default_semantic_threads,
    local_fastembed_snapshot,
    production_models,
)
from .semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    fingerprint_bytes,
    fingerprint_text,
)
from .semantic_schema import initialize_semantic_state
from .semantic_service_contracts import ModelPreparation
from .semantic_sources import semantic_source_database
from .semantic_state import register_embedding_model


# region [01] Backend contracts and construction


class BackendFactory(Protocol):
    def __call__(
        self,
        model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool,
        threads: int | None,
    ) -> EmbeddingBackend: ...


@dataclass(frozen=True, slots=True)
class ResolvedTextTokenGuard:
    counter: TextTokenCounter
    tokenizer_signature: str
    token_limit: int


def resolve_text_token_guard(
    embedding_backend: object,
    model: EmbeddingModelSpec,
) -> ResolvedTextTokenGuard:
    """Resolve and freeze the exact tokenizer contract before staging."""

    candidate = getattr(embedding_backend, "text_token_counts", None)
    if not callable(candidate):
        raise RuntimeError("text backend has no exact tokenizer counter")
    counter = cast(TextTokenCounter, candidate)
    contract_provider = getattr(
        embedding_backend,
        "text_tokenizer_contract",
        None,
    )
    if callable(contract_provider):
        contract = contract_provider()
        if not isinstance(contract, tuple) or len(contract) != 2:
            raise RuntimeError("text backend returned an invalid tokenizer contract")
        tokenizer_signature, token_limit = contract
    else:
        if model.provider.startswith("fastembed"):
            raise RuntimeError("FastEmbed text backend has no signed tokenizer contract")
        _counts, token_limit = counter(("Neocortex fixture tokenizer contract",))
        identity = f"exact-token-fit-v1\0{model.model_signature}\0{token_limit}"
        tokenizer_signature = (
            f"exact-token-fit-v1:fixture-{HASH_ALGORITHM_128}:"
            f"{xxhash.xxh3_128_hexdigest(identity.encode('utf-8'))}"
        )
    if not isinstance(tokenizer_signature, str) or not tokenizer_signature.strip():
        raise RuntimeError("text backend returned an invalid tokenizer signature")
    if (
        isinstance(token_limit, bool)
        or not isinstance(token_limit, int)
        or not 1 <= token_limit <= 1_000_000
    ):
        raise RuntimeError("text backend returned an invalid tokenizer limit")

    def checked_counter(texts: Sequence[str]) -> tuple[Sequence[int], int]:
        counts, observed_limit = counter(texts)
        if observed_limit != token_limit:
            raise RuntimeError("text tokenizer limit changed after contract binding")
        return counts, observed_limit

    return ResolvedTextTokenGuard(
        checked_counter,
        tokenizer_signature.strip(),
        token_limit,
    )


def _is_local_model_runtime_error(exc: Exception) -> bool:
    module = type(exc).__module__
    return isinstance(exc, (OSError, EOFError, UnicodeError)) or module.startswith(
        (
            "fastembed.",
            "huggingface_hub.",
            "json.",
            "onnxruntime.",
            "tokenizers.",
        )
    )


class _ReadOnlyFastEmbedBackend:
    """Translate only recognized local model-load failures to optional status."""

    def __init__(self, delegate: FastEmbedBackend) -> None:
        self._delegate = delegate

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._delegate.model

    @property
    def max_batch_size(self) -> int:
        return self._delegate.max_batch_size

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        try:
            return self._delegate.embed(requests)
        except Exception as exc:  # optional runtime types are dependency-defined
            if not _is_local_model_runtime_error(exc):
                raise
            raise SemanticModelUnavailableError("semantic_query_model_unloadable") from exc

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        try:
            return self._delegate.text_token_counts(texts)
        except Exception as exc:  # optional runtime types are dependency-defined
            if not _is_local_model_runtime_error(exc):
                raise
            raise SemanticModelUnavailableError("semantic_query_model_unloadable") from exc

    def text_tokenizer_contract(self) -> tuple[str, int]:
        try:
            return self._delegate.text_tokenizer_contract()
        except Exception as exc:  # optional runtime types are dependency-defined
            if not _is_local_model_runtime_error(exc):
                raise
            raise SemanticModelUnavailableError("semantic_query_model_unloadable") from exc


def model_cache(state_directory: Path, override: Path | None) -> Path:
    return default_semantic_model_cache(state_directory) if override is None else override


def require_local_fastembed_model(
    model: EmbeddingModelSpec,
    cache_dir: Path,
) -> None:
    """Validate one exact local snapshot without creating or enumerating paths."""

    local_fastembed_snapshot(model, cache_dir)
    availability = fastembed_availability()
    if not availability.installed:
        raise SemanticModelUnavailableError(
            "semantic_backend_unavailable",
            f"FastEmbed/ONNX CPU is required for {model.model_id}; {availability.detail}",
        )
    expected_version = FASTEMBED_RUNTIME_VERSION.removeprefix("fastembed-")
    if availability.version != expected_version:
        raise SemanticModelUnavailableError(
            "semantic_backend_version_mismatch",
            f"{model.model_id} requires fastembed=={expected_version}; "
            f"observed {availability.version}",
        )
    if "CPUExecutionProvider" not in availability.providers:
        raise SemanticModelUnavailableError(
            "semantic_backend_unavailable",
            f"ONNX Runtime CPUExecutionProvider is required for {model.model_id}; "
            f"observed providers {availability.providers}",
        )


def backend(
    model: EmbeddingModelSpec,
    *,
    cache_dir: Path,
    local_files_only: bool,
    threads: int | None,
) -> EmbeddingBackend:
    if local_files_only:
        require_local_fastembed_model(model, cache_dir)
    selected_threads = default_semantic_threads() if threads is None else threads
    try:
        embedding_backend = FastEmbedBackend(
            model,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
            threads=selected_threads,
            parallel=default_semantic_parallel(selected_threads),
            providers=("CPUExecutionProvider",),
        )
    except Exception as exc:  # optional runtime types are dependency-defined
        if not local_files_only or not _is_local_model_runtime_error(exc):
            raise
        raise SemanticModelUnavailableError("semantic_query_model_unloadable") from exc
    return _ReadOnlyFastEmbedBackend(embedding_backend) if local_files_only else embedding_backend


# endregion [01]


# region [02] Explicit readiness probes


def text_probe(embedding_backend: EmbeddingBackend) -> None:
    text = "Neocortex industrial electrical semantic readiness probe"
    request = EmbeddingRequest(
        request_id="readiness-text",
        role=EmbeddingRole.QUERY,
        fingerprint=fingerprint_text(text),
        text=text,
    )
    embedding_backend.embed((request,))


def image_probe(embedding_backend: EmbeddingBackend) -> None:
    """Load the vision model against a temporary non-user raster and remove it."""

    from PIL import Image, ImageDraw

    handle = tempfile.NamedTemporaryFile(
        prefix="neocortex-semantic-probe-",
        suffix=".png",
        delete=False,
    )
    probe_path = Path(handle.name)
    handle.close()
    try:
        image = Image.new("RGB", (64, 64), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((12, 16, 52, 48), fill="gray", outline="black", width=2)
        image.save(probe_path, format="PNG")
        image.close()
        payload = probe_path.read_bytes()
        fingerprint = fingerprint_bytes(payload)
        source_stat = probe_path.stat()
        embedding_backend.embed(
            (
                EmbeddingRequest(
                    request_id="readiness-image",
                    role=EmbeddingRole.IMAGE,
                    fingerprint=fingerprint,
                    image_path=probe_path,
                    source_revision={
                        "size_bytes": source_stat.st_size,
                        "mtime_ns": source_stat.st_mtime_ns,
                        "birthtime_ns": stat_birthtime_ns(source_stat),
                        "raw_content_xxh3_128": fingerprint.xxh3_128,
                    },
                ),
            )
        )
    finally:
        probe_path.unlink(missing_ok=True)


def prepare_semantic_models(
    state_directory: Path,
    *,
    model_cache_override: Path | None = None,
    include_compact: bool = False,
    model_ids: Sequence[str] | None = None,
    local_files_only: bool = False,
    threads: int | None = None,
    backend_factory: BackendFactory = backend,
) -> tuple[ModelPreparation, ...]:
    """Acquire/load explicit production models; this never indexes user content."""

    cache = model_cache(state_directory, model_cache_override)
    models = list(production_models())
    if model_ids is not None:
        selected_ids = set(model_ids)
        unknown = selected_ids - {model.model_id for model in models}
        if not selected_ids or unknown:
            raise ValueError(f"invalid semantic model selection: {sorted(unknown)!r}")
        models = [model for model in models if model.model_id in selected_ids]
    else:
        models = [
            model
            for model in models
            if include_compact or model.model_signature != COMPACT_TEXT_MODEL_SIGNATURE
        ]
    cache.mkdir(parents=True, exist_ok=True)
    results: list[ModelPreparation] = []
    for model in models:
        started = time.perf_counter()
        embedding_backend = backend_factory(
            model,
            cache_dir=cache,
            local_files_only=local_files_only,
            threads=threads,
        )
        if model.modality is EmbeddingModality.TEXT:
            text_probe(embedding_backend)
        else:
            image_probe(embedding_backend)
        results.append(
            ModelPreparation(
                model.model_signature,
                model.model_id,
                model.dimensions,
                time.perf_counter() - started,
            )
        )
        del embedding_backend
        gc.collect()
    return tuple(results)


# endregion [02]


# region [03] State and source prerequisites


def initialize_models(
    database: Path,
    models: Iterable[EmbeddingModelSpec],
) -> None:
    initialize_semantic_state(database)
    for model in models:
        register_embedding_model(database, model)


def require_source_databases(
    state_directory: Path,
    source_kinds: Iterable[str],
) -> None:
    selected_paths = {
        source_kind: semantic_source_database(state_directory, source_kind)
        for source_kind in source_kinds
    }
    invalid = {
        source_kind: path
        for source_kind, path in selected_paths.items()
        if path.exists() and not path.is_file()
    }
    if invalid:
        details = ", ".join(f"{source_kind}={path}" for source_kind, path in invalid.items())
        raise ValueError(f"semantic source state is not a regular file: {details}")
    missing = {
        source_kind: path for source_kind, path in selected_paths.items() if not path.is_file()
    }
    if missing:
        details = ", ".join(f"{source_kind}={path}" for source_kind, path in missing.items())
        raise FileNotFoundError(f"semantic source state is missing: {details}")


# endregion [03]
