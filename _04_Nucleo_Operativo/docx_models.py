"""Compatibility alias for the canonical DOCX model module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.models import (
        ALGORITHM_VERSION as ALGORITHM_VERSION,
    )
    from .capabilities.formats.docx.models import DocxDiagnostic as DocxDiagnostic
    from .capabilities.formats.docx.models import DocxFailure as DocxFailure
    from .capabilities.formats.docx.models import (
        DocxIntegrityStatus as DocxIntegrityStatus,
    )
    from .capabilities.formats.docx.models import DocxPart as DocxPart
    from .capabilities.formats.docx.models import (
        DocxProcessingError as DocxProcessingError,
    )
    from .capabilities.formats.docx.models import (
        DocxReviewDisposition as DocxReviewDisposition,
    )
    from .capabilities.formats.docx.models import (
        DocxRouteConfig as DocxRouteConfig,
    )
    from .capabilities.formats.docx.models import (
        DocxRouteSummary as DocxRouteSummary,
    )
    from .capabilities.formats.docx.models import DocxStatus as DocxStatus
    from .capabilities.formats.docx.models import ExtractedDocx as ExtractedDocx
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.models"
    )
