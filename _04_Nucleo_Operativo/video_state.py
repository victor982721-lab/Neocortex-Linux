"""Compatibility alias for canonical Video state."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.video.state import VIDEO_SCHEMA_VERSION as VIDEO_SCHEMA_VERSION
    from neocortex.capabilities.formats.video.state import _migrate_video_v1 as _migrate_video_v1
    from neocortex.capabilities.formats.video.state import _video_schema_contract as _video_schema_contract
    from neocortex.capabilities.formats.video.state import initialize_video_state as initialize_video_state
    from neocortex.capabilities.formats.video.state import search_video_state as search_video_state
    from neocortex.capabilities.formats.video.state import validate_video_schema as validate_video_schema
    from neocortex.capabilities.formats.video.state import video_database as video_database
    from neocortex.capabilities.formats.video.state import video_state_status as video_state_status
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.video.state")
