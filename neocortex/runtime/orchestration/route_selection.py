"""Lightweight built-in route names and selection normalization."""


# region [01] Stable route selection contract

from __future__ import annotations
__all__ = [
    "BUILTIN_ROUTE_ORDER",
    "ORGANIZABLE_ROUTE_NAMES",
    "normalize_route_selection",
]

BUILTIN_ROUTE_ORDER = (
    "pdf",
    "docx",
    "office",
    "text",
    "audio",
    "video",
    "image",
)
ORGANIZABLE_ROUTE_NAMES = frozenset({"pdf", "docx", "office", "text", "audio"})


def normalize_route_selection(
    expression: str,
    available_routes: tuple[str, ...],
) -> tuple[str, ...]:
    """Normalize and validate one route-selection expression."""

    normalized = expression.strip().casefold()
    if not normalized or normalized == "none":
        return ()
    if normalized == "all":
        return available_routes
    values = tuple(part.strip().casefold() for part in normalized.split(",") if part.strip())
    if not values:
        return ()
    if "none" in values or "all" in values:
        raise ValueError("'none' and 'all' cannot be combined with route names")
    duplicates = {name for name in values if values.count(name) > 1}
    if duplicates:
        raise ValueError(f"duplicate routes: {', '.join(sorted(duplicates))}")
    unknown = [name for name in values if name not in available_routes]
    if unknown:
        raise ValueError(
            f"unknown routes: {', '.join(unknown)}; available: {', '.join(available_routes)}"
        )
    return values
# endregion [01]
