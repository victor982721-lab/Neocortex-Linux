"""Compatibility alias for the canonical Archive SQLite owner."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.archive.state import (
        ARCHIVE_SCHEMA_VERSION as ARCHIVE_SCHEMA_VERSION,
    )
    from .capabilities.formats.archive.state import ArchiveSearchHit as ArchiveSearchHit
    from .capabilities.formats.archive.state import ArchiveStatus as ArchiveStatus
    from .capabilities.formats.archive.state import archive_database as archive_database
    from .capabilities.formats.archive.state import (
        archive_schema_contract as archive_schema_contract,
    )
    from .capabilities.formats.archive.state import (
        initialize_archive_state as initialize_archive_state,
    )
    from .capabilities.formats.archive.state import (
        list_archive_members as list_archive_members,
    )
    from .capabilities.formats.archive.state import read_archive_status as read_archive_status
    from .capabilities.formats.archive.state import search_archive_state as search_archive_state
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.archive.state"
    )
