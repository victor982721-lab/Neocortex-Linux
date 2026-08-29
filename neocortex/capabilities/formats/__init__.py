"""Format-specific capability implementations for the canonical package."""

from __future__ import annotations

from . import archive as archive
from . import audio as audio
from . import docx as docx
from . import image as image
from . import office as office
from . import pdf as pdf
from . import text as text
from . import video as video

__all__ = ["archive", "audio", "docx", "image", "office", "pdf", "text", "video"]
