"""Compatibility alias for the canonical DOCX integrity module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.docx.integrity import (
        classify_docx_exception as classify_docx_exception,
    )
    from .capabilities.formats.docx.integrity import (
        diagnostic_for_member as diagnostic_for_member,
    )
    from .capabilities.formats.docx.integrity import (
        fatal_member_error as fatal_member_error,
    )
    from .capabilities.formats.docx.integrity import (
        member_upper_bounds as member_upper_bounds,
    )
    from .capabilities.formats.docx.integrity import (
        recover_raw_deflate_member as recover_raw_deflate_member,
    )
    from .capabilities.formats.docx.integrity import (
        recovered_member_diagnostic as recovered_member_diagnostic,
    )
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.docx.integrity"
    )
