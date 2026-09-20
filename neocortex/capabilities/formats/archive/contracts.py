"""Small dependency-free contracts shared by Archive implementation slices."""

from __future__ import annotations

ARCHIVE_MIME = "application/zip"
DEFAULT_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 2_000_000


class ArchiveExtractionError(ValueError):
    """One top-level container could not be indexed safely."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ArchiveCacheInvalid(ValueError):
    """Durable Archive representation cannot support a safe cache replay."""


__all__ = [
    "ARCHIVE_MIME",
    "DEFAULT_MAX_MEMBER_BYTES",
    "DEFAULT_MAX_TEXT_CHARS",
    "ArchiveCacheInvalid",
    "ArchiveExtractionError",
]
