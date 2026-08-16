"""Compatibility alias for the canonical DOCX route module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.route import DOCX_MIME as DOCX_MIME
    from .capabilities.formats.docx.route import PDF_MIME as PDF_MIME
    from .capabilities.formats.docx.route import DocxRoute as DocxRoute
    from .capabilities.formats.docx.route import DocxRouteConfig as DocxRouteConfig
    from .capabilities.formats.docx.route import (
        DocxRouteSummary as DocxRouteSummary,
    )
    from .capabilities.formats.docx.route import _file_key as _file_key
    from .capabilities.formats.docx.route import extract_docx as extract_docx
    from .capabilities.formats.docx.route import (
        list_docx_layout_groups as list_docx_layout_groups,
    )
    from .capabilities.formats.docx.route import (
        list_missing_pdf_counterparts as list_missing_pdf_counterparts,
    )
    from .capabilities.formats.docx.route import (
        search_docx_state as search_docx_state,
    )
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.route"
    )
