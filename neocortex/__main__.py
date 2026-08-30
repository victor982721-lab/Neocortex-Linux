"""Support ``python -m neocortex`` through the installed entry point."""
# region [00] Contexto del módulo
# Módulo: neocortex/__main__.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from .interface.entrypoint import entrypoint
# endregion [01]

# region [02] Implementación

raise SystemExit(entrypoint())
# endregion [02]
