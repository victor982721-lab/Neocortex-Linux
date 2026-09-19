"""Bounded, explicit exact-vector index format for the semantic owner adapter.

The owner adapter supplies fully verified rows and bindings.  This codec has no
database, model, corpus, network, or product-default access.  Query accepts
only a validated handle and raises ``FallbackExactRequired`` for the adapter to
call the existing exact path with the same request budgets.
"""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import heapq
import importlib
import json
import math
import mmap
import os
import stat
import struct
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from .semantic_search_order import ExactSearchHeapKey, ExactSearchOrder, exact_search_order

if TYPE_CHECKING:
    import numpy as _numpy_types
    from numpy.typing import NDArray

    _StructuredArray: TypeAlias = NDArray[_numpy_types.void]
    _NormArray: TypeAlias = NDArray[_numpy_types.float64]

FORMAT_SCHEMA = "neocortex.semantic.exact-index/v1"
FORMAT_VERSION = 1
ROW_STRUCT = struct.Struct("<QIBBBxIQIQIQI")
IDENTITY_HEADER = struct.Struct("<IIIIII")
NUMERIC_CODE_STRUCT = struct.Struct("<II")
NUMERIC_NORM_STRUCT = struct.Struct("<d")
MAX_DIMENSIONS = 65_536
MAX_PAIR_COUNT = 1_024
MAX_BINDING_BYTES = 4 * 1024 * 1024
MAX_FIELD_BYTES = 4_096
MAX_PROVENANCE_BYTES = 1 * 1024 * 1024
MAX_IDENTITY_BYTES = 64 * 1024
MAX_VECTOR_BYTES = MAX_DIMENSIONS * 4
MAX_JSONL_LINE_BYTES = MAX_PROVENANCE_BYTES + MAX_IDENTITY_BYTES + MAX_VECTOR_BYTES + 64 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_NUMERIC_CODEBOOK_BYTES = 512 * 1024 * 1024
MAX_NUMERIC_NORM_BATCH = 512
MAX_VIEW_ROWS = 500_000
MAX_VIEW_BYTES = 19_000_000_000
MAX_QUERY_VECTORS = 10_000_000
MAX_QUERY_LIMIT = 10_000
MAX_DIAGNOSTIC_ITEMS = 20
MAX_DIAGNOSTIC_RANK_ENTRIES = 100_000
MAX_DIAGNOSTIC_RANK_BYTES = 64 * 1024 * 1024
VECTOR_FILES = {"float16": "vectors-f16.bin", "float32": "vectors-f32.bin"}
DTYPE_CODE = {"float16": 1, "float32": 2}
ENTITY_CODE = {"text_chunk": 1, "image_item": 2}
SCOPE_CODE = {"all": 0, "content": 1, "title": 2}
CODE_DTYPE = {v: k for k, v in DTYPE_CODE.items()}
CODE_SCOPE = {v: k for k, v in SCOPE_CODE.items()}
_NUMPY_RUNTIME_CACHE: dict[tuple[str, str, int, int, int, int, int], dict[str, object]] = {}
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = os.O_NOFOLLOW
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC
_FILE_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
WireRow: TypeAlias = tuple[int, int, int, int, int, int, int, int, int, int, int, int]


class DerivedViewError(RuntimeError):
    pass


class DerivedViewContractError(DerivedViewError):
    pass


class FallbackExactRequired(DerivedViewError):
    """Handoff to the old exact path; this module never builds or falls back."""

    def __init__(self, reason: str, *, phase: str, parameters: Mapping[str, object] | None = None) -> None:
        self.reason = reason
        self.phase = phase
        self.parameters = dict(parameters or {})
        super().__init__(f"derived view requires exact fallback ({phase}): {reason}")


def _is_eintr(error: BaseException) -> bool:
    return isinstance(error, OSError) and error.errno == errno.EINTR


def _close_fd_once(fd: int) -> tuple[bool, BaseException | None]:
    """Retire the numeric FD after one attempt, including Linux close errors.

    Linux releases a valid FD before reporting close errors, not only EINTR.
    Retrying EIO/ENOSPC/EDQUOT could close another thread's reused descriptor.
    EBADF likewise leaves no descriptor that this owner may safely retry.
    """
    try:
        os.close(fd)
    except BaseException as exc:
        return True, exc
    return True, None


def _add_cleanup_notes(primary: BaseException, errors: Sequence[tuple[str, BaseException]]) -> None:
    for label, error in errors:
        suffix = " (ownership cleared; no EINTR retry)" if _is_eintr(error) else ""
        primary.add_note(f"cleanup failed for {label}: {type(error).__name__}: {error}{suffix}")


def _raise_cleanup_errors(errors: Sequence[tuple[str, BaseException]]) -> None:
    if not errors:
        return
    first = errors[0][1]
    _add_cleanup_notes(first, errors)
    raise first


def _close_fd_after_error(fd: int, label: str, primary: BaseException) -> None:
    _relinquished, error = _close_fd_once(fd)
    if error is not None:
        _add_cleanup_notes(primary, [(label, error)])


def _close_resource_once(resource: Any) -> BaseException | None:
    try:
        resource.close()
    except BaseException as exc:
        return exc
    return None


@contextmanager
def _fd_guard(fd: int, label: str) -> Iterator[int]:
    primary: BaseException | None = None
    try:
        yield fd
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _relinquished, error = _close_fd_once(fd)
        if error is not None:
            errors = [(label, error)]
            if primary is not None:
                _add_cleanup_notes(primary, errors)
            else:
                _raise_cleanup_errors(errors)


@dataclass(frozen=True, slots=True)
class PublishedPair:
    model_signature: str
    generation_id: int
    processing_signature: str
    vector_space: str
    modality: Literal["text", "image"]
    entity_kind: Literal["text_chunk", "image_item"]
    status: Literal["ready"] = "ready"

    def __post_init__(self) -> None:
        for name in ("model_signature", "processing_signature", "vector_space", "modality", "entity_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > MAX_FIELD_BYTES:
                raise DerivedViewContractError(f"published pair {name} is blank")
        if isinstance(self.generation_id, bool) or not isinstance(self.generation_id, int) or self.generation_id < 1:
            raise DerivedViewContractError("generation_id must be positive")
        if self.status != "ready" or (self.modality, self.entity_kind) not in (("text", "text_chunk"), ("image", "image_item")):
            raise DerivedViewContractError("published pair is not ready or is incompatible")

    @property
    def key(self) -> str:
        return _canonical_json([self.model_signature, self.generation_id])

    def as_payload(self) -> dict[str, object]:
        return {
            "model_signature": self.model_signature,
            "generation_id": self.generation_id,
            "processing_signature": self.processing_signature,
            "vector_space": self.vector_space,
            "modality": self.modality,
            "entity_kind": self.entity_kind,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ExactQuery:
    query_model_signature: str
    vector_space: str
    dimensions: int
    vector: Sequence[float]
    target_modality: Literal["text", "image"]
    indexed_model_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.dimensions, bool) or not isinstance(self.dimensions, int) or not 1 <= self.dimensions <= MAX_DIMENSIONS:
            raise ValueError("dimensions must be between 1 and 65536")


@dataclass(frozen=True, slots=True)
class VectorRecord:
    ref_id: int
    entity_id: str
    item_id: str
    model_signature: str
    vector_space: str
    modality: Literal["text", "image"]
    generation_id: int
    processing_signature: str
    entity_kind: Literal["text_chunk", "image_item"]
    vector_dtype: Literal["float16", "float32"]
    dimensions: int
    vector_blob: bytes
    provenance_json: Mapping[str, object] | str | bytes
    section_scope: Literal["all", "content", "title"] = "all"
    owner_row_binding: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.dimensions, bool) or not isinstance(self.dimensions, int) or not 1 <= self.dimensions <= MAX_DIMENSIONS:
            raise DerivedViewContractError("record dimensions must be between 1 and 65536")
        if self.vector_dtype not in DTYPE_CODE:
            raise DerivedViewContractError("record vector_dtype must be float16 or float32")


@dataclass(frozen=True, slots=True)
class DerivedHit:
    ref_id: int
    entity_id: str
    item_id: str
    indexed_model_signature: str
    vector_space: str
    modality: Literal["text", "image"]
    score: float
    generation_id: int
    provenance: Mapping[str, object]
    query_model_signature: str
    handoff: Mapping[str, object]

    def as_payload(self) -> dict[str, object]:
        return {
            "ref_id": self.ref_id,
            "entity_id": self.entity_id,
            "item_id": self.item_id,
            "indexed_model_signature": self.indexed_model_signature,
            "vector_space": self.vector_space,
            "modality": self.modality,
            "score": self.score,
            "score_hex": self.score.hex(),
            "generation_id": self.generation_id,
            "provenance": dict(self.provenance),
            "query_model_signature": self.query_model_signature,
            "handoff": dict(self.handoff),
        }


@dataclass(frozen=True, slots=True)
class DerivedSearchPage:
    hits: tuple[DerivedHit, ...]
    scanned: int
    next_cursor: int | None
    complete: bool

    def as_payload(self) -> dict[str, object]:
        return {"hits": [h.as_payload() for h in self.hits], "scanned": self.scanned, "next_cursor": self.next_cursor, "complete": self.complete}


@dataclass(frozen=True, slots=True)
class _Candidate:
    ref_id: int
    entity_id: str
    item_id: str
    pair_index: int
    vector_dtype: str
    dimensions: int
    vector_offset: int
    vector_length: int
    metadata_offset: int
    metadata_length: int
    owner_row_binding: str
    score: float = 0.0

    def scored(self, score: float) -> "_Candidate":
        return _Candidate(
            self.ref_id, self.entity_id, self.item_id, self.pair_index, self.vector_dtype,
            self.dimensions, self.vector_offset, self.vector_length, self.metadata_offset,
            self.metadata_length, self.owner_row_binding, score,
        )


@dataclass(slots=True)
class _Writer:
    stream: Any
    size: int = 0
    digest: Any = None
    fence: dict[str, int] | None = None
    sealed: bool = False

    def __post_init__(self) -> None:
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> tuple[int, int]:
        if self.sealed:
            raise DerivedViewContractError("sealed writer cannot accept more bytes")
        offset = self.size
        self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)
        return offset, len(data)

    def seal(self) -> None:
        if self.sealed:
            return
        self.stream.flush()
        os.fsync(self.stream.fileno())
        info = os.fstat(self.stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != self.size:
            raise DerivedViewContractError("writer bytes differ from tracked output")
        self.fence = _stat_fence(info)
        self.sealed = True

    def close(self) -> None:
        self.seal()
        self.stream.close()


@dataclass(frozen=True, slots=True)
class ValidatedView:
    """Validated directory handle; callers close it after all explicit uses."""

    root: Path
    manifest: Mapping[str, object]
    live_owner_binding: Mapping[str, object]
    manifest_fence: Mapping[str, object]
    root_fd: int
    parent_fd: int
    root_name: str
    root_fence: Mapping[str, int]

    @property
    def row_count(self) -> int:
        return _manifest_int(self.manifest.get("row_count"), "row_count")

    @property
    def pairs(self) -> tuple[PublishedPair, ...]:
        return _pair_list(self.manifest.get("pairs"))

    def _close_fds(self) -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        for attribute, label in (("root_fd", "validated root fd"), ("parent_fd", "validated parent fd")):
            fd = getattr(self, attribute)
            if fd < 0:
                continue
            object.__setattr__(self, attribute, -1)
            _relinquished, error = _close_fd_once(fd)
            if error is not None:
                errors.append((label, error))
        return errors

    def close(self) -> None:
        _raise_cleanup_errors(self._close_fds())

    def __enter__(self) -> "ValidatedView":
        return self

    def __exit__(self, _exc_type: type[BaseException] | None, _exc_value: BaseException | None, _traceback: TracebackType | None) -> None:
        errors = self._close_fds()
        if errors and _exc_value is not None:
            _add_cleanup_notes(_exc_value, errors)
        elif errors:
            _raise_cleanup_errors(errors)

    def assert_files_stable(self) -> None:
        try:
            directory_info = os.stat(self.root_name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise FallbackExactRequired("index directory identity changed", phase="artifact_fence") from exc
        if not stat.S_ISDIR(directory_info.st_mode) or _stat_fence(directory_info) != self.root_fence:
            raise FallbackExactRequired("index directory identity changed", phase="artifact_fence")
        try:
            manifest_info = os.stat("manifest.json", dir_fd=self.root_fd, follow_symlinks=False)
        except OSError as exc:
            raise FallbackExactRequired("manifest identity changed", phase="manifest_fence") from exc
        if not stat.S_ISREG(manifest_info.st_mode) or _stat_fence(manifest_info) != self.manifest_fence:
            raise FallbackExactRequired("manifest identity changed", phase="manifest_fence")
        files = self.manifest["files"]
        assert isinstance(files, Mapping)
        for name, raw in files.items():
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                raise FallbackExactRequired("artifact file manifest invalid", phase="artifact_fence")
            try:
                info = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
            except OSError as exc:
                raise FallbackExactRequired("artifact file identity changed", phase="artifact_fence") from exc
            if not stat.S_ISREG(info.st_mode) or _stat_fence(info) != raw.get("fence"):
                raise FallbackExactRequired("artifact file identity changed", phase="artifact_fence")

    def revalidate_content(self, *, cancellation_check: Callable[[], None] | None = None) -> None:
        files = self.manifest["files"]
        assert isinstance(files, Mapping)
        for name, raw in files.items():
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                raise FallbackExactRequired("artifact file manifest invalid", phase="artifact_content")
            fd, _info = _open_regular_at(
                self.root_fd,
                name,
                expected_fence=raw.get("fence"),
                expected_bytes=raw.get("bytes") if isinstance(raw.get("bytes"), int) and not isinstance(raw.get("bytes"), bool) else None,
            )
            with _fd_guard(fd, f"content fd {name}"):
                digest = _sha256_fd(fd, cancellation_check=cancellation_check)
                after = os.fstat(fd)
            if digest != raw.get("sha256") or _stat_fence(after) != raw.get("fence"):
                raise FallbackExactRequired("artifact content changed", phase="artifact_content")
        self.assert_files_stable()

    def assert_live_owner_binding(self, live_owner_binding: Mapping[str, object]) -> None:
        if _binding(self.manifest.get("owner_binding")) != _binding(live_owner_binding):
            raise FallbackExactRequired("owner fence/head/schema/model binding changed", phase="owner_binding")


class _TargetDiagnostics:
    def __init__(self, item_ids: tuple[str, ...], evidence_mode: bool) -> None:
        self.item_ids = item_ids
        self.evidence_mode = evidence_mode
        self.best: dict[tuple[str, str], ExactSearchOrder] = {}
        self.targets: dict[str, tuple[_Candidate, PublishedPair]] = {}
        self.exhausted = False
        self.bytes = 0

    def observe(self, candidate: _Candidate, pair: PublishedPair) -> None:
        if candidate.item_id in self.item_ids:
            prior_entry = self.targets.get(candidate.item_id)
            prior = None if prior_entry is None else prior_entry[0]
            prior_pair = None if prior_entry is None else prior_entry[1]
            if prior is None:
                better = True
            else:
                assert prior_pair is not None
                better = self._order(candidate, pair) < self._order(prior, prior_pair)
            if better:
                self.targets[candidate.item_id] = (candidate, pair)
        if self.exhausted:
            return
        key = (candidate.item_id, candidate.entity_id if self.evidence_mode else "")
        entry = self._order(candidate, pair)
        if key not in self.best:
            self.bytes += 256 + 4 * (len(candidate.item_id) + len(candidate.entity_id) + len(pair.model_signature))
            if len(self.best) >= MAX_DIAGNOSTIC_RANK_ENTRIES or self.bytes > MAX_DIAGNOSTIC_RANK_BYTES:
                self.best.clear()
                self.exhausted = True
                return
        if key not in self.best or entry < self.best[key]:
            self.best[key] = entry

    @staticmethod
    def _order(candidate: _Candidate, pair: PublishedPair) -> ExactSearchOrder:
        return exact_search_order(candidate.score, candidate.item_id, candidate.entity_id, pair.model_signature, candidate.ref_id)

    def export(self, page: DerivedSearchPage, target_hits: Sequence[DerivedHit]) -> dict[str, object]:
        by_item = {hit.item_id: hit for hit in target_hits}
        rows: list[dict[str, object]] = []
        for item_id in self.item_ids:
            selected = next((hit for hit in page.hits if hit.item_id == item_id), None)
            target_entry = self.targets.get(item_id)
            candidate = None if target_entry is None else target_entry[0]
            rank = next((n for n, hit in enumerate(page.hits, 1) if hit.item_id == item_id), None)
            observed_rank = None
            if candidate is not None and not self.exhausted:
                assert target_entry is not None
                target_order = self._order(candidate, target_entry[1])
                observed_rank = 1 + sum(entry < target_order for entry in self.best.values())
            hit = selected or by_item.get(item_id)
            row: dict[str, object] = {
                "item_id": item_id,
                "observed_in_published_scope": hit is not None,
                "within_candidate_window": rank is not None,
                "candidate_rank": rank,
                "observed_rank": observed_rank if observed_rank is not None else rank,
                "raw_rank": (observed_rank if observed_rank is not None else rank) if page.complete else None,
                "rank_is_global": page.complete and (observed_rank is not None or rank is not None),
                "rank_granularity": "evidence" if self.evidence_mode else "item",
                "rank_budget_exhausted": self.exhausted,
                "stage": "candidate_selected" if rank is not None else "outside_candidate_window" if hit is not None else "not_in_published_search_scope" if page.complete else "unobserved_in_incomplete_scan",
            }
            if hit is not None:
                row.update({"raw_score": hit.score, "ref_id": hit.ref_id, "entity_id": hit.entity_id, "generation_id": hit.generation_id, "model_signature": hit.indexed_model_signature})
            rows.append(row)
        return {"target_diagnostics": rows, "target_hits": [hit.as_payload() for hit in target_hits]}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DerivedViewContractError(f"{label} must be an object")
    try:
        result = json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise DerivedViewContractError(f"{label} is not canonical JSON") from exc
    if not isinstance(result, dict):
        raise DerivedViewContractError(f"{label} must remain an object")
    return result


def _binding(value: object) -> dict[str, object]:
    result = _object(value, "owner binding")
    if len(_canonical_json(result).encode("utf-8")) > MAX_BINDING_BYTES:
        raise DerivedViewContractError("owner binding exceeds bounded size")
    for key in ("owner", "owner_fence", "schema_version", "published_heads"):
        if key not in result:
            raise DerivedViewContractError(f"owner binding lacks {key}")
    if not isinstance(result["owner_fence"], Mapping) or not result["owner_fence"]:
        raise DerivedViewContractError("owner_fence must be non-empty")
    if isinstance(result["schema_version"], bool) or not isinstance(result["schema_version"], int):
        raise DerivedViewContractError("schema_version must be integer")
    if not isinstance(result["published_heads"], list):
        raise DerivedViewContractError("published_heads must be a list")
    return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DerivedViewContractError(f"{name} must be bounded non-blank text")
    if not value.strip() or len(value.encode("utf-8")) > MAX_FIELD_BYTES:
        raise DerivedViewContractError(f"{name} must be bounded non-blank text")
    return value


def _bounded_text(name: str, value: object, *, allow_blank: bool = False, maximum: int = MAX_FIELD_BYTES) -> str:
    if not isinstance(value, str) or (not allow_blank and not value.strip()) or len(value.encode("utf-8")) > maximum:
        raise DerivedViewContractError(f"{name} must be bounded text")
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DerivedViewContractError(f"{name} must be a positive integer")
    return value


def _literal_text(name: str, value: object, allowed: tuple[str, ...]) -> str:
    text = _text(name, value)
    if text not in allowed:
        raise DerivedViewContractError(f"{name} is outside its closed set")
    return text


def _pair_list(value: object) -> tuple[PublishedPair, ...]:
    if not isinstance(value, list):
        raise DerivedViewContractError("manifest pairs must be a list")
    return tuple(_pair(item) for item in value)


def _manifest_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivedViewContractError(f"manifest {name} must be an integer")
    return value


def _modality(value: object) -> Literal["text", "image"]:
    return cast(Literal["text", "image"], _literal_text("modality", value, ("text", "image")))


def _entity_kind(value: object) -> Literal["text_chunk", "image_item"]:
    return cast(Literal["text_chunk", "image_item"], _literal_text("entity_kind", value, ("text_chunk", "image_item")))


def _vector_dtype(value: object) -> Literal["float16", "float32"]:
    return cast(Literal["float16", "float32"], _literal_text("vector_dtype", value, ("float16", "float32")))


def _section_scope(value: object) -> Literal["all", "content", "title"]:
    return cast(Literal["all", "content", "title"], _literal_text("section_scope", value, ("all", "content", "title")))


def _runtime_binding_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise FallbackExactRequired("numeric norm runtime binding is absent", phase="numeric_norms")
    required = {
        "numpy_version",
        "numpy_core_module",
        "numpy_core_file",
        "numpy_core_sha256",
        "cpu_features",
        "byteorder",
        "cache_tag",
    }
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise FallbackExactRequired("numeric norm runtime binding key is invalid", phase="numeric_norms")
        result[key] = item
    if set(result) != required:
        raise FallbackExactRequired("numeric norm runtime binding fields differ", phase="numeric_norms")
    for key in ("numpy_version", "numpy_core_module", "numpy_core_file", "cache_tag"):
        value = result[key]
        if not isinstance(value, str) or not value.strip():
            raise FallbackExactRequired("numeric norm runtime binding text is invalid", phase="numeric_norms")
    digest = result["numpy_core_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise FallbackExactRequired("numeric norm runtime binding digest is invalid", phase="numeric_norms")
    features = result["cpu_features"]
    if not isinstance(features, list) or not features or any(not isinstance(feature, str) or not feature for feature in features) or features != sorted(set(features)):
        raise FallbackExactRequired("numeric norm runtime CPU features are invalid", phase="numeric_norms")
    if not isinstance(result["byteorder"], str) or result["byteorder"] not in {"little", "big"}:
        raise FallbackExactRequired("numeric norm runtime byteorder is invalid", phase="numeric_norms")
    return result


def _cancellation_point(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _no_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise DerivedViewContractError(f"symlink ancestor is not allowed: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _artifact_parent(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError("artifact_parent must be an absolute Path")
    parent = Path(os.path.abspath(value))
    _no_symlink_ancestors(parent)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("artifact_parent must be an existing real directory")
    return parent


def _artifact_path(artifact_parent: Path, path: Path, *, allow_missing_leaf: bool) -> Path:
    parent = _artifact_parent(artifact_parent)
    if not isinstance(path, Path) or not path.is_absolute():
        raise DerivedViewContractError("destination must be an absolute Path")
    selected = Path(os.path.abspath(path))
    _no_symlink_ancestors(selected)
    if selected.parent != parent:
        raise DerivedViewContractError("destination must be a direct child of artifact_parent")
    if selected.is_symlink() or (selected.exists() and not selected.is_dir()):
        raise DerivedViewContractError("destination is not a real directory path")
    if not allow_missing_leaf and not selected.is_dir():
        raise FallbackExactRequired("view destination is absent", phase="view_open")
    return selected


def _stat_fence(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _same_directory_identity(info: os.stat_result, fence: Mapping[str, int]) -> bool:
    return stat.S_ISDIR(info.st_mode) and info.st_dev == fence.get("device") and info.st_ino == fence.get("inode")


def _same_regular_identity(info: os.stat_result, fence: Mapping[str, int]) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_dev == fence.get("device") and info.st_ino == fence.get("inode")


def _open_directory_path(path: Path) -> tuple[int, dict[str, int]]:
    if not path.is_absolute():
        raise ValueError("directory path must be absolute")
    fd = os.open(os.sep, _DIRECTORY_OPEN_FLAGS)
    owned: set[int] = {fd}
    primary: BaseException | None = None
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=fd)
            owned.add(next_fd)
            previous_fd = fd
            fd = next_fd
            relinquished, error = _close_fd_once(previous_fd)
            if relinquished:
                owned.discard(previous_fd)
            if error is not None:
                raise error
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("directory anchor is not a directory")
        result = (fd, _stat_fence(info))
        owned.discard(fd)
        return result
    except BaseException as exc:
        primary = exc
        raise
    finally:
        errors: list[tuple[str, BaseException]] = []
        for owned_fd in tuple(owned):
            relinquished, error = _close_fd_once(owned_fd)
            if relinquished:
                owned.discard(owned_fd)
            if error is not None:
                errors.append((f"directory fd {owned_fd}", error))
        if primary is not None:
            _add_cleanup_notes(primary, errors)
        elif errors:
            _raise_cleanup_errors(errors)


def _open_directory_child(parent_fd: int, name: str, *, create: bool = False) -> tuple[int, dict[str, int]]:
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("directory child name is invalid")
    if create:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    try:
        fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise FallbackExactRequired("index directory is unavailable", phase="view_open") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise DerivedViewContractError("directory child is not a real directory")
        return fd, _stat_fence(info)
    except BaseException as exc:
        _close_fd_after_error(fd, "directory child fd", exc)
        raise


def _open_regular_at(
    directory_fd: int,
    name: str,
    *,
    expected_fence: object | None = None,
    expected_bytes: int | None = None,
) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise FallbackExactRequired("artifact file is unavailable", phase="artifact_identity") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FallbackExactRequired("artifact is not regular", phase="artifact_identity")
        fence = _stat_fence(info)
        if expected_fence is not None and fence != expected_fence:
            raise FallbackExactRequired("artifact file identity changed", phase="artifact_identity")
        if expected_bytes is not None and info.st_size != expected_bytes:
            raise FallbackExactRequired("artifact file size changed", phase="artifact_identity")
        return fd, info
    except BaseException as exc:
        _close_fd_after_error(fd, "artifact read fd", exc)
        raise


def _read_text_at(directory_fd: int, name: str, *, max_bytes: int, cancellation_check: Callable[[], None] | None = None) -> tuple[str, dict[str, int]]:
    fd, before = _open_regular_at(directory_fd, name)
    chunks: list[bytes] = []
    size = 0
    with _fd_guard(fd, "manifest read fd"):
        try:
            while True:
                _cancellation_point(cancellation_check)
                block = os.read(fd, min(1024 * 1024, max_bytes + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > max_bytes:
                    raise FallbackExactRequired("manifest exceeds bounded size", phase="manifest")
            after = os.fstat(fd)
            if _stat_fence(after) != _stat_fence(before):
                raise FallbackExactRequired("manifest identity changed while reading", phase="manifest_fence")
        except OSError as exc:
            raise FallbackExactRequired("manifest unavailable or unreadable", phase="manifest") from exc
    try:
        return b"".join(chunks).decode("utf-8"), _stat_fence(before)
    except UnicodeDecodeError as exc:
        raise FallbackExactRequired("manifest is not UTF-8", phase="manifest") from exc


def _sha256_fd(fd: int, *, cancellation_check: Callable[[], None] | None = None) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while block := os.read(fd, 1024 * 1024):
        _cancellation_point(cancellation_check)
        digest.update(block)
    _cancellation_point(cancellation_check)
    return digest.hexdigest()


def _artifact_descriptor_at(
    directory_fd: int,
    name: str,
    *,
    cancellation_check: Callable[[], None] | None = None,
    expected_identity: Mapping[str, int] | None = None,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    fd, before = _open_regular_at(directory_fd, name)
    with _fd_guard(fd, f"artifact descriptor fd {name}"):
        if expected_identity is not None and not _same_regular_identity(before, expected_identity):
            raise DerivedViewContractError("artifact descriptor is not the writer inode")
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise DerivedViewContractError("artifact descriptor size differs from writer")
        digest = _sha256_fd(fd, cancellation_check=cancellation_check)
        after = os.fstat(fd)
        if _stat_fence(after) != _stat_fence(before):
            raise DerivedViewContractError("artifact changed while hashing")
        if expected_identity is not None and not _same_regular_identity(after, expected_identity):
            raise DerivedViewContractError("artifact descriptor identity changed from writer")
        if expected_bytes is not None and after.st_size != expected_bytes:
            raise DerivedViewContractError("artifact descriptor size changed from writer")
        if expected_sha256 is not None and digest != expected_sha256:
            raise DerivedViewContractError("artifact descriptor digest differs from writer")
        return {"bytes": after.st_size, "sha256": digest, "fence": _stat_fence(after)}


def _open_write_stream_at(directory_fd: int, name: str) -> Any:
    fd = os.open(name, _FILE_WRITE_FLAGS, mode=0o600, dir_fd=directory_fd)
    try:
        return os.fdopen(fd, "wb")
    except BaseException as exc:
        _close_fd_after_error(fd, f"write fd {name}", exc)
        raise


def _chmod_readonly_at(directory_fd: int, name: str) -> None:
    fd, _info = _open_regular_at(directory_fd, name)
    with _fd_guard(fd, f"chmod fd {name}"):
        os.fchmod(fd, stat.S_IRUSR)


def _writer_fence(writer: _Writer) -> Mapping[str, int]:
    fence = writer.fence
    if fence is None:
        raise DerivedViewContractError("writer identity is not sealed")
    if writer.stream.closed:
        return fence
    try:
        info = os.fstat(writer.stream.fileno())
    except OSError as exc:
        raise DerivedViewContractError("writer fd is unavailable") from exc
    if not _same_regular_identity(info, fence) or info.st_size != writer.size:
        raise DerivedViewContractError("writer fd identity or bytes changed")
    return fence


def _unlink_owned_writer_alias(directory_fd: int, name: str, writer: _Writer) -> bool:
    """Remove an alias only while its regular inode and bytes still belong to writer."""
    fence = _writer_fence(writer)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not _same_regular_identity(before, fence) or before.st_size != writer.size:
        return False
    try:
        descriptor = _artifact_descriptor_at(
            directory_fd,
            name,
            expected_identity=fence,
            expected_bytes=writer.size,
            expected_sha256=writer.digest.hexdigest(),
        )
    except BaseException:
        return False
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    descriptor_fence = descriptor.get("fence")
    if (
        not _same_regular_identity(current, fence)
        or current.st_size != writer.size
        or descriptor_fence != _stat_fence(current)
    ):
        return False
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    return True


def _publish_writer_at(
    directory_fd: int,
    temporary_name: str,
    published_name: str,
    writer: _Writer,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Publish one writer with a no-replace hard link and writer-bound checks."""
    fence = _writer_fence(writer)
    expected_bytes = writer.size
    expected_sha256 = writer.digest.hexdigest()
    try:
        source = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DerivedViewContractError("writer temporary alias is unavailable") from exc
    if not _same_regular_identity(source, fence) or source.st_size != expected_bytes:
        raise DerivedViewContractError("writer temporary alias identity changed")
    _cancellation_point(cancellation_check)
    try:
        os.link(
            temporary_name,
            published_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise DerivedViewContractError(f"refusing to overwrite derived index entry: {published_name}") from exc
    except OSError as exc:
        raise DerivedViewContractError(f"failed to publish derived index entry: {published_name}") from exc
    published = os.stat(published_name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_regular_identity(published, fence) or published.st_size != expected_bytes:
        raise DerivedViewContractError("published alias is not the writer inode")
    try:
        os.fchmod(writer.stream.fileno(), stat.S_IRUSR)
    except OSError as exc:
        raise DerivedViewContractError("failed to protect published writer inode") from exc
    _cancellation_point(cancellation_check)
    try:
        temporary = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise DerivedViewContractError("writer temporary alias disappeared during publication") from exc
    except OSError as exc:
        raise DerivedViewContractError("writer temporary alias became unavailable") from exc
    if not _same_regular_identity(temporary, fence) or temporary.st_size != expected_bytes:
        raise DerivedViewContractError("writer temporary alias was replaced during publication")
    if not _unlink_owned_writer_alias(directory_fd, temporary_name, writer):
        raise DerivedViewContractError("writer temporary alias could not be retired safely")
    # Hash after our alias retirement, which changes ctime. Do not merely
    # refresh a prior fence and thereby hide concurrent in-place corruption.
    descriptor = _artifact_descriptor_at(
        directory_fd,
        published_name,
        cancellation_check=cancellation_check,
        expected_identity=fence,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
    )
    final_info = os.fstat(writer.stream.fileno())
    final_path = os.stat(published_name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_regular_identity(final_info, fence) or _stat_fence(final_path) != _stat_fence(final_info) or descriptor["fence"] != _stat_fence(final_info):
        raise DerivedViewContractError("published alias changed during temporary retirement")
    return descriptor


def _create_output_directory(
    parent: Path,
    destination: Path,
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    parent_fd, _parent_fence = _open_directory_path(parent)
    try:
        name = destination.name
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"refusing to overwrite derived index: {destination}")
        _cancellation_point(cancellation_check)
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        root_fd, root_fence = _open_directory_child(parent_fd, name)
        return parent_fd, root_fd, root_fence
    except BaseException as exc:
        _close_fd_after_error(parent_fd, "output parent fd", exc)
        raise


def _pair(value: object) -> PublishedPair:
    if not isinstance(value, Mapping):
        raise DerivedViewContractError("manifest pair is not an object")
    return PublishedPair(
        _text("model_signature", value.get("model_signature")),
        _positive_int("generation_id", value.get("generation_id")),
        _text("processing_signature", value.get("processing_signature")),
        _text("vector_space", value.get("vector_space")),
        _modality(value.get("modality")),
        _entity_kind(value.get("entity_kind")),
        cast(Literal["ready"], _literal_text("status", value.get("status", "ready"), ("ready",))),
    )


def _pairs(values: Sequence[PublishedPair]) -> dict[str, int]:
    if len(values) > MAX_PAIR_COUNT:
        raise DerivedViewContractError("published pair count exceeds bound")
    result: dict[str, int] = {}
    for index, pair in enumerate(values):
        if pair.key in result:
            raise DerivedViewContractError("duplicate published pair key")
        result[pair.key] = index
    return result


def _preflight_manifest_size(
    binding: Mapping[str, object],
    pairs: Sequence[PublishedPair],
    *,
    max_rows: int,
    max_total_bytes: int,
    numeric_projection: bool = False,
    numeric_norms: bool = False,
    numeric_runtime: Mapping[str, object] | None = None,
) -> None:
    placeholder_fence = {
        "device": 2**63 - 1,
        "inode": 2**63 - 1,
        "mode": 2**32 - 1,
        "size": max_total_bytes,
        "mtime_ns": 2**63 - 1,
        "ctime_ns": 2**63 - 1,
    }
    file_names = ["rows.bin", "identity.bin", "metadata.bin", "vectors-f16.bin", "vectors-f32.bin"]
    if numeric_projection:
        file_names.append("numeric-codes.bin")
    if numeric_norms:
        file_names.append("numeric-norms-f64.bin")
    placeholder_files = {
        name: {"bytes": max_total_bytes, "sha256": "0" * 64, "fence": placeholder_fence}
        for name in file_names
    }
    skeleton = {
        "schema": FORMAT_SCHEMA,
        "format_version": FORMAT_VERSION,
        "owner_binding": dict(binding),
        "pairs": [pair.as_payload() for pair in pairs],
        "row_count": max_rows,
        "last_ref_id": 2**63 - 1,
        "row_struct_bytes": ROW_STRUCT.size,
        "identity_header_bytes": IDENTITY_HEADER.size,
        "order": "ref_id_asc",
        "files": placeholder_files,
        "provenance": {"mode": "sealed_source_view_binding", "validated_rows": max_rows, "encoding": "canonical_utf8_json_object"},
        "scope_counts": dict.fromkeys(SCOPE_CODE, max_rows),
        "dtype_counts": dict.fromkeys(DTYPE_CODE, max_rows),
        "bounds": {"max_rows": max_rows, "max_total_bytes": max_total_bytes, "streamed_records": True},
    }
    if numeric_projection:
        skeleton["numeric_projection"] = {
            "enabled": True,
            "file": "numeric-codes.bin",
            "row_struct_bytes": NUMERIC_CODE_STRUCT.size,
            "item_code_count": max_rows,
            "evidence_group_code_count": max_rows,
            "codebook_estimated_bytes": MAX_NUMERIC_CODEBOOK_BYTES,
            "codebook_limit_bytes": MAX_NUMERIC_CODEBOOK_BYTES,
        }
    if numeric_norms:
        skeleton["numeric_norms"] = {
            "enabled": True,
            "file": "numeric-norms-f64.bin",
            "algorithm": "L2float64axis1",
            "cardinality": max_rows,
            "scalar_dtype": "float64",
            "bytes_per_row": NUMERIC_NORM_STRUCT.size,
            "row_index_aligned": True,
            "runtime_binding": dict(numeric_runtime or {}),
        }
    if len(_canonical_json(skeleton).encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise DerivedViewContractError("manifest preflight exceeds bounded size")


def _provenance(value: Mapping[str, object] | str | bytes) -> bytes:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DerivedViewContractError("provenance is not UTF-8") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DerivedViewContractError("provenance JSON is invalid") from exc
    encoded = _canonical_json(_object(value, "provenance")).encode("utf-8")
    if len(encoded) > MAX_PROVENANCE_BYTES:
        raise DerivedViewContractError("provenance exceeds bounded size")
    return encoded


def _numpy_core_sha256(path: Path, *, cancellation_check: Callable[[], None] | None = None) -> str:
    if cancellation_check is not None:
        cancellation_check()
    try:
        fd = os.open(path, _FILE_READ_FLAGS)
    except OSError as exc:
        raise DerivedViewContractError("NumPy core binary is unavailable") from exc
    with _fd_guard(fd, "NumPy core fd"):
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DerivedViewContractError("NumPy core binary is not regular")
        if os.read(fd, 4) != b"\x7fELF":
            raise DerivedViewContractError("NumPy core binary is not ELF")
        digest = _sha256_fd(fd, cancellation_check=cancellation_check)
        after = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as exc:
            raise DerivedViewContractError("NumPy core binary changed while hashing") from exc
        if _stat_fence(after) != _stat_fence(before) or _stat_fence(current) != _stat_fence(before):
            raise DerivedViewContractError("NumPy core binary changed while hashing")
        return digest


def _numpy_native_runtime(numpy: Any) -> tuple[str, Path, list[str]]:
    """Validate the one private NumPy 2 extension used by persisted norms.

    ``_core`` is deliberately private, not a stable NumPy API. Compatibility
    is established per loaded build by its ELF digest and effective CPU flags;
    unfamiliar layouts fail closed instead of probing alternate namespaces.
    The v1 persisted module label remains the historical alias. On NumPy 2
    that alias resolves to this *same* extension (covered by the binding
    equivalence test), so changing the import does not migrate the format.
    """
    version = getattr(numpy, "__version__", None)
    try:
        major, minor = (int(value) for value in str(version).split(".")[:2])
    except ValueError as exc:
        raise DerivedViewContractError("NumPy 2 runtime version is invalid") from exc
    if not isinstance(version, str) or major != 2 or minor < 1:
        raise DerivedViewContractError("NumPy 2.1 or newer within major 2 is required")
    try:
        native = importlib.import_module("numpy._core._multiarray_umath")
    except (ImportError, AttributeError) as exc:
        raise DerivedViewContractError("NumPy core runtime binding is unavailable") from exc
    if getattr(native, "__name__", None) != "numpy._core._multiarray_umath":
        raise DerivedViewContractError("NumPy native module identity is unexpected")
    core_file_value = getattr(native, "__file__", None)
    if not isinstance(core_file_value, str) or not core_file_value:
        raise DerivedViewContractError("NumPy runtime binding is incomplete")
    core_file = Path(os.path.abspath(core_file_value))
    feature_map = getattr(native, "__cpu_features__", None)
    if not isinstance(feature_map, Mapping) or any(
        not isinstance(name, str) or not name or not isinstance(enabled, bool)
        for name, enabled in feature_map.items()
    ):
        raise DerivedViewContractError("NumPy effective CPU features are unavailable")
    cpu_features = sorted(name for name, enabled in feature_map.items() if enabled)
    if not cpu_features:
        raise DerivedViewContractError("NumPy effective CPU features are empty")
    return version, core_file, cpu_features


def _numpy_runtime_binding(numpy: Any, *, cancellation_check: Callable[[], None] | None = None) -> dict[str, object]:
    if cancellation_check is not None:
        cancellation_check()
    version, core_file, cpu_features = _numpy_native_runtime(numpy)
    try:
        info = core_file.lstat()
    except OSError as exc:
        raise DerivedViewContractError("NumPy core binary is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise DerivedViewContractError("NumPy core binary is not regular")
    byteorder = sys.byteorder
    cache_tag = getattr(sys.implementation, "cache_tag", None)
    if byteorder not in {"little", "big"} or not isinstance(cache_tag, str) or not cache_tag:
        raise DerivedViewContractError("Python runtime binding is incomplete")
    cache_key = (version, str(core_file), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    cached = _NUMPY_RUNTIME_CACHE.get(cache_key)
    if cached is not None:
        cached_features = cached.get("cpu_features")
        if not isinstance(cached_features, list):
            raise DerivedViewContractError("cached NumPy CPU feature binding is invalid")
        if _stat_fence(core_file.lstat()) != _stat_fence(info):
            raise DerivedViewContractError("NumPy core binary changed during cache lookup")
        # The binary hash may be reused; effective process settings must be
        # observed on every call, including a cache hit.
        return {**cached, "cpu_features": cpu_features, "byteorder": byteorder, "cache_tag": cache_tag}
    binding: dict[str, object] = {
        "numpy_version": version,
        "numpy_core_module": "numpy.core._multiarray_umath",
        "numpy_core_file": core_file.name,
        "numpy_core_sha256": _numpy_core_sha256(core_file, cancellation_check=cancellation_check),
        "cpu_features": cpu_features,
        "byteorder": byteorder,
        "cache_tag": cache_tag,
    }
    if _stat_fence(core_file.lstat()) != _stat_fence(info):
        raise DerivedViewContractError("NumPy core binary changed during runtime binding")
    _NUMPY_RUNTIME_CACHE[cache_key] = binding
    return {**binding, "cpu_features": list(cpu_features)}


def _decode_blob(value: object) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        result = bytes(value)
        if len(result) > MAX_VECTOR_BYTES:
            raise DerivedViewContractError("vector_blob exceeds bounded size")
        return result
    if isinstance(value, str):
        encoded = value[7:] if value.startswith("base64:") else value
        if len(encoded) > ((MAX_VECTOR_BYTES + 2) // 3) * 4:
            raise DerivedViewContractError("base64 vector_blob exceeds bounded size")
        try:
            result = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise DerivedViewContractError("vector_blob is not valid base64") from exc
        if len(result) > MAX_VECTOR_BYTES:
            raise DerivedViewContractError("decoded vector_blob exceeds bounded size")
        return result
    raise DerivedViewContractError("vector_blob must be bytes or base64 text")


def iter_jsonl_records(path: Path) -> Iterator[Mapping[str, object]]:
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
                raise DerivedViewContractError(f"JSONL line {line_number} exceeds bounded size")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DerivedViewContractError(f"invalid JSONL line {line_number}") from exc
            if not isinstance(value, Mapping):
                raise DerivedViewContractError(f"JSONL line {line_number} is not an object")
            yield value


def vector_record_from_mapping(value: Mapping[str, object]) -> VectorRecord:
    provenance = value.get("provenance_json", value.get("provenance", {}))
    if not isinstance(provenance, (Mapping, str, bytes)):
        raise DerivedViewContractError("provenance must be an object, JSON text or bytes")
    return VectorRecord(
        _positive_int("ref_id", value.get("ref_id")), _text("entity_id", value.get("entity_id")), _text("item_id", value.get("item_id")),
        _text("model_signature", value.get("model_signature")), _text("vector_space", value.get("vector_space")),
        _modality(value.get("modality")), _positive_int("generation_id", value.get("generation_id")), _text("processing_signature", value.get("processing_signature")),
        _entity_kind(value.get("entity_kind")), _vector_dtype(value.get("vector_dtype")), _positive_int("dimensions", value.get("dimensions")), _decode_blob(value.get("vector_blob")),
        provenance, _section_scope(value.get("section_scope", "all")), _bounded_text("owner_row_binding", value.get("owner_row_binding", ""), allow_blank=True, maximum=4096),
    )


def _validate_payload(payload: bytes, dimensions: int, vector_dtype: str, *, finite: bool = True) -> None:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise DerivedViewContractError("dimensions out of bounds")
    width = 2 if vector_dtype == "float16" else 4
    expected = dimensions * width
    if len(payload) != expected:
        raise DerivedViewContractError(f"invalid vector payload length: expected {expected}, got {len(payload)}")
    if not finite:
        return
    fmt = "e" if vector_dtype == "float16" else "f"
    values = struct.unpack(f"<{dimensions}{fmt}", payload)
    if any(not math.isfinite(float(value)) for value in values) or math.fsum(float(value) * float(value) for value in values) <= 0.0:
        raise DerivedViewContractError("vector must be finite and non-zero")


def _validate_record(record: VectorRecord, pair: PublishedPair) -> bytes:
    if isinstance(record.ref_id, bool) or not isinstance(record.ref_id, int) or record.ref_id < 1:
        raise DerivedViewContractError("ref_id must be positive")
    _text("entity_id", record.entity_id)
    _text("item_id", record.item_id)
    if record.modality not in ("text", "image") or record.entity_kind not in ENTITY_CODE:
        raise DerivedViewContractError("record modality/entity_kind invalid")
    if record.vector_dtype not in DTYPE_CODE:
        raise DerivedViewContractError("vector_dtype must be float16 or float32")
    if isinstance(record.generation_id, bool) or not isinstance(record.generation_id, int) or record.generation_id < 1:
        raise DerivedViewContractError("generation_id must be positive")
    if isinstance(record.dimensions, bool) or not isinstance(record.dimensions, int) or not 1 <= record.dimensions <= MAX_DIMENSIONS:
        raise DerivedViewContractError("dimensions out of bounds")
    if record.section_scope not in SCOPE_CODE or not isinstance(record.owner_row_binding, str) or not record.owner_row_binding.strip() or len(record.owner_row_binding) > 4096:
        raise DerivedViewContractError("scope or owner_row_binding invalid")
    if not (record.model_signature == pair.model_signature and record.generation_id == pair.generation_id and record.processing_signature == pair.processing_signature and record.vector_space == pair.vector_space and record.modality == pair.modality and record.entity_kind == pair.entity_kind):
        raise DerivedViewContractError("record does not match published pair")
    _validate_payload(record.vector_blob, record.dimensions, record.vector_dtype)
    return _provenance(record.provenance_json)


def _encode_identity(record: VectorRecord) -> bytes:
    values = tuple(text.encode("utf-8") for text in (record.entity_id, record.item_id, record.model_signature, record.vector_space, record.modality, record.owner_row_binding))
    encoded = IDENTITY_HEADER.pack(*(len(value) for value in values)) + b"".join(values)
    if len(encoded) > MAX_IDENTITY_BYTES:
        raise DerivedViewContractError("identity record exceeds bounded size")
    return encoded


def _decode_identity(blob: mmap.mmap | bytes, offset: int, length: int) -> tuple[str, str, str, str, str, str]:
    if offset < 0 or length < IDENTITY_HEADER.size or offset + length > len(blob):
        raise FallbackExactRequired("identity offset invalid", phase="row_identity")
    lengths = IDENTITY_HEADER.unpack(bytes(blob[offset : offset + IDENTITY_HEADER.size]))
    cursor, end = offset + IDENTITY_HEADER.size, offset + length
    result: list[str] = []
    for size in lengths:
        if cursor + size > end:
            raise FallbackExactRequired("identity field exceeds artifact", phase="row_identity")
        try:
            result.append(bytes(blob[cursor : cursor + size]).decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise FallbackExactRequired("identity is not UTF-8", phase="row_identity") from exc
        cursor += size
    if cursor != end:
        raise FallbackExactRequired("identity record trailing bytes", phase="row_identity")
    return cast(tuple[str, str, str, str, str, str], tuple(result))


def _numeric_codes(
    record: VectorRecord,
    item_codes: dict[str, int],
    evidence_codes: dict[tuple[str, str], int],
    estimated_bytes: int,
) -> tuple[int, int, int]:
    item_key = record.item_id
    evidence_key = (record.item_id, record.entity_id)
    additions = 0
    if item_key not in item_codes:
        additions += 64 + len(item_key.encode("utf-8"))
    if evidence_key not in evidence_codes:
        additions += 96 + len(item_key.encode("utf-8")) + len(record.entity_id.encode("utf-8"))
    if estimated_bytes + additions > MAX_NUMERIC_CODEBOOK_BYTES:
        raise DerivedViewContractError("numeric codebook estimate exceeds 512 MiB before row write")
    item_code = item_codes.setdefault(item_key, len(item_codes))
    evidence_code = evidence_codes.setdefault(evidence_key, len(evidence_codes))
    return item_code, evidence_code, estimated_bytes + additions


def prepare_exact_view(
    records: Iterable[VectorRecord | Mapping[str, object]],
    *,
    artifact_parent: Path,
    owner_binding: Mapping[str, object],
    pairs: Sequence[PublishedPair],
    destination: Path,
    max_rows: int = MAX_VIEW_ROWS,
    max_total_bytes: int = MAX_VIEW_BYTES,
    numeric_projection: bool = False,
    numeric_norms: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Stream a new sealed index from rows already ordered by owner ref_id."""
    if not isinstance(numeric_projection, bool) or not isinstance(numeric_norms, bool):
        raise ValueError("numeric_projection and numeric_norms must be boolean")
    if numeric_norms and not numeric_projection:
        raise ValueError("numeric_norms requires numeric_projection=True")
    if not 1 <= max_rows <= MAX_VIEW_ROWS or not 1 <= max_total_bytes <= MAX_VIEW_BYTES:
        raise ValueError("index bounds are outside the exact contract")
    binding, pair_tuple = _binding(owner_binding), tuple(pairs)
    if not pair_tuple:
        raise DerivedViewContractError("published pair selection is empty")
    if numeric_norms and len(pair_tuple) != 1:
        raise ValueError("numeric_norms requires exactly one published pair")
    numeric: Any | None = None
    numeric_runtime: dict[str, object] | None = None
    if numeric_norms:
        try:
            import numpy
        except ImportError as exc:
            raise FallbackExactRequired("NumPy is required to prepare numeric norms", phase="numeric_norm_prepare") from exc
        numeric = numpy
        numeric_runtime = _numpy_runtime_binding(numeric, cancellation_check=cancellation_check)
    pair_index = _pairs(pair_tuple)
    parent = _artifact_parent(artifact_parent)
    _cancellation_point(cancellation_check)
    _preflight_manifest_size(
        binding,
        pair_tuple,
        max_rows=max_rows,
        max_total_bytes=max_total_bytes,
        numeric_projection=numeric_projection,
        numeric_norms=numeric_norms,
        numeric_runtime=numeric_runtime,
    )
    root = _artifact_path(parent, destination, allow_missing_leaf=True)
    parent_fd, root_fd, root_fence = _create_output_directory(parent, root, cancellation_check=cancellation_check)
    committed = False
    names: tuple[str, ...] = ("rows.bin", "identity.bin", "metadata.bin", "vectors-f16.bin", "vectors-f32.bin")
    if numeric_projection:
        names += ("numeric-codes.bin",)
    if numeric_norms:
        names += ("numeric-norms-f64.bin",)
    writers: dict[str, _Writer] = {}
    manifest_writer: _Writer | None = None
    primary: BaseException | None = None
    try:
        for name in names:
            writers[name] = _Writer(_open_write_stream_at(root_fd, f".{name}.tmp"))
        previous, count, total, provenance_count = 0, 0, 0, 0
        scopes, dtypes = dict.fromkeys(SCOPE_CODE, 0), dict.fromkeys(DTYPE_CODE, 0)
        item_codes: dict[str, int] = {}
        evidence_codes: dict[tuple[str, str], int] = {}
        codebook_estimated_bytes = 0
        norm_batch: list[bytes] = []
        norm_vector_dtype: str | None = None
        norm_dimensions: int | None = None

        def flush_norm_batch() -> None:
            if not norm_batch:
                return
            assert numeric is not None and norm_vector_dtype is not None and norm_dimensions is not None
            matrix = _numeric_matrix_from_bytes(
                b"".join(norm_batch),
                dtype=norm_vector_dtype,
                rows=len(norm_batch),
                dimensions=norm_dimensions,
                numpy=numeric,
            )
            if not bool(numeric.all(numeric.isfinite(matrix))) or not bool(numeric.all(numeric.any(matrix, axis=1))):
                raise DerivedViewContractError("numeric norm batch contains non-finite or zero vectors")
            norms = numeric.linalg.norm(matrix, axis=1)
            if not bool(numeric.all(numeric.isfinite(norms))) or bool(numeric.any(norms <= 0)):
                raise DerivedViewContractError("numeric norm batch contains invalid L2 values")
            encoded = numeric.asarray(norms, dtype=numeric.dtype("<f8")).tobytes(order="C")
            if len(encoded) != len(norm_batch) * NUMERIC_NORM_STRUCT.size:
                raise DerivedViewContractError("numeric norm encoding cardinality differs")
            writers["numeric-norms-f64.bin"].write(encoded)
            norm_batch.clear()

        for raw in records:
            _cancellation_point(cancellation_check)
            record = raw if isinstance(raw, VectorRecord) else vector_record_from_mapping(raw)
            if count >= max_rows:
                raise DerivedViewContractError("max_rows exceeded")
            if record.ref_id <= previous:
                raise DerivedViewContractError("records must be strictly ordered by ref_id")
            index = pair_index.get(_canonical_json([record.model_signature, record.generation_id]))
            if index is None:
                raise DerivedViewContractError("record is outside published pair selection")
            provenance = _validate_record(record, pair_tuple[index])
            if numeric_norms:
                if norm_vector_dtype is None:
                    norm_vector_dtype, norm_dimensions = record.vector_dtype, record.dimensions
                elif (record.vector_dtype, record.dimensions) != (norm_vector_dtype, norm_dimensions):
                    raise ValueError("numeric_norms requires one vector dtype and one dimension")
            identity = _encode_identity(record)
            record_bytes = ROW_STRUCT.size + len(record.vector_blob) + len(identity) + len(provenance)
            item_code = evidence_code = 0
            if numeric_projection:
                item_code, evidence_code, codebook_estimated_bytes = _numeric_codes(
                    record,
                    item_codes,
                    evidence_codes,
                    codebook_estimated_bytes,
                )
                record_bytes += NUMERIC_CODE_STRUCT.size
            if numeric_norms:
                record_bytes += NUMERIC_NORM_STRUCT.size
            if total + record_bytes > max_total_bytes:
                raise DerivedViewContractError("max_total_bytes preflight exceeded")
            vector_offset, vector_length = writers[VECTOR_FILES[record.vector_dtype]].write(record.vector_blob)
            identity_offset, identity_length = writers["identity.bin"].write(identity)
            metadata_offset, metadata_length = writers["metadata.bin"].write(provenance)
            writers["rows.bin"].write(ROW_STRUCT.pack(record.ref_id, index, ENTITY_CODE[record.entity_kind], DTYPE_CODE[record.vector_dtype], SCOPE_CODE[record.section_scope], record.dimensions, vector_offset, vector_length, identity_offset, identity_length, metadata_offset, metadata_length))
            if numeric_projection:
                writers["numeric-codes.bin"].write(NUMERIC_CODE_STRUCT.pack(item_code, evidence_code))
            if numeric_norms:
                norm_batch.append(record.vector_blob)
                if len(norm_batch) >= MAX_NUMERIC_NORM_BATCH:
                    flush_norm_batch()
            previous, count, provenance_count = record.ref_id, count + 1, provenance_count + 1
            scopes[record.section_scope] += 1
            dtypes[record.vector_dtype] += 1
            total += record_bytes
        if numeric_norms:
            flush_norm_batch()
        _cancellation_point(cancellation_check)
        for writer in writers.values():
            writer.seal()
        files: dict[str, object] = {}
        for name in names:
            files[name] = _publish_writer_at(
                root_fd,
                f".{name}.tmp",
                name,
                writers[name],
                cancellation_check=cancellation_check,
            )
        manifest: dict[str, object] = {
            "schema": FORMAT_SCHEMA, "format_version": FORMAT_VERSION, "owner_binding": binding,
            "pairs": [pair.as_payload() for pair in pair_tuple], "row_count": count, "last_ref_id": previous,
            "row_struct_bytes": ROW_STRUCT.size, "identity_header_bytes": IDENTITY_HEADER.size, "order": "ref_id_asc",
            "files": files, "provenance": {"mode": "sealed_source_view_binding", "validated_rows": provenance_count, "encoding": "canonical_utf8_json_object"},
            "scope_counts": scopes, "dtype_counts": dtypes, "bounds": {"max_rows": max_rows, "max_total_bytes": max_total_bytes, "streamed_records": True},
        }
        if numeric_projection:
            manifest["numeric_projection"] = {
                "enabled": True,
                "file": "numeric-codes.bin",
                "row_struct_bytes": NUMERIC_CODE_STRUCT.size,
                "item_code_count": len(item_codes),
                "evidence_group_code_count": len(evidence_codes),
                "codebook_estimated_bytes": codebook_estimated_bytes,
                "codebook_limit_bytes": MAX_NUMERIC_CODEBOOK_BYTES,
            }
        if numeric_norms:
            manifest["numeric_norms"] = {
                "enabled": True,
                "file": "numeric-norms-f64.bin",
                "algorithm": "L2float64axis1",
                "cardinality": count,
                "scalar_dtype": "float64",
                "bytes_per_row": NUMERIC_NORM_STRUCT.size,
                "row_index_aligned": True,
                "vector_dtype": norm_vector_dtype,
                "dimensions": norm_dimensions,
                "runtime_binding": dict(numeric_runtime or {}),
            }
        manifest_bytes = (_canonical_json(manifest) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise DerivedViewContractError("manifest exceeds bounded size")
        if sum(writer.size for writer in writers.values()) + len(manifest_bytes) > max_total_bytes:
            raise DerivedViewContractError("max_total_bytes includes manifest bytes")
        manifest_writer = _Writer(_open_write_stream_at(root_fd, ".manifest.json.tmp"))
        manifest_writer.write(manifest_bytes)
        manifest_writer.seal()
        _publish_writer_at(
            root_fd,
            ".manifest.json.tmp",
            "manifest.json",
            manifest_writer,
            cancellation_check=cancellation_check,
        )
        committed = True
        return manifest
    except BaseException as exc:
        primary = exc
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        for writer_name, writer in writers.items():
            if not writer.stream.closed:
                if writer.fence is None:
                    try:
                        writer.stream.flush()
                        info = os.fstat(writer.stream.fileno())
                        if stat.S_ISREG(info.st_mode):
                            writer.fence = _stat_fence(info)
                    except BaseException as exc:
                        cleanup_errors.append((f"writer {writer_name} identity", exc))
                error = _close_resource_once(writer.stream)
                if error is not None:
                    cleanup_errors.append((f"writer {writer_name}", error))
        if manifest_writer is not None and not manifest_writer.stream.closed:
            if manifest_writer.fence is None:
                try:
                    manifest_writer.stream.flush()
                    info = os.fstat(manifest_writer.stream.fileno())
                    if stat.S_ISREG(info.st_mode):
                        manifest_writer.fence = _stat_fence(info)
                except BaseException as exc:
                    cleanup_errors.append(("manifest writer identity", exc))
            error = _close_resource_once(manifest_writer.stream)
            if error is not None:
                cleanup_errors.append(("manifest writer", error))
        if primary is not None and not committed:
            try:
                root_is_ours = _same_directory_identity(os.fstat(root_fd), root_fence)
            except OSError as exc:
                root_is_ours = False
                cleanup_errors.append(("failed partial root identity", exc))
            if root_is_ours:
                tracked: list[tuple[str, _Writer]] = list(writers.items())
                if manifest_writer is not None:
                    tracked.append(("manifest.json", manifest_writer))
                for name, writer in tracked:
                    if writer.fence is None:
                        continue
                    for alias in (name, f".{name}.tmp"):
                        try:
                            _unlink_owned_writer_alias(root_fd, alias, writer)
                        except OSError as exc:
                            cleanup_errors.append((f"partial entry {alias}", exc))
                try:
                    empty = not os.listdir(root_fd)
                except OSError as exc:
                    empty = False
                    cleanup_errors.append(("failed partial listing", exc))
                if empty:
                    current: os.stat_result | None
                    try:
                        current = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    except OSError as exc:
                        current = None
                        cleanup_errors.append(("failed partial parent identity", exc))
                    if current is not None and _same_directory_identity(current, root_fence):
                        try:
                            os.rmdir(root.name, dir_fd=parent_fd)
                        except OSError as exc:
                            cleanup_errors.append(("failed partial directory removal", exc))
        _root_relinquished, root_error = _close_fd_once(root_fd)
        if root_error is not None:
            cleanup_errors.append(("output root fd", root_error))
        _parent_relinquished, parent_error = _close_fd_once(parent_fd)
        if parent_error is not None:
            cleanup_errors.append(("output parent fd", parent_error))
        if primary is not None:
            _add_cleanup_notes(primary, cleanup_errors)
        elif cleanup_errors:
            _raise_cleanup_errors(cleanup_errors)
    assert primary is not None, "prepare_exact_view ended without a result or error"
    raise primary


def read_exact_manifest(
    destination: Path,
    *,
    artifact_parent: Path,
    cancellation_check: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Read only the bounded product header; owner and row trust stay outside."""
    parent = _artifact_parent(artifact_parent)
    root = _artifact_path(parent, destination, allow_missing_leaf=False)
    parent_fd, _parent_fence = _open_directory_path(parent)
    root_fd = -1
    primary: BaseException | None = None
    try:
        _cancellation_point(cancellation_check)
        root_fd, _root_fence = _open_directory_child(parent_fd, root.name)
        manifest_text, _manifest_fence = _read_text_at(
            root_fd,
            "manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
            cancellation_check=cancellation_check,
        )
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise FallbackExactRequired("manifest unavailable or invalid", phase="manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != FORMAT_SCHEMA or manifest.get("format_version") != FORMAT_VERSION:
            raise FallbackExactRequired("manifest schema unsupported", phase="manifest")
        _cancellation_point(cancellation_check)
        return manifest
    except BaseException as exc:
        primary = exc
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        if root_fd >= 0:
            _root_relinquished, root_error = _close_fd_once(root_fd)
            if root_error is not None:
                cleanup_errors.append(("manifest root fd", root_error))
        _parent_relinquished, parent_error = _close_fd_once(parent_fd)
        if parent_error is not None:
            cleanup_errors.append(("manifest parent fd", parent_error))
        if primary is not None:
            _add_cleanup_notes(primary, cleanup_errors)
        elif cleanup_errors:
            _raise_cleanup_errors(cleanup_errors)
    assert primary is not None, "read_exact_manifest ended without a result or error"
    raise primary


def validate_exact_view(
    destination: Path,
    *,
    artifact_parent: Path,
    live_owner_binding: Mapping[str, object],
    expected_pairs: Sequence[PublishedPair] | None = None,
    verify_content: bool = True,
    cancellation_check: Callable[[], None] | None = None,
) -> ValidatedView:
    if verify_content is not True:
        raise ValueError("verify_content=False is forbidden for public validation")
    parent = _artifact_parent(artifact_parent)
    root = _artifact_path(parent, destination, allow_missing_leaf=False)
    parent_fd, _parent_fence = _open_directory_path(parent)
    root_fd = -1
    primary: BaseException | None = None
    try:
        root_fd, root_fence = _open_directory_child(parent_fd, root.name)
        manifest_text, manifest_fence = _read_text_at(
            root_fd,
            "manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
            cancellation_check=cancellation_check,
        )
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise FallbackExactRequired("manifest unavailable or invalid", phase="manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != FORMAT_SCHEMA or manifest.get("format_version") != FORMAT_VERSION:
            raise FallbackExactRequired("manifest schema unsupported", phase="manifest")
        live = _binding(live_owner_binding)
        if _binding(manifest.get("owner_binding")) != live:
            raise FallbackExactRequired("owner binding differs", phase="owner_binding")
        pairs = tuple(_pair(value) for value in manifest.get("pairs", ()))
        _pairs(pairs)
        if expected_pairs is not None and tuple(pair.key for pair in expected_pairs) != tuple(pair.key for pair in pairs):
            raise FallbackExactRequired("published pairs differ", phase="owner_binding")
        count = manifest.get("row_count")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_VIEW_ROWS:
            raise FallbackExactRequired("row_count invalid", phase="manifest")
        if manifest.get("row_struct_bytes") != ROW_STRUCT.size or manifest.get("identity_header_bytes") != IDENTITY_HEADER.size:
            raise FallbackExactRequired("row format differs", phase="manifest")
        files = manifest.get("files")
        base_files = {"rows.bin", "identity.bin", "metadata.bin", "vectors-f16.bin", "vectors-f32.bin"}
        allowed_files = (
            base_files,
            base_files | {"numeric-codes.bin"},
            base_files | {"numeric-norms-f64.bin"},
            base_files | {"numeric-codes.bin", "numeric-norms-f64.bin"},
        )
        if not isinstance(files, Mapping) or set(files) not in allowed_files:
            raise FallbackExactRequired("artifact file set differs", phase="manifest")
        has_numeric_file = "numeric-codes.bin" in files
        has_norm_file = "numeric-norms-f64.bin" in files
        numeric_meta = manifest.get("numeric_projection")
        if has_numeric_file:
            if not isinstance(numeric_meta, Mapping) or numeric_meta.get("enabled") is not True or numeric_meta.get("file") != "numeric-codes.bin" or numeric_meta.get("row_struct_bytes") != NUMERIC_CODE_STRUCT.size:
                raise FallbackExactRequired("numeric projection metadata is invalid", phase="manifest")
            for key in ("item_code_count", "evidence_group_code_count", "codebook_estimated_bytes"):
                value = numeric_meta.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise FallbackExactRequired("numeric code counts are invalid", phase="manifest")
            if numeric_meta["item_code_count"] > count or numeric_meta["evidence_group_code_count"] > count or numeric_meta["codebook_estimated_bytes"] > MAX_NUMERIC_CODEBOOK_BYTES:
                raise FallbackExactRequired("numeric code bounds exceeded", phase="manifest")
        elif numeric_meta is not None:
            raise FallbackExactRequired("numeric metadata has no numeric file", phase="manifest")
        norm_meta = manifest.get("numeric_norms")
        if has_norm_file:
            if not has_numeric_file or not isinstance(norm_meta, Mapping):
                raise FallbackExactRequired("numeric norm metadata is incomplete", phase="manifest")
            if (
                norm_meta.get("enabled") is not True
                or norm_meta.get("file") != "numeric-norms-f64.bin"
                or norm_meta.get("algorithm") != "L2float64axis1"
                or norm_meta.get("scalar_dtype") != "float64"
                or norm_meta.get("bytes_per_row") != NUMERIC_NORM_STRUCT.size
                or norm_meta.get("row_index_aligned") is not True
                or isinstance(norm_meta.get("cardinality"), bool)
                or not isinstance(norm_meta.get("cardinality"), int)
                or norm_meta.get("cardinality") != count
            ):
                raise FallbackExactRequired("numeric norm metadata differs", phase="manifest")
            norm_dtype, norm_dimensions = norm_meta.get("vector_dtype"), norm_meta.get("dimensions")
            if (norm_dtype is None) != (norm_dimensions is None):
                raise FallbackExactRequired("numeric norm vector shape is incomplete", phase="manifest")
            if norm_dtype is not None and (not isinstance(norm_dtype, str) or norm_dtype not in DTYPE_CODE):
                raise FallbackExactRequired("numeric norm vector dtype is invalid", phase="manifest")
            if norm_dimensions is not None and (isinstance(norm_dimensions, bool) or not isinstance(norm_dimensions, int) or not 1 <= norm_dimensions <= MAX_DIMENSIONS):
                raise FallbackExactRequired("numeric norm vector dimensions are invalid", phase="manifest")
            _runtime_binding_payload(norm_meta.get("runtime_binding"))
        elif norm_meta is not None:
            raise FallbackExactRequired("numeric norm metadata has no numeric norm file", phase="manifest")
        for name, raw in files.items():
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                raise FallbackExactRequired("artifact file manifest invalid", phase="manifest")
            expected_bytes = raw.get("bytes")
            if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
                raise FallbackExactRequired("artifact file size is invalid", phase="manifest")
            fd, before = _open_regular_at(root_fd, name, expected_fence=raw.get("fence"), expected_bytes=expected_bytes)
            with _fd_guard(fd, f"validation content fd {name}"):
                if verify_content and _sha256_fd(fd, cancellation_check=cancellation_check) != raw.get("sha256"):
                    raise FallbackExactRequired("artifact content digest changed", phase="artifact_content")
                after = os.fstat(fd)
            if _stat_fence(after) != _stat_fence(before):
                raise FallbackExactRequired("artifact file identity changed", phase="artifact_identity")
        fd, info = _open_regular_at(root_fd, "rows.bin")
        with _fd_guard(fd, "validation rows fd"):
            row_bytes = info.st_size
        if row_bytes != count * ROW_STRUCT.size:
            raise FallbackExactRequired("row count/file size differs", phase="manifest")
        if has_numeric_file:
            fd, info = _open_regular_at(root_fd, "numeric-codes.bin")
            with _fd_guard(fd, "validation numeric code fd"):
                code_bytes = info.st_size
            if code_bytes != count * NUMERIC_CODE_STRUCT.size:
                raise FallbackExactRequired("numeric code count/file size differs", phase="manifest")
        if has_norm_file:
            fd, info = _open_regular_at(root_fd, "numeric-norms-f64.bin")
            with _fd_guard(fd, "validation numeric norm fd"):
                norm_bytes = info.st_size
            if norm_bytes != count * NUMERIC_NORM_STRUCT.size:
                raise FallbackExactRequired("numeric norm count/file size differs", phase="manifest")
        provenance = manifest.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("mode") != "sealed_source_view_binding" or provenance.get("validated_rows") != count:
            raise FallbackExactRequired("provenance is not fully sealed", phase="manifest")
        _cancellation_point(cancellation_check)
        view = ValidatedView(root, manifest, live, manifest_fence, root_fd, parent_fd, root.name, root_fence)
        root_fd = parent_fd = -1
        return view
    except BaseException as exc:
        primary = exc
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        if root_fd >= 0:
            _root_relinquished, root_error = _close_fd_once(root_fd)
            if root_error is not None:
                cleanup_errors.append(("validation root fd", root_error))
        if parent_fd >= 0:
            _parent_relinquished, parent_error = _close_fd_once(parent_fd)
            if parent_error is not None:
                cleanup_errors.append(("validation parent fd", parent_error))
        if primary is not None:
            _add_cleanup_notes(primary, cleanup_errors)
        elif cleanup_errors:
            _raise_cleanup_errors(cleanup_errors)
    assert primary is not None, "validate_exact_view ended without a result or error"
    raise primary


@contextmanager
def _mapped(view: ValidatedView, *, include_numeric: bool = False, include_norms: bool = False) -> Iterator[dict[str, mmap.mmap | None]]:
    view.assert_files_stable()
    streams: list[int] = []
    maps: dict[str, mmap.mmap | None] = {}
    primary: BaseException | None = None
    try:
        names = ["rows.bin", "identity.bin", "metadata.bin", "vectors-f16.bin", "vectors-f32.bin"]
        if include_numeric:
            names.append("numeric-codes.bin")
        if include_norms:
            names.append("numeric-norms-f64.bin")
        for name in names:
            files = view.manifest.get("files")
            raw = files.get(name) if isinstance(files, Mapping) else None
            if not isinstance(raw, Mapping):
                raise FallbackExactRequired("artifact file manifest is incomplete", phase="artifact_fence")
            expected_bytes = raw.get("bytes")
            if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
                raise FallbackExactRequired("artifact file size is invalid", phase="artifact_fence")
            stream, info = _open_regular_at(view.root_fd, name, expected_fence=raw.get("fence"), expected_bytes=expected_bytes)
            streams.append(stream)
            if info.st_size == 0:
                maps[name] = None
            else:
                maps[name] = mmap.mmap(stream, 0, access=mmap.ACCESS_READ)
        yield maps
    except BaseException as exc:
        primary = exc
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        for name, mapping in maps.items():
            if mapping is not None:
                error = _close_resource_once(mapping)
                if error is not None:
                    cleanup_errors.append((f"mmap {name}", error))
        for stream in streams:
            _relinquished, error = _close_fd_once(stream)
            if error is not None:
                cleanup_errors.append((f"artifact fd {stream}", error))
        try:
            view.assert_files_stable()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                cleanup_errors.append(("final artifact fence", exc))
        if primary is not None:
            _add_cleanup_notes(primary, cleanup_errors)
        elif cleanup_errors:
            _raise_cleanup_errors(cleanup_errors)
    if primary is not None:
        raise primary


def _row(rows: mmap.mmap, index: int) -> WireRow:
    offset = index * ROW_STRUCT.size
    if offset < 0 or offset + ROW_STRUCT.size > len(rows):
        raise FallbackExactRequired("row outside artifact", phase="row_identity")
    return cast(WireRow, ROW_STRUCT.unpack(bytes(rows[offset : offset + ROW_STRUCT.size])))


def _candidate(row: WireRow, *, identity: mmap.mmap | bytes, metadata_size: int, pairs: Mapping[int, PublishedPair], row_index: int) -> _Candidate:
    ref_id, pair_index, entity_code, dtype_code, _scope, dimensions, vector_offset, vector_length, identity_offset, identity_length, metadata_offset, metadata_length = row
    pair = pairs.get(pair_index)
    dtype = CODE_DTYPE.get(dtype_code)
    if pair is None or entity_code != ENTITY_CODE[pair.entity_kind] or dtype is None or not isinstance(dimensions, int) or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise FallbackExactRequired(f"row {row_index} binding invalid", phase="row_identity")
    if min(vector_offset, vector_length, identity_offset, identity_length, metadata_offset, metadata_length) < 0 or identity_length < IDENTITY_HEADER.size or metadata_length < 2 or metadata_offset + metadata_length > metadata_size:
        raise FallbackExactRequired(f"row {row_index} offset invalid", phase="row_identity")
    entity_id, item_id, model_signature, vector_space, modality, owner_binding = _decode_identity(identity, identity_offset, identity_length)
    if (model_signature, vector_space, modality) != (pair.model_signature, pair.vector_space, pair.modality):
        raise FallbackExactRequired(f"row {row_index} identity/pair mismatch", phase="row_identity")
    return _Candidate(int(ref_id), entity_id, item_id, int(pair_index), dtype, dimensions, int(vector_offset), int(vector_length), int(metadata_offset), int(metadata_length), owner_binding)


def _bounds(*, limit: int, max_vectors: int, after_ref_id: int, batch_size: int, diagnostic_item_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError("limit must be between 1 and 10000")
    if isinstance(max_vectors, bool) or not isinstance(max_vectors, int) or not 1 <= max_vectors <= MAX_QUERY_VECTORS:
        raise ValueError("max_vectors must be between 1 and 10000000")
    if isinstance(after_ref_id, bool) or not isinstance(after_ref_id, int) or after_ref_id < 0:
        raise ValueError("after_ref_id cannot be negative")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_QUERY_LIMIT:
        raise ValueError("batch_size must be between 1 and 10000")
    if not isinstance(diagnostic_item_ids, tuple):
        raise ValueError("diagnostic_item_ids must be a tuple")
    selected = tuple(dict.fromkeys(diagnostic_item_ids))
    if len(selected) > MAX_DIAGNOSTIC_ITEMS or any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in selected):
        raise ValueError("diagnostic_item_ids are not bounded")
    return selected


def _query_vector(query: ExactQuery) -> tuple[float, ...]:
    if not isinstance(query.query_model_signature, str) or not query.query_model_signature.strip() or not isinstance(query.vector_space, str) or not query.vector_space.strip() or query.target_modality not in ("text", "image"):
        raise ValueError("query contract is invalid")
    if isinstance(query.dimensions, bool) or not isinstance(query.dimensions, int) or not 1 <= query.dimensions <= MAX_DIMENSIONS:
        raise ValueError("dimensions must be between 1 and 65536")
    values = tuple(float(value) for value in query.vector)
    if len(values) != query.dimensions or any(not math.isfinite(value) for value in values):
        raise ValueError("query vector is invalid")
    norm_squared = math.fsum(value * value for value in values)
    if not math.isfinite(norm_squared) or norm_squared <= 0:
        raise ValueError("query vector must have finite non-zero norm")
    norm = math.sqrt(norm_squared)
    return tuple(value / norm for value in values)


def _validated_normalized_query_vector(
    query: ExactQuery,
    value: tuple[float, ...] | None,
) -> tuple[float, ...] | None:
    if value is None or not isinstance(value, tuple) or len(value) != query.dimensions:
        return None
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(item) for item in values):
        return None
    norm_squared = math.fsum(item * item for item in values)
    if not math.isfinite(norm_squared) or norm_squared <= 0 or not math.isclose(norm_squared, 1.0, rel_tol=1e-12, abs_tol=1e-12):
        return None
    return values


def _vector_bytes(mapping: mmap.mmap | bytes, candidate: _Candidate) -> bytes:
    if candidate.vector_offset + candidate.vector_length > len(mapping):
        raise FallbackExactRequired("vector outside artifact", phase="row_payload")
    payload = bytes(mapping[candidate.vector_offset : candidate.vector_offset + candidate.vector_length])
    _validate_payload(payload, candidate.dimensions, candidate.vector_dtype, finite=False)
    return payload


def _scalar_score(query_vector: tuple[float, ...], payload: bytes, dimensions: int, vector_dtype: str) -> float:
    _validate_payload(payload, dimensions, vector_dtype)
    values = struct.unpack(f"<{dimensions}{'e' if vector_dtype == 'float16' else 'f'}", payload)
    left_norm = math.sqrt(math.fsum(value * value for value in query_vector))
    right_norm = math.sqrt(math.fsum(value * value for value in values))
    score = math.fsum(left * right for left, right in zip(query_vector, values, strict=True)) / (left_norm * right_norm)
    if not math.isfinite(score):
        raise FallbackExactRequired("cosine score is not finite", phase="row_score")
    return max(-1.0, min(1.0, score))


def _score(candidates: Sequence[_Candidate], *, query_vector: tuple[float, ...], dimensions: int, vector_maps: Mapping[str, mmap.mmap | bytes | None], cancellation_check: Callable[[], None] | None) -> tuple[_Candidate, ...]:
    if not candidates:
        return ()
    numpy: Any | None = None
    try:
        import numpy as _numpy
    except ImportError:
        numpy = None
    else:
        numpy = _numpy
    if numpy is None or len(candidates) < 8:
        result: list[_Candidate] = []
        for index, candidate in enumerate(candidates):
            if cancellation_check is not None and index % 128 == 0:
                cancellation_check()
            mapping = vector_maps[candidate.vector_dtype]
            if candidate.dimensions != dimensions or mapping is None:
                raise FallbackExactRequired("vector dimensions/segment invalid", phase="row_payload")
            result.append(candidate.scored(_scalar_score(query_vector, _vector_bytes(mapping, candidate), dimensions, candidate.vector_dtype)))
        return tuple(result)
    assert numpy is not None
    query_values = numpy.asarray(query_vector, dtype=numpy.float64)
    query_norm = float(numpy.linalg.norm(query_values))
    if not numpy.isfinite(query_norm) or query_norm <= 0:
        raise FallbackExactRequired("query norm is not finite", phase="row_score")
    scores: list[float | None] = [None] * len(candidates)
    positions: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        if cancellation_check is not None and index % 128 == 0:
            cancellation_check()
        mapping = vector_maps[candidate.vector_dtype]
        if candidate.dimensions != dimensions or mapping is None:
            raise FallbackExactRequired("vector dimensions/segment invalid", phase="row_payload")
        _validate_payload(_vector_bytes(mapping, candidate), dimensions, candidate.vector_dtype, finite=False)
        positions.setdefault(candidate.vector_dtype, []).append(index)
    for dtype, indices in positions.items():
        mapping = vector_maps[dtype]
        if mapping is None:
            raise FallbackExactRequired("vector dtype segment is unavailable", phase="row_payload")
        matrix = numpy.empty((len(indices), dimensions), dtype=numpy.float64)
        np_dtype = numpy.dtype("<f2" if dtype == "float16" else "<f4")
        for matrix_row, source_index in enumerate(indices):
            matrix[matrix_row] = numpy.frombuffer(_vector_bytes(mapping, candidates[source_index]), dtype=np_dtype, count=dimensions)
        if not bool(numpy.all(numpy.isfinite(matrix))):
            raise FallbackExactRequired("vector matrix contains non-finite values", phase="row_score")
        norms = numpy.linalg.norm(matrix, axis=1)
        if not bool(numpy.all(numpy.isfinite(norms))) or bool(numpy.any(norms <= 0)):
            raise FallbackExactRequired("vector matrix contains invalid norm", phase="row_score")
        batch_scores = (matrix @ query_values) / (norms * query_norm)
        if not bool(numpy.all(numpy.isfinite(batch_scores))):
            raise FallbackExactRequired("cosine score is not finite", phase="row_score")
        for source_index, score in zip(indices, batch_scores, strict=True):
            scores[source_index] = max(-1.0, min(1.0, float(score)))
    if any(score is None for score in scores):
        raise FallbackExactRequired("scorer omitted a row", phase="row_score")
    return tuple(candidate.scored(score) for candidate, score in zip(candidates, scores, strict=True) if score is not None)


def _retain(candidate: _Candidate, *, pair: PublishedPair, key: str | tuple[str, str], limit: int, best: dict[object, tuple[ExactSearchHeapKey, int, int, object, _Candidate]], heap: list[tuple[ExactSearchHeapKey, int, int, object, _Candidate]], serial: int) -> None:
    entry = (ExactSearchHeapKey(_TargetDiagnostics._order(candidate, pair)), candidate.ref_id, serial, key, candidate)
    prior = best.get(key)
    if prior is not None:
        if entry[0] > prior[0]:
            best[key] = entry
            heapq.heappush(heap, entry)
    elif len(best) < limit:
        best[key] = entry
        heapq.heappush(heap, entry)
    else:
        while heap and best.get(heap[0][3]) != heap[0]:
            heapq.heappop(heap)
        if not heap:
            raise FallbackExactRequired("retention heap empty", phase="retention")
        if entry[0] > heap[0][0]:
            removed = heapq.heappop(heap)
            del best[removed[3]]
            best[key] = entry
            heapq.heappush(heap, entry)
    if len(heap) > max(limit * 2, limit + 64):
        heap[:] = best.values()
        heapq.heapify(heap)


def _provenance_from(mapping: mmap.mmap | bytes, candidate: _Candidate) -> dict[str, object]:
    raw = bytes(mapping[candidate.metadata_offset : candidate.metadata_offset + candidate.metadata_length])
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FallbackExactRequired("winner provenance JSON invalid", phase="winner_provenance") from exc
    return _object(value, "winner provenance")


def _hit(candidate: _Candidate, pair: PublishedPair, query: ExactQuery, metadata: mmap.mmap | bytes, *, hydrate: bool) -> DerivedHit:
    return DerivedHit(candidate.ref_id, candidate.entity_id, candidate.item_id, pair.model_signature, pair.vector_space, pair.modality, candidate.score, pair.generation_id, _provenance_from(metadata, candidate) if hydrate else {}, query.query_model_signature, {"owner": "semantic", "owner_row_binding": candidate.owner_row_binding, "ref_id": candidate.ref_id, "pair_key": pair.key, "generation_id": pair.generation_id, "model_signature": pair.model_signature, "locator_status": "owner_hydration_required"})


def _numeric_row_dtype(numpy: Any) -> Any:
    return numpy.dtype([
        ("ref_id", "<u8"), ("pair_index", "<u4"), ("entity_code", "u1"),
        ("dtype_code", "u1"), ("scope_code", "u1"), ("_pad", "u1"),
        ("dimensions", "<u4"), ("vector_offset", "<u8"), ("vector_length", "<u4"),
        ("identity_offset", "<u8"), ("identity_length", "<u4"),
        ("metadata_offset", "<u8"), ("metadata_length", "<u4"),
    ])


def _numeric_code_dtype(numpy: Any) -> Any:
    return numpy.dtype([("item_code", "<u4"), ("evidence_code", "<u4")])


def _numeric_fallback(reason: str, phase: str) -> FallbackExactRequired:
    return FallbackExactRequired(reason, phase=phase)


def _numeric_matrix_from_bytes(payload: bytes, *, dtype: str, rows: int, dimensions: int, numpy: Any) -> Any:
    np_dtype = numpy.dtype("<f2" if dtype == "float16" else "<f4")
    return numpy.frombuffer(payload, dtype=np_dtype, count=rows * dimensions).reshape(rows, dimensions).astype(numpy.float64)


def _numeric_scores(
    rows: Any,
    *,
    vector_maps: Mapping[str, mmap.mmap | bytes | None],
    query_vector: tuple[float, ...],
    dimensions: int,
    numpy: Any,
    cached_norms: Any | None = None,
) -> Any:
    if len(rows) < 8:
        raise _numeric_fallback("numeric path keeps scalar batches below eight rows", "numeric_scalar")
    dtypes = numpy.unique(rows["dtype_code"])
    if len(dtypes) != 1:
        raise _numeric_fallback("numeric batch has non-contiguous mixed dtypes", "numeric_layout")
    dtype = CODE_DTYPE.get(int(dtypes[0]))
    mapping = None if dtype is None else vector_maps.get(dtype)
    if dtype is None or mapping is None:
        raise _numeric_fallback("numeric dtype segment is unavailable", "numeric_layout")
    expected = dimensions * (2 if dtype == "float16" else 4)
    lengths = rows["vector_length"]
    offsets = rows["vector_offset"]
    if bool(numpy.any(rows["dimensions"] != dimensions)) or bool(numpy.any(lengths != expected)):
        raise _numeric_fallback("numeric row dimension or payload length differs", "numeric_payload")
    if len(offsets) > 1 and not bool(numpy.all(offsets[1:] == offsets[:-1] + expected)):
        raise _numeric_fallback("numeric vector payload is not contiguous", "numeric_layout")
    start = int(offsets[0])
    stop = start + len(rows) * expected
    if start < 0 or stop > len(mapping):
        raise _numeric_fallback("numeric vector range is outside artifact", "numeric_payload")
    matrix = _numeric_matrix_from_bytes(bytes(mapping[start:stop]), dtype=dtype, rows=len(rows), dimensions=dimensions, numpy=numpy)
    if not bool(numpy.all(numpy.isfinite(matrix))):
        raise _numeric_fallback("numeric vector contains non-finite values", "numeric_score")
    if cached_norms is None:
        norms = numpy.linalg.norm(matrix, axis=1)
    else:
        norms = numpy.asarray(cached_norms)
        if norms.dtype != numpy.dtype("<f8") or norms.shape != (len(rows),):
            raise _numeric_fallback("cached numeric norm shape or dtype differs", "numeric_norms")
        if not bool(numpy.all(numpy.any(matrix, axis=1))):
            raise _numeric_fallback("cached numeric norm has a zero scanned vector", "numeric_score")
    query_values = numpy.asarray(query_vector, dtype=numpy.float64)
    query_norm = float(numpy.linalg.norm(query_values))
    if not bool(numpy.all(numpy.isfinite(norms))) or bool(numpy.any(norms <= 0)):
        raise _numeric_fallback("cached numeric norm is not finite or positive" if cached_norms is not None else "numeric vector norm is invalid", "numeric_norms" if cached_norms is not None else "numeric_score")
    if not numpy.isfinite(query_norm) or query_norm <= 0:
        raise _numeric_fallback("numeric query norm is invalid", "numeric_score")
    scores = (matrix @ query_values) / (norms * query_norm)
    if not bool(numpy.all(numpy.isfinite(scores))):
        raise _numeric_fallback("numeric cosine score is not finite", "numeric_score")
    return numpy.maximum(-1.0, numpy.minimum(1.0, scores))


def verify_exact_records(
    view: ValidatedView,
    records: Iterable[VectorRecord],
    *,
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    """Verify every sealed row against the owner's authoritative stream."""
    view.assert_files_stable()
    files = view.manifest.get("files")
    if not isinstance(files, Mapping):
        raise FallbackExactRequired("artifact file manifest is incomplete", phase="manifest")
    has_numeric = "numeric-codes.bin" in files
    has_norms = "numeric-norms-f64.bin" in files
    if has_norms and not has_numeric:
        raise FallbackExactRequired("numeric norm cache lacks numeric projection", phase="numeric_norms")
    pairs = view.pairs
    pair_by_key = {pair.key: index for index, pair in enumerate(pairs)}
    numeric_meta = view.manifest.get("numeric_projection")
    if has_numeric and not isinstance(numeric_meta, Mapping):
        raise FallbackExactRequired("numeric projection metadata is incomplete", phase="manifest")
    norm_meta = view.manifest.get("numeric_norms")
    numpy: Any | None = None
    norm_cache: _NormArray | None = None
    norm_vector_dtype: str | None = None
    norm_dimensions: int | None = None
    if has_norms:
        if not isinstance(norm_meta, Mapping):
            raise FallbackExactRequired("numeric norm metadata is incomplete", phase="numeric_norms")
        norm_vector_dtype = norm_meta.get("vector_dtype")
        norm_dimensions = norm_meta.get("dimensions")
        stored_runtime = _runtime_binding_payload(norm_meta.get("runtime_binding"))
        if view.row_count:
            if not isinstance(norm_vector_dtype, str) or norm_vector_dtype not in DTYPE_CODE or isinstance(norm_dimensions, bool) or not isinstance(norm_dimensions, int) or not 1 <= norm_dimensions <= MAX_DIMENSIONS:
                raise FallbackExactRequired("numeric norm vector shape is invalid", phase="numeric_norms")
            try:
                import numpy as _numpy
            except ImportError as exc:
                raise FallbackExactRequired("NumPy is unavailable for authoritative norm verification", phase="numeric_norms") from exc
            numpy = _numpy
            try:
                current_runtime = _numpy_runtime_binding(numpy, cancellation_check=cancellation_check)
            except DerivedViewContractError as exc:
                raise FallbackExactRequired(str(exc), phase="numeric_norms") from exc
            if _canonical_json(current_runtime) != _canonical_json(stored_runtime):
                raise FallbackExactRequired("numeric norm cache runtime binding differs", phase="numeric_norms")
        elif (norm_vector_dtype is not None or norm_dimensions is not None):
            raise FallbackExactRequired("empty numeric norm vector shape is not empty", phase="numeric_norms")

    with _mapped(view, include_numeric=has_numeric, include_norms=has_norms) as maps:
        try:
            rows_blob = maps["rows.bin"]
            identity = maps["identity.bin"]
            metadata = maps["metadata.bin"]
            codes_blob = maps.get("numeric-codes.bin") if has_numeric else None
            norms_blob = maps.get("numeric-norms-f64.bin") if has_norms else None
            count = view.row_count
            if count and (rows_blob is None or identity is None or metadata is None or (has_numeric and codes_blob is None) or (has_norms and norms_blob is None)):
                raise FallbackExactRequired("authoritative verification segment is absent", phase="artifact_identity")
            if has_norms and count:
                assert numpy is not None and norms_blob is not None
                if len(norms_blob) != count * NUMERIC_NORM_STRUCT.size:
                    raise FallbackExactRequired("numeric norm cache cardinality differs", phase="numeric_norms")
                norm_cache = cast("_NormArray", numpy.frombuffer(norms_blob, dtype=numpy.dtype("<f8"), count=count))
                if norm_cache.shape != (count,):
                    raise FallbackExactRequired("numeric norm cache shape differs", phase="numeric_norms")
            item_codes: dict[str, int] = {}
            evidence_codes: dict[tuple[str, str], int] = {}
            codebook_estimated_bytes = 0
            vector_offsets = {"float16": 0, "float32": 0}
            identity_offset = metadata_offset = 0
            norm_payloads: list[bytes] = []
            scope_counts = dict.fromkeys(SCOPE_CODE, 0)
            dtype_counts = dict.fromkeys(DTYPE_CODE, 0)
            previous_ref = 0
            record_iter = iter(records)
            sentinel = object()
            for row_index in range(count):
                _cancellation_point(cancellation_check)
                if rows_blob is None or identity is None or metadata is None:
                    raise FallbackExactRequired("authoritative verification segment is absent", phase="artifact_identity")
                raw_record = next(record_iter, sentinel)
                if raw_record is sentinel or not isinstance(raw_record, VectorRecord):
                    raise FallbackExactRequired("authoritative record cardinality/type differs", phase="owner_binding")
                record = raw_record
                if isinstance(record.ref_id, bool) or not isinstance(record.ref_id, int) or record.ref_id < 1:
                    raise FallbackExactRequired("authoritative record ref_id is invalid", phase="owner_binding")
                if record.ref_id <= previous_ref:
                    raise FallbackExactRequired("authoritative records are not strictly ordered", phase="owner_binding")
                pair_index = pair_by_key.get(_canonical_json([record.model_signature, record.generation_id]))
                if pair_index is None:
                    raise FallbackExactRequired("authoritative record is outside published pairs", phase="owner_binding")
                pair = pairs[pair_index]
                try:
                    provenance = _validate_record(record, pair)
                except (DerivedViewError, TypeError, ValueError) as exc:
                    raise FallbackExactRequired(f"authoritative record {row_index} is invalid", phase="owner_binding") from exc
                row = _row(rows_blob, row_index)
                ref_id, row_pair, entity_code, dtype_code, scope_code, dimensions, vector_offset, vector_length, row_identity_offset, identity_length, row_metadata_offset, metadata_length = row
                row_ref_id, row_pair_index, row_entity_code, row_dtype_code, row_scope_code, row_dimensions = int(ref_id), int(row_pair), int(entity_code), int(dtype_code), int(scope_code), int(dimensions)
                row_vector_offset, row_vector_length = int(vector_offset), int(vector_length)
                row_identity_offset, row_identity_length = int(row_identity_offset), int(identity_length)
                row_metadata_offset, row_metadata_length = int(row_metadata_offset), int(metadata_length)
                if row_ref_id != record.ref_id or row_pair_index != pair_index or row_entity_code != ENTITY_CODE[record.entity_kind] or row_dtype_code != DTYPE_CODE[record.vector_dtype] or row_scope_code != SCOPE_CODE[record.section_scope] or row_dimensions != record.dimensions:
                    raise FallbackExactRequired(f"authoritative row {row_index} identity differs", phase="owner_binding")
                expected_vector_offset = vector_offsets[record.vector_dtype]
                if row_vector_offset != expected_vector_offset or row_vector_length != len(record.vector_blob):
                    raise FallbackExactRequired(f"authoritative row {row_index} vector layout differs", phase="owner_binding")
                vector_map = maps["vectors-f16.bin" if record.vector_dtype == "float16" else "vectors-f32.bin"]
                if vector_map is None or bytes(vector_map[row_vector_offset : row_vector_offset + row_vector_length]) != bytes(record.vector_blob):
                    raise FallbackExactRequired(f"authoritative row {row_index} vector bytes differ", phase="owner_binding")
                expected_identity = _encode_identity(record)
                if row_identity_offset != identity_offset or row_identity_length != len(expected_identity) or bytes(identity[row_identity_offset : row_identity_offset + row_identity_length]) != expected_identity:
                    raise FallbackExactRequired(f"authoritative row {row_index} identity bytes differ", phase="owner_binding")
                if row_metadata_offset != metadata_offset or row_metadata_length != len(provenance) or bytes(metadata[row_metadata_offset : row_metadata_offset + row_metadata_length]) != provenance:
                    raise FallbackExactRequired(f"authoritative row {row_index} provenance differs", phase="owner_binding")
                if has_numeric:
                    assert codes_blob is not None
                    code_offset = row_index * NUMERIC_CODE_STRUCT.size
                    if code_offset + NUMERIC_CODE_STRUCT.size > len(codes_blob):
                        raise FallbackExactRequired("numeric code row is outside artifact", phase="numeric_codes")
                    item_code, evidence_code = NUMERIC_CODE_STRUCT.unpack(bytes(codes_blob[code_offset : code_offset + NUMERIC_CODE_STRUCT.size]))
                    expected_item, expected_evidence, codebook_estimated_bytes = _numeric_codes(record, item_codes, evidence_codes, codebook_estimated_bytes)
                    if (item_code, evidence_code) != (expected_item, expected_evidence):
                        raise FallbackExactRequired(f"authoritative row {row_index} numeric code differs", phase="numeric_codes")
                if has_norms:
                    assert norm_cache is not None and norm_vector_dtype is not None and norm_dimensions is not None
                    if (record.vector_dtype, record.dimensions) != (norm_vector_dtype, norm_dimensions):
                        raise FallbackExactRequired(f"authoritative row {row_index} norm shape differs", phase="numeric_norms")
                    norm_payloads.append(bytes(record.vector_blob))
                    if len(norm_payloads) >= MAX_NUMERIC_NORM_BATCH:
                        assert numpy is not None
                        matrix = _numeric_matrix_from_bytes(b"".join(norm_payloads), dtype=norm_vector_dtype, rows=len(norm_payloads), dimensions=norm_dimensions, numpy=numpy)
                        if not bool(numpy.all(numpy.isfinite(matrix))) or not bool(numpy.all(numpy.any(matrix, axis=1))):
                            raise FallbackExactRequired("authoritative norm input is non-finite or zero", phase="numeric_norms")
                        expected_norms = numpy.asarray(numpy.linalg.norm(matrix, axis=1), dtype=numpy.dtype("<f8"))
                        start = row_index + 1 - len(norm_payloads)
                        actual_norms = norm_cache[start : row_index + 1].copy()
                        if actual_norms.shape != expected_norms.shape or not bool(numpy.all(numpy.isfinite(actual_norms))) or bool(numpy.any(actual_norms <= 0)) or actual_norms.tobytes(order="C") != expected_norms.tobytes(order="C"):
                            raise FallbackExactRequired("authoritative norm cache differs", phase="numeric_norms")
                        norm_payloads.clear()
                vector_offsets[record.vector_dtype] += len(record.vector_blob)
                identity_offset += len(expected_identity)
                metadata_offset += len(provenance)
                scope_counts[record.section_scope] += 1
                dtype_counts[record.vector_dtype] += 1
                previous_ref = record.ref_id
            extra = next(record_iter, sentinel)
            if extra is not sentinel:
                raise FallbackExactRequired("authoritative record cardinality exceeds index", phase="owner_binding")
            if has_norms and norm_payloads:
                assert numpy is not None and norm_cache is not None and norm_vector_dtype is not None and norm_dimensions is not None
                matrix = _numeric_matrix_from_bytes(b"".join(norm_payloads), dtype=norm_vector_dtype, rows=len(norm_payloads), dimensions=norm_dimensions, numpy=numpy)
                if not bool(numpy.all(numpy.isfinite(matrix))) or not bool(numpy.all(numpy.any(matrix, axis=1))):
                    raise FallbackExactRequired("authoritative norm input is non-finite or zero", phase="numeric_norms")
                expected_norms = numpy.asarray(numpy.linalg.norm(matrix, axis=1), dtype=numpy.dtype("<f8"))
                start = count - len(norm_payloads)
                actual_norms = norm_cache[start:count].copy()
                if actual_norms.shape != expected_norms.shape or not bool(numpy.all(numpy.isfinite(actual_norms))) or bool(numpy.any(actual_norms <= 0)) or actual_norms.tobytes(order="C") != expected_norms.tobytes(order="C"):
                    raise FallbackExactRequired("authoritative norm cache differs", phase="numeric_norms")
            last_ref = view.manifest.get("last_ref_id", 0)
            if isinstance(last_ref, bool) or not isinstance(last_ref, int) or previous_ref != last_ref:
                raise FallbackExactRequired("authoritative last_ref_id differs", phase="owner_binding")
            if has_numeric:
                assert isinstance(numeric_meta, Mapping)
                if numeric_meta.get("item_code_count") != len(item_codes) or numeric_meta.get("evidence_group_code_count") != len(evidence_codes) or numeric_meta.get("codebook_estimated_bytes") != codebook_estimated_bytes:
                    raise FallbackExactRequired("authoritative numeric code cardinality differs", phase="numeric_codes")
            if view.manifest.get("scope_counts") != scope_counts or view.manifest.get("dtype_counts") != dtype_counts:
                raise FallbackExactRequired("authoritative scope or dtype counts differ", phase="owner_binding")
            if rows_blob is not None and len(rows_blob) != count * ROW_STRUCT.size:
                raise FallbackExactRequired("authoritative row cardinality differs", phase="owner_binding")
            for dtype, offset in vector_offsets.items():
                mapping = maps["vectors-f16.bin" if dtype == "float16" else "vectors-f32.bin"]
                if mapping is not None and len(mapping) != offset:
                    raise FallbackExactRequired("authoritative vector segment has trailing bytes", phase="owner_binding")
            if identity is not None and len(identity) != identity_offset:
                raise FallbackExactRequired("authoritative identity segment has trailing bytes", phase="owner_binding")
            if metadata is not None and len(metadata) != metadata_offset:
                raise FallbackExactRequired("authoritative metadata segment has trailing bytes", phase="owner_binding")
            if has_norms and norms_blob is not None and len(norms_blob) != count * NUMERIC_NORM_STRUCT.size:
                raise FallbackExactRequired("authoritative norm segment has trailing bytes", phase="numeric_norms")
        finally:
            norm_cache = None
    _cancellation_point(cancellation_check)
    view.assert_files_stable()


def _numeric_winner_groups(
    best_score: Any, valid: Any, limit: int, numpy: Any,
    *, tie_order: Callable[[int], ExactSearchOrder],
) -> Any:
    """Partition scores, then resolve the boundary with the shared total key."""
    if len(valid) <= limit:
        return valid
    scores = best_score[valid]
    threshold = numpy.partition(scores, len(scores) - limit)[len(scores) - limit]
    above = valid[scores > threshold]
    ties = valid[scores == threshold]
    needed = limit - len(above)
    if len(ties) > needed:
        ties = numpy.asarray(
            heapq.nsmallest(needed, ties, key=tie_order), dtype=valid.dtype,
        )
    return numpy.concatenate((above, ties))


def _query_numeric(
    view: ValidatedView,
    query: ExactQuery,
    query_vector: tuple[float, ...],
    *,
    live_owner_binding: Mapping[str, object],
    selected_pairs: set[int],
    signatures: Mapping[str, int],
    limit: int,
    max_vectors: int,
    after_ref_id: int,
    batch_size: int,
    text_scope: str,
    evidence_mode: bool,
    diagnostic_item_ids: tuple[str, ...],
    diagnostics: dict[str, object] | None,
    cancellation_check: Callable[[], None] | None,
    hydrate_provenance: bool,
    numeric_norms: bool = False,
    score_scope: Callable[[int, int], AbstractContextManager[Any]] | None = None,
    workspace_scope: Callable[[int], AbstractContextManager[Any]] | None = None,
) -> DerivedSearchPage:
    """Use prepared numeric codes and, optionally, a sealed norm sidecar."""
    numeric_meta = view.manifest.get("numeric_projection")
    if not isinstance(numeric_meta, Mapping) or numeric_meta.get("enabled") is not True:
        raise _numeric_fallback("numeric projection is absent", "numeric_norms" if numeric_norms else "numeric_manifest")
    assert isinstance(numeric_meta, Mapping)
    norm_meta = view.manifest.get("numeric_norms")
    if numeric_norms:
        if (
            not isinstance(norm_meta, Mapping)
            or norm_meta.get("enabled") is not True
            or norm_meta.get("file") != "numeric-norms-f64.bin"
            or norm_meta.get("algorithm") != "L2float64axis1"
            or norm_meta.get("scalar_dtype") != "float64"
            or norm_meta.get("bytes_per_row") != NUMERIC_NORM_STRUCT.size
            or norm_meta.get("row_index_aligned") is not True
            or isinstance(norm_meta.get("cardinality"), bool)
            or not isinstance(norm_meta.get("cardinality"), int)
            or norm_meta.get("cardinality") != view.row_count
        ):
            raise _numeric_fallback("numeric norm cache metadata is absent or invalid", "numeric_norms")
        norm_vector_dtype, norm_dimensions = norm_meta.get("vector_dtype"), norm_meta.get("dimensions")
        if view.row_count and (
            not isinstance(norm_vector_dtype, str)
            or norm_vector_dtype not in DTYPE_CODE
            or isinstance(norm_dimensions, bool)
            or not isinstance(norm_dimensions, int)
            or norm_dimensions != query.dimensions
        ):
            raise _numeric_fallback("numeric norm cache vector shape differs from query", "numeric_norms")
        files = view.manifest.get("files")
        norm_raw = files.get("numeric-norms-f64.bin") if isinstance(files, Mapping) else None
        if not isinstance(norm_raw, Mapping):
            raise _numeric_fallback("numeric norm cache file metadata is absent", "numeric_norms")
        norm_bytes = norm_raw.get("bytes")
        if isinstance(norm_bytes, bool) or not isinstance(norm_bytes, int) or norm_bytes < 0:
            raise _numeric_fallback("numeric norm cache file size metadata is invalid", "numeric_norms")
        norm_fd, norm_before = _open_regular_at(
            view.root_fd,
            "numeric-norms-f64.bin",
            expected_fence=norm_raw.get("fence"),
            expected_bytes=norm_bytes,
        )
        with _fd_guard(norm_fd, "numeric norm cache fd"):
            if _sha256_fd(norm_fd, cancellation_check=cancellation_check) != norm_raw.get("sha256"):
                raise FallbackExactRequired("numeric norm cache content changed", phase="artifact_content")
            norm_after = os.fstat(norm_fd)
        if _stat_fence(norm_after) != _stat_fence(norm_before):
            raise FallbackExactRequired("numeric norm cache identity changed", phase="artifact_identity")
    if len(selected_pairs) != 1 or any(value != 1 for value in signatures.values()) or diagnostic_item_ids or diagnostics is not None:
        raise _numeric_fallback("numeric fast path supports one pair and no rank diagnostics", "numeric_contract")
    if batch_size > MAX_NUMERIC_NORM_BATCH:
        raise _numeric_fallback("numeric batch exceeds the 512-row matrix bound", "numeric_batch_bound")
    if max_vectors < 8 or batch_size < 8:
        raise _numeric_fallback("numeric fast path keeps scalar-sized requests classic", "numeric_scalar")
    try:
        import numpy
    except ImportError as exc:
        raise _numeric_fallback("NumPy is unavailable for numeric fast path", "numeric_import") from exc
    if numeric_norms:
        try:
            current_runtime = _numpy_runtime_binding(numpy, cancellation_check=cancellation_check)
        except DerivedViewContractError as exc:
            raise _numeric_fallback(str(exc), "numeric_norms") from exc
        assert isinstance(norm_meta, Mapping)
        stored_runtime = _runtime_binding_payload(norm_meta.get("runtime_binding"))
        if _canonical_json(current_runtime) != _canonical_json(stored_runtime):
            raise _numeric_fallback("numeric norm cache runtime binding differs", "numeric_norms")
    with _mapped(view, include_numeric=True, include_norms=numeric_norms) as maps, ExitStack() as workspace:
        rows_blob = maps["rows.bin"]
        codes_blob = maps.get("numeric-codes.bin")
        norms_blob = maps.get("numeric-norms-f64.bin") if numeric_norms else None
        identity = maps["identity.bin"]
        metadata = maps["metadata.bin"]
        if rows_blob is None or codes_blob is None or identity is None or metadata is None or (numeric_norms and norms_blob is None):
            raise _numeric_fallback("numeric artifact segment is absent", "numeric_artifact")
        row_dtype = _numeric_row_dtype(numpy)
        code_dtype = _numeric_code_dtype(numpy)
        if row_dtype.itemsize != ROW_STRUCT.size or code_dtype.itemsize != NUMERIC_CODE_STRUCT.size:
            raise _numeric_fallback("numeric structured dtype does not match sealed format", "numeric_format")
        if len(rows_blob) != view.row_count * row_dtype.itemsize or len(codes_blob) != view.row_count * code_dtype.itemsize:
            raise _numeric_fallback("numeric row/code cardinality differs", "numeric_format")
        rows: _StructuredArray | None = None
        codes: _StructuredArray | None = None
        raw: _StructuredArray | None = None
        group_codes_all = norm_cache_all = cached_norms = None
        try:
            # The internal dtype factories and cardinality/size checks above
            # establish these structured views; casts do not create aliases.
            rows = cast("_StructuredArray", numpy.frombuffer(rows_blob, dtype=row_dtype, count=view.row_count))
            codes = cast("_StructuredArray", numpy.frombuffer(codes_blob, dtype=code_dtype, count=view.row_count))
            if numeric_norms:
                assert norms_blob is not None
                if len(norms_blob) != view.row_count * NUMERIC_NORM_STRUCT.size:
                    raise _numeric_fallback("numeric norm cache byte cardinality differs", "numeric_norms")
                norm_cache_all = numpy.frombuffer(norms_blob, dtype=numpy.dtype("<f8"), count=view.row_count)
                if norm_cache_all.shape != (view.row_count,):
                    raise _numeric_fallback("numeric norm cache cardinality differs", "numeric_norms")
            group_count_value = numeric_meta["evidence_group_code_count"] if evidence_mode else numeric_meta["item_code_count"]
            if isinstance(group_count_value, bool) or not isinstance(group_count_value, int):
                raise _numeric_fallback("numeric group count is invalid", "numeric_codes")
            group_count = group_count_value
            if not 1 <= group_count <= view.row_count:
                raise _numeric_fallback("numeric group count is invalid", "numeric_codes")
            if workspace_scope is not None:
                # Scores/row indexes/seen retain 17 bytes per group. Top-K
                # partitioning and masks need at most another 32 bytes per
                # observed group; bounded identity keys and batch temporaries
                # coexist with these arrays independently of the score matrix.
                observed_groups = min(group_count, max_vectors)
                retained_groups = min(observed_groups, limit)
                identity_bytes = min(
                    len(identity) * 4,
                    retained_groups * MAX_IDENTITY_BYTES * 4,
                )
                workspace.enter_context(workspace_scope(
                    group_count * 17 + observed_groups * 32 + identity_bytes
                    + retained_groups * 1024 + batch_size * 512 + 4096
                ))
            group_name = "evidence_code" if evidence_mode else "item_code"
            group_codes_all = codes[group_name]
            best_score = numpy.full(group_count, -numpy.inf, dtype=numpy.float64)
            best_row = numpy.full(group_count, -1, dtype=numpy.int64)
            seen = numpy.zeros(group_count, dtype=numpy.bool_)
            scanned = 0
            last_ref = after_ref_id
            has_more = False
            start = int(numpy.searchsorted(rows["ref_id"], after_ref_id, side="right"))
            pair_index = next(iter(selected_pairs))
            pairs_by_index = dict(enumerate(view.pairs))

            def row_order(row_index: int, score: float) -> ExactSearchOrder:
                # Decode identities only at score ties.  No vector/model reread
                # or unbounded decoded identity cache is introduced.
                if cancellation_check is not None:
                    cancellation_check()
                candidate = _candidate(
                    _row(rows_blob, row_index), identity=identity,
                    metadata_size=len(metadata), pairs=pairs_by_index, row_index=row_index,
                ).scored(score)
                return _TargetDiagnostics._order(candidate, pairs_by_index[candidate.pair_index])

            while start < view.row_count:
                stop = min(view.row_count, start + batch_size)
                raw = rows[start:stop]
                mask = raw["pair_index"] == pair_index
                if text_scope == "content":
                    mask &= raw["scope_code"] != SCOPE_CODE["title"]
                elif text_scope == "title":
                    mask &= raw["scope_code"] == SCOPE_CODE["title"]
                eligible = numpy.flatnonzero(mask)
                if len(eligible):
                    remaining = max_vectors - scanned
                    if remaining <= 0:
                        has_more = True
                        break
                    if len(eligible) > remaining:
                        eligible = eligible[:remaining]
                        has_more = True
                    absolute = eligible + start
                    if cancellation_check is not None:
                        cancellation_check()
                    if numeric_norms:
                        assert norm_cache_all is not None
                        assert isinstance(norm_vector_dtype, str) and isinstance(norm_dimensions, int)
                        if bool(numpy.any(rows[absolute]["dtype_code"] != DTYPE_CODE[norm_vector_dtype])) or bool(numpy.any(rows[absolute]["dimensions"] != norm_dimensions)):
                            raise _numeric_fallback("numeric norm cache vector shape differs from scanned rows", "numeric_norms")
                        cached_norms = norm_cache_all[absolute].copy()
                    with score_scope(len(absolute), query.dimensions) if score_scope else nullcontext():
                        scores = _numeric_scores(
                            rows[absolute],
                            vector_maps={"float16": maps["vectors-f16.bin"], "float32": maps["vectors-f32.bin"]},
                            query_vector=query_vector,
                            dimensions=query.dimensions,
                            numpy=numpy,
                            cached_norms=cached_norms,
                        )
                    cached_norms = None
                    groups = group_codes_all[absolute]
                    if bool(numpy.any(groups >= group_count)):
                        raise _numeric_fallback("numeric group code exceeds manifest", "numeric_codes")
                    refs = rows["ref_id"][absolute]
                    local_order = numpy.lexsort((numpy.bitwise_not(refs), -scores, groups))
                    ordered_groups = groups[local_order]
                    first = numpy.empty(len(local_order), dtype=numpy.bool_)
                    first[0] = True
                    if len(first) > 1:
                        first[1:] = ordered_groups[1:] != ordered_groups[:-1]
                    local = local_order[first]
                    if not evidence_mode:
                        # Only groups whose top two scores tie need identity
                        # decoding.  The common singleton/no-tie batch stays
                        # vectorized; do not rescan the batch for every group.
                        starts = numpy.flatnonzero(first)
                        positions = numpy.flatnonzero(starts + 1 < len(local_order))
                        left = starts[positions]
                        right = left + 1
                        tied = (ordered_groups[left] == ordered_groups[right]) & (
                            scores[local_order[left]] == scores[local_order[right]]
                        )
                        for position in positions[tied]:
                            end = int(starts[position + 1]) if position + 1 < len(starts) else len(local_order)
                            members = local_order[int(starts[position]):end]
                            ties = members[scores[members] == scores[local[position]]]
                            def tie_order(index: int, rows: Any = absolute, batch_scores: Any = scores) -> ExactSearchOrder:
                                return row_order(int(rows[index]), float(batch_scores[index]))
                            local[position] = min(ties, key=tie_order)
                    local_groups = groups[local]
                    local_scores = scores[local]
                    old_scores = best_score[local_groups]
                    better = (~seen[local_groups]) | (local_scores > old_scores)
                    for position in numpy.flatnonzero(seen[local_groups] & (local_scores == old_scores)):
                        group = int(local_groups[position])
                        better[position] = row_order(int(absolute[local[position]]), float(local_scores[position])) < row_order(int(best_row[group]), float(old_scores[position]))
                    if bool(numpy.any(better)):
                        changed = local_groups[better]
                        best_score[changed] = local_scores[better]
                        best_row[changed] = absolute[local][better]
                        seen[changed] = True
                    scanned += len(absolute)
                    last_ref = int(refs[-1])
                    if has_more:
                        break
                start = stop
            valid = numpy.flatnonzero(seen)
            if not len(valid):
                return DerivedSearchPage((), scanned, last_ref if has_more else None, not has_more)
            winner_groups = _numeric_winner_groups(
                best_score, valid, limit, numpy,
                tie_order=lambda group: row_order(int(best_row[int(group)]), float(best_score[int(group)])),
            )
            winners: list[_Candidate] = []
            for group in winner_groups:
                row_index = int(best_row[int(group)])
                if row_index < 0:
                    raise _numeric_fallback("numeric group has no winner row", "numeric_retention")
                if int(codes[group_name][row_index]) != int(group):
                    raise _numeric_fallback("numeric group winner binding differs", "numeric_retention")
                candidate = _candidate(_row(rows_blob, row_index), identity=identity, metadata_size=len(metadata), pairs=dict(enumerate(view.pairs)), row_index=row_index)
                winners.append(candidate.scored(float(best_score[int(group)])))
            winners.sort(key=lambda candidate: _TargetDiagnostics._order(candidate, _pair_for_candidate(view, candidate)))
            page = DerivedSearchPage(tuple(_hit(candidate, _pair_for_candidate(view, candidate), query, metadata, hydrate=hydrate_provenance) for candidate in winners), scanned, last_ref if has_more else None, not has_more)
            return page

        finally:
            rows = codes = raw = group_codes_all = norm_cache_all = cached_norms = None


def _pair_for_candidate(view: ValidatedView, candidate: _Candidate) -> PublishedPair:
    return view.pairs[candidate.pair_index]


def query_exact_view(
    view: ValidatedView,
    query: ExactQuery,
    *,
    live_owner_binding: Mapping[str, object],
    limit: int = 20,
    max_vectors: int = 50_000,
    after_ref_id: int = 0,
    batch_size: int = 512,
    text_scope: Literal["all", "content", "title"] = "all",
    evidence_mode: bool = False,
    diagnostic_item_ids: Sequence[str] = (),
    diagnostics: dict[str, object] | None = None,
    cancellation_check: Callable[[], None] | None = None,
    hydrate_provenance: bool = True,
    numeric: bool = False,
    numeric_norms: bool = False,
    _normalized_query_vector: tuple[float, ...] | None = None,
    _cancellation_already_checked: bool = False,
    score_scope: Callable[[int, int], AbstractContextManager[Any]] | None = None,
    workspace_scope: Callable[[int], AbstractContextManager[Any]] | None = None,
) -> DerivedSearchPage:
    """Score a validated artifact; caller handles ``FallbackExactRequired``."""
    if text_scope not in SCOPE_CODE or not isinstance(evidence_mode, bool) or not isinstance(numeric, bool) or not isinstance(numeric_norms, bool) or not isinstance(_cancellation_already_checked, bool) or (numeric_norms and not numeric) or (query.target_modality == "image" and text_scope != "all"):
        raise ValueError("query scope/evidence contract is invalid")
    selected_diagnostics = _bounds(limit=limit, max_vectors=max_vectors, after_ref_id=after_ref_id, batch_size=batch_size, diagnostic_item_ids=diagnostic_item_ids)
    normalized_query_vector = _validated_normalized_query_vector(query, _normalized_query_vector)
    if _normalized_query_vector is not None and normalized_query_vector is None:
        _cancellation_point(cancellation_check)
        raise ValueError("_normalized_query_vector is not a finite normalized query vector")
    if normalized_query_vector is not None:
        if not _cancellation_already_checked:
            _cancellation_point(cancellation_check)
        if not isinstance(query.query_model_signature, str) or not query.query_model_signature.strip() or not isinstance(query.vector_space, str) or not query.vector_space.strip() or query.target_modality not in ("text", "image"):
            raise ValueError("query contract is invalid")
        query_vector = normalized_query_vector
    else:
        _cancellation_point(cancellation_check)
        query_vector = _query_vector(query)
    view.assert_live_owner_binding(live_owner_binding)
    view.assert_files_stable()
    pair_tuple = view.pairs
    pair_by_index = dict(enumerate(pair_tuple))
    signatures: dict[str, int] = {}
    for value in query.indexed_model_signatures:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("indexed_model_signatures contain blank")
        signatures[value] = signatures.get(value, 0) + 1
    if any(signature not in {pair.model_signature for pair in pair_tuple} for signature in signatures):
        if query.indexed_model_signatures:
            raise FallbackExactRequired("requested indexed model is absent", phase="query_model_binding")
    selected_pairs = {index for index, pair in pair_by_index.items() if pair.modality == query.target_modality and pair.vector_space == query.vector_space and (not signatures or pair.model_signature in signatures)}
    if not selected_pairs and query.indexed_model_signatures:
        raise FallbackExactRequired("no compatible selected pair", phase="query_model_binding")
    if not selected_pairs:
        return DerivedSearchPage((), 0, None, True)
    if view.row_count == 0:
        return DerivedSearchPage((), 0, None, True)
    if numeric:
        return _query_numeric(
            view,
            query,
            query_vector,
            live_owner_binding=live_owner_binding,
            selected_pairs=selected_pairs,
            signatures=signatures,
            limit=limit,
            max_vectors=max_vectors,
            after_ref_id=after_ref_id,
            batch_size=batch_size,
            text_scope=text_scope,
            evidence_mode=evidence_mode,
            diagnostic_item_ids=selected_diagnostics,
            diagnostics=diagnostics,
            cancellation_check=cancellation_check,
            hydrate_provenance=hydrate_provenance,
            numeric_norms=numeric_norms,
            score_scope=score_scope,
            workspace_scope=workspace_scope,
        )
    target = _TargetDiagnostics(selected_diagnostics, evidence_mode) if selected_diagnostics else None
    best: dict[object, tuple[ExactSearchHeapKey, int, int, object, _Candidate]] = {}
    heap: list[tuple[ExactSearchHeapKey, int, int, object, _Candidate]] = []
    batch: list[_Candidate] = []
    scored_count = 0
    serial = 0
    has_more, last_ref = False, after_ref_id
    with _mapped(view) as maps:
        rows, identity, metadata = maps["rows.bin"], maps["identity.bin"], maps["metadata.bin"]
        if rows is None or identity is None or metadata is None:
            raise FallbackExactRequired("non-empty view segment absent", phase="query_artifact")

        def score_and_retain(candidates: Sequence[_Candidate]) -> None:
            nonlocal scored_count, serial
            if not candidates:
                return
            with score_scope(len(candidates), query.dimensions) if score_scope else nullcontext():
                scored = _score(
                    candidates,
                    query_vector=query_vector,
                    dimensions=query.dimensions,
                    vector_maps={
                        "float16": maps["vectors-f16.bin"],
                        "float32": maps["vectors-f32.bin"],
                    },
                    cancellation_check=cancellation_check,
                )
            for candidate in scored:
                pair = pair_by_index[candidate.pair_index]
                if target is not None:
                    target.observe(candidate, pair)
                key: str | tuple[str, str] = (
                    (candidate.item_id, candidate.entity_id)
                    if evidence_mode
                    else candidate.item_id
                )
                _retain(candidate, pair=pair, key=key, limit=limit, best=best, heap=heap, serial=serial)
                serial += 1
            scored_count += len(scored)

        for index in range(view.row_count):
            if cancellation_check is not None and index % 128 == 0:
                cancellation_check()
            raw = _row(rows, index)
            if int(raw[0]) <= after_ref_id or int(raw[1]) not in selected_pairs or (text_scope == "content" and int(raw[4]) == SCOPE_CODE["title"]) or (text_scope == "title" and int(raw[4]) != SCOPE_CODE["title"]):
                continue
            raw_pair = pair_by_index[int(raw[1])]
            copies = signatures.get(raw_pair.model_signature, 1)
            if scored_count + len(batch) >= max_vectors:
                has_more = True
                break
            candidate = _candidate(raw, identity=identity, metadata_size=len(metadata), pairs=pair_by_index, row_index=index)
            for _copy in range(copies):
                if scored_count + len(batch) >= max_vectors:
                    has_more = True
                    break
                batch.append(candidate)
                last_ref = candidate.ref_id
                if len(batch) >= batch_size:
                    score_and_retain(batch)
                    batch.clear()
            if has_more:
                break
        score_and_retain(batch)
        ordered = tuple(entry[4] for entry in sorted(best.values(), key=lambda entry: entry[0].order))
        page = DerivedSearchPage(tuple(_hit(candidate, pair_by_index[candidate.pair_index], query, metadata, hydrate=hydrate_provenance) for candidate in ordered), scored_count, last_ref if has_more else None, not has_more)
        if target is not None and diagnostics is not None:
            target_entries = tuple(target.targets.values())
            target_hits = tuple(_hit(candidate, pair, query, metadata, hydrate=hydrate_provenance) for candidate, pair in target_entries)
            diagnostics.update(target.export(page, target_hits))
    return page


def assert_public_handoff(page: DerivedSearchPage) -> None:
    for hit in page.hits:
        if not hit.entity_id or not hit.item_id or not hit.indexed_model_signature or hit.handoff.get("locator_status") != "owner_hydration_required":
            raise DerivedViewContractError("public handoff is incomplete or claims locator authority")


__all__ = [
    "DerivedHit", "DerivedSearchPage", "DerivedViewContractError", "ExactQuery", "FallbackExactRequired",
    "PublishedPair", "ValidatedView", "VectorRecord", "assert_public_handoff", "iter_jsonl_records",
    "prepare_exact_view", "query_exact_view", "read_exact_manifest", "validate_exact_view", "vector_record_from_mapping", "verify_exact_records",
]
