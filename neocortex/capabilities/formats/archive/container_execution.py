"""Independent bounded ZIP container extraction execution.

This module owns source revalidation, resource admission estimates and the
read-only extraction phase.  SQLite publication remains in the route facade.
Route callbacks are resolved lazily to preserve injection boundaries.
"""

from __future__ import annotations

import os
import sqlite3
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any, Literal

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import snapshot_path, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.workflow.actions.action_policy import same_snapshot
from neocortex.platform.zip_safety import ZipStructureError, inspect_zip_stream
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken

from .contracts import ArchiveExtractionError
from .traversal import (
    _ArchiveContainerGroup,
    _ArchiveObservationSpool,
    _ContainerCounters,
)



ArchiveRouteConfig = Any


def _route():
    from . import route

    return route


def _archive_resources():
    return _route()._ARCHIVE_RESOURCES


def _walk_zip(*args, **kwargs):
    return _route()._walk_zip(*args, **kwargs)


def _WalkBudget(*args, **kwargs):
    return _route()._WalkBudget(*args, **kwargs)



@dataclass(frozen=True, slots=True)
class _ContainerOutcome:
    status: Literal["complete", "partial", "error"]
    counters: _ContainerCounters


@dataclass(frozen=True, slots=True)
class _ArchiveContainerTask:
    snapshot: FileSnapshot
    config: ArchiveRouteConfig
    group: _ArchiveContainerGroup
    cached: sqlite3.Row | None = None


@dataclass(slots=True)
class _PreparedArchiveContainer:
    snapshot: FileSnapshot
    counters: _ContainerCounters
    spool: _ArchiveObservationSpool | None = None
    failure: ArchiveExtractionError | None = None


def _archive_container_memory(config: ArchiveRouteConfig) -> int:
    return max(
        1,
        config.max_total_uncompressed_bytes
        + min(config.max_total_text_chars * 8, 256 * 1024 * 1024),
    )


def _archive_container_capacity(
    gate, config: ArchiveRouteConfig, *, media_possible: bool = True
) -> int:
    """Leave progress space for a member before admitting another ZIP parent."""

    from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

    parent = _archive_container_memory(config)
    member = min(config.max_member_bytes, config.max_total_uncompressed_bytes)
    interpreter = 64 * 1024 * 1024
    member_workspace = 4 * 1024 * 1024 + member * 2 + min(
        member, config.max_text_chars
    ) * 12
    child = interpreter + member_workspace
    if media_possible:
        # A ZIP can contain disguised PDF/images: extensions cannot prove
        # that only text will be processed. Leave enough room for one largest
        # bounded native child per parent until contents have been inspected.
        child += (
            config.pdf_worker_memory_bytes * (2 if config.ocr_mode != "never" else 1)
            + member * 2 + config.max_text_chars * 6 + 320 * 1024
        )
    try:
        return gate.worker_capacity(estimated_bytes=parent + child, native_threads=0)
    except MemoryBudgetExceeded:
        # An archive may contain only short text despite a generous safety
        # limit. Allow one parent; check actual member demand before its pool.
        return gate.worker_capacity(max_workers=1, estimated_bytes=parent, native_threads=0)


def _extract_archive_container(task: _ArchiveContainerTask) -> _PreparedArchiveContainer:
    """Extract an independent ZIP to owned typed observations, without SQLite."""

    with task.group.extracting():
        return _extract_archive_container_owned(task)


def _extract_archive_container_owned(task: _ArchiveContainerTask) -> _PreparedArchiveContainer:

    from neocortex.runtime.control.global_resources import current_resource_grant
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    grant = current_resource_grant()
    context = _archive_resources().get()
    cancellation = current_worker_cancellation() or (
        CancellationToken() if context is None else context[1]
    )
    config, snapshot = task.config, task.snapshot
    counters = _ContainerCounters()
    spool = _ArchiveObservationSpool(config, grant)
    try:
        cancellation.checkpoint()
        _require_current_source(snapshot, "ZIP source changed after inventory")
        with open(native_io_path(snapshot.path), "rb", buffering=0) as source:
            if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                raise ArchiveExtractionError(
                    "archive_source_changed", "ZIP source changed before opening", retryable=True
                )
            inspect_zip_stream(
                source, snapshot.size, max_members=config.max_members,
                max_central_directory_bytes=config.max_central_directory_bytes,
            )
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                _walk_zip(
                    spool, archive, snapshot, file_key_from_snapshot(snapshot),
                    prefix="", depth=1,
                    budget=_route()._WalkBudget(config.max_members, config.max_total_uncompressed_bytes),
                    counters=counters, config=config, run_id=0, cancellation=cancellation,
                )
            if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                raise ArchiveExtractionError(
                    "archive_source_changed", "ZIP source changed during traversal", retryable=True
                )
        _require_current_source(snapshot, "ZIP source path changed during traversal")
        del archive
        if grant is not None:
            # Traversal buffers and nested ZIP objects have gone out of scope.
            # Keep only enough RAM to decode/compress one owned record during
            # publication; the anonymous stream retains its separate charge.
            grant.shrink_transient_bytes(min(
                _archive_container_memory(config),
                4 * 1024 * 1024 + spool.max_record_size * 12,
            ))
        return _PreparedArchiveContainer(snapshot, counters, spool=spool)
    except CancellationRequested:
        spool.close()
        raise
    except ArchiveExtractionError as failure:
        spool.close()
        return _PreparedArchiveContainer(snapshot, counters, failure=failure)
    except (OSError, RuntimeError, ZipStructureError, zipfile.BadZipFile, zlib.error) as exc:
        spool.close()
        corrupt_failure = ArchiveExtractionError(
            "archive_corrupt_container", f"{type(exc).__name__}: {exc}"
        )
        return _PreparedArchiveContainer(snapshot, counters, failure=corrupt_failure)
    except BaseException:
        spool.close()
        raise


def _require_current_source(snapshot: FileSnapshot, message: str) -> None:
    try:
        current = snapshot_path(snapshot.path)
    except OSError as exc:
        raise ArchiveExtractionError(
            "archive_source_changed",
            f"{message}: {type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc
    if not same_snapshot(snapshot, current):
        raise ArchiveExtractionError(
            "archive_source_changed",
            message,
            retryable=True,
        )
