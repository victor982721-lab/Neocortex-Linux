"""Compatibility alias for the canonical DOCX integrity module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.docx.integrity import (
        classify_docx_exception as classify_docx_exception,
        diagnostic_for_member as diagnostic_for_member,
        fatal_member_error as fatal_member_error,
        member_upper_bounds as member_upper_bounds,
        recover_raw_deflate_member as recover_raw_deflate_member,
        recovered_member_diagnostic as recovered_member_diagnostic,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.docx.integrity")
