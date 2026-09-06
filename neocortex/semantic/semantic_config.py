"""Pinned semantic model contracts and resource defaults for Neocortex."""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .semantic_chunking import TextChunkingConfig
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    VectorDType,
)


# region [01] Processing signatures and model identifiers

SEMANTIC_PIPELINE_VERSION = "neocortex-semantic-pipeline-v2"
FASTEMBED_RUNTIME_VERSION = "fastembed-0.8.0"
TEXT_ENCODER_CONTRACT_VERSION = (
    f"{FASTEMBED_RUNTIME_VERSION}|explicit-l2-v1|reject-token-truncation-v1"
)
IMAGE_ENCODER_CONTRACT_VERSION = f"{FASTEMBED_RUNTIME_VERSION}|explicit-l2-v1|source-xxh3-verify-v1"

TEXT_MODEL_ID = "jinaai/jina-embeddings-v2-base-es"
TEXT_MODEL_SIGNATURE = f"{TEXT_ENCODER_CONTRACT_VERSION}|{TEXT_MODEL_ID}|float16"
TEXT_VECTOR_SPACE = "jina-embeddings-v2-base-es-v1"

# Retrieval-only abstention floor revalidated against the current mixed
# document corpus with positive paraphrases and negative controls.  The lower
# common floor retains relevant PDF siblings previously discarded at 0.50,
# while applying the same fail-closed rule to Office/DOCX results that used to
# bypass calibration entirely.  Index-time quality gates remove encoded binary
# and formula dumps before this score policy is evaluated.  Scores remain
# cosine similarities, never probabilities or classification confidence.
TEXT_RETRIEVAL_CALIBRATION_SIGNATURE = "semantic-text-retrieval-abstention-jina-mixed-v2"
TEXT_RETRIEVAL_CALIBRATION_BACKEND = "fastembed"
TEXT_RETRIEVAL_SCORE_FLOORS = (
    ("archive", 0.42),
    ("audio", 0.42),
    ("code", 0.42),
    ("docx", 0.42),
    ("image", 0.42),
    ("odt", 0.42),
    ("pdf", 0.42),
    ("pptx", 0.42),
    ("text", 0.42),
    ("xlsx", 0.42),
)

COMPACT_TEXT_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
COMPACT_TEXT_MODEL_SIGNATURE = (
    f"{TEXT_ENCODER_CONTRACT_VERSION}|{COMPACT_TEXT_MODEL_ID}|mean-pooling|float16"
)
COMPACT_TEXT_VECTOR_SPACE = "paraphrase-multilingual-minilm-l12-v2-mean-pooling"

CLIP_TEXT_MODEL_ID = "Qdrant/clip-ViT-B-32-text"
CLIP_IMAGE_MODEL_ID = "Qdrant/clip-ViT-B-32-vision"
CLIP_VECTOR_SPACE = "openai-clip-vit-b-32-shared-v1"
CLIP_TEXT_MODEL_SIGNATURE = f"{TEXT_ENCODER_CONTRACT_VERSION}|{CLIP_TEXT_MODEL_ID}|float16"
CLIP_IMAGE_MODEL_SIGNATURE = f"{IMAGE_ENCODER_CONTRACT_VERSION}|{CLIP_IMAGE_MODEL_ID}|float16"


# endregion [01]


# region [02] Typed model specifications


def multilingual_text_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        model_signature=TEXT_MODEL_SIGNATURE,
        vector_space=TEXT_VECTOR_SPACE,
        modality=EmbeddingModality.TEXT,
        model_id=TEXT_MODEL_ID,
        model_version="fastembed-registry-0.8.0",
        dimensions=768,
        provider="fastembed-onnx-cpu",
        supported_roles=(EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
        vector_dtype=VectorDType.FLOAT16,
        provenance={
            "license": "Apache-2.0",
            "languages": "Spanish-English",
            "normalization": "explicit-l2-in-adapter",
            "calibration": "retrieval-only-not-classification-calibrated",
            "selection": "quality-profile-local-retrieval-smoke-v1",
        },
    )


def text_retrieval_score_floor(
    *,
    model_signature: str,
    pipeline: object,
    backend: object,
    source_kind: str,
) -> float | None:
    """Return an exact-contract retrieval floor, or abstain from calibration."""

    if (
        model_signature != TEXT_MODEL_SIGNATURE
        or pipeline != SEMANTIC_PIPELINE_VERSION
        or backend != TEXT_RETRIEVAL_CALIBRATION_BACKEND
    ):
        return None
    return next(
        (
            floor
            for calibrated_source, floor in TEXT_RETRIEVAL_SCORE_FLOORS
            if source_kind == calibrated_source
        ),
        None,
    )


def compact_multilingual_text_model() -> EmbeddingModelSpec:
    """Lower-storage fallback kept in a vector space separate from quality mode."""

    return EmbeddingModelSpec(
        model_signature=COMPACT_TEXT_MODEL_SIGNATURE,
        vector_space=COMPACT_TEXT_VECTOR_SPACE,
        modality=EmbeddingModality.TEXT,
        model_id=COMPACT_TEXT_MODEL_ID,
        model_version="fastembed-registry-0.8.0-mean-pooling",
        dimensions=384,
        provider="fastembed-onnx-cpu",
        supported_roles=(EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
        vector_dtype=VectorDType.FLOAT16,
        provenance={
            "license": "Apache-2.0",
            "languages": "multilingual-about-50",
            "normalization": "explicit-l2-in-adapter",
            "calibration": "retrieval-only-not-classification-calibrated",
            "profile": "compact",
        },
    )


def clip_text_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        model_signature=CLIP_TEXT_MODEL_SIGNATURE,
        vector_space=CLIP_VECTOR_SPACE,
        modality=EmbeddingModality.TEXT,
        model_id=CLIP_TEXT_MODEL_ID,
        model_version="openai-clip-vit-b-32-fastembed-registry-0.8.0",
        dimensions=512,
        provider="fastembed-onnx-cpu",
        supported_roles=(EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
        vector_dtype=VectorDType.FLOAT16,
        provenance={
            "license": "MIT",
            "purpose": "shared text-to-image retrieval space",
            "normalization": "explicit-l2-in-adapter",
            "calibration": "retrieval-only-not-classification-calibrated",
        },
    )


def clip_image_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        model_signature=CLIP_IMAGE_MODEL_SIGNATURE,
        vector_space=CLIP_VECTOR_SPACE,
        modality=EmbeddingModality.IMAGE,
        model_id=CLIP_IMAGE_MODEL_ID,
        model_version="openai-clip-vit-b-32-fastembed-registry-0.8.0",
        dimensions=512,
        provider="fastembed-onnx-cpu",
        supported_roles=(EmbeddingRole.IMAGE,),
        vector_dtype=VectorDType.FLOAT16,
        provenance={
            "license": "MIT",
            "purpose": "shared image-to-text retrieval space",
            "normalization": "explicit-l2-in-adapter",
            "calibration": "retrieval-only-not-classification-calibrated",
        },
    )


def production_models() -> tuple[EmbeddingModelSpec, ...]:
    return (
        multilingual_text_model(),
        compact_multilingual_text_model(),
        clip_text_model(),
        clip_image_model(),
    )


def text_chunking_for_model(model: EmbeddingModelSpec) -> TextChunkingConfig:
    """Choose a conservative pre-tokenizer window for a pinned text encoder.

    The production staging route also fits every candidate with the exact
    tokenizer and the FastEmbed boundary independently refuses truncation.
    These bounds retain natural context and overlap before that exact guard.
    """

    from .semantic_chunking import TextChunkingConfig

    if model.model_signature == COMPACT_TEXT_MODEL_SIGNATURE:
        return TextChunkingConfig(
            max_chars=448,
            max_terms=80,
            overlap_chars=64,
            overlap_terms=12,
            min_natural_break_chars=64,
            algorithm_version="natural-window-minilm-128-exact-token-guard-v2",
        )
    if model.model_signature == TEXT_MODEL_SIGNATURE:
        return TextChunkingConfig(
            max_chars=1_600,
            max_terms=280,
            overlap_chars=192,
            overlap_terms=40,
            min_natural_break_chars=128,
            algorithm_version="natural-window-jina-512-exact-token-guard-v2",
        )
    return TextChunkingConfig(
        max_chars=1_024,
        max_terms=192,
        overlap_chars=128,
        overlap_terms=24,
        min_natural_break_chars=96,
        algorithm_version="natural-window-unknown-model-exact-token-guard-v2",
    )


# endregion [02]


# region [03] Read-only FastEmbed cache contracts


_TEXT_RUNTIME_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


@dataclass(frozen=True, slots=True)
class FastEmbedCacheContract:
    """Pinned Hugging Face snapshot files needed before backend construction."""

    repository_id: str
    required_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository_id, str):
            raise ValueError("repository_id must be a string")
        repository_parts = self.repository_id.split("/")
        if (
            len(repository_parts) != 2
            or "\\" in self.repository_id
            or any(
                not part or any(character.isspace() for character in part) or part in {".", ".."}
                for part in repository_parts
            )
        ):
            raise ValueError("repository_id must be a canonical owner/name pair")
        if not isinstance(self.required_files, tuple):
            raise ValueError("required_files must be a tuple of strings")
        if not self.required_files:
            raise ValueError("required_files must be nonempty and unique")
        for value in self.required_files:
            if not isinstance(value, str):
                raise ValueError("required model files must be strings")
        if len(set(self.required_files)) != len(self.required_files):
            raise ValueError("required_files must be nonempty and unique")
        for value in self.required_files:
            raw_parts = value.split("/")
            if (
                not value.strip()
                or value.startswith("/")
                or value.endswith("/")
                or "\\" in value
                or any(part.strip() in {"", ".", ".."} for part in raw_parts)
            ):
                raise ValueError("required model files must be safe relative paths")
            path = PurePosixPath(value)
            if path.is_absolute() or path.parts != tuple(raw_parts):
                raise ValueError("required model files must be safe relative paths")


_FASTEMBED_CACHE_CONTRACTS = {
    TEXT_MODEL_SIGNATURE: FastEmbedCacheContract(
        repository_id=TEXT_MODEL_ID,
        required_files=("onnx/model.onnx", *_TEXT_RUNTIME_FILES),
    ),
    COMPACT_TEXT_MODEL_SIGNATURE: FastEmbedCacheContract(
        repository_id="qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
        required_files=("model_optimized.onnx", *_TEXT_RUNTIME_FILES),
    ),
    CLIP_TEXT_MODEL_SIGNATURE: FastEmbedCacheContract(
        repository_id=CLIP_TEXT_MODEL_ID,
        required_files=("model.onnx", *_TEXT_RUNTIME_FILES),
    ),
    CLIP_IMAGE_MODEL_SIGNATURE: FastEmbedCacheContract(
        repository_id=CLIP_IMAGE_MODEL_ID,
        required_files=("model.onnx", "preprocessor_config.json"),
    ),
}


def fastembed_cache_contract(model_signature: str) -> FastEmbedCacheContract:
    """Return the explicit local-cache contract for one production model."""

    try:
        return _FASTEMBED_CACHE_CONTRACTS[model_signature]
    except KeyError as exc:
        raise ValueError(f"no FastEmbed cache contract for model: {model_signature}") from exc


class SemanticModelUnavailableError(RuntimeError):
    """An exact local model prerequisite is missing or cannot be used."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        if not reason.strip():
            raise ValueError("semantic model unavailability reason cannot be blank")
        self.reason = reason
        self.detail = detail
        super().__init__(reason if detail is None else f"{reason}: {detail}")


def local_fastembed_snapshot(model: EmbeddingModelSpec, cache_dir: Path) -> Path:
    """Inspect the requested pinned snapshot only, without importing a backend.

    File presence is not proof of model compatibility or successful inference.
    The backend and exact tokenizer contracts are checked separately at use.
    """

    contract = fastembed_cache_contract(model.model_signature)
    if not cache_dir.is_dir():
        raise SemanticModelUnavailableError(
            "semantic_model_cache_missing", f"{model.model_id} requires a local cache at {cache_dir}",
        )
    repository = cache_dir / ("models--" + contract.repository_id.replace("/", "--"))
    reference = repository / "refs" / "main"
    if not reference.is_file():
        raise SemanticModelUnavailableError(
            "semantic_query_model_not_cached", f"{model.model_id} requires {reference}",
        )
    try:
        if not 1 <= reference.stat().st_size <= 256:
            raise SemanticModelUnavailableError("semantic_query_model_cache_invalid")
        commit = reference.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise SemanticModelUnavailableError("semantic_query_model_cache_invalid") from exc
    if not 40 <= len(commit) <= 64 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise SemanticModelUnavailableError("semantic_query_model_cache_invalid")
    snapshot = repository / "snapshots" / commit
    if not snapshot.is_dir():
        raise SemanticModelUnavailableError(
            "semantic_query_model_not_cached", f"{model.model_id} requires snapshot {snapshot}",
        )
    for relative_path in contract.required_files:
        candidate = snapshot.joinpath(*relative_path.split("/"))
        try:
            valid = candidate.is_file() and candidate.stat().st_size > 0
        except OSError as exc:
            raise SemanticModelUnavailableError("semantic_query_model_cache_invalid") from exc
        if not valid:
            raise SemanticModelUnavailableError(
                "semantic_query_model_cache_incomplete",
                f"{model.model_id} requires nonempty local file {candidate}",
            )
    return snapshot


# endregion [03]


# region [04] Bounded local runtime defaults


def default_semantic_model_cache(state_directory: Path) -> Path:
    if os.name != "nt":
        from neocortex.platform.policy import current_platform_policy

        return current_platform_policy().models_directory / "fastembed"
    return state_directory.parent / "models" / "fastembed"


def default_semantic_threads() -> int:
    from neocortex.runtime.control.cpu_runtime import effective_cpu_count

    return max(1, min(8, effective_cpu_count()))


def default_semantic_parallel(threads: int) -> int:
    """Choose bounded FastEmbed batch parallelism without oversubscription."""

    if threads < 1:
        raise ValueError("semantic threads must be positive")
    # The production generation runner is itself a multiprocessing child.
    # FastEmbed's ``parallel>1`` tries to create grandchildren there, which
    # Python rejects for nested/daemonic workers. Keep the safe single-batch
    # path inside that process and reserve overlap for direct callers.
    import multiprocessing

    process = multiprocessing.current_process()
    if process.daemon or process.name != "MainProcess":
        return 1
    # ``threads`` controls ONNX intra-model work; ``parallel`` overlaps
    # independent FastEmbed batches. Two workers are useful on the supported
    # hosts, while smaller thread pools stay single-worker to bound memory.
    return max(1, min(2, threads // 8))


# endregion [04]
