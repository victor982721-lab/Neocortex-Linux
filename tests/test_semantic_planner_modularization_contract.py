"""Contracts for the incremental Semantic planner module boundaries."""
# region [00] Contexto del módulo
# Módulo: tests/test_semantic_planner_modularization_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from neocortex.semantic import semantic_planner, semantic_service


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


EXPECTED_PLANNER_ALL = [
    "CONTENT_BATCH_SIZE",
    "DEFAULT_MAX_SCRATCH_BYTES",
    "PLAN_ALGORITHM_VERSION",
    "SemanticPlanBlocked",
    "SemanticScratchLimitExceeded",
    "plan_semantic_index",
    "semantic_plan_payload",
]
EXPECTED_PLANNER_SIGNATURE = (
    "(state_directory: 'Path', *, scope: 'str' = 'all', "
    "source_kinds: 'Sequence[str]' = ('pdf', 'docx', 'xlsx', 'pptx', 'odt', "
    "'audio', 'archive', 'text', 'video'), text_model: "
    "'EmbeddingModelSpec | None' = None, "
    "embed_ocr_text: 'bool' = True, chunking: 'TextChunkingConfig | None' = "
    "None, cost_calibrations: 'Sequence[SemanticCostCalibration]' = (), "
    "execution_signature: 'str | None' = None, scratch_directory: 'Path | "
    "None' = None, max_scratch_bytes: 'int' = 536870912, "
    "cancellation_check: 'CancellationCheck | None' = None) -> 'SemanticPlan'"
)
EXPECTED_SERVICE_SIGNATURE = EXPECTED_PLANNER_SIGNATURE.replace(
    "'CancellationCheck | None'",
    "'Callable[[], None] | None'",
)


def test_planner_and_service_keep_distinct_public_boundaries() -> None:
    assert semantic_planner.__all__ == EXPECTED_PLANNER_ALL
    assert str(inspect.signature(semantic_planner.plan_semantic_index)) == (
        EXPECTED_PLANNER_SIGNATURE
    )
    assert str(inspect.signature(semantic_service.plan_semantic_index)) == (
        EXPECTED_SERVICE_SIGNATURE
    )
    assert semantic_planner.plan_semantic_index.__module__ == (
        "neocortex.semantic.semantic_planner"
    )
    assert semantic_service.plan_semantic_index.__module__ == (
        "neocortex.semantic.semantic_service"
    )
    assert semantic_service.plan_semantic_index is not (semantic_planner.plan_semantic_index)
    assert semantic_service.semantic_plan_payload is not (semantic_planner.semantic_plan_payload)


def test_planner_exception_identity_module_and_pickle_are_stable() -> None:
    blocked = semantic_planner.SemanticPlanBlocked
    scratch = semantic_planner.SemanticScratchLimitExceeded

    assert semantic_service.SemanticPlanBlocked is blocked
    assert issubclass(scratch, blocked)
    assert blocked.__module__ == "neocortex.semantic.semantic_plan_errors"
    assert scratch.__module__ == "neocortex.semantic.semantic_plan_errors"
    for exception_type in (blocked, scratch):
        original = exception_type("stable semantic planner exception")
        restored = pickle.loads(pickle.dumps(original))
        assert type(restored) is exception_type
        assert restored.args == original.args


def test_extracted_error_module_reexports_exact_exception_objects() -> None:
    errors = __import__(
        "neocortex.semantic.semantic_plan_errors",
        fromlist=["SemanticPlanBlocked"],
    )

    assert errors.__all__ == [
        "SemanticPlanBlocked",
        "SemanticScratchLimitExceeded",
    ]
    assert errors.SemanticPlanBlocked is semantic_planner.SemanticPlanBlocked
    assert errors.SemanticScratchLimitExceeded is (semantic_planner.SemanticScratchLimitExceeded)
    assert errors.SemanticPlanBlocked.__module__ == ("neocortex.semantic.semantic_plan_errors")
    assert errors.SemanticScratchLimitExceeded.__module__ == (
        "neocortex.semantic.semantic_plan_errors"
    )


def test_extracted_scratch_module_keeps_planner_compatibility_aliases() -> None:
    scratch = __import__(
        "neocortex.semantic.semantic_plan_scratch",
        fromlist=["CONTENT_BATCH_SIZE"],
    )

    assert scratch.__all__ == [
        "CONTENT_BATCH_SIZE",
        "DEFAULT_MAX_SCRATCH_BYTES",
    ]
    assert scratch.CONTENT_BATCH_SIZE == semantic_planner.CONTENT_BATCH_SIZE
    assert scratch.DEFAULT_MAX_SCRATCH_BYTES == (semantic_planner.DEFAULT_MAX_SCRATCH_BYTES)
    assert scratch._ScratchBudget is semantic_planner._ScratchBudget
    assert scratch._ContentAccumulator is semantic_planner._ContentAccumulator
    assert scratch._create_scratch_database is (semantic_planner._create_scratch_database)


def test_extracted_results_module_keeps_required_compatibility_aliases() -> None:
    results = __import__(
        "neocortex.semantic.semantic_plan_results",
        fromlist=["_WorkloadSpec"],
    )

    for name in (
        "_WorkloadSpec",
        "_PlanConfiguration",
        "_ScratchPlanResult",
        "_plan_text_source",
        "_plan_images",
        "_prepare_plan_configuration",
        "_cost_calibrations_by_key",
        "_freeze_workload",
    ):
        assert getattr(results, name) is getattr(semantic_planner, name)
    assert results.build_plan_signature_payload is not (
        semantic_planner._plan_payload_for_signature
    )
    assert results.assemble_semantic_plan is not (semantic_planner._assemble_semantic_plan)
    for wrapper in (
        semantic_planner._plan_payload_for_signature,
        semantic_planner._assemble_semantic_plan,
    ):
        assert wrapper.__module__ == "neocortex.semantic.semantic_planner"


def test_extracted_owner_module_keeps_dynamic_facade_seams() -> None:
    owners = __import__(
        "neocortex.semantic.semantic_plan_owners",
        fromlist=["_validate_source_schema"],
    )

    for name in (
        "_validate_source_schema",
        "_validate_dedup_schema",
        "_validate_semantic_cache",
    ):
        assert getattr(owners, name) is getattr(semantic_planner, name)
    for name in (
        "_plan_text_database_group",
        "_validated_dedup_schema",
        "_semantic_reuse_snapshot",
        "_plan_source_snapshots",
    ):
        planner_wrapper = getattr(semantic_planner, name)
        assert planner_wrapper is not getattr(owners, name)
        assert planner_wrapper.__module__ == "neocortex.semantic.semantic_planner"

    required_callbacks = {
        "_plan_text_database_group": (
            "validate_source_schema",
            "plan_text_source",
        ),
        "_validated_dedup_schema": ("validate_dedup_schema",),
        "_semantic_reuse_snapshot": ("validate_semantic_cache",),
        "_plan_source_snapshots": (
            "plan_text_database_group",
            "validated_dedup_schema",
            "validate_source_schema",
            "plan_images",
        ),
    }
    for function_name, callback_names in required_callbacks.items():
        parameters = inspect.signature(getattr(owners, function_name)).parameters
        for callback_name in callback_names:
            assert parameters[callback_name].default is inspect.Parameter.empty

    assert owners.PLANNER_BUSY_TIMEOUT_MS == 25
    assert owners.PLANNER_BUSY_RETRY_ATTEMPTS == 8
    assert owners.PLANNER_BUSY_RETRY_DELAY_SECONDS == 0.025
    assert owners._retry_busy.__name__ == "_retry_busy"
    assert owners._semantic_reuse_snapshot.__name__ == ("_semantic_reuse_snapshot")


@pytest.mark.parametrize(
    "import_order",
    (
        (
            "neocortex.semantic.semantic_planner",
            "neocortex.semantic.semantic_service",
        ),
        (
            "neocortex.semantic.semantic_service",
            "neocortex.semantic.semantic_planner",
        ),
    ),
    ids=("planner-first", "service-first"),
)
def test_planner_service_cold_import_orders_preserve_identity(
    import_order: tuple[str, str],
) -> None:
    repository = Path(__file__).resolve().parents[1]
    script = (
        "import importlib, inspect, sys\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        f"first = importlib.import_module({import_order[0]!r})\n"
        f"second = importlib.import_module({import_order[1]!r})\n"
        "planner = importlib.import_module('neocortex.semantic.semantic_planner')\n"
        "service = importlib.import_module('neocortex.semantic.semantic_service')\n"
        f"assert planner.__all__ == {EXPECTED_PLANNER_ALL!r}\n"
        "assert service.SemanticPlanBlocked is planner.SemanticPlanBlocked\n"
        "assert service.plan_semantic_index is not planner.plan_semantic_index\n"
        "assert service.semantic_plan_payload is not planner.semantic_plan_payload\n"
        "assert planner.SemanticPlanBlocked.__module__ == "
        "'neocortex.semantic.semantic_plan_errors'\n"
        "assert planner.SemanticScratchLimitExceeded.__module__ == "
        "'neocortex.semantic.semantic_plan_errors'\n"
        f"assert str(inspect.signature(planner.plan_semantic_index)) == "
        f"{EXPECTED_PLANNER_SIGNATURE!r}\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "import_order",
    (
        (
            "neocortex.semantic.semantic_plan_owners",
            "neocortex.semantic.semantic_planner",
        ),
        (
            "neocortex.semantic.semantic_planner",
            "neocortex.semantic.semantic_plan_owners",
        ),
    ),
    ids=("owners-first", "planner-first"),
)
def test_owner_planner_cold_import_orders_keep_live_facade_bindings(
    import_order: tuple[str, str],
) -> None:
    repository = Path(__file__).resolve().parents[1]
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        f"importlib.import_module({import_order[0]!r})\n"
        f"importlib.import_module({import_order[1]!r})\n"
        "owners = importlib.import_module("
        "'neocortex.semantic.semantic_plan_owners')\n"
        "planner = importlib.import_module("
        "'neocortex.semantic.semantic_planner')\n"
        "results = importlib.import_module("
        "'neocortex.semantic.semantic_plan_results')\n"
        "for name in ('_validate_source_schema', '_validate_dedup_schema', "
        "'_validate_semantic_cache'):\n"
        "    assert getattr(planner, name) is getattr(owners, name)\n"
        "for name in ('_plan_text_database_group', '_validated_dedup_schema', "
        "'_semantic_reuse_snapshot', '_plan_source_snapshots'):\n"
        "    assert getattr(planner, name) is not getattr(owners, name)\n"
        "assert planner._plan_text_source is results._plan_text_source\n"
        "assert planner._plan_images is results._plan_images\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


# endregion [02]
