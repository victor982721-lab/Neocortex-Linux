"""Compatibility alias for the canonical Archive result contracts."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.archive.models import ArchiveRouteSummary as ArchiveRouteSummary
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.archive.models"
    )
