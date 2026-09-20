"""Incremental, bounded indexing of ZIP members, including nested ZIP files."""

from __future__ import annotations

from neocortex.runtime.control.locking import FrameworkRunLock

from ..fts_lookup import insert_format_fts_row

import inspect
import json
import os
import sqlite3
import sys
import time
import zipfile
import zlib
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from neocortex.foundation.hash_compat import sha256

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.progress import ProgressCallback

from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_stream,
)
from .materialization import (
    ArchiveMaterializationLimits,
    ArchiveManifest,
    materialize_archive,
)
from neocortex.foundation.processing_provenance import (
    ProcessingProvenance,
    build_processing_provenance,
    distribution_component,
    resolve_tesseract_runtime,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.framework_route_state import FrameworkRouteState
from .models import ArchiveRouteSummary
from .logical import (
    LogicalDocumentEvidence,
    issue_diagnosis,
)
from .contracts import (
    ARCHIVE_MIME as _ARCHIVE_MIME,
    ArchiveCacheInvalid as _ArchiveCacheInvalidContract,
    ArchiveExtractionError as _ArchiveExtractionError,
    DEFAULT_MAX_MEMBER_BYTES as _DEFAULT_MAX_MEMBER_BYTES,
    DEFAULT_MAX_TEXT_CHARS as _DEFAULT_MAX_TEXT_CHARS,
)


# region [01] Public route contract and safety defaults


ARCHIVE_MIME = _ARCHIVE_MIME
ARCHIVE_ROUTE_VERSION = "archive-route-v3"
DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_MEMBERS = 20_000
DEFAULT_MAX_MEMBER_BYTES = _DEFAULT_MAX_MEMBER_BYTES
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = _DEFAULT_MAX_TEXT_CHARS
DEFAULT_MAX_TOTAL_TEXT_CHARS = 20_000_000
DEFAULT_MAX_COMPRESSION_RATIO = 200.0
DEFAULT_PDF_MAX_PAGES = 500
DEFAULT_PDF_TIMEOUT_SECONDS = 60.0
DEFAULT_PDF_WORKER_MEMORY_BYTES = 768 * 1024 * 1024
DEFAULT_OCR_MAX_PAGES = 50
DEFAULT_OCR_DPI = 200
DEFAULT_OCR_MAX_RENDER_PIXELS = 40_000_000
DEFAULT_OCR_TIMEOUT_SECONDS = 30.0
MAX_MEMBER_NAME_CHARS = 2_048
MAX_MEMBER_SEGMENT_CHARS = 255
MAX_EMBEDDED_DOCUMENT_MEMBERS = 20_000
MAX_EMBEDDED_XML_BYTES = 64 * 1024 * 1024
ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
_ARCHIVE_RESOURCES: ContextVar[tuple[Any, CancellationToken] | None] = ContextVar(
    "archive_resources", default=None
)


@contextmanager
def _archive_resource_scope(gate, cancellation: CancellationToken):
    token = _ARCHIVE_RESOURCES.set((gate, cancellation))
    try:
        yield
    finally:
        _ARCHIVE_RESOURCES.reset(token)


def _coordinated_archive_gate():
    context = _ARCHIVE_RESOURCES.get()
    if context is None or not callable(getattr(context[0], "worker_capacity", None)):
        return None
    return context[0]


def _archive_cancellation() -> CancellationToken | None:
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    worker = current_worker_cancellation()
    context = _ARCHIVE_RESOURCES.get()
    return worker if worker is not None else (None if context is None else context[1])


@dataclass(frozen=True, slots=True)
class ArchiveRouteConfig:
    state_path: Path
    max_file_bytes: int | None = None
    max_documents: int | None = None
    retry_errors: bool = False
    retry_recoverable_errors: bool = field(default=False, kw_only=True)
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    max_depth: int = DEFAULT_MAX_DEPTH
    max_members: int = DEFAULT_MAX_MEMBERS
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES
    max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO
    pdf_max_pages: int = DEFAULT_PDF_MAX_PAGES
    pdf_timeout_seconds: float = DEFAULT_PDF_TIMEOUT_SECONDS
    pdf_worker_memory_bytes: int = DEFAULT_PDF_WORKER_MEMORY_BYTES
    ocr_mode: Literal["auto", "never", "always"] = "auto"
    ocr_lang: str = "spa+eng"
    ocr_dpi: int = DEFAULT_OCR_DPI
    ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES
    ocr_max_render_pixels: int = DEFAULT_OCR_MAX_RENDER_PIXELS
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    # Materialization is intentionally opt-in and defaults to virtual/read-only
    # behavior.  The application projection enables it only for an explicit
    # Framework ``apply_actions`` request and points it at state-managed output.
    materialize_on_apply: bool = field(default=False, kw_only=True)
    materialization_directory: Path | None = field(default=None, kw_only=True)

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return _archive_processing_provenance(
            self.max_depth,
            self.max_members,
            self.max_central_directory_bytes,
            self.max_member_bytes,
            self.max_total_uncompressed_bytes,
            self.max_text_chars,
            self.max_total_text_chars,
            self.max_compression_ratio,
            self.pdf_max_pages,
            self.pdf_timeout_seconds,
            self.pdf_worker_memory_bytes,
            self.ocr_mode,
            self.ocr_lang,
            self.ocr_dpi,
            self.ocr_max_pages,
            self.ocr_max_render_pixels,
            self.ocr_timeout_seconds,
            self.tesseract_cmd,
            self.tessdata_dir,
        )


@lru_cache(maxsize=64)
def _archive_processing_provenance(
    max_depth: int,
    max_members: int,
    max_central_directory_bytes: int,
    max_member_bytes: int,
    max_total_uncompressed_bytes: int,
    max_text_chars: int,
    max_total_text_chars: int,
    max_compression_ratio: float,
    pdf_max_pages: int,
    pdf_timeout_seconds: float,
    pdf_worker_memory_bytes: int,
    ocr_mode: str,
    ocr_lang: str,
    ocr_dpi: int,
    ocr_max_pages: int,
    ocr_max_render_pixels: int,
    ocr_timeout_seconds: float,
    tesseract_cmd: str | None,
    tessdata_dir: str | None,
) -> ProcessingProvenance:
    ocr_component = (
        {
            "name": "tesseract-runtime",
            "kind": "native-executable",
            "status": "disabled",
        }
        if ocr_mode == "never"
        else resolve_tesseract_runtime(
            command=tesseract_cmd,
            tessdata_dir=tessdata_dir,
            language=ocr_lang,
            timeout_seconds=min(30.0, ocr_timeout_seconds),
        ).component
    )
    return build_processing_provenance(
        "archive-route",
        ARCHIVE_ROUTE_VERSION,
        {
            "max_depth": max_depth,
            "max_members": max_members,
            "max_central_directory_bytes": max_central_directory_bytes,
            "max_member_bytes": max_member_bytes,
            "max_total_uncompressed_bytes": max_total_uncompressed_bytes,
            "max_text_chars": max_text_chars,
            "max_total_text_chars": max_total_text_chars,
            "max_compression_ratio": max_compression_ratio,
            "pdf_max_pages": pdf_max_pages,
            "pdf_timeout_seconds": pdf_timeout_seconds,
            "pdf_worker_memory_bytes": pdf_worker_memory_bytes,
            "ocr_mode": ocr_mode,
            "ocr_language": ocr_lang,
            "ocr_dpi": ocr_dpi,
            "ocr_max_pages": ocr_max_pages,
            "ocr_max_render_pixels": ocr_max_render_pixels,
            "ocr_timeout_seconds": ocr_timeout_seconds,
            "tesseract_source": "explicit" if tesseract_cmd else "path",
            "tessdata_source": "explicit" if tessdata_dir else "default",
            "member_name_policy": "portable-posix-no-traversal-exact-case-v1",
            "nested_path_notation": "container.zip!/member.zip!/file",
        },
        (
            python_runtime_component(),
            distribution_component("sha256", "sha256"),
            distribution_component("pymupdf", "PyMuPDF"),
            distribution_component("pillow", "Pillow"),
            distribution_component("pytesseract", "pytesseract"),
            ocr_component,
        ),
        compatibility_tag=ARCHIVE_ROUTE_VERSION,
    )


# Keep historical class identities/pickle FQNs on the facade while the
# dependency-free contracts module is imported by helper slices.
ArchiveExtractionError = _ArchiveExtractionError
ArchiveExtractionError.__module__ = __name__
_ArchiveCacheInvalid = _ArchiveCacheInvalidContract
_ArchiveCacheInvalid.__module__ = __name__


# endregion [01]




# region [02] Safe member names, formats and bounded text extraction

# Member-level ZIP policy and extraction live in a separate module.  The
# aliases below preserve the historical route namespace and keep the route
# as the single compatibility surface for existing callers/tests.
from . import member_processing as _member_processing  # noqa: E402

_DRIVE_PREFIX: Any = _member_processing._DRIVE_PREFIX
_PLAIN_TEXT_EXTENSIONS: Any = _member_processing._PLAIN_TEXT_EXTENSIONS
_HTML_EXTENSIONS: Any = _member_processing._HTML_EXTENSIONS
_NESTED_ARCHIVE_EXTENSIONS: Any = _member_processing._NESTED_ARCHIVE_EXTENSIONS
_ZIP_MAGIC_PREFIXES: Any = _member_processing._ZIP_MAGIC_PREFIXES
_IMAGE_SIGNATURES: Any = _member_processing._IMAGE_SIGNATURES
_IMAGE_EXTENSIONS: Any = _member_processing._IMAGE_EXTENSIONS
_SUPPORTED_COMPRESSIONS: Any = _member_processing._SUPPORTED_COMPRESSIONS
_normalized_member_name = _member_processing._normalized_member_name
_member_is_special = _member_processing._member_is_special
_compression_ratio = _member_processing._compression_ratio
_VisibleHTML = _member_processing._VisibleHTML
_decode_text = _member_processing._decode_text
_bounded_text = _member_processing._bounded_text
_xml_text = _member_processing._xml_text
_html_text = _member_processing._html_text
_zip_document_kind = _member_processing._zip_document_kind
_WalkBudget = _member_processing._WalkBudget
_read_zip_member = _member_processing._read_zip_member
_inspect_logical_document = _member_processing._inspect_logical_document
_embedded_part_selected = _member_processing._embedded_part_selected
_extract_embedded_zip_document = _member_processing._extract_embedded_zip_document
_image_media_type = _member_processing._image_media_type
_ExtractedContent = _member_processing._ExtractedContent

def _extract_media_text(
    payload: bytes,
    *,
    kind: Literal["pdf", "image"],
    char_limit: int,
    config: ArchiveRouteConfig,
) -> tuple[str | None, bool, str | None, str | None]:
    command = (
        sys.executable,
        "-m",
        "neocortex.capabilities.formats.archive.text_worker",
        "--kind",
        kind,
        "--max-input-bytes",
        str(config.max_member_bytes),
        "--max-pages",
        str(config.pdf_max_pages),
        "--max-chars",
        str(char_limit),
        "--ocr-mode",
        config.ocr_mode,
        "--ocr-lang",
        config.ocr_lang,
        "--ocr-dpi",
        str(config.ocr_dpi),
        "--ocr-max-pages",
        str(config.ocr_max_pages),
        "--max-render-pixels",
        str(config.ocr_max_render_pixels),
        "--ocr-timeout",
        str(config.ocr_timeout_seconds),
        *(("--tesseract-cmd", config.tesseract_cmd) if config.tesseract_cmd else ()),
        *(("--tessdata-dir", config.tessdata_dir) if config.tessdata_dir else ()),
    )
    gate = _coordinated_archive_gate()
    cancellation = _archive_cancellation()
    stdout_limit = max(64 * 1024, char_limit * 6 + 64 * 1024)
    ocr_enabled = config.ocr_mode != "never"
    # Tesseract is a sequential descendant of the PDF/image worker and
    # inherits its address-space ceiling. Both may be resident concurrently.
    child_resident_bound = config.pdf_worker_memory_bytes * (2 if ocr_enabled else 1)
    # OCR retains at most one bounded page image and its text output at once.
    temporary_bound = len(payload) + (
        config.ocr_max_render_pixels * 8 + 256 * 1024 if ocr_enabled else 0
    )
    parent_grant = None
    if gate is not None:
        from neocortex.runtime.control.global_resources import current_resource_grant

        parent_grant = current_resource_grant()
        if parent_grant is not None:
            parent_grant.release_cpu()
    # The child's address-space ceiling is a conservative admission bound,
    # not a measured RSS claim. Input/capture buffers and its stdin temporary
    # are separate charges while the parent retains its ZIP/member payload.
    admission = (
        nullcontext()
        if gate is None
        else gate.admit(
            len(payload) * 2 + stdout_limit + 256 * 1024,
            resident_bytes=child_resident_bound,
            temp_bytes=temporary_bound,
            cpu_slots=1,
            native_threads=1,
            io_slots=1,
            phase=f"archive-{kind}-worker",
            cancellation=cancellation,
        )
    )
    try:
        if cancellation is not None:
            cancellation.checkpoint()
        with admission as child_grant:
            environment = {
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "OMP_THREAD_LIMIT": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
            if child_grant is not None:
                environment = child_grant.subprocess_env(environment)
            completed = run_bounded_capture(
                command,
                input_bytes=payload,
                timeout_seconds=config.pdf_timeout_seconds,
                stdout_limit_bytes=stdout_limit,
                stderr_limit_bytes=256 * 1024,
                environment=environment,
                memory_limit_bytes=config.pdf_worker_memory_bytes,
                cancellation=cancellation,
                **({"on_started": child_grant.register_process} if child_grant is not None else {}),
            )
            if cancellation is not None:
                cancellation.checkpoint()
    except (OSError, RuntimeError, SubprocessOutputLimitError) as exc:
        return None, False, f"{kind}_worker_error:{type(exc).__name__}", None
    finally:
        if parent_grant is not None:
            parent_grant.checkpoint()
    try:
        result = json.loads(completed.stdout.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        return None, False, f"{kind}_worker_invalid_output", None
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("ok"):
        reason = result.get("reason") if isinstance(result, dict) else None
        detail = str(reason or f"{kind}_worker_exit_{completed.returncode}")
        return None, False, detail, None
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, False, f"{kind}_ocr_no_text", str(result.get("extraction_mode") or "metadata")
    return (
        text,
        bool(result.get("truncated")),
        None,
        str(result.get("extraction_mode") or "native"),
    )


def _extract_member_content(
    name: str,
    payload: bytes,
    *,
    zip_kind: str | None,
    char_limit: int,
    budget: _WalkBudget,
    config: ArchiveRouteConfig,
) -> _ExtractedContent:
    # Pass the route-owned worker explicitly so monkeypatching the process
    # boundary remains effective while the pure member logic stays modular.
    return _member_processing._extract_member_content(
        name,
        payload,
        zip_kind=zip_kind,
        char_limit=char_limit,
        budget=budget,
        config=config,
        media_extractor=_extract_media_text,
    )

# endregion [02]


# region [03] Recursive traversal and durable publication


from . import cache as _archive_cache  # noqa: E402

_member_key = _archive_cache._member_key
_virtual_path = _archive_cache._virtual_path
_delete_container = _archive_cache._delete_container
_cached_container = _archive_cache._cached_container
_cached_archive_text = _archive_cache._cached_archive_text
_cached_archive_fts_rows = _archive_cache._cached_archive_fts_rows
_repair_cached_container_fts = _archive_cache._repair_cached_container_fts
_prune_stale_containers = _archive_cache._prune_stale_containers

def _refresh_cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> int:
    return _archive_cache._refresh_cached_container(
        connection, snapshot, run_id, max_text_chars=max_text_chars,
        text_decoder=lambda row: _cached_archive_text(row, max_text_chars=max_text_chars),
    )


from . import traversal as _archive_traversal  # noqa: E402

_ContainerCounters = _archive_traversal._ContainerCounters
_MemberObservation = _archive_traversal._MemberObservation
_LogicalObservation = _archive_traversal._LogicalObservation
_IssueObservation = _archive_traversal._IssueObservation
_ArchiveObservationSpool = _archive_traversal._ArchiveObservationSpool
_ArchiveMemberWork = _archive_traversal._ArchiveMemberWork
_ArchiveContainerGroup = _archive_traversal._ArchiveContainerGroup
_ARCHIVE_CONTAINER_GROUP = _archive_traversal._ARCHIVE_CONTAINER_GROUP
_STOP_ARCHIVE_WALK: Any = _archive_traversal._STOP_ARCHIVE_WALK
_ARCHIVE_PROCESS_MIN_BYTES = _archive_traversal._ARCHIVE_PROCESS_MIN_BYTES
_archive_work_admission: Any = _archive_traversal._archive_work_admission
_archive_member_memory = _archive_traversal._archive_member_memory
_archive_member_uses_process = _archive_traversal._archive_member_uses_process
_extract_archive_member = _archive_traversal._extract_archive_member
_iter_archive_members = _archive_traversal._iter_archive_members
_walk_zip = _archive_traversal._walk_zip


def _prepare_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
    run_id: int,
) -> str:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    if (
        connection.execute(
            "SELECT 1 FROM containers WHERE container_key=?", (container_key,)
        ).fetchone()
        is not None
    ):
        _delete_container(connection, container_key)
    connection.execute(
        """INSERT INTO containers(
        container_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'building',?,?)""",
        (
            container_key,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            run_id,
            time.time_ns(),
        ),
    )
    return container_key


def _record_issue(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    container_key: str,
    counters: _ContainerCounters,
    *,
    member_chain: str | None,
    depth: int,
    code: str,
    detail: str,
) -> None:
    counters.issues += 1
    if issue_diagnosis(code)[0] != "identification_only":
        counters.coverage_issues += 1
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_IssueObservation(member_chain, depth, code, detail[:2_000]))
        return
    connection.execute(
        """INSERT INTO archive_issues(
        container_key,member_chain,archive_depth,reason_code,detail,created_ns)
        VALUES(?,?,?,?,?,?)""",
        (container_key, member_chain, depth, code, detail[:2_000], time.time_ns()),
    )


def _store_member(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    container_key: str,
    member_chain: str,
    member_path: str,
    depth: int,
    info: zipfile.ZipInfo,
    content: _ExtractedContent,
    signature: str,
    run_id: int,
    *,
    document_role: str = "archive_member",
    logical_document_chain: str | None = None,
) -> None:
    file_key = _member_key(container_key, member_chain)
    path = _virtual_path(snapshot.path, member_chain)
    conflict = connection.execute(
        "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
        (path, file_key),
    ).fetchone()
    if conflict is not None:
        raise ArchiveExtractionError(
            "archive_virtual_path_collision",
            f"two archive members map to the same visible path: {path}",
        )
    text = content.text
    encoded = None if text is None else text.encode("utf-8")
    status = "indexed" if text is not None else "metadata_only"
    if content.kind == "archive":
        status = "archive"
    connection.execute(
        """INSERT INTO documents(
        file_key,container_key,path,container_path,member_chain,member_path,
        archive_depth,content_kind,media_type,size,compressed_size,crc32,
        mtime_ns,birthtime_ns,processing_signature,status,text_zlib,text_chars,
        text_xxh3_128,detail,error_type,error_message,last_seen_run_id,updated_ns,
        document_role,logical_document_chain,independently_organizable)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            file_key,
            container_key,
            path,
            snapshot.path,
            member_chain,
            member_path,
            depth,
            content.kind,
            content.media_type,
            int(info.file_size),
            int(info.compress_size),
            int(info.CRC),
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            status,
            None if encoded is None else zlib.compress(encoded, level=6),
            0 if text is None else len(text),
            None if encoded is None else sha256.sha256_128_hexdigest(encoded),
            content.detail,
            content.issue_code,
            content.detail if content.issue_code else None,
            run_id,
            time.time_ns(),
            document_role,
            logical_document_chain,
            # A conventional archive member remains a separately identifiable
            # logical candidate even though it is still a virtual resource and
            # cannot be moved by the current backend.  Only parts of a known
            # document package are inseparable components.
            int(document_role != "document_component"),
        ),
    )
    insert_format_fts_row(
        connection, "document_fts",
        ("file_key", "path", "container_path", "container_name", "member_chain", "content_kind", "body"),
        (
            file_key,
            path,
            snapshot.path,
            Path(snapshot.path).name,
            member_chain,
            content.kind,
            text or "",
        ),
    )


def _observe_member(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    snapshot: FileSnapshot,
    container_key: str,
    member_chain: str,
    member_path: str,
    depth: int,
    info: zipfile.ZipInfo,
    content: _ExtractedContent,
    signature: str,
    run_id: int,
    *,
    document_role: str = "archive_member",
    logical_document_chain: str | None = None,
) -> None:
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_MemberObservation(
            member_chain, member_path, depth, info, content,
            document_role, logical_document_chain,
        ))
        return
    _store_member(
        connection, snapshot, container_key, member_chain, member_path, depth,
        info, content, signature, run_id, document_role=document_role,
        logical_document_chain=logical_document_chain,
    )


def _store_logical_observation(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    container_key: str,
    member_chain: str,
    observation: LogicalDocumentEvidence,
    *,
    name: str,
    depth: int,
    counters: _ContainerCounters,
    diagnose: bool = True,
) -> None:
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_LogicalObservation(member_chain, observation, name, depth))
    else:
        connection.execute(
            """INSERT INTO archive_logical_documents(
            container_key,member_chain,physical_media_type,declared_mime,logical_kind,
            proposed_extension,evidence_json,identification_status,integrity_status,opening_status)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                container_key,
                member_chain,
                ARCHIVE_MIME,
                observation.declared_mime,
                observation.logical_kind,
                observation.proposed_extension,
                json.dumps(observation.evidence),
                observation.identification_status,
                observation.integrity_status,
                observation.opening_status,
            ),
        )
    if not diagnose:
        return
    code = None
    if observation.identified:
        if PurePosixPath(name).suffix.casefold() != observation.proposed_extension:
            code = "archive_logical_extension_mismatch"
    else:
        code = f"archive_logical_{observation.identification_status}"
    if code:
        _record_issue(
            connection,
            container_key,
            counters,
            member_chain=member_chain or None,
            depth=depth,
            code=code,
            detail=json.dumps(
                {
                    "physical_media_type": ARCHIVE_MIME,
                    "declared_mime": observation.declared_mime,
                    "logical_kind_inference": observation.logical_kind,
                    "proposed_extension": observation.proposed_extension,
                    "structural_member_names": observation.evidence,
                    "identification_status": observation.identification_status,
                    "integrity_status": observation.integrity_status,
                    "opening_status": observation.opening_status,
                    "effect_authorized": False,
                },
                sort_keys=True,
            ),
        )


def _metadata_content(
    *,
    detail: str | None = None,
    issue_code: str | None = None,
) -> _ExtractedContent:
    return _ExtractedContent(
        None,
        "binary",
        "application/octet-stream",
        detail,
        issue_code,
    )


def _publish_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    container_key: str,
    counters: _ContainerCounters,
    run_id: int,
) -> None:
    status_value = "partial" if counters.coverage_issues else "complete"
    connection.execute(
        """UPDATE containers SET status=?,member_count=?,indexed_count=?,
        metadata_only_count=?,nested_archive_count=?,issue_count=?,text_chars=?,
        max_depth=?,error_type=NULL,error_message=NULL,retryable=0,
        last_seen_run_id=?,updated_ns=? WHERE container_key=?""",
        (
            status_value,
            counters.members,
            counters.indexed,
            counters.metadata_only,
            counters.nested_archives,
            counters.issues,
            counters.text_chars,
            counters.max_depth,
            run_id,
            time.time_ns(),
            container_key,
        ),
    )


def _store_container_error(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
    run_id: int,
    failure: ArchiveExtractionError,
) -> None:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    if (
        connection.execute(
            "SELECT 1 FROM containers WHERE container_key=?", (container_key,)
        ).fetchone()
        is not None
    ):
        _delete_container(connection, container_key)
    connection.execute(
        """INSERT INTO containers(
        container_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        error_type,error_message,retryable,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'error',?,?,?,?,?)""",
        (
            container_key,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            failure.code,
            str(failure)[:2_000],
            int(failure.retryable),
            run_id,
            time.time_ns(),
        ),
    )
    _record_issue(
        connection,
        container_key,
        _ContainerCounters(),
        member_chain=None,
        depth=0,
        code=failure.code,
        detail=str(failure),
    )
    connection.execute(
        "UPDATE containers SET issue_count=1 WHERE container_key=?", (container_key,)
    )


# endregion [03]


# region [04] Incremental route facade


from . import container_execution as _archive_container_execution  # noqa: E402

_ContainerOutcome = _archive_container_execution._ContainerOutcome
_ArchiveContainerTask = _archive_container_execution._ArchiveContainerTask
_PreparedArchiveContainer = _archive_container_execution._PreparedArchiveContainer
_archive_container_memory = _archive_container_execution._archive_container_memory
_archive_container_capacity = _archive_container_execution._archive_container_capacity
_extract_archive_container = _archive_container_execution._extract_archive_container
_extract_archive_container_owned = _archive_container_execution._extract_archive_container_owned
_require_current_source = _archive_container_execution._require_current_source


class ArchiveRoute:
    def __init__(
        self,
        config: ArchiveRouteConfig,
        framework_state: FrameworkRouteState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.memory_gate = memory_gate
        self.cancellation = cancellation or CancellationToken()

    def _validate(self) -> None:
        if not isinstance(self.config.retry_recoverable_errors, bool):
            raise ValueError("archive retry_recoverable_errors must be a boolean")
        if not isinstance(self.config.materialize_on_apply, bool):
            raise ValueError("archive materialize_on_apply must be a boolean")
        if self.config.materialize_on_apply and self.config.materialization_directory is not None:
            directory = self.config.materialization_directory
            if not isinstance(directory, Path) or not directory.is_absolute():
                raise ValueError(
                    "archive materialization_directory must be an absolute path"
                )
        positive = {
            "max_depth": self.config.max_depth,
            "max_members": self.config.max_members,
            "max_central_directory_bytes": self.config.max_central_directory_bytes,
            "max_member_bytes": self.config.max_member_bytes,
            "max_total_uncompressed_bytes": self.config.max_total_uncompressed_bytes,
            "max_text_chars": self.config.max_text_chars,
            "max_total_text_chars": self.config.max_total_text_chars,
            "max_compression_ratio": self.config.max_compression_ratio,
            "pdf_max_pages": self.config.pdf_max_pages,
            "pdf_timeout_seconds": self.config.pdf_timeout_seconds,
            "pdf_worker_memory_bytes": self.config.pdf_worker_memory_bytes,
            "ocr_dpi": self.config.ocr_dpi,
            "ocr_max_pages": self.config.ocr_max_pages,
            "ocr_max_render_pixels": self.config.ocr_max_render_pixels,
            "ocr_timeout_seconds": self.config.ocr_timeout_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"archive {name} must be positive")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("archive max_documents must be positive")
        if self.config.ocr_mode not in {"auto", "never", "always"}:
            raise ValueError("archive ocr_mode is invalid")
        if not self.config.ocr_lang.strip():
            raise ValueError("archive ocr_lang must not be blank")

    def _selected_counts(self) -> tuple[int, int, int]:
        pool, eligible = self.framework_state.selected_route_candidate_counts(
            self.run_id,
            ARCHIVE_MIME,
            self.config.max_file_bytes,
            "archive",
            self.config.selection,
        )
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return pool, eligible, selected

    def _memory_admission(self):
        if self.memory_gate is None:
            return nullcontext()
        nested_payloads = min(
            self.config.max_total_uncompressed_bytes,
            self.config.max_member_bytes * self.config.max_depth,
        )
        estimate = nested_payloads + min(
            self.config.max_total_text_chars * 4,
            128 * 1024 * 1024,
        )
        if callable(getattr(self.memory_gate, "worker_capacity", None)):
            # Ordered extraction may retain several sibling payloads. Their
            # total is still bounded by the shared decompression budget.
            estimate += self.config.max_total_uncompressed_bytes - nested_payloads
            return self.memory_gate.admit(
                max(1, estimate), cpu_slots=0, native_threads=0,
                phase="archive-retained", cancellation=self.cancellation,
            )
        return self.memory_gate.admit(max(1, estimate))

    def _materialization_limits(self) -> ArchiveMaterializationLimits:
        """Project route bounds into the apply-only materialization service."""

        return ArchiveMaterializationLimits(
            max_members=self.config.max_members,
            max_member_bytes=self.config.max_member_bytes,
            max_total_uncompressed_bytes=self.config.max_total_uncompressed_bytes,
            max_total_temp_bytes=self.config.max_total_uncompressed_bytes,
            max_depth=self.config.max_depth,
            max_compression_ratio=self.config.max_compression_ratio,
            max_central_directory_bytes=self.config.max_central_directory_bytes,
            timeout_seconds=max(1.0, float(self.config.pdf_timeout_seconds)),
        )

    def _materialization_destination(self, container_key: str) -> Path:
        base = self.config.materialization_directory
        if base is None:
            base = self.config.state_path.parent / "archive-materialized"
        # State-managed output is separated by the immutable container identity;
        # no source basename or member name can escape this directory.
        safe_key = container_key.replace(":", "_")
        return Path(base) / safe_key

    def _record_materialization_manifest(
        self,
        connection: sqlite3.Connection,
        container_key: str,
        counters: _ContainerCounters,
        manifest: ArchiveManifest,
    ) -> None:
        status = str(getattr(manifest, "status", "partial"))
        digest_value = getattr(manifest, "manifest_digest", None)
        counters.materialization_manifest_digest = (
            None if digest_value is None else str(digest_value)
        )
        outputs = tuple(getattr(manifest, "outputs", ()) or ())
        counters.materialization_applied += sum(
            str(getattr(output, "status", "")) == "applied" for output in outputs
        )
        counters.materialization_reused += sum(
            str(getattr(output, "status", "")) == "reused" for output in outputs
        )
        counters.materialization_collisions += sum(
            str(getattr(output, "status", "")) == "collision" for output in outputs
        )
        normalized = bool(getattr(manifest, "container_normalized", False))
        classification = getattr(manifest, "classification", None)
        preserved_unit = bool(getattr(classification, "preserve_as_unit", False))
        if preserved_unit and status == "complete":
            counters.materialization_units_preserved += 1
        if status != "complete" or counters.materialization_collisions:
            counters.materialization_pending += 1
        reason_code = (
            "archive_materialization_complete"
            if status == "complete" and normalized
            else "archive_materialization_unit_preserved"
            if status == "complete" and preserved_unit
            else "archive_materialization_collision"
            if counters.materialization_collisions
            else f"archive_materialization_{status}"
        )
        classification_kind = getattr(classification, "kind", None)
        evidence = {
            "manifest_digest": counters.materialization_manifest_digest,
            "status": status,
            "container_normalized": normalized,
            "classification": None if classification_kind is None else str(classification_kind),
            "outputs": len(outputs),
            "applied": counters.materialization_applied,
            "reused": counters.materialization_reused,
            "collisions": counters.materialization_collisions,
            "source_preserved": True,
            "manifest_path": getattr(manifest, "manifest_path", None),
        }
        _record_issue(
            connection,
            container_key,
            counters,
            member_chain=None,
            depth=0,
            code=reason_code,
            detail=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        )

    def _materialize_container(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        container_key: str,
        counters: _ContainerCounters,
    ) -> None:
        """Materialize to state-managed staging only after explicit apply."""

        if not self.config.materialize_on_apply:
            return
        destination = self._materialization_destination(container_key)
        try:
            materialize_kwargs: dict[str, object] = {
                "apply": True,
                "limits": self._materialization_limits(),
            }
            # Preserve injected/legacy materializers that implement the
            # pre-registration signature, without a risky TypeError retry
            # that could duplicate a partially applied materialization.
            materialize_parameters: Any
            try:
                materialize_parameters = inspect.signature(materialize_archive).parameters
            except (TypeError, ValueError):
                materialize_parameters = {}
            if (
                "scratch_directory" in materialize_parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in materialize_parameters.values()
                )
            ):
                materialize_kwargs["scratch_directory"] = (
                    self.config.state_path.parent
                    / "scratch"
                    / "archive-materialization"
                )
            for name, value in {
                "manifest_directory": self.config.state_path.parent / "archive-manifests",
                "artifact_registry_root": self.config.state_path.parent / "artifacts",
            }.items():
                if name in materialize_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in materialize_parameters.values()
                ):
                    materialize_kwargs[name] = value
            materialize_callable: Any = materialize_archive
            with _archive_work_admission(
                phase="archive-materialize",
                temp_bytes=self.config.max_total_uncompressed_bytes,
            ):
                manifest = materialize_callable(snapshot.path, destination, **materialize_kwargs)
        except CancellationRequested:
            raise
        except Exception as exc:
            counters.materialization_pending += 1
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=None,
                depth=0,
                code="archive_materialization_error",
                detail=json.dumps(
                    {
                        "status": "partial",
                        "error_type": type(exc).__name__,
                        "detail": str(exc)[:1_000],
                        "destination": str(destination),
                        "source_preserved": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            return
        self._record_materialization_manifest(connection, container_key, counters, manifest)

    def _process_container(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
    ) -> _ContainerOutcome:
        counters = _ContainerCounters()
        try:
            _require_current_source(snapshot, "ZIP source changed after inventory")
            with _archive_resource_scope(self.memory_gate, self.cancellation), self._memory_admission():
                try:
                    source_handle = open(native_io_path(snapshot.path), "rb", buffering=0)
                except OSError as exc:
                    raise ArchiveExtractionError(
                        "archive_source_unavailable",
                        f"cannot open ZIP source: {type(exc).__name__}: {exc}",
                        retryable=True,
                    ) from exc
                with source_handle as source:
                    if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                        raise ArchiveExtractionError(
                            "archive_source_changed",
                            "ZIP source changed before it was opened",
                            retryable=True,
                        )
                    inspect_zip_stream(
                        source,
                        snapshot.size,
                        max_members=self.config.max_members,
                        max_central_directory_bytes=(self.config.max_central_directory_bytes),
                    )
                    container_key = _prepare_container(
                        connection,
                        snapshot,
                        self.config.processing_signature,
                        self.run_id,
                    )
                    source.seek(0)
                    with zipfile.ZipFile(source) as archive:
                        _walk_zip(
                            connection,
                            archive,
                            snapshot,
                            container_key,
                            prefix="",
                            depth=1,
                            budget=_WalkBudget(
                                self.config.max_members,
                                self.config.max_total_uncompressed_bytes,
                            ),
                            counters=counters,
                            config=self.config,
                            run_id=self.run_id,
                            cancellation=self.cancellation,
                        )
                    if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                        raise ArchiveExtractionError(
                            "archive_source_changed",
                            "ZIP source changed during traversal",
                            retryable=True,
                        )
                self._materialize_container(connection, snapshot, container_key, counters)
            _require_current_source(snapshot, "ZIP source path changed during traversal")
            _publish_container(
                connection,
                snapshot,
                file_key_from_snapshot(snapshot),
                counters,
                self.run_id,
            )
            return _ContainerOutcome(
                "partial" if counters.coverage_issues else "complete", counters
            )
        except ArchiveExtractionError:
            raise
        except (
            OSError,
            RuntimeError,
            ZipStructureError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
        ) as exc:
            raise ArchiveExtractionError(
                "archive_corrupt_container",
                f"{type(exc).__name__}: {exc}",
            ) from exc

    def _publish_prepared_container(
        self, connection: sqlite3.Connection, prepared: _PreparedArchiveContainer
    ) -> _ContainerOutcome:
        # elastic_map owns the bounded publication CPU admission and retains
        # the container/spool lease until this result has been consumed.
        if prepared.failure is not None:
            raise prepared.failure
        if prepared.spool is None:
            raise RuntimeError("archive container has no observation spool")
        snapshot, counters = prepared.snapshot, prepared.counters
        _require_current_source(snapshot, "ZIP source changed before publication")
        key = _prepare_container(connection, snapshot, self.config.processing_signature, self.run_id)
        replay_counters = _ContainerCounters()
        for observation in prepared.spool.observations():
            self.cancellation.checkpoint()
            if isinstance(observation, _MemberObservation):
                _store_member(
                    connection, snapshot, key, observation.member_chain,
                    observation.member_path, observation.depth, observation.info,
                    observation.content, self.config.processing_signature, self.run_id,
                    document_role=observation.document_role,
                    logical_document_chain=observation.logical_document_chain,
                )
            elif isinstance(observation, _LogicalObservation):
                _store_logical_observation(
                    connection, key, observation.member_chain, observation.observation,
                    name=observation.name, depth=observation.depth,
                    counters=replay_counters, diagnose=False,
                )
            else:
                _record_issue(
                    connection, key, replay_counters, member_chain=observation.member_chain,
                    depth=observation.depth, code=observation.code, detail=observation.detail,
                )
        _require_current_source(snapshot, "ZIP source changed while publishing observations")
        self._materialize_container(connection, snapshot, key, counters)
        _require_current_source(snapshot, "ZIP source changed before container publication")
        _publish_container(connection, snapshot, key, counters, self.run_id)
        return _ContainerOutcome("partial" if counters.coverage_issues else "complete", counters)

    def run(self) -> ArchiveRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        lock_path = self.config.state_path.with_suffix(
            self.config.state_path.suffix + ".route.lock"
        )
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            FrameworkRunLock(lock_path),
            _archive_resource_scope(self.memory_gate, self.cancellation),
        ):
            return self._run_locked()

    def _run_locked(self) -> ArchiveRouteSummary:
        from .runner import run_locked

        return run_locked(self)


# endregion [04]


__all__ = (
    "ARCHIVE_MIME",
    "ARCHIVE_ROUTE_VERSION",
    "ArchiveExtractionError",
    "ArchiveRoute",
    "ArchiveRouteConfig",
    "ArchiveRouteSummary",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.archive.route"
del _defined_value

# The physical implementation is split across small Archive-owned modules,
# but the historical route namespace remains the pickle/import compatibility
# boundary.  Keep this explicit rather than mutating every imported helper.
for _compat_name in (
    "_normalized_member_name",
    "_member_is_special",
    "_compression_ratio",
    "_VisibleHTML",
    "_decode_text",
    "_bounded_text",
    "_xml_text",
    "_html_text",
    "_zip_document_kind",
    "_WalkBudget",
    "_read_zip_member",
    "_inspect_logical_document",
    "_embedded_part_selected",
    "_extract_embedded_zip_document",
    "_image_media_type",
    "_ExtractedContent",
    "_metadata_content",
    "_member_key",
    "_virtual_path",
    "_delete_container",
    "_cached_container",
    "_cached_archive_text",
    "_cached_archive_fts_rows",
    "_repair_cached_container_fts",
    "_refresh_cached_container",
    "_prune_stale_containers",
    "_ContainerCounters",
    "_MemberObservation",
    "_LogicalObservation",
    "_IssueObservation",
    "_ArchiveObservationSpool",
    "_ArchiveMemberWork",
    "_ArchiveContainerGroup",
    "_archive_work_admission",
    "_archive_member_memory",
    "_archive_member_uses_process",
    "_extract_archive_member",
    "_iter_archive_members",
    "_walk_zip",
    "_ContainerOutcome",
    "_ArchiveContainerTask",
    "_PreparedArchiveContainer",
    "_archive_container_memory",
    "_archive_container_capacity",
    "_extract_archive_container",
    "_extract_archive_container_owned",
    "_require_current_source",
):
    _compat_value = globals().get(_compat_name)
    if getattr(_compat_value, "__module__", None) is not None:
        _compat_value.__module__ = __name__
del _compat_name, _compat_value
