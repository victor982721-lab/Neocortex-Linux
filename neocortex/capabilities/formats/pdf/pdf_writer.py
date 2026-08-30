"""Process-local serialization for all SQLite writes to persistent PDF state."""
# region [00] Contexto del módulo
# Módulo: neocortex/pdf_writer.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
from contextlib import contextmanager
from threading import RLock
# endregion [01]

# region [02] Implementación


_PDF_WRITE_LOCK = RLock()


@contextmanager
def serialized_pdf_write():
    """Permit one parent writer transaction while extraction remains parallel."""

    with _PDF_WRITE_LOCK:
        yield
# endregion [02]
