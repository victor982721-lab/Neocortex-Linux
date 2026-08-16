"""Compatibility alias for the canonical DOCX state module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.state import SCHEMA_VERSION as SCHEMA_VERSION
    from .capabilities.formats.docx.state import (
        UNKNOWN_BIRTHTIME_NS as UNKNOWN_BIRTHTIME_NS,
    )
    from .capabilities.formats.docx.state import (
        connect_docx_state as connect_docx_state,
    )
    from .capabilities.formats.docx.state import docx_database as docx_database
    from .capabilities.formats.docx.state import (
        initialize_docx_state as initialize_docx_state,
    )
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.state"
    )
