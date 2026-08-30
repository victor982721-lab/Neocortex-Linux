# region [00] Contexto del módulo
# Módulo: tests/test_framework_state_facade.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
"""Direct ownership contracts for framework persistence modules."""

from __future__ import annotations

from neocortex.persistence.framework_route_state import (
    FrameworkRouteState,
    ReviewCandidateReconciliation,
)
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_common import (
    CACHE_PRUNE_BATCH_SIZE,
    FileActionSpec,
)
from neocortex.persistence.framework_state_writer import FrameworkState


def test_framework_state_contracts_are_owned_by_their_modules() -> None:
    assert FrameworkState.__module__.endswith("framework_state_writer")
    assert FrameworkRouteState.__module__.endswith("framework_route_state")
    assert ReviewCandidateReconciliation.__module__.endswith("framework_route_state")
    assert FileActionSpec == tuple[str, str, str | None, str | None, str | None, bool]
    assert isinstance(SCHEMA_VERSION, int)
    assert CACHE_PRUNE_BATCH_SIZE > 0
