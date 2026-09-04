"""Read-only curation previews built from published NeoCortex state."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CURATION_PREVIEW_SCHEMA_VERSION": (".preview", "CURATION_PREVIEW_SCHEMA_VERSION"),
    "CurationItem": (".preview", "CurationItem"),
    "CurationPlanPage": (".preview", "CurationPlanPage"),
    "CurationPreview": (".preview", "CurationPreview"),
    "CurationStateError": (".preview", "CurationStateError"),
    "build_curation_plan_page": (".preview", "build_curation_plan_page"),
    "build_curation_preview": (".preview", "build_curation_preview"),
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
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CurationItem",
    "CurationPlanPage",
    "CurationPreview",
    "CurationStateError",
    "build_curation_plan_page",
    "build_curation_preview",
]
