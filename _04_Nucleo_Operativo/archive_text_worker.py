"""Compatibility alias and executable wrapper for the canonical Archive worker."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.text_worker import main as main
else:
    if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
        _canonical = import_module("neocortex.capabilities.formats.archive.text_worker")
        raise SystemExit(_canonical.main())
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.archive.text_worker"
    )
