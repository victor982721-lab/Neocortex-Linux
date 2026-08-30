"""Compatibility alias for canonical Documents module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.documents.document_taxonomy_models import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.documents.document_taxonomy_models")
