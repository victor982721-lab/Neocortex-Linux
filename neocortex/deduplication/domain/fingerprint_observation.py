"""Content observations whose reuse remains bound to a physical change version."""

from __future__ import annotations

from dataclasses import dataclass

from .models import FileSnapshot
from .errors import FileChangedError


@dataclass(frozen=True, slots=True)
class FingerprintObservation:
    """A requested digest and the full digest already read to validate it.

    ``ctime_ns`` is a change version, never a birth time or document revision.
    It only permits reuse inside the current planning observation; durable
    cache lookup still reads content on every run.
    """

    snapshot: FileSnapshot
    algorithm: str
    digest: bytes
    full_digest: bytes
    ctime_ns: int
    computed: bool
    cache_hit: bool = False
    full_reads: int = 0
    partial_reads: int = 0
    full_read_bytes: int = 0
    partial_read_bytes: int = 0
    validation_read_bytes: int = 0
    reused_full_digest: bool = False


@dataclass(frozen=True, slots=True)
class ExactComparisonObservation:
    equal: bool
    read_bytes: int


class FingerprintReadFailure(FileChangedError):
    """Preserve completed I/O accounting when a content observation abstains."""

    def __init__(
        self, message: str, *, full_reads: int = 0, partial_reads: int = 0,
        full_read_bytes: int = 0, partial_read_bytes: int = 0,
        validation_read_bytes: int = 0, exact_comparison_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.full_reads = full_reads
        self.partial_reads = partial_reads
        self.full_read_bytes = full_read_bytes
        self.partial_read_bytes = partial_read_bytes
        self.validation_read_bytes = validation_read_bytes
        self.exact_comparison_bytes = exact_comparison_bytes
