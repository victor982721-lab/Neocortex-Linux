"""Application bootstrap, requests and supervised-process control."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .controller import WorkerController as WorkerController
    from .request import ROUTE_ORDER as ROUTE_ORDER
    from .request import RunRequest as RunRequest

_EXPORTS: Final = {
    "ROUTE_ORDER": (".request", "ROUTE_ORDER"),
    "RunRequest": (".request", "RunRequest"),
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


__all__ = ["ROUTE_ORDER", "RunRequest", "WorkerController"]
