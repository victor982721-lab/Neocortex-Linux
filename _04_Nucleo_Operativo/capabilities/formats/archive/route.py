"""Compatibility alias for the canonical recursive Archive route."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.route import (
        ARCHIVE_MIME as ARCHIVE_MIME,
        ARCHIVE_ROUTE_VERSION as ARCHIVE_ROUTE_VERSION,
        ArchiveExtractionError as ArchiveExtractionError,
        ArchiveRoute as ArchiveRoute,
        ArchiveRouteConfig as ArchiveRouteConfig,
        ArchiveRouteSummary as ArchiveRouteSummary,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.archive.route")
