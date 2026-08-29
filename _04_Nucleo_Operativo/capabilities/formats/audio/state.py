"""Compatibility alias for the canonical Audio state module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio.state import (
        AUDIO_SCHEMA_VERSION as AUDIO_SCHEMA_VERSION,
        _AUDIO_V1_SCHEMA_DDL as _AUDIO_V1_SCHEMA_DDL,
        _audio_schema_contract as _audio_schema_contract,
        _audio_schema_ddl as _audio_schema_ddl,
        _migrate_audio_v1_path_collation as _migrate_audio_v1_path_collation,
        audio_database as audio_database,
        initialize_audio_state as initialize_audio_state,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.audio.state")
