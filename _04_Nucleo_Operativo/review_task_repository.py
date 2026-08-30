"""Compatibility alias for canonical ReviewTask repository."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.review.review_task_repository import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.workflow.review.review_task_repository")
