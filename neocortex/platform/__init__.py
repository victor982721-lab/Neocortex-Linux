"""Import-light canonical platform contracts and bounded primitives."""

from __future__ import annotations


def preserve_legacy_module(
    namespace: dict[str, object],
    legacy_module: str,
) -> None:
    """Keep historical pickle and monkeypatch identities after relocation."""

    canonical_module = namespace["__name__"]
    def preserve(value: object) -> None:
        if getattr(value, "__module__", None) == canonical_module:
            try:
                value.__module__ = legacy_module
            except (AttributeError, TypeError):
                pass
        if not isinstance(value, type):
            return
        for member in value.__dict__.values():
            if isinstance(member, (classmethod, staticmethod)):
                member = member.__func__
            elif isinstance(member, property):
                member = member.fget
            if getattr(member, "__module__", None) == canonical_module:
                try:
                    member.__module__ = legacy_module
                except (AttributeError, TypeError):
                    pass

    for value in tuple(namespace.values()):
        preserve(value)


__all__: tuple[str, ...] = ()
