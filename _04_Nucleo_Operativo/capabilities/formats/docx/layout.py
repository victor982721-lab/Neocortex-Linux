"""Compatibility alias for the canonical DOCX layout module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.layout import TextBudget as TextBudget
    from neocortex.capabilities.formats.docx.layout import layout_result as layout_result
    from neocortex.capabilities.formats.docx.layout import (
        normalized_text_digest as normalized_text_digest,
        xml_text_and_layout as xml_text_and_layout,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.layout")
