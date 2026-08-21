"""Compatibility alias for canonical Office state."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.office.state import OFFICE_SCHEMA_VERSION as OFFICE_SCHEMA_VERSION
    from .capabilities.formats.office.state import _OFFICE_V1_SCHEMA_DDL as _OFFICE_V1_SCHEMA_DDL
    from .capabilities.formats.office.state import (
        _create_office_v1_schema as _create_office_v1_schema,
    )
    from .capabilities.formats.office.state import (
        _migrate_office_v2_path_collation as _migrate_office_v2_path_collation,
    )
    from .capabilities.formats.office.state import (
        _office_v1_schema_contract as _office_v1_schema_contract,
    )
    from .capabilities.formats.office.state import (
        initialize_office_state as initialize_office_state,
    )
    from .capabilities.formats.office.state import office_database as office_database
    from .capabilities.formats.office.state import search_office_state as search_office_state
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.office.state")
