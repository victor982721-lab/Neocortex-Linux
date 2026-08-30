"""Compatibility alias for canonical value review repository."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.review.value_review_repository import *  # noqa: F403
else:
    sys.modules[__name__] = import_module(
        "neocortex.workflow.review.value_review_repository"
    )
