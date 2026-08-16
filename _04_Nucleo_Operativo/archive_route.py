"""Compatibility alias for the canonical recursive Archive route."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.archive.route import ARCHIVE_MIME as ARCHIVE_MIME
    from .capabilities.formats.archive.route import (
        ARCHIVE_ROUTE_VERSION as ARCHIVE_ROUTE_VERSION,
    )
    from .capabilities.formats.archive.route import (
        ArchiveExtractionError as ArchiveExtractionError,
    )
    from .capabilities.formats.archive.route import ArchiveRoute as ArchiveRoute
    from .capabilities.formats.archive.route import ArchiveRouteConfig as ArchiveRouteConfig
    from .capabilities.formats.archive.route import ArchiveRouteSummary as ArchiveRouteSummary
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.archive.route"
    )
