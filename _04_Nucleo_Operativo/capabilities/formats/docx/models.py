"""Compatibility alias for the canonical DOCX model module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.models import (
        ALGORITHM_VERSION as ALGORITHM_VERSION,
        DocxDiagnostic as DocxDiagnostic,
        DocxFailure as DocxFailure,
        DocxIntegrityStatus as DocxIntegrityStatus,
        DocxPart as DocxPart,
        DocxProcessingError as DocxProcessingError,
        DocxReviewDisposition as DocxReviewDisposition,
        DocxRouteConfig as DocxRouteConfig,
        DocxRouteSummary as DocxRouteSummary,
        DocxStatus as DocxStatus,
        ExtractedDocx as ExtractedDocx,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.models")
