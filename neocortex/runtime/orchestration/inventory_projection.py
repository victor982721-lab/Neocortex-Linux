"""Bounded, detached Code inputs prepared by the inventory's existing owner."""

from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Iterable
from contextlib import ExitStack
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TextIO

if TYPE_CHECKING:
    from neocortex.runtime.control.global_resources import ResourceGrant
    from neocortex.deduplication import FileSnapshot

_MEMORY_BYTES = 8 * 1024 * 1024
_MEMORY_RECORDS = 4096


class _SnapshotOwner(Protocol):
    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]: ...


@dataclass(frozen=True, slots=True)
class CodeInventoryProjection:
    """Replay a bounded tuple or private spool without reopening inventory SQLite."""

    records: tuple[FileSnapshot, ...] = ()
    _spool: TextIO | None = field(default=None, repr=False)
    _resources: ExitStack = field(default_factory=ExitStack, repr=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False)
    _closed: bool = field(default=False, repr=False)
    _resource_context: Context = field(default_factory=copy_context, repr=False)

    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]:
        from neocortex.deduplication import FileSnapshot

        if type(scan_id) is not int or scan_id <= 0:
            raise ValueError("Code inventory projection scan_id must be positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("Code inventory projection is closed")
            if self._spool is None:
                yield from self.records
            else:
                self._spool.seek(0)
                for line in self._spool:
                    yield FileSnapshot(*json.loads(line))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                object.__setattr__(self, "_closed", True)
                object.__setattr__(self, "records", ())
                self._resource_context.run(self._resources.close)


def _serialized(snapshot: FileSnapshot) -> str:
    return json.dumps((
        snapshot.path, snapshot.volume_id, snapshot.file_id, snapshot.size,
        snapshot.mtime_ns, snapshot.birthtime_ns,
    ), ensure_ascii=True, separators=(",", ":")) + "\n"


def build_code_inventory_projection(
    index: _SnapshotOwner,
    scan_id: int,
    *,
    cancellation: object | None = None,
) -> CodeInventoryProjection:
    from neocortex.deduplication import FileSnapshot
    from neocortex.code.ingestion.code_candidate_scope import is_project_marker
    from neocortex.code.ingestion.code_detection import likely_code_candidate
    from neocortex.runtime.control.global_resources import resource_gate

    result = CodeInventoryProjection()
    gate = resource_gate("preparation")
    grant: ResourceGrant | None = None
    checkpoint = getattr(cancellation, "checkpoint", None)
    rows: list[FileSnapshot] = []
    estimated = 0
    spool_bytes = 0
    buffered_lines: list[str] = []
    buffered_bytes = 0

    def flush_spool() -> None:
        nonlocal spool_bytes, buffered_bytes
        if not buffered_lines:
            return
        assert result._spool is not None
        if grant is not None:
            grant.resize_temp_bytes(
                spool_bytes + buffered_bytes, file_descriptor=result._spool.fileno(),
            )
        result._spool.write("".join(buffered_lines))
        result._spool.flush()
        spool_bytes += buffered_bytes
        if grant is not None:
            grant.resize_temp_bytes(spool_bytes, file_descriptor=result._spool.fileno())
        buffered_lines.clear()
        buffered_bytes = 0

    def write_snapshot(snapshot: FileSnapshot) -> None:
        nonlocal buffered_bytes
        line = _serialized(snapshot)
        if buffered_bytes + len(line) > 128 * 1024:
            flush_spool()
        buffered_lines.append(line)
        buffered_bytes += len(line)
    try:
        if gate is not None:
            # Keep the retained lease's context with its resource owner. The
            # projection outlives preparation and must not become an ambient
            # execution grant inherited by unrelated format routes.
            grant = result._resource_context.run(
                result._resources.enter_context,
                gate.admit(_MEMORY_BYTES, phase="code_inventory_projection", io_slots=1),
            )
        for ordinal, snapshot in enumerate(index.snapshots(scan_id)):
            if callable(checkpoint):
                checkpoint()
            if grant is not None and ordinal % 128 == 0:
                grant.checkpoint()
            if not isinstance(snapshot, FileSnapshot):
                raise TypeError("inventory owner returned an invalid FileSnapshot")
            if not (likely_code_candidate(snapshot.path) or is_project_marker(snapshot.path)):
                continue
            estimated += len(snapshot.path.encode("utf-8", "surrogatepass")) * 4 + 256
            if result._spool is None and (
                len(rows) >= _MEMORY_RECORDS or estimated >= _MEMORY_BYTES // 2
            ):
                object.__setattr__(result, "_spool", result._resources.enter_context(
                    tempfile.TemporaryFile(mode="w+t", encoding="utf-8"),
                ))
                for row in rows:
                    write_snapshot(row)
                rows.clear()
            if result._spool is None:
                rows.append(snapshot)
            else:
                write_snapshot(snapshot)
        object.__setattr__(result, "records", tuple(rows))
        if result._spool is not None:
            flush_spool()
        if grant is not None:
            grant.release_cpu()
        return result
    except BaseException:
        result.close()
        raise
