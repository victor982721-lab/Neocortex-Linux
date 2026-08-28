"""Compatibility alias for the canonical DOCX route module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.route import DOCX_MIME as DOCX_MIME
    from neocortex.capabilities.formats.docx.route import PDF_MIME as PDF_MIME
    from neocortex.capabilities.formats.docx.route import DocxRoute as DocxRoute
    from neocortex.capabilities.formats.docx.route import DocxRouteConfig as DocxRouteConfig
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary as DocxRouteSummary
    from neocortex.capabilities.formats.docx.route import _file_key as _file_key
    from neocortex.capabilities.formats.docx.route import extract_docx as extract_docx
    from neocortex.capabilities.formats.docx.route import (
        list_docx_layout_groups as list_docx_layout_groups,
        list_missing_pdf_counterparts as list_missing_pdf_counterparts,
        search_docx_state as search_docx_state,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.route")
