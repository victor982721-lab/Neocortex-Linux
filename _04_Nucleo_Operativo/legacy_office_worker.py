"""Compatibility alias and executable wrapper for the canonical Office worker."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.office.legacy_worker import main as main
elif __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    from neocortex.capabilities.formats.office.legacy_worker import main

    raise SystemExit(main())
else:
    sys.modules[__name__] = import_module(
        "neocortex.capabilities.formats.office.legacy_worker"
    )
