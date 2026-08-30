"""Stable facade for durable framework state repositories.
# region [00] Contexto del módulo
# Módulo: neocortex/state.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The single-writer lifecycle/cache repository and the short-lived concurrent
route/review repository intentionally live in separate modules.  Existing
imports continue to use this module as the public compatibility surface.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
from neocortex.persistence.framework_route_state import (
    REVIEW_RECONCILIATION_BATCH_SIZE,
    FrameworkRouteState,
    ReviewCandidateReconciliation,
)
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_common import CACHE_PRUNE_BATCH_SIZE, FileActionSpec
from neocortex.persistence.framework_state_writer import FrameworkState
# endregion [01]

# region [02] Implementación


__all__ = (
    "CACHE_PRUNE_BATCH_SIZE",
    "REVIEW_RECONCILIATION_BATCH_SIZE",
    "SCHEMA_VERSION",
    "FileActionSpec",
    "FrameworkRouteState",
    "FrameworkState",
    "ReviewCandidateReconciliation",
)
# endregion [02]
