"""Compatibility alias for shared bounded ZIP primitives."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform.shared.zip_safety import (
        DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES as DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    )
    from .platform.shared.zip_safety import LOCAL_FILE_SIGNATURE as LOCAL_FILE_SIGNATURE
    from .platform.shared.zip_safety import RAW_DEFLATE_CHUNK_BYTES as RAW_DEFLATE_CHUNK_BYTES
    from .platform.shared.zip_safety import RawDeflateMember as RawDeflateMember
    from .platform.shared.zip_safety import ZipStructure as ZipStructure
    from .platform.shared.zip_safety import ZipStructureError as ZipStructureError
    from .platform.shared.zip_safety import inspect_zip_bytes as inspect_zip_bytes
    from .platform.shared.zip_safety import inspect_zip_stream as inspect_zip_stream
    from .platform.shared.zip_safety import inspect_zip_structure as inspect_zip_structure
    from .platform.shared.zip_safety import read_raw_deflate_member as read_raw_deflate_member
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.platform.shared.zip_safety"
    )
