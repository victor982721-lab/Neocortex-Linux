"""Compatibility alias for canonical Video state."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.video.state import VIDEO_SCHEMA_VERSION as VIDEO_SCHEMA_VERSION
    from .capabilities.formats.video.state import _migrate_video_v1 as _migrate_video_v1
    from .capabilities.formats.video.state import _video_schema_contract as _video_schema_contract
    from .capabilities.formats.video.state import initialize_video_state as initialize_video_state
    from .capabilities.formats.video.state import search_video_state as search_video_state
    from .capabilities.formats.video.state import validate_video_schema as validate_video_schema
    from .capabilities.formats.video.state import video_database as video_database
    from .capabilities.formats.video.state import video_state_status as video_state_status
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.video.state")
