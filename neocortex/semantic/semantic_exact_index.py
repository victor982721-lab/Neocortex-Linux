"""Explicit, optional exact-index preparation and verified handle lifecycle.

A persistent artifact is not an authority.  Opening it checks its complete
representation against the native published-owner rows.  Repeated queries
accept that verified handle, never a path that silently rebuilds or validates
an entire owner within a bounded search.  No database writes, models, pruning,
or automatic cache discovery occur here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from neocortex.persistence.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from neocortex.persistence.sqlite_immutable import (
    SQLiteImmutableFence,
    capture_sqlite_read_fence,
)

from . import semantic_exact_index_format as _format
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    ExactSearchPage,
    ExactSearchQuery,
    SearchHit,
    canonical_json,
    normalize_vector,
)
from .semantic_vector_search import (
    VectorSearchBudget,
    VectorSearchPage,
    VectorSearchRequest,
    VectorSearchUnavailable,
)
from .semantic_repository_common import _load_model
from .semantic_schema import (
    SemanticStateError,
    _validate_semantic_read_schema,
    semantic_database,
)

TextScope = Literal["all", "content", "title"]
MAX_EXACT_INDEX_ROWS = 500_000
MAX_EXACT_INDEX_BYTES = 4_000_000_000
MAX_EXACT_INDEX_MODELS = 1_024
READY_FILE = "exact-index-ready.json"
READY_SCHEMA = "neocortex.semantic.exact-index-ready/v1"
_HANDLE_PROOF = object()


class ExactIndexUnavailable(SemanticStateError):
    """A requested optional artifact cannot be safely reused."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or f"exact index unavailable: {reason}")


class _Checks:
    def __init__(self, callback: Callable[[], None] | None) -> None:
        self.callback = callback
        self.cancelled: BaseException | None = None

    def checkpoint(self) -> None:
        if self.callback is not None:
            try:
                self.callback()
            except BaseException as exc:
                self.cancelled = exc
                raise

    def preserve_cancellation(self, error: BaseException) -> None:
        if self.cancelled is not None:
            if self.cancelled is error:
                raise error
            raise self.cancelled from error


@dataclass(frozen=True, slots=True)
class _Owner:
    path: Path
    fence: SQLiteImmutableFence
    schema_version: int
    models: Mapping[str, EmbeddingModelSpec]
    heads: Mapping[str, int]
    pair: _format.PublishedPair
    scope: TextScope
    binding: Mapping[str, object]


def _absolute(path: Path, label: str) -> Path:
    selected = Path(path)
    if not selected.is_absolute() or ".." in selected.parts:
        raise ValueError(f"{label} must be an absolute path without parent traversal")
    return selected


def _cleanup_resources(*actions: Callable[[], None]) -> None:
    """Attempt every cleanup without replacing an exception already in flight."""
    primary = sys.exception()
    first: BaseException | None = None
    for action in actions:
        try:
            action()
        except BaseException as exc:
            if primary is not None:
                primary.add_note(f"exact index cleanup: {type(exc).__name__}: {exc}")
            elif first is None:
                first = exc
            else:
                first.add_note(f"additional cleanup: {type(exc).__name__}: {exc}")
    if first is not None:
        raise first


@contextmanager
def _directory_fd(path: Path) -> Iterator[int]:
    selected = _absolute(path, "directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in selected.parts[1:]:
            following = os.open(component, flags, dir_fd=descriptor)
            previous = descriptor
            descriptor = following
            # On Linux close can release a descriptor even when it reports an
            # error. Transfer ownership first; never retry the numeric old FD.
            os.close(previous)
        yield descriptor
    finally:
        _cleanup_resources(lambda: os.close(descriptor))


def _source_path(path: Path) -> Path:
    selected = _absolute(path, "semantic database")
    try:
        with _directory_fd(selected.parent) as parent:
            descriptor = os.open(
                selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ExactIndexUnavailable("source_not_regular")
            finally:
                _cleanup_resources(lambda: os.close(descriptor))
    except OSError as exc:
        raise ExactIndexUnavailable("source_unavailable", str(exc)) from exc
    return selected


def _cold_operation_scope() -> None:
    # The existing reuse context lends the same connection, including any
    # caller's progress handler.  An explicit cold operation must not replace
    # that handler or silently acquire a larger independent snapshot budget.
    from .semantic_schema import _SEMANTIC_READ_CONTEXT

    if _SEMANTIC_READ_CONTEXT.get() is not None:
        raise ExactIndexUnavailable("cold_open_requires_independent_scope")


def _limits(max_rows: int, max_total_bytes: int) -> None:
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_EXACT_INDEX_ROWS:
        raise ValueError("max_rows must be between 1 and 500000")
    if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int) or not 1 <= max_total_bytes <= MAX_EXACT_INDEX_BYTES:
        raise ValueError("max_total_bytes must be between 1 and 4000000000")


def _scope(value: object) -> TextScope:
    if not isinstance(value, str) or value not in {"all", "content", "title"}:
        raise ValueError("text_scope must be all, content, or title")
    return cast(TextScope, value)


def _fence_payload(fence: SQLiteImmutableFence) -> dict[str, object]:
    def identity(value: object) -> dict[str, int]:
        return {
            key: int(getattr(value, key))
            for key in ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
        }
    return {
        "main": identity(fence.main),
        "sidecars": [{"suffix": suffix, **identity(value)} for suffix, value in fence.sidecars],
    }


def _model_payload(model: EmbeddingModelSpec) -> dict[str, object]:
    return {
        "model_signature": model.model_signature,
        "vector_space": model.vector_space,
        "modality": model.modality.value,
        "dimensions": model.dimensions,
        "vector_dtype": model.vector_dtype.value,
        "model_id": model.model_id,
        "model_version": model.model_version,
        "provider": model.provider,
        "normalization": model.normalization,
        "distance": model.distance,
        "supported_roles": [role.value for role in model.supported_roles],
        "provenance": dict(model.provenance),
    }


def _owner_contract(
    connection: sqlite3.Connection,
    source: Path,
    signature: str,
    scope: TextScope,
    fence: SQLiteImmutableFence,
    checks: _Checks,
) -> _Owner:
    checks.checkpoint()
    version = _validate_semantic_read_schema(connection)
    rows = connection.execute(
        "SELECT model_signature FROM embedding_models WHERE active=1 ORDER BY model_signature LIMIT ?",
        (MAX_EXACT_INDEX_MODELS + 1,),
    ).fetchall()
    if len(rows) > MAX_EXACT_INDEX_MODELS:
        raise ExactIndexUnavailable("model_bound")
    models: dict[str, EmbeddingModelSpec] = {}
    for row in rows:
        checks.checkpoint()
        model = _load_model(connection, str(row[0]))
        models[model.model_signature] = model
    selected = models.get(signature)
    if selected is None or selected.modality is not EmbeddingModality.TEXT:
        raise ExactIndexUnavailable("active_text_model_required")
    head_rows = connection.execute(
        """SELECT h.model_signature,h.generation_id,h.published_ns,
            g.model_signature,g.status,g.processing_signature
        FROM published_embedding_heads h
        JOIN embedding_generations g ON g.generation_id=h.generation_id
        ORDER BY h.model_signature LIMIT ?""",
        (MAX_EXACT_INDEX_MODELS + 1,),
    ).fetchall()
    if len(head_rows) > MAX_EXACT_INDEX_MODELS:
        raise ExactIndexUnavailable("head_bound")
    heads: dict[str, int] = {}
    head_payloads: list[dict[str, object]] = []
    pair: _format.PublishedPair | None = None
    for row in head_rows:
        checks.checkpoint()
        head_model = str(row[0])
        if head_model != str(row[3]) or str(row[4]) != "ready":
            raise ExactIndexUnavailable("published_head_invalid")
        heads[head_model] = int(row[1])
        head_payloads.append({
            "model_signature": head_model, "generation_id": int(row[1]),
            "published_ns": int(row[2]), "status": "ready",
            "processing_signature": str(row[5]),
        })
        if head_model == signature:
            pair = _format.PublishedPair(
                signature, int(row[1]), str(row[5]), selected.vector_space,
                "text", "text_chunk",
            )
    if pair is None:
        raise ExactIndexUnavailable("published_head_required")
    binding: dict[str, object] = {
        "owner": "semantic", "owner_path": str(source),
        "owner_fence": _fence_payload(fence), "schema_version": version,
        "published_heads": head_payloads,
        "active_models": [_model_payload(model) for model in models.values()],
        "exact_text_scope": scope, "selected_model_signature": signature,
    }
    return _Owner(source, fence, version, models, heads, pair, scope, binding)


def _native_rows(
    connection: sqlite3.Connection, owner: _Owner, *, max_rows: int, checks: _Checks,
) -> Iterator[_format.VectorRecord]:
    # This is the same source relation as the public exact search, not an
    # independent cache/provider or an approximation of published membership.
    from .semantic_search_repository import _search_sql

    sql = _search_sql(EmbeddingModality.TEXT, 1, text_scope=owner.scope)
    cursor = connection.execute(sql, (owner.pair.model_signature, owner.pair.generation_id, 0, max_rows + 1))
    count = 0
    while True:
        checks.checkpoint()
        rows = cursor.fetchmany(512)
        if not rows:
            break
        for row in rows:
            checks.checkpoint()
            count += 1
            if count > max_rows:
                raise ExactIndexUnavailable("source_row_bound")
            dtype = str(row["vector_dtype"])
            if dtype not in {"float16", "float32"}:
                raise ExactIndexUnavailable("source_vector_dtype")
            yield _format.VectorRecord(
                int(row["ref_id"]), str(row["entity_id"]), str(row["item_id"]),
                str(row["model_signature"]), str(row["vector_space"]), "text",
                int(row["generation_id"]), owner.pair.processing_signature, "text_chunk",
                cast(Literal["float16", "float32"], dtype),
                int(row["dimensions"]), bytes(row["vector_blob"]),
                str(row["provenance_json"]), owner.scope,
                f"semantic.member:{int(row['ref_id'])}",
            )


def _source_stable(owner: _Owner) -> None:
    if capture_sqlite_read_fence(owner.path) != owner.fence:
        raise ExactIndexUnavailable("source_changed")


def _manifest_digest(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(dict(manifest)).encode("utf-8")).hexdigest()


def _ready_payload(manifest: Mapping[str, object]) -> dict[str, object]:
    # Publication marker only.  It is NOT a trusted attestation at OPEN.
    return {"schema": READY_SCHEMA, "manifest_digest": _manifest_digest(manifest)}


def _unlink_owned_at(parent: int, name: str, identity: os.stat_result) -> None:
    try:
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns) != (
        identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns,
    ):
        raise ExactIndexUnavailable("completion_marker_replaced")
    os.unlink(name, dir_fd=parent)


def _remove_ready(directory: Path, identity: os.stat_result) -> None:
    with _directory_fd(directory) as parent:
        _unlink_owned_at(parent, READY_FILE, identity)
        os.fsync(parent)


def _write_ready(directory: Path, manifest: Mapping[str, object]) -> os.stat_result:
    encoded = (canonical_json(_ready_payload(manifest)) + "\n").encode("utf-8")
    temporary = ".exact-index-ready.tmp"
    with _directory_fd(directory) as parent:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent,
        )
        identity: os.stat_result | None = None
        linked = False

        def remove_owned_temporary() -> None:
            if identity is None:
                return  # No verified identity: leave the unpublished file.
            observed = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
                raise ExactIndexUnavailable("completion_temporary_changed")
            os.unlink(temporary, dir_fd=parent)

        try:
            try:
                identity = os.fstat(descriptor)
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("incomplete exact-index completion marker")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                identity = os.fstat(descriptor)
                observed = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
                    raise ExactIndexUnavailable("completion_temporary_changed")
                os.link(temporary, READY_FILE, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                linked = True
                os.fsync(parent)
            finally:
                # Only this newly created unpublished helper file is removed.
                _cleanup_resources(lambda: os.close(descriptor), remove_owned_temporary)
        except BaseException:
            if linked and identity is not None:
                owned_identity = identity
                _cleanup_resources(lambda: _unlink_owned_at(parent, READY_FILE, owned_identity))
            raise
        assert identity is not None
        return identity


def _read_ready(directory: Path, manifest: Mapping[str, object]) -> None:
    with _directory_fd(directory) as parent:
        descriptor = os.open(READY_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4_096:
                raise ExactIndexUnavailable("completion_marker_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                value = json.loads(stream.read(4_097))
            if value != _ready_payload(manifest):
                raise ExactIndexUnavailable("completion_marker_invalid")
        finally:
            _cleanup_resources(lambda: os.close(descriptor))


def _header_selection(directory: Path, header: Mapping[str, object], max_rows: int, max_total_bytes: int) -> tuple[str, TextScope]:
    count = header.get("row_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= max_rows:
        raise ExactIndexUnavailable("artifact_row_bound")
    files = header.get("files")
    if not isinstance(files, Mapping):
        raise ExactIndexUnavailable("artifact_metadata")
    total = 0
    with _directory_fd(directory) as parent:
        for name in (*files.keys(), "manifest.json", READY_FILE):
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                raise ExactIndexUnavailable("artifact_metadata")
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExactIndexUnavailable("artifact_file_type")
            total += info.st_size
    if total > max_total_bytes:
        raise ExactIndexUnavailable("artifact_byte_bound")
    binding = header.get("owner_binding")
    if not isinstance(binding, Mapping):
        raise ExactIndexUnavailable("artifact_metadata")
    signature = binding.get("selected_model_signature")
    if not isinstance(signature, str) or not signature.strip() or len(signature.encode("utf-8")) > 4_096:
        raise ExactIndexUnavailable("artifact_model")
    return signature, _scope(binding.get("exact_text_scope"))


class ExactIndexHandle:
    """A process-local verified handle; close it or use a context manager.

    Source rows are verified during explicit construction/opening, not during
    each warm query.  A handle serializes its own queries and cannot authorize
    a different owner, scope, head, runtime, or artifact after invalidation.
    """

    def __init__(self, owner: _Owner, view: _format.ValidatedView, *, _proof: object) -> None:
        if _proof is not _HANDLE_PROOF:
            raise TypeError("use prepare_exact_index or open_exact_index")
        self._owner = owner
        self._view = view
        self._lock = threading.RLock()
        self._invalid: str | None = None
        self._closed = False
        self._used = self._fallbacks = self._scanned = 0
        self._last_fallback: str | None = None

    def __enter__(self) -> ExactIndexHandle:
        if self._closed:
            raise ExactIndexUnavailable("handle_closed")
        return self

    def __exit__(self, *_args: object) -> None:
        _cleanup_resources(self.close)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._view.close()

    def summary(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema": READY_SCHEMA, "directory": str(self._view.root),
                "row_count": self._view.row_count,
                "model_signature": self._owner.pair.model_signature,
                "text_scope": self._owner.scope,
                "owner_schema_version": self._owner.schema_version,
                "verification": "native_owner_rows_at_prepare_or_open",
                "closed": self._closed, "invalidated": self._invalid,
                "manifest_digest": _manifest_digest(self._view.manifest),
            }

    def usage_summary(self) -> dict[str, object]:
        with self._lock:
            return {
                "used_queries": self._used, "fallback_queries": self._fallbacks,
                "rows_scanned": self._scanned, "last_fallback_reason": self._last_fallback,
            }

    def _fallback(self, reason: str) -> None:
        self._fallbacks += 1
        self._last_fallback = reason


def prepare_exact_index(
    database: Path,
    destination: Path,
    *,
    model_signature: str,
    text_scope: TextScope = "content",
    max_rows: int = MAX_EXACT_INDEX_ROWS,
    max_total_bytes: int = MAX_EXACT_INDEX_BYTES,
    cancellation_check: Callable[[], None] | None = None,
) -> ExactIndexHandle:
    """Explicitly create a new private exact artifact from one published head.

    The parent must already exist.  Existing outputs are never replaced or
    pruned.  A failed new artifact is not published as a verified handle;
    publication requires the adapter's final completion marker.
    """
    _limits(max_rows, max_total_bytes)
    _cold_operation_scope()
    scope = _scope(text_scope)
    if not isinstance(model_signature, str) or not model_signature.strip() or len(model_signature.encode("utf-8")) > 4_096:
        raise ValueError("model_signature must be bounded nonblank text")
    source = _source_path(database)
    output = _absolute(destination, "destination")
    if source == output or source.is_relative_to(output):
        raise ValueError("index destination must not contain the source owner")
    with _directory_fd(output.parent):
        if output.exists() or output.is_symlink():
            raise FileExistsError("exact index destination must be new")
    checks = _Checks(cancellation_check)
    checks.checkpoint()
    marker_bytes = len((canonical_json(_ready_payload({})) + "\n").encode("utf-8"))
    if max_total_bytes <= marker_bytes:
        raise ExactIndexUnavailable("artifact_byte_bound")
    view: _format.ValidatedView | None = None
    marker: os.stat_result | None = None
    try:
        fence = capture_sqlite_read_fence(source)
        with semantic_database(source, readonly=True) as connection:
            bridge = SQLiteCancellationBridge(checks.checkpoint)
            with sqlite_cancellation_scope(connection, bridge):
                owner = _owner_contract(connection, source, model_signature, scope, fence, checks)
                manifest = _format.prepare_exact_view(
                    _native_rows(connection, owner, max_rows=max_rows, checks=checks),
                    owner_binding=owner.binding, pairs=(owner.pair,), destination=output,
                    artifact_parent=output.parent, max_rows=max_rows,
                    max_total_bytes=max_total_bytes - marker_bytes,
                    numeric_projection=True, numeric_norms=True,
                    cancellation_check=checks.checkpoint,
                )
        _source_stable(owner)
        checks.checkpoint()
        row_count = manifest["row_count"]
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 1:
            raise ExactIndexUnavailable("published_scope_empty")
        view = _format.validate_exact_view(
            output, artifact_parent=output.parent, live_owner_binding=owner.binding,
            expected_pairs=(owner.pair,), verify_content=True,
            cancellation_check=checks.checkpoint,
        )
        _source_stable(owner)
        checks.checkpoint()
        # Publish only after complete validation. Directory timestamps change
        # solely because of this new marker; every data-file fence is retained.
        marker = _write_ready(output, manifest)
        view = replace(view, root_fence=_format._stat_fence(os.fstat(view.root_fd)))
        view.assert_files_stable()
        _source_stable(owner)
        checks.checkpoint()
        result = ExactIndexHandle(owner, view, _proof=_HANDLE_PROOF)
        view = None
        return result
    except (_format.DerivedViewError, SemanticStateError, OSError, KeyError, ValueError, sqlite3.Error) as exc:
        checks.preserve_cancellation(exc)
        if isinstance(exc, ExactIndexUnavailable):
            raise
        raise ExactIndexUnavailable("prepare_failed", str(exc)) from exc
    finally:
        if view is not None:
            if marker is not None:
                _cleanup_resources(lambda: _remove_ready(output, marker), view.close)
            else:
                _cleanup_resources(view.close)


def open_exact_index(
    database: Path,
    directory: Path,
    *,
    max_rows: int = MAX_EXACT_INDEX_ROWS,
    max_total_bytes: int = MAX_EXACT_INDEX_BYTES,
    cancellation_check: Callable[[], None] | None = None,
) -> ExactIndexHandle:
    """Verify a stored artifact against all native source rows once.

    This explicit cold operation can read O(N) source data and norm values;
    callers must keep it outside a warm query and reuse/close the handle.
    No source, artifact, model, or application configuration is written.
    """
    _limits(max_rows, max_total_bytes)
    _cold_operation_scope()
    source = _source_path(database)
    selected = _absolute(directory, "directory")
    checks = _Checks(cancellation_check)
    checks.checkpoint()
    view: _format.ValidatedView | None = None
    try:
        header = _format.read_exact_manifest(
            selected, artifact_parent=selected.parent, cancellation_check=checks.checkpoint,
        )
        signature, scope = _header_selection(selected, header, max_rows, max_total_bytes)
        _read_ready(selected, header)
        fence = capture_sqlite_read_fence(source)
        with semantic_database(source, readonly=True) as connection:
            bridge = SQLiteCancellationBridge(checks.checkpoint)
            with sqlite_cancellation_scope(connection, bridge):
                owner = _owner_contract(connection, source, signature, scope, fence, checks)
                view = _format.validate_exact_view(
                    selected, artifact_parent=selected.parent, live_owner_binding=owner.binding,
                    expected_pairs=(owner.pair,), verify_content=True,
                    cancellation_check=checks.checkpoint,
                )
                _format.verify_exact_records(
                    view, _native_rows(connection, owner, max_rows=max_rows, checks=checks),
                    cancellation_check=checks.checkpoint,
                )
        _source_stable(owner)
        checks.checkpoint()
        result = ExactIndexHandle(owner, view, _proof=_HANDLE_PROOF)
        view = None
        return result
    except (_format.DerivedViewError, SemanticStateError, OSError, KeyError, ValueError, sqlite3.Error) as exc:
        checks.preserve_cancellation(exc)
        if isinstance(exc, ExactIndexUnavailable):
            raise
        raise ExactIndexUnavailable("open_verification_failed", str(exc)) from exc
    finally:
        if view is not None:
            _cleanup_resources(view.close)


def _selected_pairs(owner: _Owner, query: ExactSearchQuery) -> tuple[tuple[str, int], ...]:
    model = owner.models.get(query.query_model_signature)
    if model is None:
        raise KeyError(f"unknown active embedding model {query.query_model_signature!r}")
    if model.vector_space != query.vector_space or model.dimensions != query.dimensions:
        raise ValueError("query vector is incompatible with its registered model")
    available = tuple(sorted(
        signature for signature, candidate in owner.models.items()
        if candidate.vector_space == query.vector_space
        and candidate.modality is query.target_modality and candidate.dimensions == query.dimensions
    ))
    selected = query.indexed_model_signatures or available
    missing = set(selected).difference(available)
    if missing:
        raise ValueError(f"indexed models are absent or incompatible: {sorted(missing)}")
    if not available:
        raise ValueError("no compatible indexed models are registered")
    return tuple((signature, owner.heads[signature]) for signature in selected if signature in owner.heads)


def _remaining_rows(view: _format.ValidatedView, after_ref_id: int) -> int:
    # Binary metadata lookup only; no candidate/payload scan or revalidation.
    low, high = 0, view.row_count
    with _format._mapped(view) as maps:
        rows = maps["rows.bin"]
        if rows is None:
            return 0
        while low < high:
            middle = (low + high) // 2
            if int(_format._row(rows, middle)[0]) <= after_ref_id:
                low = middle + 1
            else:
                high = middle
    return view.row_count - low


def _try_exact_index_page(
    path: Path, query: ExactSearchQuery, normalized_query: tuple[float, ...],
    *, exact_index: ExactIndexHandle, limit: int, max_vectors: int,
    after_ref_id: int, batch_size: int, text_scope: TextScope, evidence_mode: bool,
    diagnostic_item_ids: tuple[str, ...], cancellation_check: Callable[[], None] | None,
) -> ExactSearchPage | None:
    """Return None only for a pre-scan fallback; later uncertainty abstains."""
    if not isinstance(exact_index, ExactIndexHandle):
        raise TypeError("exact_index must be a verified ExactIndexHandle")
    handle = exact_index
    with handle._lock:
        if handle._closed or handle._invalid is not None:
            handle._fallback(handle._invalid or "handle_closed")
            return None
        if path.absolute() != handle._owner.path:
            handle._fallback("different_owner")
            return None
        try:
            _source_stable(handle._owner)
            handle._view.assert_files_stable()
        except (ExactIndexUnavailable, _format.DerivedViewError, OSError) as exc:
            invalid_reason = getattr(exc, "reason", "artifact_changed")
            reason_text = invalid_reason if isinstance(invalid_reason, str) else "artifact_changed"
            handle._invalid = reason_text
            handle._fallback(reason_text)
            return None
        pairs = _selected_pairs(handle._owner, query)
        prepared = ((handle._owner.pair.model_signature, handle._owner.pair.generation_id),)
        reason = (
            "unsupported_modality" if query.target_modality is not EmbeddingModality.TEXT else
            "scope_mismatch" if text_scope != handle._owner.scope else
            "published_pairs_mismatch" if pairs != prepared else
            "target_diagnostics" if diagnostic_item_ids else
            "batch_not_numeric" if not 8 <= batch_size <= 512 else None
        )
        if reason is not None:
            handle._fallback(reason)
            return None
        count = min(max_vectors, _remaining_rows(handle._view, after_ref_id))
        if count < 8 or 0 < count % batch_size < 8:
            handle._fallback("scalar_page_or_tail")
            return None
        checks = _Checks(cancellation_check)
        try:
            page = _format.query_exact_view(
                handle._view,
                _format.ExactQuery(query.query_model_signature, query.vector_space, query.dimensions, query.vector, "text", query.indexed_model_signatures),
                live_owner_binding=handle._owner.binding,
                limit=limit, max_vectors=max_vectors, after_ref_id=after_ref_id,
                batch_size=batch_size, text_scope=text_scope, evidence_mode=evidence_mode,
                diagnostic_item_ids=(), diagnostics=None,
                cancellation_check=checks.checkpoint, hydrate_provenance=True,
                numeric=True, numeric_norms=True,
                _normalized_query_vector=normalized_query, _cancellation_already_checked=True,
            )
            _source_stable(handle._owner)
            handle._view.assert_files_stable()
            result = ExactSearchPage(tuple(
                SearchHit(
                    hit.ref_id, hit.entity_id, hit.item_id, hit.indexed_model_signature,
                    hit.vector_space, EmbeddingModality(hit.modality), hit.score,
                    hit.generation_id, dict(hit.provenance), hit.query_model_signature,
                ) for hit in page.hits
            ), page.scanned, page.next_cursor, page.complete)
        except (_format.DerivedViewError, ExactIndexUnavailable, OSError) as exc:
            checks.preserve_cancellation(exc)
            handle._invalid = "query_artifact_changed"
            raise ExactIndexUnavailable("query_artifact_changed", "exact index changed during query; no implicit retry") from exc
        handle._used += 1
        handle._scanned += result.scanned
        return result


class PersistedExactVectorSearch:
    """Protocol adapter over an explicitly prepared, verified exact handle.

    Creation never opens/rebuilds an artifact. By default closing the adapter
    closes its handle; the compatibility dispatcher borrows a caller's handle.
    """

    def __init__(self, handle: ExactIndexHandle, *, owns_handle: bool = True) -> None:
        if not isinstance(handle, ExactIndexHandle):
            raise TypeError("handle must be a verified ExactIndexHandle")
        self.handle = handle
        self._owns_handle = owns_handle
        self._closed = False

    def search_page(
        self, request: VectorSearchRequest, budget: VectorSearchBudget,
        cancelled: Callable[[], None] | None = None,
    ) -> VectorSearchPage | VectorSearchUnavailable:
        if self._closed:
            return VectorSearchUnavailable("persisted_exact", "adapter_closed")
        normalized, _ = normalize_vector(request.query.vector, request.query.dimensions)
        page = _try_exact_index_page(
            request.owner_path, request.query, normalized,
            exact_index=self.handle, limit=budget.limit, max_vectors=budget.max_vectors,
            after_ref_id=request.after_ref_id, batch_size=budget.batch_size,
            text_scope=request.text_scope, evidence_mode=request.evidence_mode,
            diagnostic_item_ids=request.diagnostic_item_ids, cancellation_check=cancelled,
        )
        if page is None:
            reason = self.handle.usage_summary()["last_fallback_reason"]
            return VectorSearchUnavailable("persisted_exact", str(reason))
        return VectorSearchPage(
            page, "persisted_exact", request.snapshot_id,
            "complete" if page.complete else "partial",
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_handle:
                self.handle.close()


__all__ = [
    "MAX_EXACT_INDEX_BYTES", "MAX_EXACT_INDEX_ROWS", "ExactIndexHandle",
    "ExactIndexUnavailable", "PersistedExactVectorSearch", "open_exact_index", "prepare_exact_index",
]
