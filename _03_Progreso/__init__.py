"""Reusable progress event schema and normalized Rich renderer."""
# region [00] Contexto del módulo
# Módulo: _03_Progreso/__init__.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from .models import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from .reporters import LineProgress, NullProgress, RecordingProgress, RichProgress
# endregion [01]

# region [02] Implementación

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
# endregion [02]
