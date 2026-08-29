"""Compatibility alias for the canonical Office route."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.office.route import MAX_XLSX_CELLS as MAX_XLSX_CELLS
    from neocortex.capabilities.formats.office.route import OFFICE_MIME_FORMATS as OFFICE_MIME_FORMATS
    from neocortex.capabilities.formats.office.route import ODT_MIME as ODT_MIME
    from neocortex.capabilities.formats.office.route import PPTX_MIME as PPTX_MIME
    from neocortex.capabilities.formats.office.route import XLSX_MIME as XLSX_MIME
    from neocortex.capabilities.formats.office.route import OfficeRoute as OfficeRoute
    from neocortex.capabilities.formats.office.route import OfficeRouteConfig as OfficeRouteConfig
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary as OfficeRouteSummary
    from neocortex.capabilities.formats.office.route import _file_key as _file_key
    from neocortex.capabilities.formats.office.route import (
        extract_office_document as extract_office_document,
    )
    from neocortex.capabilities.formats.office.route import search_office_state as search_office_state
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.office.route")
