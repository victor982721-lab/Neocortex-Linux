"""Pure validation primitives for immutable Knowledge contracts."""
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_contract_validation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
# endregion [01]

# region [02] Implementación


def required_text(name: str, value: str) -> str:
    """Return normalized required text or fail on blank input."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be blank")
    return normalized


def optional_text(name: str, value: str | None) -> str | None:
    """Preserve optional text while rejecting a present blank value."""

    if value is not None and not value.strip():
        raise ValueError(f"{name} cannot be blank when present")
    return value


__all__ = ["optional_text", "required_text"]
# endregion [02]
