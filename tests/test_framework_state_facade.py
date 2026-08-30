# region [00] Contexto del módulo
# Módulo: tests/test_framework_state_facade.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import importlib
# endregion [01]

# region [02] Implementación


def test_state_facade_preserves_public_import_contract() -> None:
    facade = importlib.import_module("neocortex.persistence.state")
    route_repository = importlib.import_module(
        "neocortex.persistence.framework_route_state"
    )
    schema = importlib.import_module("neocortex.persistence.framework_schema")
    shared = importlib.import_module("neocortex.persistence.framework_state_common")
    writer = importlib.import_module("neocortex.persistence.framework_state_writer")

    assert facade.FrameworkState is writer.FrameworkState
    assert facade.FrameworkRouteState is route_repository.FrameworkRouteState
    assert (
        facade.ReviewCandidateReconciliation
        is route_repository.ReviewCandidateReconciliation
    )
    assert facade.FileActionSpec is shared.FileActionSpec
    assert facade.CACHE_PRUNE_BATCH_SIZE == shared.CACHE_PRUNE_BATCH_SIZE
    assert (
        facade.REVIEW_RECONCILIATION_BATCH_SIZE
        == route_repository.REVIEW_RECONCILIATION_BATCH_SIZE
    )
    assert facade.SCHEMA_VERSION == schema.SCHEMA_VERSION
    assert set(facade.__all__) == {
        "CACHE_PRUNE_BATCH_SIZE",
        "REVIEW_RECONCILIATION_BATCH_SIZE",
        "SCHEMA_VERSION",
        "FileActionSpec",
        "FrameworkRouteState",
        "FrameworkState",
        "ReviewCandidateReconciliation",
    }


def test_state_facade_classes_are_physically_separated() -> None:
    facade = importlib.import_module("neocortex.persistence.state")

    assert facade.FrameworkState.__module__.endswith("framework_state_writer")
    assert facade.FrameworkRouteState.__module__.endswith("framework_route_state")
    assert facade.FrameworkState.__module__ != facade.FrameworkRouteState.__module__
# endregion [02]
