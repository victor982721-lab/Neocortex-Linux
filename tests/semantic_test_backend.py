"""Deterministic embedding backend reserved for semantic test fixtures."""
# region [00] Contexto del módulo
# Módulo: tests/semantic_test_backend.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from typing import Sequence

from neocortex.foundation.hash_compat import xxhash

from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModelSpec,
    EmbeddingRequest,
    normalize_vector,
)
# endregion [01]

# region [02] Implementación


class DeterministicTestBackend:
    """Produce stable non-semantic vectors without a model dependency."""

    def __init__(
        self,
        model: EmbeddingModelSpec,
        *,
        batch_size: int = 32,
    ) -> None:
        if model.provider != "test-deterministic":
            raise ValueError(
                "DeterministicTestBackend requires the test-deterministic provider"
            )
        if not 1 <= batch_size <= 4096:
            raise ValueError("batch_size must be between 1 and 4096")
        self._model = model
        self._batch_size = batch_size

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return self._batch_size

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        if len(requests) > self.max_batch_size:
            raise ValueError("request batch exceeds the deterministic test bound")
        results: list[BackendEmbedding] = []
        for request in requests:
            if request.role not in self.model.supported_roles:
                raise ValueError(f"unsupported test role {request.role.value!r}")
            seed_material = (
                f"{self.model.model_signature}\0{request.role.value}\0"
                f"{request.fingerprint.xxh3_128}\0"
                f"{request.fingerprint.xxh3_64_guard}"
            ).encode("utf-8")
            vector = []
            for dimension in range(self.model.dimensions):
                raw = xxhash.xxh3_64_intdigest(seed_material, seed=dimension)
                vector.append((raw / ((1 << 64) - 1)) * 2.0 - 1.0)
            normalized, _ = normalize_vector(vector, self.model.dimensions)
            results.append(
                BackendEmbedding(
                    request_id=request.request_id,
                    vector=normalized,
                    provenance={"backend": "deterministic-test-only"},
                )
            )
        return tuple(results)

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        return tuple(len(text.split()) + 2 for text in texts), 512

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return "deterministic-test-tokenizer-v1", 512


# endregion [02]
