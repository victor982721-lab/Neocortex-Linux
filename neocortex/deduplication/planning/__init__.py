"""Bounded duplicate-plan orchestration."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .pipeline import (
        FingerprintProvider as FingerprintProvider,
        PlanningSession as PlanningSession,
    )
    from .planner import DedupPlanner as DedupPlanner

_EXPORTS: Final = {
    "DedupPlanner": (".planner", "DedupPlanner"),
    "FingerprintProvider": (".pipeline", "FingerprintProvider"),
    "PlanningSession": (".pipeline", "PlanningSession"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DedupPlanner",
    "FingerprintProvider",
    "PlanningSession",
]
