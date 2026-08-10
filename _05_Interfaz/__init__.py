"""PySide6 desktop interface for the NeoCortex framework."""
# region [00] Contexto del módulo
# Módulo: _05_Interfaz/__init__.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from collections.abc import Sequence
# endregion [01]

# region [02] Implementación


def main(arguments: Sequence[str] | None = None) -> int:
    """Load the optional Qt stack only when the desktop UI is requested."""

    from .app import main as run_application

    return run_application(arguments)


__all__ = ["main"]
# endregion [02]
