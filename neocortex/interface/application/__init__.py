"""Application bootstrap, requests and supervised-process control."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .controller import WorkerController as WorkerController
    from .request import ExecutionProfile as ExecutionProfile
    from .request import FULL_DEADLINE_SECONDS as FULL_DEADLINE_SECONDS
    from .request import FULL_MAX_ITEMS as FULL_MAX_ITEMS
    from .request import PILOT_DEADLINE_SECONDS as PILOT_DEADLINE_SECONDS
    from .request import PILOT_MAX_ITEMS as PILOT_MAX_ITEMS
    from .request import PROFILE_DEFAULTS as PROFILE_DEFAULTS
    from .request import ROUTE_ORDER as ROUTE_ORDER
    from .request import RunRequest as RunRequest

_EXPORTS: Final = {
    "ROUTE_ORDER": (".request", "ROUTE_ORDER"),
    "RunRequest": (".request", "RunRequest"),
    "ExecutionProfile": (".request", "ExecutionProfile"),
    "PILOT_MAX_ITEMS": (".request", "PILOT_MAX_ITEMS"),
    "PILOT_DEADLINE_SECONDS": (".request", "PILOT_DEADLINE_SECONDS"),
    "FULL_MAX_ITEMS": (".request", "FULL_MAX_ITEMS"),
    "FULL_DEADLINE_SECONDS": (".request", "FULL_DEADLINE_SECONDS"),
    "PROFILE_DEFAULTS": (".request", "PROFILE_DEFAULTS"),
    "WorkerController": (".controller", "WorkerController"),
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
    "FULL_DEADLINE_SECONDS",
    "FULL_MAX_ITEMS",
    "PILOT_DEADLINE_SECONDS",
    "PILOT_MAX_ITEMS",
    "PROFILE_DEFAULTS",
    "ROUTE_ORDER",
    "ExecutionProfile",
    "RunRequest",
    "WorkerController",
]
