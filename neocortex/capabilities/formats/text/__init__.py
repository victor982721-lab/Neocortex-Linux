"""Import-light canonical namespace for the Text capability."""

from __future__ import annotations


def preserve_legacy_module(
    namespace: dict[str, object],
    legacy_module: str,
) -> None:
    """Keep historical pickle and monkeypatch identities after relocation."""

    canonical_module = namespace["__name__"]
    for value in tuple(namespace.values()):
        if getattr(value, "__module__", None) != canonical_module:
            continue
        try:
            value.__module__ = legacy_module
        except (AttributeError, TypeError):
            continue


__all__: tuple[str, ...] = ()
