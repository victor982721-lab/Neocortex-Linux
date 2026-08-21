"""Stable progress contracts with lazy presentation adapters."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .events import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

if TYPE_CHECKING:
    from .line import LineProgress as LineProgress
    from .reporters import NullProgress as NullProgress
    from .reporters import RecordingProgress as RecordingProgress
    from .rich import RichProgress as RichProgress

_LAZY_EXPORTS = {
    "LineProgress": (".line", "LineProgress"),
    "NullProgress": (".reporters", "NullProgress"),
    "RecordingProgress": (".reporters", "RecordingProgress"),
    "RichProgress": (".rich", "RichProgress"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "LineProgress",
    "NullProgress",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressMetric",
    "RecordingProgress",
    "RichProgress",
    "emit_progress",
]
