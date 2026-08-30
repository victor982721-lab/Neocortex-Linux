"""Semantic planning errors and primary-preserving cleanup support."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/semantic_plan_errors.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from collections.abc import Callable
# endregion [01]

# region [02] Implementación


_PLANNER_COMPATIBILITY_MODULE = "_04_Nucleo_Operativo.semantic_planner"


class SemanticPlanBlocked(RuntimeError):
    """The requested exact plan cannot be proven from read-only owner state."""


class SemanticScratchLimitExceeded(SemanticPlanBlocked):
    """The planner exhausted its explicit private scratch storage allowance."""


SemanticPlanBlocked.__module__ = _PLANNER_COMPATIBILITY_MODULE
SemanticScratchLimitExceeded.__module__ = _PLANNER_COMPATIBILITY_MODULE


def cleanup_preserving_primary(
    cleanup: Callable[[], object],
    primary: BaseException,
    *,
    label: str,
) -> None:
    """Run cleanup without replacing an already-raised primary exception."""

    try:
        cleanup()
    except BaseException as cleanup_error:
        primary.add_note(
            f"{label} failed: {type(cleanup_error).__name__}: {cleanup_error}"
        )


__all__ = [
    "SemanticPlanBlocked",
    "SemanticScratchLimitExceeded",
]
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.semantic_plan_errors")
