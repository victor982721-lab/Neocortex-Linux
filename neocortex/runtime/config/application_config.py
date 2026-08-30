"""Compatible application configuration and domain projections.
# region [00] Contexto del módulo
# Módulo: neocortex/runtime/config/application_config.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The flat configuration remains the public construction boundary while runtime
owners migrate to explicit domain contracts.  Projections are deliberately
computed from the current instance so canonical path replacements cannot leave
stale nested state behind.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

from typing import TypeAlias

from .application_config_projections import (
    archive_route_config_from_application,
    audio_route_config_from_application,
    code_route_config_from_application,
    docx_route_config_from_application,
    global_resource_limits_from_application,
    image_route_config_from_application,
    office_route_config_from_application,
    pdf_route_config_from_application,
    text_route_config_from_application,
    video_route_config_from_application,
)
from neocortex.runtime.models import FrameworkConfig
# endregion [01]

# region [02] Implementación

__all__ = [
    "ApplicationConfig",
    "FrameworkConfig",
    "archive_route_config_from_application",
    "audio_route_config_from_application",
    "code_route_config_from_application",
    "docx_route_config_from_application",
    "global_resource_limits_from_application",
    "image_route_config_from_application",
    "office_route_config_from_application",
    "pdf_route_config_from_application",
    "text_route_config_from_application",
    "video_route_config_from_application",
]


# ``FrameworkConfig`` is a durable legacy import and a frozen slotted dataclass.
# Keeping object identity during this migration preserves its constructor,
# dataclasses.replace behavior, equality, hashing and derived database paths.
ApplicationConfig: TypeAlias = FrameworkConfig
# endregion [02]
