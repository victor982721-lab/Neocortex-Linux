"""Compatibility alias for the canonical Archive extraction worker."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.text_worker import main as main
else:
    _canonical = import_module("neocortex.capabilities.formats.archive.text_worker")
    if __name__ == "__main__":
        raise SystemExit(_canonical.main())
    sys.modules[__name__] = _canonical
