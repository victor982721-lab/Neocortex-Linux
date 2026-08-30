"""Compatibility alias for canonical self-analysis manifest codec."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.self_analysis.self_analysis_manifest import *  # noqa: F403
else:
    sys.modules[__name__] = import_module(
        "neocortex.workflow.self_analysis.self_analysis_manifest"
    )
