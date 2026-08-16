"""Compatibility alias for the canonical DOCX layout module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.layout import TextBudget as TextBudget
    from .capabilities.formats.docx.layout import layout_result as layout_result
    from .capabilities.formats.docx.layout import (
        normalized_text_digest as normalized_text_digest,
    )
    from .capabilities.formats.docx.layout import (
        xml_text_and_layout as xml_text_and_layout,
    )
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.layout"
    )
