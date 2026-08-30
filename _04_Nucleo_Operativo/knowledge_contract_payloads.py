"""Compatibility alias for canonical Knowledge plane module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.knowledge.knowledge_contract_payloads import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.knowledge.knowledge_contract_payloads")
