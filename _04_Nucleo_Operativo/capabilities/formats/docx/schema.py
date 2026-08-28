"""Compatibility alias for the canonical DOCX schema module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.schema import (
        DOCX_SCHEMA_VERSION as DOCX_SCHEMA_VERSION,
        UNKNOWN_BIRTHTIME_NS as UNKNOWN_BIRTHTIME_NS,
        create_fresh_docx_schema as create_fresh_docx_schema,
        migrate_docx_schema as migrate_docx_schema,
        validate_docx_metadata as validate_docx_metadata,
        validate_docx_schema as validate_docx_schema,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.schema")
