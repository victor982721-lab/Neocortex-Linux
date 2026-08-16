"""Import-light canonical namespace for the Image capability implementation."""

from __future__ import annotations


def _preserve_legacy_module(
    namespace: dict[str, object],
    legacy_module: str,
) -> None:
    """Keep pickle/runtime FQNs stable after a physical module relocation."""

    canonical_module = namespace["__name__"]
    for value in tuple(namespace.values()):
        if getattr(value, "__module__", None) != canonical_module:
            continue
        try:
            value.__module__ = legacy_module
        except (AttributeError, TypeError):
            continue


__all__: tuple[str, ...] = ()
