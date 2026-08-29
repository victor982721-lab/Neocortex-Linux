"""Format-specific capability implementations for the canonical package."""

from __future__ import annotations

from . import archive as archive
from . import audio as audio
from . import docx as docx

__all__ = ["archive", "audio", "docx"]
