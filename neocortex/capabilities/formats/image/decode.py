"""Scoped Pillow decode policy and conservative recovery validation."""

from __future__ import annotations
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

from PIL import Image, ImageFile


# region [01] Decode policy contracts


STRICT_DECODE_PROVENANCE = "pillow-strict-v1"
RECOVERED_DECODE_PROVENANCE = "pillow-truncated-recovery-v1"
RECOVERED_DECODE_CONFIDENCE_CAP = 0.72

_RECOVERABLE_ERROR_MARKERS = (
    "broken data stream when reading image file",
    "image file is truncated",
)
_PILLOW_DECODE_LOCK = threading.RLock()


class RecoveredImageContentError(OSError):
    """A tolerant decode completed but yielded no trustworthy content."""


class ContentMetrics(Protocol):
    @property
    def brightness_std(self) -> float: ...

    @property
    def entropy(self) -> float: ...

    @property
    def quantized_colors(self) -> int: ...


def is_recoverable_decode_error(exc: BaseException) -> bool:
    """Permit tolerance only for Pillow's known incomplete-stream failures."""

    if not isinstance(exc, OSError):
        return False
    message = str(exc).casefold()
    return any(marker in message for marker in _RECOVERABLE_ERROR_MARKERS)


def recovered_content_is_meaningful(features: ContentMetrics) -> bool:
    """Reject uniform filler produced by a permissive incomplete decode."""

    return not (
        features.quantized_colors <= 2
        and features.brightness_std <= 0.01
        and features.entropy <= 0.15
    )


# endregion [01]


# region [02] Process-local Pillow flag isolation


@contextmanager
def pillow_decode_scope(*, allow_truncated: bool) -> Iterator[None]:
    """Set and restore Pillow's process-global truncated-image flag safely."""

    with _PILLOW_DECODE_LOCK:
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = allow_truncated
        try:
            yield
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous


@contextmanager
def open_image_no_follow(path: Path) -> Iterator[Image.Image]:
    """Open one regular image through an identity-stable no-following descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise OSError(f"image source is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            descriptor = -1
            with Image.open(stream) as image:
                yield image
    finally:
        if descriptor >= 0:
            os.close(descriptor)


# endregion [02]
