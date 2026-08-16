"""Compatibility alias for shared bounded content-type detection."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform.shared.content_types import DETECTOR_VERSION as DETECTOR_VERSION
    from .platform.shared.content_types import HEADER_LIMIT as HEADER_LIMIT
    from .platform.shared.content_types import ZIP_MEMBER_LIMIT as ZIP_MEMBER_LIMIT
    from .platform.shared.content_types import ZIP_MIMETYPE_LIMIT as ZIP_MIMETYPE_LIMIT
    from .platform.shared.content_types import (
        ZIP_STRUCTURE_MEMBER_LIMIT as ZIP_STRUCTURE_MEMBER_LIMIT,
    )
    from .platform.shared.content_types import DetectedType as DetectedType
    from .platform.shared.content_types import detect_content_type as detect_content_type
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.platform.shared.content_types"
    )
