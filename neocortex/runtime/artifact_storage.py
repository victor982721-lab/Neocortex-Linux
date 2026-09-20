"""Atomic manifest and retention-receipt storage primitives.

This module has no registry owner state.  It only publishes bounded JSON with
no-clobber/replace semantics and derives receipt names/digests.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifact_models import MAX_MANIFEST_BYTES, _canonical_json


def _write_json_atomic(path: Path, payload: Mapping[str, Any], *, exclusive: bool) -> None:
    """Publish one bounded JSON document without exposing a partial payload."""

    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("artifact manifest exceeds the durable size limit")
    parent = path.parent
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # link()+unlink() gives an atomic no-clobber publication on Linux:
            # a replay/race cannot replace a manifest owned by another claim.
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def _manifest_file_issue(metadata: os.stat_result) -> str | None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "manifest_type_drift"
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        return "manifest_protection_drift"
    return None

def _retention_receipt_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("receipt_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()

def _retention_receipt_name(operation_id: str) -> str:
    return f"receipt-{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()}.json"
