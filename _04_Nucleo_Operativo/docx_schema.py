"""Compatibility alias for the canonical DOCX schema module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.schema import (
        DOCX_SCHEMA_VERSION as DOCX_SCHEMA_VERSION,
    )
    from .capabilities.formats.docx.schema import (
        UNKNOWN_BIRTHTIME_NS as UNKNOWN_BIRTHTIME_NS,
    )
    from .capabilities.formats.docx.schema import (
        create_fresh_docx_schema as create_fresh_docx_schema,
    )
    from .capabilities.formats.docx.schema import (
        migrate_docx_schema as migrate_docx_schema,
    )
    from .capabilities.formats.docx.schema import (
        validate_docx_metadata as validate_docx_metadata,
    )
    from .capabilities.formats.docx.schema import (
        validate_docx_schema as validate_docx_schema,
    )
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.schema"
    )
