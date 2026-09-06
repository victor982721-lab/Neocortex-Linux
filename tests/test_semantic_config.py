# region [00] Contexto del módulo
# Módulo: tests/test_semantic_config.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import multiprocessing
from typing import Any

import pytest

from neocortex.semantic.semantic_config import (
    FastEmbedCacheContract,
    default_semantic_parallel,
    fastembed_cache_contract,
    multilingual_text_model,
    production_models,
    text_chunking_for_model,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


def _contract(
    repository_id: Any = "owner/model",
    required_files: Any = ("model.onnx",),
) -> FastEmbedCacheContract:
    return FastEmbedCacheContract(repository_id, required_files)


def test_production_fastembed_cache_contracts_remain_valid() -> None:
    contracts = tuple(
        fastembed_cache_contract(model.model_signature) for model in production_models()
    )

    assert len(contracts) == 4
    assert all(contract.repository_id.count("/") == 1 for contract in contracts)
    assert all(contract.required_files for contract in contracts)


def test_retrieval_policy_does_not_mutate_the_registered_model_contract() -> None:
    model = multilingual_text_model()

    assert "retrieval_abstention" not in model.provenance


def test_quality_text_chunking_keeps_measured_jina_headroom() -> None:
    chunking = text_chunking_for_model(multilingual_text_model())

    assert (
        chunking.max_chars,
        chunking.max_terms,
        chunking.overlap_chars,
        chunking.overlap_terms,
        chunking.min_natural_break_chars,
    ) == (1_600, 280, 192, 40, 128)
    assert "jina-512-exact-token-guard-v2" in chunking.algorithm_version


@pytest.mark.parametrize("threads,parallel", ((1, 1), (7, 1), (8, 1), (16, 2), (64, 2)))
def test_semantic_parallelism_is_bounded_by_thread_pool(
    threads: int,
    parallel: int,
) -> None:
    assert default_semantic_parallel(threads) == parallel


def test_semantic_parallelism_rejects_nonpositive_threads() -> None:
    with pytest.raises(ValueError, match="semantic threads must be positive"):
        default_semantic_parallel(0)


def test_semantic_parallelism_disables_grandchildren_in_daemonic_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        multiprocessing,
        "current_process",
        lambda: type("Worker", (), {"daemon": True})(),
    )
    assert default_semantic_parallel(16) is None


def test_semantic_parallelism_disables_nested_spawn_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        multiprocessing,
        "current_process",
        lambda: type("SpawnWorker", (), {"daemon": False, "name": "SpawnProcess-1"})(),
    )
    assert default_semantic_parallel(16) is None


@pytest.mark.parametrize("repository_id", (None, 42, True))
def test_repository_id_rejects_non_string_values(repository_id: object) -> None:
    with pytest.raises(ValueError, match=r"^repository_id must be a string$"):
        _contract(repository_id=repository_id)


@pytest.mark.parametrize(
    "repository_id",
    (
        "",
        " ",
        "/name",
        "owner/",
        "   /name",
        "owner/   ",
        " owner/name",
        "owner/name ",
        "owner//name",
        "owner/name/",
        "owner/name/extra",
        "./name",
        "../name",
        "owner/.",
        "owner/..",
        r"owner\nested/name",
        r"owner/name\nested",
    ),
)
def test_repository_id_rejects_noncanonical_owner_name_pairs(
    repository_id: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"^repository_id must be a canonical owner/name pair$",
    ):
        _contract(repository_id=repository_id)


@pytest.mark.parametrize("required_files", ([], "model.onnx"))
def test_required_files_rejects_non_tuple_containers(required_files: object) -> None:
    with pytest.raises(
        ValueError,
        match=r"^required_files must be a tuple of strings$",
    ):
        _contract(required_files=required_files)


@pytest.mark.parametrize("required_file", (None, 42, True))
def test_required_files_reject_non_string_entries(required_file: object) -> None:
    with pytest.raises(ValueError, match=r"^required model files must be strings$"):
        _contract(required_files=(required_file,))


@pytest.mark.parametrize(
    "required_file",
    (
        "",
        "   ",
        "/model.onnx",
        "model.onnx/",
        "onnx//model.onnx",
        "./model.onnx",
        "onnx/./model.onnx",
        "../model.onnx",
        "onnx/../model.onnx",
        r"onnx\model.onnx",
        "onnx/   /model.onnx",
    ),
)
def test_required_files_reject_unsafe_raw_relative_paths(
    required_file: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"^required model files must be safe relative paths$",
    ):
        _contract(required_files=(required_file,))


def test_required_files_remain_nonempty_and_unique() -> None:
    with pytest.raises(
        ValueError,
        match=r"^required_files must be nonempty and unique$",
    ):
        _contract(required_files=())
    with pytest.raises(
        ValueError,
        match=r"^required_files must be nonempty and unique$",
    ):
        _contract(required_files=("model.onnx", "model.onnx"))


def test_valid_nested_required_files_preserve_exact_contract_values() -> None:
    contract = _contract(
        repository_id="qdrant/model-name_v1",
        required_files=("onnx/model.onnx", "config.json"),
    )

    assert contract.repository_id == "qdrant/model-name_v1"
    assert contract.required_files == ("onnx/model.onnx", "config.json")


# endregion [02]
