"""Compatibility alias for canonical Audio state."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.audio.state import AUDIO_SCHEMA_VERSION as AUDIO_SCHEMA_VERSION
    from .capabilities.formats.audio.state import _AUDIO_V1_SCHEMA_DDL as _AUDIO_V1_SCHEMA_DDL
    from .capabilities.formats.audio.state import _audio_schema_contract as _audio_schema_contract
    from .capabilities.formats.audio.state import _audio_schema_ddl as _audio_schema_ddl
    from .capabilities.formats.audio.state import (
        _migrate_audio_v1_path_collation as _migrate_audio_v1_path_collation,
    )
    from .capabilities.formats.audio.state import audio_database as audio_database
    from .capabilities.formats.audio.state import initialize_audio_state as initialize_audio_state
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.audio.state")
