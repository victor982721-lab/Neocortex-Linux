"""Binary cache evidence shared by PDF extraction and the deduplication index."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

from neocortex.deduplication import DedupIndex, FileSnapshot, FULL_ALGORITHM, full_fingerprint


# region [01] Fingerprint resolution
# Reuse persisted XXH3 evidence and read the source only when strict validation asks for it.


def binary_fingerprint(
    index: DedupIndex,
    snapshot: FileSnapshot,
    *,
    required: bool,
    refresh: bool = False,
) -> str | None:
    digest = None if refresh else index.cached_fingerprint(snapshot, FULL_ALGORITHM)
    if digest is None and required:
        digest = full_fingerprint(snapshot)
        index.store_fingerprint(snapshot, FULL_ALGORITHM, digest)
    return None if digest is None else digest.hex()


# endregion [01]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_cache")
