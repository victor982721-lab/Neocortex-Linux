"""Compatibility alias for the canonical Archive SQLite owner."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.state import (
        ARCHIVE_SCHEMA_VERSION as ARCHIVE_SCHEMA_VERSION,
        ArchiveSearchHit as ArchiveSearchHit,
        ArchiveStatus as ArchiveStatus,
        archive_database as archive_database,
        archive_schema_contract as archive_schema_contract,
        initialize_archive_state as initialize_archive_state,
        list_archive_members as list_archive_members,
        read_archive_status as read_archive_status,
        search_archive_state as search_archive_state,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.archive.state")
