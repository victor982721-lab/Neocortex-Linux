"""Compatibility alias for the canonical DOCX state module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.state import (
        SCHEMA_VERSION as SCHEMA_VERSION,
        UNKNOWN_BIRTHTIME_NS as UNKNOWN_BIRTHTIME_NS,
        connect_docx_state as connect_docx_state,
        docx_database as docx_database,
        initialize_docx_state as initialize_docx_state,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.state")
