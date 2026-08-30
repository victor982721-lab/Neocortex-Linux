"""Identity-preserving compatibility facade for SQLite cancellation."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/sqlite_cancellation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from neocortex.sqlite_cancellation import (
    CancellationCheck as CancellationCheck,
    DEFAULT_PROGRESS_INSTRUCTIONS as DEFAULT_PROGRESS_INSTRUCTIONS,
    SQLiteCancellationBridge as SQLiteCancellationBridge,
    sqlite_cancellation_scope as sqlite_cancellation_scope,
)
# endregion [01]

# region [02] Implementación


__all__ = [
    "DEFAULT_PROGRESS_INSTRUCTIONS",
    "CancellationCheck",
    "SQLiteCancellationBridge",
    "sqlite_cancellation_scope",
]
# endregion [02]
