"""Bounded ZIP traversal and owned container extraction execution.

The route facade keeps durable SQLite publication; this module owns the
ordered member walk, worker admission and typed observation spool.  All route
callbacks are resolved lazily so the historical route namespace remains the
patch/injection boundary.
"""

from __future__ import annotations

import io
import pickle
import tempfile
import threading
import sqlite3
import zipfile
import zlib
from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from neocortex.deduplication import FileSnapshot
from neocortex.platform.zip_safety import (
    ZipStructureError,
    inspect_zip_bytes,
)
from neocortex.runtime.control.cancellation import CancellationToken

from .logical import LogicalDocumentEvidence, LOGICAL_MEDIA_TYPES
from .member_processing import (
    MAX_EMBEDDED_DOCUMENT_MEMBERS,
    MAX_MEMBER_NAME_CHARS,
    _NESTED_ARCHIVE_EXTENSIONS,
    _SUPPORTED_COMPRESSIONS,
    _ZIP_MAGIC_PREFIXES,
    _ExtractedContent,
    _WalkBudget as _WalkBudgetType,
)
from .contracts import ARCHIVE_MIME, ArchiveExtractionError

ArchiveRouteConfig = Any
ARCHIVE_READ_CHUNK_BYTES = 64 * 1024


def _route():
    from . import route

    return route


def _coordinated_archive_gate():
    return _route()._coordinated_archive_gate()


def _archive_cancellation():
    return _route()._archive_cancellation()


def _archive_container_capacity(gate, config, *, media_possible=True):
    return _route()._archive_container_capacity(gate, config, media_possible=media_possible)


def _archive_container_memory(config):
    return _route()._archive_container_memory(config)


def _extract_member_content(*args, **kwargs):
    return _route()._extract_member_content(*args, **kwargs)


def _record_issue(*args, **kwargs):
    return _route()._record_issue(*args, **kwargs)


def _observe_member(*args, **kwargs):
    return _route()._observe_member(*args, **kwargs)


def _store_member(*args, **kwargs):
    return _route()._store_member(*args, **kwargs)


def _store_logical_observation(*args, **kwargs):
    return _route()._store_logical_observation(*args, **kwargs)


def _normalized_member_name(*args, **kwargs):
    return _route()._normalized_member_name(*args, **kwargs)


def _member_is_special(*args, **kwargs):
    return _route()._member_is_special(*args, **kwargs)


def _compression_ratio(*args, **kwargs):
    return _route()._compression_ratio(*args, **kwargs)


def _read_zip_member(*args, **kwargs):
    return _route()._read_zip_member(*args, **kwargs)


def _inspect_logical_document(*args, **kwargs):
    return _route()._inspect_logical_document(*args, **kwargs)


def _embedded_part_selected(*args, **kwargs):
    return _route()._embedded_part_selected(*args, **kwargs)


def _metadata_content(*args, **kwargs):
    return _route()._metadata_content(*args, **kwargs)


def _image_media_type(*args, **kwargs):
    return _route()._image_media_type(*args, **kwargs)


def _WalkBudget(*args, **kwargs):
    return _route()._WalkBudget(*args, **kwargs)


@dataclass(slots=True)
class _ContainerCounters:
    members: int = 0
    indexed: int = 0
    metadata_only: int = 0
    nested_archives: int = 0
    issues: int = 0
    coverage_issues: int = 0
    text_chars: int = 0
    max_depth: int = 0
    materialization_applied: int = 0
    materialization_reused: int = 0
    materialization_pending: int = 0
    materialization_collisions: int = 0
    materialization_units_preserved: int = 0
    materialization_manifest_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _MemberObservation:
    member_chain: str
    member_path: str
    depth: int
    info: zipfile.ZipInfo
    content: _ExtractedContent
    document_role: str
    logical_document_chain: str | None


@dataclass(frozen=True, slots=True)
class _LogicalObservation:
    member_chain: str
    observation: LogicalDocumentEvidence
    name: str
    depth: int


@dataclass(frozen=True, slots=True)
class _IssueObservation:
    member_chain: str | None
    depth: int
    code: str
    detail: str


class _ArchiveObservationSpool:
    """One owned anonymous stream; never load a pickle supplied by the corpus.

    Only the three in-process observation dataclasses are serialized. Each
    record uses its own memo, so previous member text is not retained in RAM.
    The task lease owns actual temporary bytes until the consumer closes it.
    """

    def __init__(self, config: ArchiveRouteConfig, grant) -> None:
        self.stream = tempfile.TemporaryFile()
        self.grant = grant
        self.size = 0
        self.max_record_size = 0
        self.record_limit = (
            config.max_total_text_chars * 4 + config.max_central_directory_bytes * 4 + 1024 * 1024
        )
        self.total_limit = (
            config.max_total_text_chars * 8
            + config.max_members * (MAX_MEMBER_NAME_CHARS * 8 + 20_000)
            + config.max_central_directory_bytes * 4
        )

    def append(self, observation: _MemberObservation | _LogicalObservation | _IssueObservation):
        payload = pickle.dumps(observation, protocol=5)
        if len(payload) > self.record_limit or self.size + len(payload) + 8 > self.total_limit:
            raise ArchiveExtractionError("archive_spool_limit", "archive observation spool is full")
        new_size = self.size + len(payload) + 8
        if self.grant is not None:
            from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

            try:
                self.grant.resize_temp_bytes(
                    new_size, directory=tempfile.gettempdir(), file_descriptor=self.stream.fileno()
                )
            except MemoryBudgetExceeded as exc:
                raise ArchiveExtractionError("archive_spool_limit", str(exc)) from exc
        self.stream.write(len(payload).to_bytes(8, "little"))
        self.stream.write(payload)
        self.size = new_size
        self.max_record_size = max(self.max_record_size, len(payload))

    def observations(self):
        self.stream.seek(0)
        while header := self.stream.read(8):
            if len(header) != 8:
                raise ArchiveExtractionError("archive_spool_invalid", "truncated owned spool header")
            size = int.from_bytes(header, "little")
            if size > self.record_limit:
                raise ArchiveExtractionError("archive_spool_invalid", "oversized owned spool record")
            payload = self.stream.read(size)
            if len(payload) != size:
                raise ArchiveExtractionError("archive_spool_invalid", "truncated owned spool record")
            observation = pickle.loads(payload)
            if not isinstance(observation, (_MemberObservation, _LogicalObservation, _IssueObservation)):
                raise ArchiveExtractionError("archive_spool_invalid", "unknown owned observation")
            yield observation

    def close(self) -> None:
        stream = getattr(self, "stream", None)
        if stream is not None:
            stream.close()

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _ArchiveMemberWork:
    info: zipfile.ZipInfo
    name: str
    member_chain: str
    payload: bytes | None
    zip_kind: str | None
    content: _ExtractedContent | None
    nested_observation: LogicalDocumentEvidence | None
    config: ArchiveRouteConfig
    extraction_required: bool = False


_STOP_ARCHIVE_WALK = object()


_ARCHIVE_PROCESS_MIN_BYTES = 256 * 1024


class _ArchiveContainerGroup:
    def __init__(self) -> None:
        self.active = 0
        self.lock = threading.Lock()

    @contextmanager
    def extracting(self):
        with self.lock:
            self.active += 1
        token = _ARCHIVE_CONTAINER_GROUP.set(self)
        try:
            yield
        finally:
            _ARCHIVE_CONTAINER_GROUP.reset(token)
            with self.lock:
                self.active -= 1

    def member_capacity(self, gate, config: ArchiveRouteConfig) -> int:
        target = _archive_container_capacity(gate, config, media_possible=False)
        with self.lock:
            count = max(1, self.active)
        # All maps use one route budget. Dividing the conservative parent+
        # child target prevents every ZIP from growing a full independent
        # pool and exhausting memory with idle interpreters. Re-evaluate it
        # as peers finish and resources return.
        return 0 if target == 0 else max(1, target // count)


_ARCHIVE_CONTAINER_GROUP: ContextVar[_ArchiveContainerGroup | None] = ContextVar(
    "archive_container_group", default=None
)


@contextmanager
def _archive_work_admission(
    *, phase: str, estimated_bytes: int = 0, cpu_slots: int = 1, temp_bytes: int = 0
):
    gate = _coordinated_archive_gate()
    if gate is None:
        yield None
        return
    from neocortex.runtime.control.global_resources import current_resource_grant, resource_grant_scope

    parent = current_resource_grant()
    resume_parent = parent is not None and parent.cpu_slots > 0
    if resume_parent and parent is not None:
        parent.release_cpu()
    try:
        with gate.admit(
            estimated_bytes, cpu_slots=cpu_slots, native_threads=cpu_slots, io_slots=1, phase=phase,
            cancellation=_archive_cancellation(), temp_bytes=temp_bytes,
        ) as grant:
            with resource_grant_scope(grant) if grant is not None else nullcontext():
                yield grant
    finally:
        if resume_parent and parent is not None:
            parent.checkpoint()


def _archive_member_memory(work: _ArchiveMemberWork) -> int:
    size = len(work.payload or b"")
    return 4 * 1024 * 1024 + size * 2 + min(size, work.config.max_text_chars) * 12


def _archive_member_uses_process(work: _ArchiveMemberWork) -> bool:
    payload = work.payload or b""
    return not (
        payload.startswith(b"%PDF-")
        or PurePosixPath(work.name).suffix.casefold() == ".pdf"
        or _image_media_type(work.name, payload) is not None
    )


def _extract_archive_member(work: _ArchiveMemberWork) -> _ArchiveMemberWork:
    """Pure text/XML runs in processes; media supervises its bounded child."""

    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    cancellation = current_worker_cancellation()
    if cancellation is not None:
        cancellation.checkpoint()
    content = work.content
    if content is None:
        content = _extract_member_content(
            work.name,
            work.payload or b"",
            zip_kind=None,
            char_limit=work.config.max_text_chars,
            budget=_WalkBudget(work.config.max_members, work.config.max_total_uncompressed_bytes),
            config=work.config,
        )
    if cancellation is not None:
        cancellation.checkpoint()
    return replace(work, payload=None, content=content)


def _iter_archive_members(
    entries: Sequence[zipfile.ZipInfo],
    prepare: Callable[[zipfile.ZipInfo], _ArchiveMemberWork | object | None],
    *,
    budget: _WalkBudgetType,
    counters: _ContainerCounters,
    config: ArchiveRouteConfig,
    cancellation: CancellationToken,
) -> Generator[_ArchiveMemberWork, None, None]:
    """Drain siblings before nested packages so one shared budget stays exact."""

    from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map

    gate = _coordinated_archive_gate()
    group = _ARCHIVE_CONTAINER_GROUP.get()
    # A new interpreter costs much more than a few tiny text members. Reserve
    # persistent processes when declared *uncompressed* work can amortize it;
    # short packages use their supervisors for bounded reads and brief parsing.
    # This does not claim CPU parallelism for the Python work in those threads.
    declared_work = sum(
        info.file_size for info in entries if 0 < info.file_size <= config.max_member_bytes
    )
    executor_kind: Literal["thread", "process"] = (
        "process" if declared_work >= _ARCHIVE_PROCESS_MIN_BYTES else "thread"
    )
    iterator = iter(entries)
    exhausted = False
    barrier: _ArchiveMemberWork | None = None

    deferred: deque[zipfile.ZipInfo] = deque()
    stopped = False

    def batch() -> Iterator[zipfile.ZipInfo]:
        nonlocal exhausted
        while barrier is None and not stopped:
            cancellation.checkpoint()
            if deferred:
                yield deferred.popleft()
                continue
            try:
                info = next(iterator)
            except StopIteration:
                exhausted = True
                return
            # No blocking admissions or payload reads in source iteration:
            # admitted workers may already be waiting for owner preparation.
            yield info

    def prepared(info: zipfile.ZipInfo):
        nonlocal barrier, stopped
        if stopped:
            return ImmediateResult(None)
        if barrier is not None:
            # Speculation after a nested package stops before reading it or
            # spending the shared budget. This deque is bounded by the map.
            deferred.append(info)
            return ImmediateResult(None)
        work = prepare(info)
        if work is _STOP_ARCHIVE_WALK:
            stopped = True
            deferred.clear()
            return ImmediateResult(None)
        if not isinstance(work, _ArchiveMemberWork):
            return ImmediateResult(None)
        if work.zip_kind not in {None, "corrupt_archive"}:
            barrier = work
            return ImmediateResult(None)
        if work.content is not None:
            return ImmediateResult(replace(work, payload=None))
        if not _archive_member_uses_process(work):
            require_capacity(
                _archive_member_memory(work)
                + config.pdf_worker_memory_bytes * (2 if config.ocr_mode != "never" else 1)
                + len(work.payload or b"") * 2 + config.max_text_chars * 6 + 320 * 1024
            )
        return work

    def require_capacity(demand: int) -> None:
        from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

        interpreter = 64 * 1024 * 1024 if executor_kind == "process" else 0
        if gate is not None:
            try:
                gate.worker_capacity(
                    estimated_bytes=_archive_container_memory(config) + demand + interpreter,
                    native_threads=1,
                )
            except MemoryBudgetExceeded as exc:
                raise ArchiveExtractionError("archive_resource_limit", str(exc)) from exc

    def estimate(info: zipfile.ZipInfo) -> int:
        readable = (
            0 < info.file_size <= config.max_member_bytes
            and not info.is_dir() and not info.flag_bits & 0x1
            and not _member_is_special(info)
            and info.compress_type in _SUPPORTED_COMPRESSIONS
            and _compression_ratio(info) <= config.max_compression_ratio
        )
        size = info.file_size if readable else 0
        demand = 4 * 1024 * 1024 + size * 2 + min(size, config.max_text_chars) * 12
        require_capacity(demand)
        return demand

    while (not exhausted or deferred) and not stopped:
        with elastic_map(
            # Resolve through the route facade at submission time.  Besides
            # preserving the public injection seam, this keeps process-worker
            # pickling valid when tests/callers replace the extractor.
            _route()._extract_archive_member,
            batch(),
            gate=gate,
            capacity=(
                None if group is None
                else lambda: group.member_capacity(gate, config)
            ),
            estimated_bytes=estimate,
            native_threads=1,
            io_slots=1,
            phase="archive-member-extract",
            prepare=prepared,
            executor_kind=executor_kind,
            process_predicate=_archive_member_uses_process,
            cancellation=cancellation,
        ) as results:
            for result in results:
                if result is not None:
                    yield result
        if barrier is not None:
            work, barrier = barrier, None
            with _archive_work_admission(
                phase="archive-package", estimated_bytes=_archive_member_memory(work)
            ):
                if work.content is None:
                    remaining = config.max_total_text_chars - counters.text_chars
                    content = (
                        _metadata_content(
                            detail="container text budget is exhausted",
                            issue_code="archive_total_text_limit",
                        )
                        if remaining < 1 else _extract_member_content(
                            work.name, work.payload or b"", zip_kind=work.zip_kind,
                            char_limit=min(config.max_text_chars, remaining),
                            budget=budget, config=config,
                        )
                    )
                    work = replace(work, content=content)
                yield work


def _walk_zip(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    archive: zipfile.ZipFile,
    snapshot: FileSnapshot,
    container_key: str,
    *,
    prefix: str,
    depth: int,
    budget: _WalkBudgetType,
    counters: _ContainerCounters,
    config: ArchiveRouteConfig,
    run_id: int,
    cancellation: CancellationToken,
    component_of: str | None = None,
) -> None:
    logical_document: LogicalDocumentEvidence | None = None
    prefetched: dict[str, bytes] = {}
    if not prefix:
        try:
            logical_document, prefetched = _inspect_logical_document(
                archive, budget=budget, config=config
            )
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=None,
                depth=0,
                code=exc.code,
                detail=str(exc),
            )
        if logical_document is not None:
            _store_logical_observation(
                connection,
                container_key,
                "",
                logical_document,
                name=snapshot.path,
                depth=0,
                counters=counters,
            )
    own_logical_document = logical_document is not None and logical_document.identified
    component_chain = "" if own_logical_document else component_of
    is_component = component_chain is not None
    logical_text_parts: list[str] = []
    seen_names: set[str] = set()
    def prepare_entry(info: zipfile.ZipInfo):
        payload: bytes | None = None
        zip_kind: str | None = None
        cancellation.checkpoint()
        try:
            budget.observe_member()
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=prefix.rstrip("!/") or None,
                depth=depth,
                code=exc.code,
                detail=str(exc),
            )
            return _STOP_ARCHIVE_WALK
        counters.max_depth = max(counters.max_depth, depth)
        try:
            name = _normalized_member_name(info)
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                # Preserve the rejected *name as data* so issues remain
                # filterable without pretending it is a safe virtual path.
                member_chain=f"{prefix}{info.orig_filename[:MAX_MEMBER_NAME_CHARS]}",
                depth=depth,
                code=exc.code,
                detail=str(exc),
            )
            return None
        member_chain = f"{prefix}{name}"
        if name in seen_names:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=member_chain,
                depth=depth,
                code="archive_duplicate_member",
                detail=f"duplicate member name in one archive: {name}",
            )
            return None
        seen_names.add(name)
        if info.is_dir():
            return None
        counters.members += 1
        nested_observation: LogicalDocumentEvidence | None = None
        if info.flag_bits & 0x1:
            content = _metadata_content(
                detail="encrypted ZIP members are not read",
                issue_code="archive_encrypted_member",
            )
        elif _member_is_special(info):
            content = _metadata_content(
                detail="symlink or special ZIP member is not read",
                issue_code="archive_special_member",
            )
        elif info.compress_type not in _SUPPORTED_COMPRESSIONS:
            content = _metadata_content(
                detail=f"unsupported ZIP compression method {info.compress_type}",
                issue_code="archive_unsupported_compression",
            )
        elif _compression_ratio(info) > config.max_compression_ratio:
            content = _metadata_content(
                detail=(
                    f"declared compression ratio {_compression_ratio(info):.2f} "
                    f"exceeds {config.max_compression_ratio:.2f}"
                ),
                issue_code="archive_compression_ratio_limit",
            )
        elif info.file_size > config.max_member_bytes:
            content = _metadata_content(
                detail=(
                    f"member declares {info.file_size} bytes; limit is {config.max_member_bytes}"
                ),
                issue_code="archive_member_size_limit",
            )
        elif name not in prefetched and not budget.can_read(int(info.file_size)):
            content = _metadata_content(
                detail="member would exceed the total decompression budget",
                issue_code="archive_total_uncompressed_limit",
            )
        else:
            try:
                payload = (
                    prefetched.pop(name)
                    if name in prefetched
                    else _read_zip_member(
                        archive,
                        info,
                        budget=budget,
                        max_bytes=config.max_member_bytes,
                    )
                )
            except (
                ArchiveExtractionError,
                OSError,
                RuntimeError,
                zipfile.BadZipFile,
                zlib.error,
            ) as exc:
                code = (
                    exc.code
                    if isinstance(exc, ArchiveExtractionError)
                    else "archive_member_read_error"
                )
                content = _metadata_content(
                    detail=f"{type(exc).__name__}: {exc}"[:500],
                    issue_code=code,
                )
            else:
                suffix = PurePosixPath(name).suffix.casefold()
                zip_kind = None
                if payload.startswith(_ZIP_MAGIC_PREFIXES) or suffix in _NESTED_ARCHIVE_EXTENSIONS:
                    try:
                        inspect_zip_bytes(
                            payload,
                            max_members=MAX_EMBEDDED_DOCUMENT_MEMBERS,
                            max_central_directory_bytes=config.max_central_directory_bytes,
                        )
                        with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                            nested_observation, _ = _inspect_logical_document(
                                nested,
                                budget=budget,
                                config=config,
                            )
                        zip_kind = (
                            nested_observation.logical_kind
                            if nested_observation is not None and nested_observation.identified
                            else "archive"
                        )
                    except ArchiveExtractionError as exc:
                        zip_kind = "archive"
                        _record_issue(
                            connection,
                            container_key,
                            counters,
                            member_chain=member_chain,
                            depth=depth,
                            code=exc.code,
                            detail=str(exc),
                        )
                    except (
                        OSError,
                        RuntimeError,
                        ZipStructureError,
                        zipfile.BadZipFile,
                        zlib.error,
                    ):
                        zip_kind = "corrupt_archive"
                    if nested_observation is not None:
                        _store_logical_observation(
                            connection,
                            container_key,
                            member_chain,
                            nested_observation,
                            name=name,
                            depth=depth,
                            counters=counters,
                        )
                if zip_kind == "archive":
                    content = _ExtractedContent(
                        None,
                        "archive",
                        "application/zip",
                    )
                elif zip_kind == "corrupt_archive":
                    content = _metadata_content(
                        detail="nested ZIP structure is corrupt or unsupported",
                        issue_code="archive_nested_corrupt",
                    )
                else:
                    content = None
        return _ArchiveMemberWork(
            info, name, member_chain, payload, zip_kind, content, nested_observation, config,
            content is None,
        )

    def sequential_members():
        for info in archive.infolist():
            work = prepare_entry(info)
            if work is _STOP_ARCHIVE_WALK:
                break
            if not isinstance(work, _ArchiveMemberWork):
                continue
            if work.content is None:
                remaining = config.max_total_text_chars - counters.text_chars
                content = (
                    _metadata_content(
                        detail="container text budget is exhausted",
                        issue_code="archive_total_text_limit",
                    ) if remaining < 1 else _extract_member_content(
                        work.name, work.payload or b"", zip_kind=work.zip_kind,
                        char_limit=min(config.max_text_chars, remaining),
                        budget=budget, config=config,
                    )
                )
                work = replace(work, content=content)
            yield work

    if _coordinated_archive_gate() is not None:
        from neocortex.runtime.control.global_resources import current_resource_grant

        structure_grant = current_resource_grant()
        if structure_grant is not None:
            # Central-directory and logical-package inspection are finished.
            # Keep their memory; member jobs now own CPU and I/O execution.
            structure_grant.release_cpu()
    members = (
        sequential_members()
        if _coordinated_archive_gate() is None else _iter_archive_members(
            archive.infolist(), prepare_entry, budget=budget, counters=counters,
            config=config, cancellation=cancellation,
        )
    )
    with closing(members):
        for work in members:
            info, name, member_chain = work.info, work.name, work.member_chain
            payload = work.payload
            nested_observation = work.nested_observation
            content = work.content
            if content is None:
                raise RuntimeError("archive member was not extracted")
            # Worker results arrive in original order. Only the owner spends the
            # shared text budget, including results computed speculatively ahead.
            if content.kind != "archive":
                remaining = config.max_total_text_chars - counters.text_chars
                if remaining < 1 and work.extraction_required:
                    content = _metadata_content(
                        detail="container text budget is exhausted",
                        issue_code="archive_total_text_limit",
                    )
                elif content.text is not None and len(content.text) > remaining:
                    content = replace(
                        content, text=content.text[:remaining], detail="text_truncated",
                        issue_code="archive_text_limit",
                    )

            _observe_member(
                connection,
                snapshot,
                container_key,
                member_chain,
                name,
                depth,
                info,
                content,
                config.processing_signature,
                run_id,
                document_role=(
                    "document_component"
                    if is_component
                    else "logical_document"
                    if nested_observation is not None and nested_observation.identified
                    else "archive_member"
                ),
                logical_document_chain=(
                    component_chain
                    if is_component
                    else member_chain
                    if nested_observation is not None and nested_observation.identified
                    else None
                ),
            )
            if (
                is_component
                and content.text
                and logical_document is not None
                and _embedded_part_selected(name, logical_document.logical_kind or "")
            ):
                logical_text_parts.append(content.text)
            if content.text is None:
                counters.metadata_only += 1
            else:
                counters.indexed += 1
                counters.text_chars += len(content.text)
            if content.issue_code:
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code=content.issue_code,
                    detail=content.detail or content.issue_code,
                )

            if content.kind != "archive":
                continue
            counters.nested_archives += 1
            if depth >= config.max_depth:
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code="archive_depth_limit",
                    detail=f"nested ZIP depth exceeds configured limit {config.max_depth}",
                )
                continue
            try:
                if payload is None:
                    raise RuntimeError("nested ZIP payload was released before traversal")
                remaining_members = config.max_members - budget.members_seen
                if remaining_members < 1:
                    raise ArchiveExtractionError(
                        "archive_member_count_limit",
                        "no member budget remains for nested ZIP traversal",
                    )
                inspect_zip_bytes(
                    payload,
                    max_members=remaining_members,
                    max_central_directory_bytes=config.max_central_directory_bytes,
                )
                with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                    from neocortex.runtime.control.global_resources import current_resource_grant

                    parent_grant = current_resource_grant() if _coordinated_archive_gate() else None
                    if parent_grant is not None:
                        parent_grant.release_cpu()
                    try:
                        _walk_zip(
                            connection,
                            nested,
                            snapshot,
                            container_key,
                            prefix=f"{member_chain}!/",
                            depth=depth + 1,
                            budget=budget,
                            counters=counters,
                            config=config,
                            run_id=run_id,
                            cancellation=cancellation,
                            component_of=component_chain,
                        )
                    finally:
                        if parent_grant is not None:
                            parent_grant.checkpoint()
            except (
                ArchiveExtractionError,
                OSError,
                RuntimeError,
                ZipStructureError,
                zipfile.BadZipFile,
                zlib.error,
            ) as exc:
                code = exc.code if isinstance(exc, ArchiveExtractionError) else "archive_nested_corrupt"
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code=code,
                    detail=f"{type(exc).__name__}: {exc}"[:2_000],
                )

    if own_logical_document and logical_document is not None:
        if _coordinated_archive_gate() is not None:
            from neocortex.runtime.control.global_resources import current_resource_grant

            final_grant = current_resource_grant()
            if final_grant is not None:
                final_grant.checkpoint(drain=True)
        # The root is a physical file-backed logical document, not a fabricated
        # ZIP entry. Depth/chain/role explicitly distinguish it from members.
        root_info = zipfile.ZipInfo(Path(snapshot.path).name)
        root_info.file_size = root_info.compress_size = snapshot.size
        root_info.CRC = 0  # no member CRC exists for the physical root
        text = "\n".join(logical_text_parts)[: config.max_total_text_chars]
        kind = logical_document.logical_kind or "archive"
        _observe_member(
            connection,
            snapshot,
            container_key,
            "",
            "",
            0,
            root_info,
            _ExtractedContent(
                text or None,
                kind,
                LOGICAL_MEDIA_TYPES.get(kind, ARCHIVE_MIME),
                "logical_document_projection; integrity=not_verified; opening=not_verified",
            ),
            config.processing_signature,
            run_id,
            document_role="logical_document",
            logical_document_chain="",
        )
